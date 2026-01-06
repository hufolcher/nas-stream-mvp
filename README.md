# nas-stream-mvp
API &amp; associated front to test several way to stream files from most used codecs (h265, h264, vp9, av1 etc...) with most used web video protocol (HLS, DASH etc...).

## Backend
I strongly advise to mount your smb repo the way described here to avoid parsing, streaming issues later:

### Mount smb server
I strongly advise to mount your smb repo the way described here to avoid parsing, streaming issues later:


You can mount nas share as clean and readable `/mnt/nas` path, can be usefull to pass it as volum to docker.

```
sudo mount -t cifs "//$NAS_HOST/$NAS_SHARE" /mnt/nas -o username=$NAS_USER,password=$NAS_PASS,vers=3.0,sec=ntlmssp,iocharset=utf8,uid=$(id -u),gid=$(id -u)
```

You can unmount if needed:
```
sudo umount /mnt/nas
```

## Backend
### Build the app
First, build the backend docker container:
```
docker build -t nas-stream-backend .
```

### Run the app
To run the Python backend as a docker container launch:

```
docker run -p 8000:8000 \
  -e LOG_LEVEL=TRACE \
  --mount type=bind,source="/mnt/nas/Vidéos/Films",target=/media,readonly \
  nas-stream-backend
```
With LOG_LEVEL you can adjust verbosity of logs.


## Frontend
Build the front:

```
docker build --build-arg VITE_API_BASE="http://localhost:8000" -t nas-stream-frontend .
```

Run the front in dedicated docker:
```
docker run -p 5173:80 nas-stream-frontend
```

## Fullstack

```
MEDIA_PATH="/mnt/nas/Vidéos/Films" LOG_LEVEL="INFO" docker compose up --build
```




