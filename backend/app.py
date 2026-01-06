import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/media")).resolve()
HLS_ROOT = Path(os.environ.get("HLS_ROOT", "/hls")).resolve()
HLS_ROOT.mkdir(parents=True, exist_ok=True)

SCAN_EXTS = {".mkv", ".mp4", ".webm", ".mov", ".avi", ".m4v"}

HLS_TIME = int(os.environ.get("HLS_TIME", "2"))
HLS_LIST_SIZE = int(os.environ.get("HLS_LIST_SIZE", "6"))

app = FastAPI(title="NAS Stream MVP")

# For dev simplicity; tighten in real deployment
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/hls", StaticFiles(directory=str(HLS_ROOT)), name="hls")

FILES: Dict[str, Dict] = {}
PROCS: Dict[str, subprocess.Popen] = {}
PROCS_LOCK = threading.Lock()


def ffprobe_video_info(path: Path) -> Dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-select_streams", "v:0",
        str(path),
    ]
    out = subprocess.check_output(cmd)
    data = json.loads(out.decode("utf-8"))
    if not data.get("streams"):
        raise RuntimeError("No video stream found")
    s = data["streams"][0]
    return {
        "codec": s.get("codec_name"),
        "profile": s.get("profile"),
        "pix_fmt": s.get("pix_fmt"),
        "width": s.get("width"),
        "height": s.get("height"),
        "r_frame_rate": s.get("r_frame_rate"),
    }


def stable_id_for_path(p: Path) -> str:
    h = hashlib.sha1(str(p).encode("utf-8")).hexdigest()
    return h[:16]


def scan_files() -> None:
    global FILES
    if not MEDIA_ROOT.exists():
        raise RuntimeError(f"MEDIA_ROOT does not exist: {MEDIA_ROOT}")

    found: Dict[str, Dict] = {}

    print("Scanning media files in:", MEDIA_ROOT)

    for p in MEDIA_ROOT.rglob("*"):
        print("Scanning:", p)
        if not p.is_file():
            continue
        if p.suffix.lower() not in SCAN_EXTS:
            continue

        file_id = stable_id_for_path(p)
        try:
            info = ffprobe_video_info(p)
        except Exception as e:
            info = {"codec": "unknown", "error": str(e)}

        found[file_id] = {
            "id": file_id,
            "path": str(p),
            "name": p.name,
            "codec": info.get("codec", "unknown"),
            "profile": info.get("profile"),
            "pix_fmt": info.get("pix_fmt"),
            "width": info.get("width"),
            "height": info.get("height"),
            "r_frame_rate": info.get("r_frame_rate"),
        }

    FILES = dict(sorted(found.items(), key=lambda kv: ((kv[1].get("codec") or ""), kv[1]["name"].lower())))


def ensure_hls_running(file_id: str) -> str:
    if file_id not in FILES:
        raise HTTPException(status_code=404, detail="Unknown file id")

    src = Path(FILES[file_id]["path"])
    if not src.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    out_dir = HLS_ROOT / file_id
    out_dir.mkdir(parents=True, exist_ok=True)
    playlist = out_dir / "index.m3u8"

    # If playlist is fresh, assume OK
    if playlist.exists() and (time.time() - playlist.stat().st_mtime) < 10:
        return f"/hls/{file_id}/index.m3u8"

    with PROCS_LOCK:
        proc = PROCS.get(file_id)
        if proc and proc.poll() is None:
            return f"/hls/{file_id}/index.m3u8"

        # Clean any previous output
        for child in out_dir.glob("*"):
            try:
                child.unlink()
            except Exception:
                pass

        seg_pattern = str(out_dir / "seg_%05d.ts")

        # Always produce H.264 + AAC HLS output
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-re",
            "-i", str(src),
            "-map", "0:v:0",
            "-map", "0:a?",

            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "18",
            "-tune", "zerolatency",
            "-pix_fmt", "yuv420p",
            "-profile:v", "high",
            "-level", "4.1",
            "-g", "48",
            "-keyint_min", "48",
            "-sc_threshold", "0",

            "-c:a", "aac",
            "-b:a", "128k",
            "-ac", "2",

            "-f", "hls",
            "-hls_time", str(HLS_TIME),
            "-hls_list_size", str(HLS_LIST_SIZE),
            "-hls_flags", "delete_segments+append_list",
            "-hls_segment_filename", seg_pattern,
            str(playlist),
        ]

        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        PROCS[file_id] = p

    return f"/hls/{file_id}/index.m3u8"


@app.on_event("startup")
def _startup():
    scan_files()


@app.get("/api/health")
def health():
    return {"ok": True, "media_root": str(MEDIA_ROOT), "count": len(FILES)}


@app.post("/api/rescan")
def rescan():
    scan_files()
    return {"count": len(FILES)}


@app.get("/api/files")
def list_files():
    return list(FILES.values())


@app.post("/api/stream/{file_id}/start")
def start_stream(file_id: str):
    hls_url = ensure_hls_running(file_id)
    return {"hls_url": hls_url}


@app.post("/api/stream/{file_id}/stop")
def stop_stream(file_id: str):
    with PROCS_LOCK:
        proc = PROCS.get(file_id)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        PROCS.pop(file_id, None)

    out_dir = HLS_ROOT / file_id
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)

    return {"stopped": True}
