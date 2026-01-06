  # nas-stream-mvp
  
  API and associated frontend to experiment with web-compatible video streaming from a heterogeneous media library (**H.265** / **HEVC**, **H.264** / **AVC**, **VP9**, **AV1**, etc.) using HTTP-based adaptive streaming protocols (**HLS** today, **DASH planned**).
  
  This project is intentionally MVP-oriented: correctness and observability first, optimization later.
    
  ## Architecture overview
  
  **Backend**:
  - Scans a media directory
  - Extracts codec metadata via ffprobe
  - On demand, launches ffmpeg to produce **HLS** output (**H.264 + AAC**)
  - Serves playlists and segments as static files
  - Exposes a control API (start / stop / rescan / status)
  
  **Frontend**:
  - Lists available media
  - Triggers stream start
  - Plays HLS using browser-compatible tooling (hls.js)
  - All traffic goes through a single origin via Nginx proxy (/api, /hls)
    
  ## Backend
  
  ### Mounting your SMB / NAS share (strongly recommended)
  
  Do not use **GVFS** paths inside Docker (**/run/user/.../gvfs**). They are unstable, slow, and problematic for FFmpeg.
  
  Mount your NAS as a real **CIFS** filesystem:

  ```
  sudo mount -t cifs \"//$NAS_HOST/$NAS_SHARE\" /mnt/nas -o username=$NAS_USER,password=$NAS_PASS,vers=3.0,sec=ntlmssp,iocharset=utf8,uid=$(id -u),gid=$(id -g)
  ```

  This provides:
  - Stable POSIX paths
  - Correct file metadata
  - Predictable performance under **FFmpeg**
  
  Unmount when needed:
  ```
  sudo umount /mnt/nas
  ```
  ---
  
  ### Build the backend image
  ```
  docker build -t nas-stream-backend .
  ```
  **The image contains**:
  - **Python** + **FastAPI**
  - **FFmpeg** / **FFprobe**
  - No external runtime dependencies
    
  ### Run the backend container
  ```
  docker run -p 8000:8000 -e LOG_LEVEL=TRACE --mount type=bind,source=\"/mnt/nas/Vidéos/Films\",target=/media,readonly nas-stream-backend
  ```
  **Environment variables**:
  - **MEDIA_ROOT** (default /media)
  - **HLS_ROOT** (default /hls)
  - **HLS_TIME** (segment duration, seconds)
  - **HLS_LIST_SIZE** (playlist window)
  - **LOG_LEVEL** (INFO, DEBUG, TRACE)
    
  ### FFmpeg behavior (important)
  
  The backend always outputs **HLS** in **H.264 + AAC**, regardless of source codec.
  
  **Rationale**:
  - Browser compatibility
  - Deterministic playback behavior
  - Simplified frontend logic
  
  **Current characteristics**:
  - CPU-bound software encoding (libx264)
  - First segment latency depends on GOP size, segment duration, encoder preset, and source resolution / bitrate
  
  This behavior is expected and addressed in the roadmap.
    
  ## Frontend
  
  ### Build the frontend image
  ```
  docker build --build-arg VITE_API_BASE=\"http://localhost:8000\" -t nas-stream-frontend .
  ```
  Note: **VITE_API_BASE** is a build-time variable.
  
  ---
  
  ### Run the frontend container
  ```
  docker run -p 5173:80 nas-stream-frontend
  ```
  The frontend:
  - Is served by Nginx
  - Proxies /api and /hls to the backend
  - Avoids CORS and cross-origin issues
    
  ## Full stack (Docker Compose)
  ```
  MEDIA_PATH="/mnt/nas/Vidéos/Films" LOG_LEVEL="INFO" docker compose up --build
  ```
  This:
  - Mounts your NAS into the backend
  - Persists generated HLS segments
  - Exposes frontend on http://localhost:5173 and backend API on http://localhost:8000
    
  ## API summary
  
  - `GET  /api/files` – list scanned media
  - `POST /api/stream/{id}/start` – start HLS generation
  - `POST /api/stream/{id}/stop` – stop FFmpeg and cleanup
  - `GET  /hls/{id}/index.m3u8` – HLS playlist (served as static files)
  - `GET  /api/scan/status` – scan progress and metrics
  
  
  ## Roadmap (engineering priorities)
  
  ### 1. CPU usage and startup latency
  - Reduce HLS segment duration (**HLS_TIME**=1)
  - Align GOP size with segment duration
  - Use faster encoder presets (superfast, ultrafast)
  - Conditional passthrough for browser-compatible **H.264**
  - Delay API response until playlist exists
  
  ### 2. Hardware acceleration
  - **VAAPI** (Intel)
  - **NVENC** (NVIDIA)
  - **AMF** (AMD)
  - Runtime detection and per-file strategy selection
  
  ### 3. Smarter stream lifecycle
  - Reference counting per file
  - Idle timeout for **FFmpeg** processes
  - Multiple viewers, single encoder instance
  - Graceful restart and resume
  
  ### 4. Protocol expansion
  - **MPEG-DASH** (**fMP4**)
  - Low-latency **HLS**
  - **CMAF** unification
  
  ### 5. Indexing and caching
  - Persistent scan index
  - Deferred ffprobe on demand
  - Codec-aware preflight checks
  
  
  ## Non-goals (for now)
  
  - DRM
  - Authentication or ACLs
  - Multi-tenant isolation
  - Production-grade scheduling

