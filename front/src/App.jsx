import React, { useEffect, useMemo, useState } from "react";
import Hls from "hls.js";

const API = import.meta.env.VITE_API_BASE || "http://localhost:8000";

function groupByCodec(files) {
    const groups = {};
    for (const f of files) {
        const c = f.codec || "unknown";
        (groups[c] ||= []).push(f);
    }
    for (const k of Object.keys(groups)) {
        groups[k].sort((a, b) => a.name.localeCompare(b.name));
    }
    return groups;
}

export default function App() {
    const [files, setFiles] = useState([]);
    const [selected, setSelected] = useState(null);
    const [hlsUrl, setHlsUrl] = useState(null);
    const [error, setError] = useState(null);

    const grouped = useMemo(() => groupByCodec(files), [files]);

    useEffect(() => {
        (async () => {
            try {
                const r = await fetch(`${API}/api/files`);
                if (!r.ok) throw new Error(`files: HTTP ${r.status}`);
                setFiles(await r.json());
            } catch (e) {
                setError(String(e));
            }
        })();
    }, []);

    useEffect(() => {
        if (!hlsUrl) return;

        const video = document.getElementById("player");
        if (!video) return;

        const fullUrl = `${API}${hlsUrl}`;

        // reset
        video.pause();
        video.removeAttribute("src");
        video.load();

        if (Hls.isSupported()) {
            const hls = new Hls({ lowLatencyMode: true });
            hls.loadSource(fullUrl);
            hls.attachMedia(video);
            hls.on(Hls.Events.ERROR, (_, data) => console.error("hls.js error", data));
            return () => hls.destroy();
        }

        if (video.canPlayType("application/vnd.apple.mpegurl")) {
            video.src = fullUrl;
            video.play().catch(() => { });
            return;
        }

        setError("HLS not supported in this browser.");
    }, [hlsUrl]);

    async function startStream(file) {
        setError(null);
        setSelected(file);
        setHlsUrl(null);

        try {
            const r = await fetch(`${API}/api/stream/${file.id}/start`, { method: "POST" });
            if (!r.ok) throw new Error(`start: HTTP ${r.status}`);
            const data = await r.json();
            setHlsUrl(data.hls_url);
        } catch (e) {
            setError(String(e));
        }
    }

    return (
        <div style={{ display: "flex", gap: 24, padding: 16, fontFamily: "sans-serif" }}>
            <div style={{ width: "45%", maxHeight: "90vh", overflow: "auto" }}>
                <h2>Files (grouped by codec)</h2>
                <div style={{ fontSize: 12, opacity: 0.7 }}>API: {API}</div>
                {error && <div style={{ color: "crimson", marginTop: 8 }}>{error}</div>}

                {Object.keys(grouped).sort().map((codec) => (
                    <div key={codec} style={{ marginTop: 16 }}>
                        <h3 style={{ marginBottom: 8 }}>
                            {codec} ({grouped[codec].length})
                        </h3>
                        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
                            {grouped[codec].map((f) => (
                                <li key={f.id} style={{ marginBottom: 6 }}>
                                    <button
                                        onClick={() => startStream(f)}
                                        style={{
                                            width: "100%",
                                            textAlign: "left",
                                            padding: "8px 10px",
                                            borderRadius: 10,
                                            border: selected?.id === f.id ? "2px solid #333" : "1px solid #ccc",
                                            background: "white",
                                            cursor: "pointer",
                                        }}
                                    >
                                        <div style={{ fontWeight: 600 }}>{f.name}</div>
                                        <div style={{ fontSize: 12, opacity: 0.75 }}>
                                            {f.width}x{f.height} · {f.profile || "?"} · {f.pix_fmt || "?"} · {f.r_frame_rate || "?"}
                                        </div>
                                    </button>
                                </li>
                            ))}
                        </ul>
                    </div>
                ))}
            </div>

            <div style={{ width: "55%" }}>
                <h2>Player</h2>
                <video
                    id="player"
                    controls
                    autoPlay
                    muted
                    playsInline
                    style={{ width: "100%", background: "#000", borderRadius: 12 }}
                />
                {hlsUrl && (
                    <div style={{ marginTop: 8, fontSize: 12, opacity: 0.8 }}>
                        Streaming: {hlsUrl}
                    </div>
                )}
            </div>
        </div>
    );
}
