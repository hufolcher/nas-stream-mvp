import hashlib
import json
import logging
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

# -----------------------------
# Logging setup
# -----------------------------
TRACE_LEVEL_NUM = 5
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self, message, *args, **kws):
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace  # type: ignore[attr-defined]

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("nas_stream_mvp")

# -----------------------------
# Config
# -----------------------------
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

# Scan progress (MVP)
SCAN_LOCK = threading.Lock()
SCAN_STATUS: Dict[str, object] = {
    "state": "idle",  # idle | scanning | done | error
    "started_at": None,  # float epoch seconds
    "finished_at": None,  # float epoch seconds
    "duration_s": None,  # float
    "media_root": str(MEDIA_ROOT),
    # counters
    "scanned_paths": 0,  # number of paths visited by rglob
    "file_candidates": 0,  # number of files seen
    "accepted_media": 0,  # files with matching extension
    "indexed": 0,  # number of entries added to found/FILES so far
    "ffprobe_ok": 0,
    "ffprobe_failed": 0,
    # optional info
    "last_path": None,
    "last_error": None,
    "codec_counts": {},  # dict codec->count (computed at end)
}


log.info(
    "Process starting with config: MEDIA_ROOT=%s HLS_ROOT=%s HLS_TIME=%s HLS_LIST_SIZE=%s LOG_LEVEL=%s",
    MEDIA_ROOT,
    HLS_ROOT,
    HLS_TIME,
    HLS_LIST_SIZE,
    LOG_LEVEL,
)


def ffprobe_video_info(path: Path) -> Dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-select_streams",
        "v:0",
        str(path),
    ]
    log.debug("ffprobe: executing for path=%s", path)
    log.trace("ffprobe: cmd=%s", cmd)

    out = subprocess.check_output(cmd)
    data = json.loads(out.decode("utf-8"))
    if not data.get("streams"):
        log.warning("ffprobe: no video streams found path=%s", path)
        raise RuntimeError("No video stream found")

    s = data["streams"][0]
    info = {
        "codec": s.get("codec_name"),
        "profile": s.get("profile"),
        "pix_fmt": s.get("pix_fmt"),
        "width": s.get("width"),
        "height": s.get("height"),
        "r_frame_rate": s.get("r_frame_rate"),
    }
    log.trace("ffprobe: parsed info path=%s info=%s", path, info)
    return info


def stable_id_for_path(p: Path) -> str:
    st = p.stat()
    key = f"{p}|{st.st_size}|{int(st.st_mtime)}"
    file_id = hashlib.md5(key.encode("utf-8")).hexdigest()[:12]
    log.trace(
        "stable_id_for_path: path=%s size=%s mtime=%s id=%s",
        p,
        st.st_size,
        int(st.st_mtime),
        file_id,
    )
    return file_id


def scan_files() -> None:
    # Initialize scan status
    with SCAN_LOCK:
        SCAN_STATUS.update(
            {
                "state": "scanning",
                "started_at": time.time(),
                "finished_at": None,
                "duration_s": None,
                "media_root": str(MEDIA_ROOT),
                "scanned_paths": 0,
                "file_candidates": 0,
                "accepted_media": 0,
                "indexed": 0,
                "ffprobe_ok": 0,
                "ffprobe_failed": 0,
                "last_path": None,
                "last_error": None,
                "codec_counts": {},
            }
        )

    global FILES
    log.info("scan_files: starting scan MEDIA_ROOT=%s", MEDIA_ROOT)

    if not MEDIA_ROOT.exists():
        log.error("scan_files: MEDIA_ROOT does not exist: %s", MEDIA_ROOT)
        with SCAN_LOCK:
            SCAN_STATUS["state"] = "error"
            SCAN_STATUS["finished_at"] = time.time()
            SCAN_STATUS["duration_s"] = (
                float(SCAN_STATUS["finished_at"]) - float(SCAN_STATUS["started_at"])
                if SCAN_STATUS.get("started_at") is not None
                else None
            )
            SCAN_STATUS["last_error"] = f"MEDIA_ROOT does not exist: {MEDIA_ROOT}"
        raise RuntimeError(f"MEDIA_ROOT does not exist: {MEDIA_ROOT}")

    start_ts = time.time()
    found: Dict[str, Dict] = {}

    scanned_paths = 0
    file_candidates = 0
    accepted_media = 0
    ffprobe_ok = 0
    ffprobe_failed = 0
    indexed = 0

    # Note: rglob over network mounts can be slow; log progress at DEBUG.
    for p in MEDIA_ROOT.rglob("*"):
        scanned_paths += 1
        log.debug("scan_files: visiting path=%s", p)

        with SCAN_LOCK:
            SCAN_STATUS["scanned_paths"] = scanned_paths
            SCAN_STATUS["last_path"] = str(p)

        if not p.is_file():
            log.trace("scan_files: skip (not a file) path=%s", p)
            continue

        file_candidates += 1
        with SCAN_LOCK:
            SCAN_STATUS["file_candidates"] = file_candidates

        ext = p.suffix.lower()
        if ext not in SCAN_EXTS:
            log.trace("scan_files: skip (ext=%s not in SCAN_EXTS) path=%s", ext, p)
            continue

        accepted_media += 1
        with SCAN_LOCK:
            SCAN_STATUS["accepted_media"] = accepted_media

        file_id = stable_id_for_path(p)

        info: Dict
        try:
            info = ffprobe_video_info(p)
            ffprobe_ok += 1
            with SCAN_LOCK:
                SCAN_STATUS["ffprobe_ok"] = ffprobe_ok
        except Exception as e:
            ffprobe_failed += 1
            info = {"codec": "unknown", "error": str(e)}
            log.warning(
                "scan_files: ffprobe failed path=%s id=%s err=%s",
                p,
                file_id,
                e,
                exc_info=log.isEnabledFor(logging.DEBUG),
            )
            with SCAN_LOCK:
                SCAN_STATUS["ffprobe_failed"] = ffprobe_failed
                SCAN_STATUS["last_error"] = f"{type(e).__name__}: {e}"

        record = {
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

        found[file_id] = record
        indexed += 1
        with SCAN_LOCK:
            SCAN_STATUS["indexed"] = indexed

        log.debug(
            "scan_files: indexed id=%s codec=%s name=%s",
            file_id,
            record.get("codec"),
            record.get("name"),
        )
        log.trace("scan_files: record=%s", record)

    # preserve your sorting logic exactly
    FILES = dict(
        sorted(
            found.items(),
            key=lambda kv: ((kv[1].get("codec") or ""), kv[1]["name"].lower()),
        )
    )

    duration = time.time() - start_ts
    log.info(
        "scan_files: completed in %.2fs; scanned_paths=%d file_candidates=%d accepted_media=%d indexed=%d ffprobe_ok=%d ffprobe_failed=%d",
        duration,
        scanned_paths,
        file_candidates,
        accepted_media,
        len(FILES),
        ffprobe_ok,
        ffprobe_failed,
    )

    # codec distribution (INFO)
    codec_counts: Dict[str, int] = {}
    for v in FILES.values():
        c = v.get("codec") or "unknown"
        codec_counts[c] = codec_counts.get(c, 0) + 1
    codec_counts_sorted = dict(sorted(codec_counts.items(), key=lambda x: x[0]))
    log.info("scan_files: codec distribution=%s", codec_counts_sorted)

    # Finalize scan status
    finished_ts = time.time()
    with SCAN_LOCK:
        SCAN_STATUS["state"] = "done"
        SCAN_STATUS["finished_at"] = finished_ts
        SCAN_STATUS["duration_s"] = finished_ts - float(SCAN_STATUS["started_at"])
        SCAN_STATUS["codec_counts"] = codec_counts_sorted
        # Keep last_path as the last visited path; clear last_error only if you prefer:
        # SCAN_STATUS["last_error"] = None


def ensure_hls_running(file_id: str) -> str:
    log.info("ensure_hls_running: requested file_id=%s", file_id)

    if file_id not in FILES:
        log.warning("ensure_hls_running: unknown file id=%s", file_id)
        raise HTTPException(status_code=404, detail="Unknown file id")

    src = Path(FILES[file_id]["path"])
    log.debug("ensure_hls_running: resolved src=%s exists=%s", src, src.exists())
    if not src.exists():
        log.error("ensure_hls_running: file missing on disk id=%s src=%s", file_id, src)
        raise HTTPException(status_code=404, detail="File not found on disk")

    out_dir = HLS_ROOT / file_id
    out_dir.mkdir(parents=True, exist_ok=True)
    playlist = out_dir / "index.m3u8"

    log.debug("ensure_hls_running: out_dir=%s playlist=%s", out_dir, playlist)

    # If playlist is fresh, assume OK (keep original logic)
    if playlist.exists():
        age = time.time() - playlist.stat().st_mtime
        log.debug("ensure_hls_running: playlist exists age=%.2fs", age)
        if age < 10:
            log.info(
                "ensure_hls_running: using existing fresh playlist for id=%s", file_id
            )
            return f"/hls/{file_id}/index.m3u8"

    with PROCS_LOCK:
        proc = PROCS.get(file_id)
        if proc and proc.poll() is None:
            log.info(
                "ensure_hls_running: ffmpeg already running id=%s pid=%s",
                file_id,
                proc.pid,
            )
            return f"/hls/{file_id}/index.m3u8"

        # Clean any previous output (keep original logic)
        log.info(
            "ensure_hls_running: cleaning existing HLS output id=%s dir=%s",
            file_id,
            out_dir,
        )
        cleaned = 0
        for child in out_dir.glob("*"):
            try:
                child.unlink()
                cleaned += 1
            except Exception as e:
                log.warning(
                    "ensure_hls_running: failed to delete %s err=%s",
                    child,
                    e,
                    exc_info=log.isEnabledFor(logging.DEBUG),
                )
        log.debug("ensure_hls_running: cleaned_files=%d id=%s", cleaned, file_id)

        seg_pattern = str(out_dir / "seg_%05d.ts")
        log.debug("ensure_hls_running: seg_pattern=%s", seg_pattern)

        # Always produce H.264 + AAC HLS output (keep original cmd logic)
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-re",
            "-i",
            str(src),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "high",
            "-level",
            "4.1",
            "-g",
            "48",
            "-keyint_min",
            "48",
            "-sc_threshold",
            "0",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-f",
            "hls",
            "-hls_time",
            str(HLS_TIME),
            "-hls_list_size",
            str(HLS_LIST_SIZE),
            "-hls_flags",
            "delete_segments+append_list",
            "-hls_segment_filename",
            seg_pattern,
            str(playlist),
        ]

        log.info(
            "ensure_hls_running: starting ffmpeg id=%s src=%s -> %s",
            file_id,
            src,
            playlist,
        )
        log.trace("ensure_hls_running: ffmpeg cmd=%s", cmd)

        try:
            p = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception as e:
            log.error(
                "ensure_hls_running: failed to start ffmpeg id=%s err=%s",
                file_id,
                e,
                exc_info=True,
            )
            raise

        PROCS[file_id] = p
        log.info("ensure_hls_running: ffmpeg started id=%s pid=%s", file_id, p.pid)

    return f"/hls/{file_id}/index.m3u8"


@app.on_event("startup")
def _startup():
    log.info("startup: beginning")
    scan_files()
    log.info("startup: completed; indexed_files=%d", len(FILES))


@app.get("/api/health")
def health():
    log.debug(
        "health: MEDIA_ROOT=%s exists=%s files=%d",
        MEDIA_ROOT,
        MEDIA_ROOT.exists(),
        len(FILES),
    )
    return {"ok": True, "media_root": str(MEDIA_ROOT), "count": len(FILES)}


@app.post("/api/rescan")
def rescan():
    log.info("rescan: requested")
    scan_files()
    log.info("rescan: completed; count=%d", len(FILES))
    return {"count": len(FILES)}


@app.get("/api/files")
def list_files():
    log.debug("list_files: returning %d items", len(FILES))
    return list(FILES.values())


@app.post("/api/stream/{file_id}/start")
def start_stream(file_id: str):
    log.info("start_stream: file_id=%s", file_id)
    hls_url = ensure_hls_running(file_id)
    log.info("start_stream: started file_id=%s hls_url=%s", file_id, hls_url)
    return {"hls_url": hls_url}


@app.post("/api/stream/{file_id}/stop")
def stop_stream(file_id: str):
    log.info("stop_stream: requested file_id=%s", file_id)

    terminated = False
    pid = None

    with PROCS_LOCK:
        proc = PROCS.get(file_id)
        if proc and proc.poll() is None:
            pid = proc.pid
            log.info("stop_stream: terminating ffmpeg file_id=%s pid=%s", file_id, pid)
            proc.terminate()
            try:
                proc.wait(timeout=3)
                terminated = True
                log.info(
                    "stop_stream: terminated ffmpeg file_id=%s pid=%s", file_id, pid
                )
            except Exception:
                log.warning(
                    "stop_stream: terminate timeout, killing ffmpeg file_id=%s pid=%s",
                    file_id,
                    pid,
                )
                proc.kill()
                terminated = True
        PROCS.pop(file_id, None)

    out_dir = HLS_ROOT / file_id
    if out_dir.exists():
        log.info("stop_stream: removing HLS dir file_id=%s dir=%s", file_id, out_dir)
        shutil.rmtree(out_dir, ignore_errors=True)

    log.info(
        "stop_stream: completed file_id=%s terminated=%s pid=%s",
        file_id,
        terminated,
        pid,
    )
    return {"stopped": True}


@app.get("/api/scan/status")
def scan_status():
    with SCAN_LOCK:
        # return a shallow copy to avoid mutation while serializing
        return dict(SCAN_STATUS)
