# TAK Video Restreamer

## Table of Contents

- [Overview](#overview)
- [Security Notice](#security-notice)
- [Features](#features)
- [Quick Start](#quick-start)
- [Architecture](#architecture)
- [Access Points](#access-points)
- [Connecting Clients](#connecting-clients)
- [Viewing Streams](#viewing-streams)
- [Authentication & Security](#authentication--security)
- [ABR HLS Streaming](#abr-hls-streaming-adaptive-bitrate)
- [HLS CORS Proxy](#hls-cors-proxy-external-embedding)
- [Stream Standby](#stream-standby)
- [Docker Health Check](#docker-health-check)
- [GPU Encoding](#gpu-encoding-optional)
- [Stream Behavior](#stream-behavior)
- [API Documentation](#api-documentation)
- [Configuration](#configuration)
- [File Structure](#file-structure)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Known Issues](#known-issues)

----

## Overview

Flask + MediaMTX + FFmpeg server that ingests video from drones, cameras, and encoding tools over RTSP/RTSPS/SRT/RTMP and makes it available to ATAK, WinTAK, CloudTAK, browsers, and any HLS or RTSP client. Comes with a web UI for stream monitoring, recording, and KLV metadata extraction.

- **ABR HLS** — Adaptive bitrate transcoding with configurable renditions
- **Authentication** — Session login, API keys, rate limiting, audit logging
- **CORS Proxy** — Embed HLS streams in external apps without cross-origin issues
- **RTSPS** — Encrypted RTSP on port 8555 with in-app certificate management
- **Stream Standby** — Holds stream state when a publisher disconnects
- **MPEG-TS demuxing** — UAS/drone feeds carrying MPEG-TS over RTSP are automatically unwrapped into elementary tracks so CloudTAK and other external consumers can play them directly

----

## Security Notice

**Before exposing this server to any untrusted network, you MUST change the defaults.**

Out of the box the server ships with well-known development credentials so it works on first launch. Treat any deployment using the defaults as insecure.

| Setting | Default | Where to change | Why it matters |
| --- | --- | --- | --- |
| `ADMIN_PASSWORD` | `changeme` | [docker-compose.yml](docker-compose.yml) / env var | Full admin access to the Web UI and REST API. |
| `ADMIN_USERNAME` | `admin` | [docker-compose.yml](docker-compose.yml) / env var | Pair with a strong password. |
| `SECRET_KEY` | `change-me-to-a-random-secret-key` | [docker-compose.yml](docker-compose.yml) / env var | Signs Flask session cookies. Anyone with this value can forge sessions. Generate with `python -c "import secrets; print(secrets.token_hex(32))"`. |
| MediaMTX API (port 8889) | bound to `127.0.0.1` | [docker-compose.yml](docker-compose.yml) | Do not publish on `0.0.0.0` — the API is unauthenticated. |
| TLS certificates | self-signed (if auto-generated) | Settings page or `data/certs/` | Use a real CA (e.g. Let's Encrypt) for production. ATAK requires trusted CA certs. |
| SRT passphrases | none | [mediaMTX.yml](mediaMTX.yml) `srtPublishPassphrase` / `srtReadPassphrase` | Without passphrases, anyone reaching port 8890 can publish or read streams. |

The Web UI displays a persistent warning banner whenever the default `ADMIN_PASSWORD` is still in use.

Other hardening recommendations:
- Run behind a reverse proxy (nginx, Caddy, Traefik) terminating HTTPS for the Web UI.
- Restrict published ports at the firewall to only those clients actually need.
- Rotate the `SECRET_KEY` if you suspect it has been disclosed (this invalidates all sessions).
- Use API keys (managed via the Settings page) rather than the admin password for programmatic access.

To report a security vulnerability, please open a private security advisory on GitHub rather than a public issue.

----

## Features

### KLV Metadata Processing (MISB ST 0601.19)
- All 142 ST 0601.19 items are identified by name; items 1–93 are fully decoded with
  their specified ranges, scale factors, and out-of-range indicators. Items 94–142 are
  reported with their raw bytes and `decoded: false` where the conversion is not yet
  implemented, rather than being given a guessed one
- **Checksum (item 1)** written on every generated packet and verified on every parsed one
- BER-OID item keys and BER lengths handled separately, per ST 0601.19 §7.1
- Repeated items are preserved in packet order (ST 0601.19 Table 1 allows some to repeat)
- Read KLV from MOV/MP4/TS files or raw `.klv.bin` dumps (`utils/read_klv.py`)
- KLV data tracks carried end-to-end on ingest — see [KLV transport constraints](#klv-transport-constraints)
- **Automatic KLV timestamp repair** for UAS feeds that stamp metadata with B-frame timestamps
- **Preflight validation** of KLV integrity and TAK client compatibility, via CLI, REST API, and the web UI
- NaN/Infinity sanitization for valid JSON output

### Recording with Re-encoding
- Auto-detects H.264/H.265 codecs
- Optional re-encoding to fix packet loss corruption
- UTC timecode with frame accuracy
- QuickTime-compatible output (avc1/hvc1 tags)
- Automatic tmcd track injection
- Thumbnail generation (async background processing)

### Stream Management
- Pull from external RTSP/SRT sources
- Multi-protocol support (RTSP/RTSPS/SRT/HLS/RTMP)
- Real-time monitoring via WebSocket
- Auto-reconnection with configurable retry logic
- Per-stream recording controls

### Video Transcoding
- Timecode correction to match button press time
- Output formats: MOV, MP4, MXF
- STANAG 4609 KLV written as a `.klv.bin` sidecar (MP4/MXF options)
- RESTful APIs for transcoding operations
- **GPU-accelerated encoding** (optional, NVIDIA NVENC)
- Background processing with WebSocket status updates

----

## Quick Start

### With Docker (Recommended)

```bash
# Start the server
docker-compose up -d

# Access Web UI (login: admin / changeme)
http://localhost:3000

# Check logs
docker logs tak-video-restreamer --follow

# Stop the server
docker-compose down

# Rebuild after code changes
docker-compose up -d --build
```

### Without Docker — Windows (run-local.ps1)

The easiest way to run locally on Windows. The script auto-downloads MediaMTX, patches the config for local paths, sets all required environment variables, starts MediaMTX in the background, and runs Flask in the foreground.

**Prerequisites:** Python 3.11+ with venv at `venv_media\`, FFmpeg in PATH

```powershell
# 1. Create venv and install dependencies (first time only)
python -m venv venv_media
.\venv_media\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. Run everything
.\run-local.ps1

# Access Web UI at http://localhost:3000 (login: admin / changeme)
# Press Ctrl+C to stop both MediaMTX and Flask
```

The script creates `mediamtx-local/` with the downloaded binary and a patched config. Data is stored in `data/`.

### Without Docker — Linux / macOS (run-local.sh)

The easiest way to run locally on Linux or macOS. Mirrors `run-local.ps1`: auto-creates a venv, installs requirements, downloads the MediaMTX Linux binary, patches the config for local paths, generates a self-signed cert, sets every required environment variable, starts MediaMTX in the background, and runs Flask in the foreground. Ctrl+C stops both.

**Prerequisites:** Python 3.11+, `ffmpeg` in `PATH`, `curl`, `openssl` (optional, for RTSPS).

```bash
# Make it executable (first time only)
chmod +x run-local.sh

# Run everything
./run-local.sh

# Access Web UI at http://localhost:3000 (login: admin / changeme)
# Press Ctrl+C to stop both MediaMTX and Flask
```

The script honours `ADMIN_USERNAME`, `ADMIN_PASSWORD`, and `SECRET_KEY` if you set them before launching — otherwise it falls back to the defaults (and auto-generates a random `SECRET_KEY`). It reuses `./.venv` or `./venv` if either already exists, otherwise creates `./.venv`. State lives in `./data/`, the patched config and binary live in `./mediamtx-local/`.

```bash
# Production-style local run
ADMIN_PASSWORD='strong-password-here' \
SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
./run-local.sh
```

### Without Docker — Linux / macOS (Manual)

If you'd rather drive everything yourself instead of using `run-local.sh`:

**Prerequisites:** Python 3.11+, FFmpeg, MediaMTX binary

```bash
# 1. Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create data directories
mkdir -p data/streams data/logs data/certs data/hls

# 4. Set environment variables
export PORT=3000
export MEDIAMTX_API_URL=http://127.0.0.1:8889
export MEDIAMTX_RTSP_URL=rtsp://127.0.0.1:8554
export STREAMS_DIR=./data/streams
export DATA_DIR=./data
export LOGS_DIR=./data/logs
export HLS_OUTPUT_DIR=./data/hls
export FFMPEG_LOG_DIR=./data/logs/ffmpeg
export ADMIN_USERNAME=admin
export ADMIN_PASSWORD=changeme

# 5. Start MediaMTX (in separate terminal)
./mediamtx mediaMTX.yml

# 6. Start Flask application
python main.py

# Access Web UI at http://localhost:3000
```

For environment variables and port configuration, see [Configuration](#configuration) below.

----

## Architecture

- **Flask 3.0** (Python 3.11) — REST API and WebSocket server (Gunicorn + eventlet, single worker)
- **MediaMTX v1.19.2** — Multi-protocol streaming engine; `rtspDemuxMpegts: yes` is set under `pathDefaults` so MPEG-TS-over-RTSP publishes from UAS/drone tools are unwrapped into elementary tracks (H.264/H.265/AAC/KLV) before routing — external clients like CloudTAK get native tracks instead of an opaque MPEG-TS blob
- **FFmpeg** — Video processing, recording, and transcoding
- **Flask-Login** — Session-based authentication
- **Flask-Limiter** — Rate limiting (5/min on login)
- **certbot** — Let's Encrypt certificate automation (in-container)

## Access Points

- **Web UI**: http://localhost:3000 (login required)
- **RTSP (TCP)**: rtsp://localhost:8554/{stream}
- **RTSP (UDP)**: rtsp://localhost:8554/{stream} (specify UDP in client)
- **RTSPS (TLS)**: rtsps://localhost:8555/{stream}
- **HLS (Browser)**: http://localhost:8888/{stream}/index.m3u8
- **ABR HLS**: http://localhost:3000/hls/{stream}/master.m3u8
- **ABR Player**: http://localhost:3000/hls/{stream}/player
- **HLS CORS Proxy**: http://localhost:3000/api/hls/proxy/{stream}/index.m3u8
- **SRT Publish**: srt://localhost:8890?streamid=publish:{stream}
- **SRT Read**: srt://localhost:8890?streamid=read:{stream}
- **MediaMTX API**: http://localhost:8889 (localhost only)

### Protocol Details

#### RTSP (Real-Time Streaming Protocol)
- **Port**: 8554
- **Transport**: TCP (default) or UDP
- **Usage**: `ffplay rtsp://localhost:8554/mystream`
- **UDP Mode**: `ffplay -rtsp_transport udp rtsp://localhost:8554/mystream`
- Standard protocol for IP cameras and professional streaming

#### RTSPS (RTSP over TLS)

- **Port**: 8555
- **Encryption Mode**: `optional` (accepts both RTSP and RTSPS) — configurable to `strict` (RTSPS only)
- **Usage**: `ffplay rtsps://localhost:8555/mystream`
- Encrypted RTSP for secure transmission
- TLS certificates managed via Settings page, TLS API, or file system
- In-app generation of self-signed certificates and Let's Encrypt integration
- **ATAK Note**: ATAK only trusts certificates from recognized Certificate Authorities — self-signed certs will not work with ATAK
- **Note**: Self-signed certificates will show security warnings in clients — use trusted CA certificates for production

#### SRT (Secure Reliable Transport)
- **Port**: 8890
- **Publish**: `srt://localhost:8890?streamid=publish:mystream`
- **Read**: `srt://localhost:8890?streamid=read:mystream`
- Low-latency protocol ideal for streaming over unreliable networks
- Built-in error correction and encryption
- **Stream Format**: MediaMTX requires `streamid` parameter with format `action:pathname` where action is either `publish` or `read`
- **Security**: Optional passphrases can be configured in mediaMTX.yml:
  ```yaml
  pathDefaults:
    srtPublishPassphrase: "your-publish-password"  # Require password to publish
    srtReadPassphrase: "your-read-password"        # Require password to view
  ```
- **With Passphrase**: Add `&passphrase=yourpassword` to SRT URL:
  ```bash
  # Publishing with passphrase
  srt://localhost:8890?streamid=publish:mystream&passphrase=your-publish-password
  
  # Reading with passphrase
  srt://localhost:8890?streamid=read:mystream&passphrase=your-read-password
  ```
  - Publishing (sending video): `streamid=publish:streamname`
  - Reading (viewing video): `streamid=read:streamname`
- **Example**:
  ```bash
  # Publish with FFmpeg
  ffmpeg -re -i input.mp4 -c copy -f mpegts "srt://localhost:8890?streamid=publish:drone1"
  
  # View with FFplay
  ffplay "srt://localhost:8890?streamid=read:drone1"
  
  # View with VLC
  vlc "srt://localhost:8890?streamid=read:drone1"
  ```

#### RTMP (Real-Time Messaging Protocol)
- **Port**: 1935
- **Publish**: `rtmp://SERVER-IP:1935/stream_name`
- **Read**: `rtmp://SERVER-IP:1935/stream_name`
- Legacy protocol originally developed by Adobe, widely supported by encoders and streaming software
- **OBS Studio**:
  1. Settings → Stream → Service: Custom
  2. Server: `rtmp://SERVER-IP:1935/`
  3. Stream Key: `stream_name`
- **FFmpeg Publish**:
  ```bash
  ffmpeg -re -i input.mp4 -c copy -f flv rtmp://SERVER-IP:1935/mystream
  ```
- **FFplay Read**:
  ```bash
  ffplay rtmp://SERVER-IP:1935/mystream
  ```
- **Note**: RTMP is not included in the Quick Connect panel on the Web UI dashboard. Use the URLs above directly in your encoder or player.

#### HLS (HTTP Live Streaming)
- **Port**: 8888
- **URL Format**: `http://localhost:8888/{stream}/index.m3u8`
- **Browser-Compatible**: Use `{stream}_hls` for H.264 transcoding
- **Usage**: 
  - Open directly in web browsers (Safari, Edge, Chrome with extensions)
  - Use HLS.js for advanced browser playback
  - View in VLC: `vlc http://localhost:8888/mystream/index.m3u8`
- Native browser playback without plugins
- Adaptive bitrate streaming support
- **Latency**: ~3-10 seconds (segment-based buffering)
- **Best for**: Web dashboards, embedding in web apps, remote viewing
- **H.265/HEVC Compatibility**: Most browsers don't support H.265. For H.265 streams, use the `_hls` suffix which auto-transcodes to H.264:
  - Original H.265 stream: `http://localhost:8888/mystream/index.m3u8` (VLC, FFplay only)
  - Browser-compatible H.264: `http://localhost:8888/mystream_hls/index.m3u8` (Chrome, Firefox, Safari, Edge)
- **Configuration**:
  ```yaml
  hls: yes
  hlsAddress: :8888
  hlsVariant: fmp4             # Supports H.264, H.265, AV1
  hlsSegmentCount: 3           # Number of segments to keep
  hlsSegmentDuration: 1s       # Length of each segment
  hlsAlwaysRemux: no           # Generate on-demand (saves resources)
  ```
- **Example URLs**:
  - Original codec: `http://YOUR-IP:8888/drone1/index.m3u8`
  - Browser H.264: `http://YOUR-IP:8888/drone1_hls/index.m3u8`
  - Direct browser: Use "Play HLS in Browser" button in web UI
  - Embed in HTML:
    ```html
    <video id="video" controls></video>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
    <script>
      var video = document.getElementById('video');
      var hls = new Hls();
      hls.loadSource('http://YOUR-IP:8888/drone1_hls/index.m3u8');
      hls.attachMedia(video);
    </script>
    ```

----

## Connecting Clients

How to publish (send) video to the server from cameras, drones, and encoding software.

### Publishing via RTSP

Most IP cameras and encoding apps can publish to an RTSP endpoint:

```
rtsp://SERVER-IP:8554/stream_name
```

The stream name (path) is created automatically when the first publisher connects. Use any name you want — `drone1`, `camera-north`, `uas`, etc.

**OBS Studio:**
1. Settings → Stream → Service: Custom
2. Server: `rtsp://SERVER-IP:8554/obs-stream`
3. Start Streaming

**FFmpeg (file or device):**
```bash
ffmpeg -re -i input.mp4 -c copy -f rtsp rtsp://SERVER-IP:8554/mystream
```

**IP Cameras (Axis, Hikvision, Dahua, etc.):**
Use the Pull Stream feature instead — enter the camera's RTSP URL in the web UI and the server pulls the feed automatically with auto-reconnection.

### Publishing via SRT

SRT is ideal for publishing over the internet or unreliable networks:

```
srt://SERVER-IP:8890?streamid=publish:stream_name
```

**FFmpeg:**
```bash
ffmpeg -re -i input.mp4 -c copy -f mpegts "srt://SERVER-IP:8890?streamid=publish:drone1"
```

**OBS Studio:**
1. Settings → Stream → Service: Custom
2. Server: `srt://SERVER-IP:8890?streamid=publish:obs-stream`
3. Start Streaming

**Larix Broadcaster (iOS/Android):**
1. Settings → Connections → New Connection
2. URL: `srt://SERVER-IP:8890?streamid=publish:mobile-cam`

### Publishing via RTSPS (Encrypted)

For encrypted publishing over TLS:

```
rtsps://SERVER-IP:8555/stream_name
```

Requires TLS certificates configured on the server (see [TLS Certificates](#tls-certificates-rtsps)).

### Pull Streams (Server-Initiated)

Instead of pushing from the source, the server can pull from an external RTSP/SRT source:

1. Go to the **Web UI** → click a stream → **Pull Stream**
2. Enter the source URL: `rtsp://192.168.1.100:554/stream`
3. If the camera requires authentication, either:
   - Embed credentials directly in the URL: `rtsp://user:password@192.168.1.100:554/stream`
   - Or tick **Use Credentials** and enter the username and password in the fields below the URL
4. The server connects, pulls the feed, and re-publishes it locally with auto-reconnection

Or via API:
```bash
# Without credentials
curl -u admin:changeme -X POST http://SERVER-IP:3000/api/streams/camera1/pull \
  -H "Content-Type: application/json" \
  -d '{"url": "rtsp://192.168.1.100:554/stream"}'

# With credentials (either format works)
curl -u admin:changeme -X POST http://SERVER-IP:3000/api/streams/camera1/pull \
  -H "Content-Type: application/json" \
  -d '{"url": "rtsp://192.168.1.100:554/stream", "username": "user", "password": "pass"}'
```

### Connecting ATAK UAS Tool

1. Install both ATAK and the UAS tool
2. Start both, connect to the UAS, ensure you can see video within ATAK
3. Go to **Settings** → Tool Preferences → UAS Tool Preferences → Video Broadcast
4. Set your stream settings as needed.

Note: if using SRT, you may need to add the `streamid=publish:STREAM_NAME` UAS may not add it for you. 

###   Connecting ATAK Helmcam

1. Install Helmcam
2. Connect your encoding hardware to the EUD
3. Start Helmcam 
4. Go to **Edit Configuration**
5. Set the Stream Destination then save the configuration 
6. Start the stream

### Connecting ATAK ICU

1. Install ICU
1. Go to Settings
1. Select your camera, orientation
1. Set you Media Preferences as needed for your mission
1. Tap Broadcast Preferences
1.  Change Destination to "Local Area Network"
1. Set your Delivery Method to RTSP or RTSPS
1. Set the RTSP Server Address in the format of `ip:port/path` or `url:port/path`
1. Hit back until you see the camera view
1. Start the broadcast 



----

## Viewing Streams

How to watch streams from the server on different devices and applications.

### Watching in ATAK and WinTAK

Use the built in TAK video playing to watch the video with in ATAK.

Note: iTAK does not support SRT or RTSPS, if using iTAK, you must use RTSP. 

### Web Browser

| Method | URL | Notes |
|--------|-----|-------|
| **Web UI Dashboard** | `http://SERVER-IP:3000` | Stream list with one-click HLS playback |
| **ABR Player** | `http://SERVER-IP:3000/hls/{stream}/player` | Built-in player (auto-detects ABR or native HLS) |
| **Video Wall** | `http://SERVER-IP:3000/videowall` | Multi-stream grid view |
| **Direct HLS (H.264)** | `http://SERVER-IP:8888/{stream}_hls/index.m3u8` | Safari native; Chrome needs hls.js |

> **Tip:** Chrome desktop cannot play `.m3u8` natively. Use the `/player` URL or install an HLS browser extension.

### VLC Media Player

```bash
# RTSP
vlc rtsp://SERVER-IP:8554/drone1

# RTSPS (encrypted)
vlc rtsps://SERVER-IP:8555/drone1

# SRT
vlc "srt://SERVER-IP:8890?streamid=read:drone1"

# HLS
vlc http://SERVER-IP:8888/drone1/index.m3u8

# ABR HLS (adaptive bitrate)
vlc http://SERVER-IP:3000/hls/drone1/master.m3u8
```

### FFplay

```bash
ffplay rtsp://SERVER-IP:8554/drone1
ffplay -rtsp_transport udp rtsp://SERVER-IP:8554/drone1    # Lower latency
ffplay "srt://SERVER-IP:8890?streamid=read:drone1"
ffplay http://SERVER-IP:8888/drone1/index.m3u8
```

### ATAK (Android Team Awareness Kit)

ATAK supports RTSP, RTSPS, and SRT video feeds natively:

- **RTSP**: `rtsp://SERVER-IP:8554/drone1`
- **RTSPS**: `rtsps://SERVER-IP:8555/drone1` (requires trusted CA certificate — self-signed will not work)
- **SRT**: `srt://SERVER-IP:8890?streamid=read:drone1`
- **HLS**: `http://SERVER-IP:3000/hls/drone1/master.m3u8` (ABR adaptive bitrate)

For RTSPS with ATAK, use a certificate from a recognized Certificate Authority (Let's Encrypt, commercial CA). ATAK rejects self-signed certificates.

### Mobile Devices (iOS / Android)

| App | Protocols | Notes |
|-----|-----------|-------|
| **VLC Mobile** | RTSP, HLS | Free, supports all protocols |
| **ATAK** | RTSP, RTSPS, SRT, HLS | TAK ecosystem integration |
| **Safari (iOS)** | HLS | Native `.m3u8` playback, use ABR URL |
| **Larix Player** | SRT, RTSP | Low-latency SRT playback |

**iOS Safari** — open directly:
```
http://SERVER-IP:3000/hls/drone1/master.m3u8
```

**Android** — use VLC or the web UI player:
```
http://SERVER-IP:3000/hls/drone1/player
```

### Embedding in Web Applications

Use the HLS CORS Proxy for cross-origin embedding (no auth required):

```html
<video id="video" controls autoplay muted></video>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<script>
  var hls = new Hls();
  hls.loadSource('http://SERVER-IP:3000/api/hls/proxy/drone1/index.m3u8');
  hls.attachMedia(document.getElementById('video'));
</script>
```

See [HLS CORS Proxy](#hls-cors-proxy-external-embedding) and [Embedding ABR HLS](#embedding-abr-hls) for details and error recovery patterns.

----

## Authentication & Security

All pages and API endpoints require authentication. Three methods are supported:

### Login Methods

| Method | Usage | Details |
|--------|-------|---------|
| **Session (Cookie)** | Web UI login form at `/login` | 7-day remember-me cookie, `HttpOnly`, `SameSite=Lax` |
| **API Key** | `X-API-Key` header | Generated via Settings UI or API, never stored in plaintext |
| **Basic Auth** | `Authorization: Basic …` header | Standard HTTP Basic for scripts/curl |

### Default Credentials

| Variable | Default | Set via |
|----------|---------|---------|
| `ADMIN_USERNAME` | `admin` | `docker-compose.yml` environment |
| `ADMIN_PASSWORD` | `changeme` | `docker-compose.yml` environment |

A warning banner appears on every page until the default password is changed.

### Rate Limiting

- **Login endpoint**: 5 attempts per minute (Flask-Limiter)
- Failed login attempts are recorded in the audit log

### Audit Logging

All security events are logged to `data/logs/audit.log`:
- `login` / `login_failed` — successful and failed logins
- `logout` — session ended
- `api_key_created` / `api_key_revoked` — API key lifecycle

View recent entries via the Settings page or `GET /api/audit?lines=200`.

### Public Endpoints (No Auth Required)

The following paths bypass authentication:
- `/api/health` — health check
- `/api/auth/login`, `/api/auth/status` — login flow
- `/login`, `/static/` — login page and static assets
- `/hls/` — ABR HLS playlists and segments
- `/api/hls/proxy/` — CORS-enabled HLS proxy for external embedding

### Quick Examples

```bash
# Session login
curl -c cookies.txt -X POST http://localhost:3000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"changeme"}'

# Use session cookie
curl -b cookies.txt http://localhost:3000/api/streams

# Use API key
curl -H "X-API-Key: your-api-key" http://localhost:3000/api/streams

# Use Basic auth
curl -u admin:changeme http://localhost:3000/api/streams
```

----

## ABR HLS Streaming (Adaptive Bitrate)

Multi-rendition HLS transcoding with configurable quality levels served directly from the Flask application.

### How It Works

1. Enable ABR for a stream via the web UI or `POST /api/streams/{stream}/abr`
2. FFmpeg reads the RTSP source and transcodes into multiple renditions (e.g. High + Low)
3. HLS playlists and segments are written to `data/hls/{stream}/`
4. A master playlist at `/hls/{stream}/master.m3u8` lists all variants
5. Clients (HLS.js, VideoJS, Safari) auto-switch quality based on bandwidth

### Architecture: Orphan and Move On

The ABR service uses an "orphan and move on" pattern for FFmpeg process management:

- **Stall detection**: A background thread checks variant playlist mtimes every 10 seconds. If no playlist has been updated in 30 seconds (`STALL_TIMEOUT`), the stream is considered stalled.
- **No blocking waits**: When restarting, the old FFmpeg process is sent `SIGKILL` but never `wait()`-ed on. This prevents hangs if FFmpeg enters D-state (uninterruptible sleep, e.g. NFS mounts, stuck I/O).
- **Automatic restart**: On stall or unexpected exit, a new FFmpeg process is spawned immediately with fresh RTSP connections.
- **Startup grace period**: Stall detection is suppressed for the first 15 seconds (`STARTUP_GRACE`) to allow FFmpeg to probe, connect, and write the first segments.

### FFmpeg stderr Handling

> **Critical implementation detail**: FFmpeg's stderr output is redirected to log files (`/opt/app/logs/ffmpeg/{stream}.log`), **not** to `subprocess.PIPE`.

Using `subprocess.PIPE` for stderr without reading from it causes a **pipe buffer deadlock**: the OS pipe buffer is ~64KB on Linux, and FFmpeg's progress output (`frame=`, `size=`, `bitrate=` lines) fills it within ~200-230 seconds. Once full, FFmpeg's `write()` call blocks indefinitely, freezing the entire transcoding pipeline. Redirecting to a file eliminates the buffer limit entirely.

FFmpeg is also started with `-loglevel warning` to suppress progress lines and reduce log volume. On stall or exit, the last 15 lines of the log file are printed to the application log for diagnostics.

### Configurable Settings

Rendition profiles are configurable in **Settings → ABR Settings**:

| Setting | High Default | Low Default | Options |
|---------|-------------|-------------|---------|
| Resolution | 1280×720 | 640×360 | 1920×1080, 1280×720, 854×480, 640×360, 426×240 |
| Bitrate | 2500 kbps | 800 kbps | 4000, 2500, 1500, 1000, 800, 500, 300 kbps |
| Framerate | 30 fps | 15 fps | 30, 25, 15, 10 fps |

Settings are applied immediately to running ABR processes.

### ABR URLs

| URL | Description |
|-----|-------------|
| `/hls/{stream}/master.m3u8` | Raw ABR master playlist (for VLC, ffplay, TAK, any HLS client) |
| `/hls/{stream}/player` | Embedded hls.js player page (for browser viewing) |
| `/hls/{stream}/v0/index.m3u8` | High-quality variant playlist |
| `/hls/{stream}/v1/index.m3u8` | Low-quality variant playlist |

> **Note**: Chrome desktop cannot play `.m3u8` files natively. Use the `/player` URL for browser viewing, or the raw `.m3u8` URLs with apps that have native HLS support (VLC, ffplay, Safari, TAK, mobile browsers).

### Embedded Player

The built-in player at `/hls/{stream}/player` provides:
- **Automatic source detection**: Uses ABR master playlist when ABR is active, falls back to MediaMTX native HLS (via CORS proxy) when ABR is not running
- Playback via hls.js with automatic quality switching (ABR mode) or single-stream playback (Native mode)
- Quality selector for manual rendition override (ABR mode only)
- Source mode label showing "ABR mode" or "Native mode"
- Full error recovery: `recoverMediaError()` for media errors, `startLoad()` for network drops, automatic stream reload as last resort
- Status overlay during reconnection

### Embedding ABR HLS

For embedding in your own page, use hls.js with error recovery:

```html
<video id="video" controls></video>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<script>
  var video = document.getElementById('video');
  if (Hls.isSupported()) {
    var hls = new Hls({ lowLatencyMode: true, backBufferLength: 90 });
    hls.loadSource('http://YOUR-IP:3000/hls/drone1/master.m3u8');
    hls.attachMedia(video);

    // Error recovery (important for long-running streams)
    hls.on(Hls.Events.ERROR, function(event, data) {
      if (data.fatal) {
        if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
          hls.recoverMediaError();
        } else if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
          hls.startLoad();
        } else {
          hls.destroy();
          hls = new Hls({ lowLatencyMode: true, backBufferLength: 90 });
          hls.loadSource('http://YOUR-IP:3000/hls/drone1/master.m3u8');
          hls.attachMedia(video);
        }
      }
    });
  }
</script>
```

----

## HLS CORS Proxy (External Embedding)

A CORS-enabled proxy that forwards MediaMTX HLS streams through the Flask server, allowing external web applications to embed video using standard players like VideoJS or HLS.js without cross-origin issues.

### URL Format

```
http://localhost:3000/api/hls/proxy/{stream}/{filename}
```

- **No authentication required** — designed for embedding in external systems
- **Full CORS headers** — `Access-Control-Allow-Origin: *`
- **Preflight support** — `OPTIONS` requests return `204`
- **m3u8 rewriting** — segment URLs in playlists are rewritten to go through the proxy

### Supported File Types

`.m3u8`, `.ts`, `.m4s`, `.mp4`

### Example: VideoJS Player

```html
<video-js id="player" class="vjs-default-skin" controls autoplay muted
          width="640" height="360">
  <source src="http://YOUR-IP:3000/api/hls/proxy/drone1/index.m3u8"
          type="application/x-mpegURL">
</video-js>
<script src="https://vjs.zencdn.net/8.10.0/video.min.js"></script>
<link href="https://vjs.zencdn.net/8.10.0/video-js.css" rel="stylesheet">
```

### Example: HLS.js

```javascript
var hls = new Hls();
hls.loadSource('http://YOUR-IP:3000/api/hls/proxy/drone1/index.m3u8');
hls.attachMedia(document.getElementById('video'));
```

### Environment Variable

| Variable | Default | Description |
|----------|---------|-------------|
| `MEDIAMTX_HLS_URL` | `http://localhost:8888` | Upstream MediaMTX HLS address |

----

## Stream Standby

When a publisher disconnects, the stream enters **standby** mode instead of disappearing. This preserves stream state (name, viewers, recording config) so it can resume instantly when the publisher reconnects.

### Configuration

| Setting | Default | Range | Description |
|---------|---------|-------|-------------|
| `standby_enabled` | `true` | bool | Enable/disable standby tracking |
| `standby_timeout_minutes` | `60` | 0–14400 (10 days) | Auto-remove standby streams after this time (0 = never) |

Configure in **Settings → Stream Standby** or via `POST /api/settings`.

### Behavior

1. Publisher connects → stream marked **active**
2. Publisher disconnects → stream marked **standby** with `disconnect_time`
3. Background sweep runs every 30 seconds, removes expired standby streams
4. State persists across server restarts (`data/standby_streams.json`)
5. WebSocket event `stream_standby_update` broadcasts on every change

### Manual Removal

```http
DELETE /api/streams/{stream_name}/standby
```

Remove a stream from standby tracking immediately.

----

## Docker Health Check

The container includes a built-in health check:

```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:3000/health || exit 1
```

Check container health:
```bash
docker inspect --format='{{.State.Health.Status}}' tak-video-restreamer
```

----

## GPU Encoding (Optional)

### Overview
NVIDIA NVENC is available as an optional drop-in replacement for CPU-based libx264. It handles the same workloads faster and at lower CPU cost — useful when transcoding several streams simultaneously or running on hardware where CPU headroom is limited. Quality at similar settings is comparable to software encoding.

### System Requirements

**Hardware:**
- NVIDIA GPU with NVENC support (GTX 1050+, RTX series, Tesla, Quadro)
- Minimum 2GB VRAM recommended
- PCIe connection to host system

**Software:**
- Docker with GPU support (`nvidia-docker2`)
- NVIDIA GPU drivers (version 470+)
- NVIDIA Container Toolkit installed on host

**Supported GPUs:**
- Consumer: GTX 1050 Ti, GTX 1060/1070/1080, RTX 2060/3060/4060 and higher
- Professional: Quadro P series, RTX A series, Tesla T4/V100
- Full list: https://developer.nvidia.com/video-encode-and-decode-gpu-support-matrix-new

### Installation

**1. Install NVIDIA Container Toolkit (Ubuntu/Debian):**
```bash
# Add repository
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | sudo apt-key add -
curl -s -L https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list | \
  sudo tee /etc/apt/sources.list.d/nvidia-docker.list

# Install
sudo apt-get update
sudo apt-get install -y nvidia-docker2

# Restart Docker
sudo systemctl restart docker

# Test GPU access
docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi
```

**2. Enable GPU in docker-compose.yml:**
```yaml
services:
  media:
    environment:
      - ENABLE_GPU_ENCODING=1  # Enable GPU encoding
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

**3. Restart container:**
```bash
docker-compose down
docker-compose up -d

# Verify GPU is detected
docker logs tak-video-restreamer | grep "GPU Encoding"
# Should show: "GPU Encoding: ENABLED - Using NVENC hardware acceleration"
```

### Performance Comparison

**CPU Encoding (libx264, 16 cores):**
- 1080p@30fps: ~60-80 fps encoding speed
- 4K@30fps: ~15-20 fps encoding speed
- Uses 80-100% CPU across all cores

**GPU Encoding (NVENC, RTX 3060):**
- 1080p@30fps: ~300-400 fps encoding speed (4-5x faster)
- 4K@30fps: ~100-120 fps encoding speed (5-6x faster)
- Uses <5% CPU, 20-30% GPU utilization

**Quality:**
- NVENC produces comparable quality to CPU encoding at similar bitrates
- Uses hardware-optimized presets (p1-p7, where p4 = medium quality)
- VBR mode with constant quality target (CQ=23, similar to CRF=18)

### Transcoding Options (All Support GPU)

1. **MOV with corrected timecode** - H.264 encoding
2. **MP4 + KLV backup** - H.264 encoding, KLV written to a separate `.klv.bin` sidecar
3. **MXF + KLV backup** - MPEG-2 encoding (CPU only, no NVENC support), KLV sidecar

None of these embed KLV into the container. To produce a stream with a real embedded
KLV data track, mux MPEG-TS with FFmpeg and keep the data stream with `-map 0` — see
[KLV transport constraints](#klv-transport-constraints).

### Troubleshooting

**GPU not detected:**
```bash
# Check NVIDIA drivers
nvidia-smi

# Verify Docker GPU access
docker run --rm --gpus all ubuntu nvidia-smi

# Check container logs
docker logs tak-video-restreamer | grep -i gpu
```

**FFmpeg NVENC errors:**
```bash
# Test NVENC availability in container
docker exec tak-video-restreamer ffmpeg -encoders | grep nvenc
# Should show: h264_nvenc, hevc_nvenc

# Check GPU memory
nvidia-smi --query-gpu=memory.used,memory.free --format=csv
```

**Concurrent encoding limit:**
- Consumer GPUs (GTX/RTX): 2-3 concurrent NVENC sessions
- Professional GPUs (Quadro/Tesla): Unlimited sessions
- Use `NVIDIA_VISIBLE_DEVICES=0` to limit to specific GPU

### Disabling GPU Encoding

To switch back to CPU encoding:

1. **Comment out environment variable:**
```yaml
# - ENABLE_GPU_ENCODING=1
```

2. **Restart container:**
```bash
docker-compose restart
```

GPU encoding is disabled by default for maximum compatibility.

----

## Stream Behavior

### Stream Path Uniqueness

**Each stream name (path) is unique and can only have one active publisher at a time.**

When a publisher connects to MediaMTX:
1. **First publisher connects to `drone1`** → Stream path is created and becomes active
2. **Second publisher connects to same `drone1` path** → MediaMTX **replaces the first publisher** with the second
3. The stream name remains `drone1`, but the video source has changed

**Key behaviors:**
- Stream names act as unique identifiers throughout the system
- Multiple readers/viewers can watch the same stream simultaneously
- Only one recording process per stream name is allowed
- Only one pull stream (external source) per stream name is allowed
- Attempting to start a duplicate recording returns an error: `Already recording`

### Recording and File Management

**Recordings are organized by stream name:**
```
data/streams/
├── drone1/
│   ├── recording-2026-01-22T10-30-00-000Z.mov
│   ├── recording-2026-01-22T11-00-00-000Z.mov
│   └── recording-2026-01-22T11-30-00-000Z_transcoded.klv.bin
├── drone2/
│   └── recording-2026-01-22T10-00-00-000Z.mov
└── camera1/
    └── recording-2026-01-22T09-00-00-000Z.mov
```

- All recordings for a stream go into the same directory: `data/streams/{stream_name}/`
- Recording filenames include ISO 8601 timestamps to prevent conflicts
- Each recording can have associated metadata files (KLV JSON, thumbnails)

### Stop vs Delete Operations

**Stop Stream** (`POST /api/streams/{stream_name}/stop`):
- Terminates all active processes (recording, pull streams, test publishers)
- Kicks all connected viewers/readers
- **Preserves the stream path** in MediaMTX
- Stream can immediately accept new publishers
- Use when temporarily pausing activity

**Delete Stream** (`DELETE /api/streams/{stream_name}`):
- Performs all Stop Stream actions
- **Removes the stream path** from the system
- Cleans up configuration and state
- Stream must be recreated before use
- Use for permanent removal

----

## API Documentation

### Stream Management

#### List Streams
```http
GET /api/streams
```

Returns list of all active streams with status information.

**Response:**
```json
[
  {
    "name": "drone1",
    "ready": true,
    "numReaders": 2,
    "bytesReceived": 1048576,
    "recording": true,
    "lastDataTime": "2025-12-09T21:34:33+00:00"
  }
]
```

#### List Stream Paths
```http
GET /api/streams/paths
```

Returns a simple list of all stream path names.

**Response:**
```json
[
  "drone1",
  "drone2",
  "camera1"
]
```

#### Get Stream Details
```http
GET /api/streams/{stream_name}
```

Get detailed information about a specific stream including recording and pull status.

**Response:**
```json
{
  "name": "drone1",
  "ready": true,
  "numReaders": 2,
  "bytesReceived": 1048576,
  "bytesSent": 524288,
  "recording": true,
  "pulling": false,
  "recordingInfo": {
    "startTime": 1732220400.5,
    "duration": 120.5,
    "outputFile": "/opt/app/streams/drone1/recording-2025-11-21T20-00-00-500Z.mov"
  }
}
```

#### Create Stream
```http
POST /api/streams/{stream_name}
```

Create a new stream path. Stream will be ready to accept publishers.

**Response:**
```json
{
  "success": true,
  "message": "Stream drone1 ready"
}
```

#### Stop Stream
```http
POST /api/streams/{stream_name}/stop
```

Stop all stream processes while preserving the stream path. This endpoint:
- Stops active recording (if running)
- Stops pull stream (if running)
- Stops test publishers (if running)
- Kicks all connected viewers/readers
- Disables auto-retry for pull streams
- **Keeps the stream path** - can immediately accept new publishers

**Response:**
```json
{
  "success": true,
  "message": "Stream drone1 stopped (stopped: recording, pull stream) (kicked 2 connections)"
}
```

#### Delete Stream
```http
DELETE /api/streams/{stream_name}
```

Permanently delete stream and stop all active processes. This endpoint:
- Performs all Stop Stream actions
- Removes the stream path from MediaMTX
- Cleans up stream configuration
- Requires recreation before the stream can be used again

**Response:**
```json
{
  "success": true,
  "message": "Stream drone1 deleted"
}
```

#### Start Recording
```http
POST /api/streams/{stream_name}/record
Content-Type: application/json

{
  "reencode": true,   // Optional: re-encode to fix corruption
  "copyMode": true    // Optional: force stream-copy (no re-encode) for fastest recording
}
```

#### Stop Recording
```http
POST /api/streams/{stream_name}/stop-record
```

Stop active recording for the stream.

**Response:**
```json
{
  "success": true,
  "message": "Recording stopped for drone1"
}
```

#### Get Stream Recording Status
```http
GET /api/streams/{stream_name}/recording-status
```

Get recording status for a specific stream.

**Response (Recording Active):**
```json
{
  "name": "drone1",
  "recording": true,
  "startTime": 1732220400.5,
  "duration": 120.5,
  "outputFile": "/opt/app/streams/drone1/recording.mov",
  "reencode": true
}
```

**Response (Not Recording):**
```json
{
  "name": "drone1",
  "recording": false
}
```

#### Get All Recording Status
```http
GET /api/recording-status
```

Get recording status for all streams.

**Response:**
```json
{
  "drone1": {
    "recording": true,
    "startTime": 1732220400.5,
    "duration": 120.5,
    "outputFile": "/opt/app/streams/drone1/recording.mov"
  },
  "camera1": {
    "recording": false
  }
}
```

#### Start Pull Stream
```http
POST /api/streams/{stream_name}/pull
Content-Type: application/json

{
  "url": "rtsp://source.example.com/stream",
  "username": "optional",
  "password": "optional"
}
```

#### Stop Pull Stream
```http
POST /api/streams/{stream_name}/stop-pull
```

Stop pulling from external source.

**Response:**
```json
{
  "success": true,
  "message": "Pull stream stopped for camera1"
}
```

#### Get Stream Pull Status
```http
GET /api/streams/{stream_name}/pull-status
```

Get pull stream status for a specific stream.

**Response:**
```json
{
  "name": "camera1",
  "active": true,
  "sourceUrl": "rtsp://192.168.1.100:554/stream",
  "retryCount": 2,
  "autoRetry": true,
  "uptime": 3600.5
}
```

#### Get All Pull Status
```http
GET /api/pull-status
```

Get status of all pull streams.

**Response:**
```json
[
  {
    "name": "camera1",
    "active": true,
    "sourceUrl": "rtsp://192.168.1.100:554/stream",
    "retryCount": 0,
    "autoRetry": true,
    "uptime": 3600.5
  },
  {
    "name": "camera2",
    "active": false,
    "sourceUrl": "rtsp://192.168.1.101:554/stream",
    "retryCount": 5,
    "autoRetry": true,
    "uptime": 0
  }
]
```

### Recording Management

#### List Recordings
```http
GET /api/recordings
```

Returns all recorded files grouped by stream. Each file includes a `status` field: `"recording"` (actively being recorded) or `"finalized"` (recording complete).

#### Download Recording
```http
GET /api/recordings/{stream_name}/{filename}
```

#### Delete Recording
```http
DELETE /api/recordings/{stream_name}/{filename}
```

#### Get Thumbnail
```http
GET /api/recordings/{stream_name}/{filename}/thumbnail
```

Get thumbnail image for a recording (JPEG format).

**Response:** Binary image data (image/jpeg)

#### Generate Thumbnail
```http
POST /api/recordings/{stream_name}/{filename}/generate-thumbnail
```

Generate or regenerate thumbnail for a recording.

**Response:**
```json
{
  "success": true,
  "message": "Thumbnail generated",
  "thumbnailPath": "/opt/app/streams/drone1/recording.jpg"
}
```

#### Get Recording Metadata
```http
GET /api/recordings/{stream_name}/{filename}/metadata
```

Get metadata embedded in the recording file.

**Response:**
```json
{
  "title": "Recording: drone1",
  "date": "2025-11-21",
  "custom_stream": "drone1",
  "comment": "Recorded from stream - drone1",
  "keywords": "",
  "duration": 120.5,
  "codec": "h264",
  "resolution": "1920x1080"
}
```

### Transcoding APIs

#### Get Transcode Options
```http
GET /api/transcode/options
```

Returns available transcode options:
- Option 1: MOV with corrected timecode (no KLV) — the default
- Option 2: MP4 + KLV backup file
- Option 3: MXF + KLV backup (broadcast standard)

The web UI builds its option list from this endpoint, so the two can never drift apart.

#### Start Transcode
```http
POST /api/transcode
Content-Type: application/json

{
  "inputFile": "/opt/app/streams/drone1/recording.mov",
  "option": 1,  // 1-3, default is 1 (MOV with corrected timecode)
  "streamName": "drone1"  // Optional
}
```

Response:
```json
{
  "success": true,
  "message": "Transcode started",
  "transcodeId": "transcode_1732220400_1234",
  "inputFile": "/opt/app/streams/drone1/recording.mov",
  "option": 4,
  "expectedOutput": "/opt/app/streams/drone1/recording_transcoded.ts"
}
```

Transcode completion is broadcast via WebSocket with event type `transcode_complete`.

#### Get Transcode Status
```http
GET /api/transcode/{transcodeId}/status
```

Get status of a specific transcode job.

**Response (Running):**
```json
{
  "id": "transcode_1732220400_1234",
  "inputFile": "/opt/app/streams/drone1/recording.mov",
  "option": 4,
  "streamName": "drone1",
  "status": "running",
  "progress": 0,
  "startTime": 1732220400.5,
  "elapsed": 45.2,
  "expectedOutput": "/opt/app/streams/drone1/recording_transcoded.ts",
  "error": null
}
```

**Response (Completed):**
```json
{
  "id": "transcode_1732220400_1234",
  "inputFile": "/opt/app/streams/drone1/recording.mov",
  "option": 4,
  "streamName": "drone1",
  "status": "completed",
  "progress": 100,
  "startTime": 1732220400.5,
  "elapsed": 120.5,
  "completedTime": 1732220521.0,
  "duration": 120.5,
  "expectedOutput": "/opt/app/streams/drone1/recording_transcoded.ts",
  "outputFile": "/opt/app/streams/drone1/recording_transcoded.ts",
  "outputSize": 45678900,
  "error": null
}
```

**Response (Failed):**
```json
{
  "id": "transcode_1732220400_1234",
  "inputFile": "/opt/app/streams/drone1/recording.mov",
  "option": 4,
  "status": "failed",
  "progress": 0,
  "startTime": 1732220400.5,
  "elapsed": 30.2,
  "completedTime": 1732220430.7,
  "duration": 30.2,
  "error": "FFmpeg error: Input file not found"
}
```

#### Get All Transcode Status
```http
GET /api/transcode/status
```

Get status of all transcode jobs.

**Response:**
```json
{
  "active": [
    {
      "id": "transcode_1732220400_1234",
      "inputFile": "recording.mov",
      "option": 4,
      "status": "running",
      "progress": 45,
      "startTime": 1732220400.5,
      "elapsed": 30.5
    }
  ],
  "completed": [
    {
      "id": "transcode_1732220300_5678",
      "inputFile": "recording2.mov",
      "option": 4,
      "status": "completed",
      "progress": 100,
      "startTime": 1732220300.0,
      "elapsed": 120.5,
      "duration": 120.5,
      "outputFile": "recording2_transcoded.ts",
      "outputSize": 45678900
    }
  ],
  "failed": [],
  "total": 2
}
```

### KLV Transport Constraints

Two things silently strip or corrupt KLV. Both cost real debugging time, so they are
worth knowing before you wire up a source.

**1. KLV cannot be published over RTSP.** FFmpeg's RTP muxer has no SMPTE336M
payloader — mapping a KLV track into `-f rtsp` fails outright:

```
[rtp @ ...] Unsupported codec klv
```

Worse, FFmpeg's default stream selection never picks data streams, so a plain
`-c copy -f rtsp` publish *succeeds* and quietly drops the KLV. MediaMTX then reports
`tracks: ["H264"]` and there is nothing downstream to display. **Publish over SRT, and
always pass `-map 0`:**

```bash
ffmpeg -re -i input.ts -map 0 -c copy -f mpegts \
  "srt://localhost:8890?streamid=publish:drone1"
```

MediaMTX's RTSP *output* handles KLV correctly — it advertises the track as
`a=rtpmap:96 SMPTE336M/90000` (RFC 6597). The limitation is only on the publish side.

**2. Some UAS muxers stamp KLV with the video's B-frame timestamps.** The KLV PTS then
runs backwards across every reordered GOP. MPEG-TS tolerates this because DTS stays
monotonic, but RTP carries only one timestamp, so clients reading from MediaMTX's RTSP
output drop the packets with `non monotonically increasing dts`. TVR repairs this
automatically on pull streams via the `repair_klv_timestamps` setting (default on). To
fix a file offline, without re-encoding video:

```bash
ffmpeg -i broken.ts -map 0 -c copy -bsf:d "setts=pts=DTS:dts=DTS" -f mpegts fixed.ts
```

**TAK client playback.** WinTAK (PGSCMedia) is stricter than FFmpeg or VLC and fails
with an opaque `Media Fatal Error` on variable frame rate, High profile, or B-frames.
For a stream WinTAK reliably decodes:

```bash
-c:v libx264 -profile:v main -bf 0 -r 30 -fps_mode:v cfr -pix_fmt yuv420p
```

To check a source against all of the above, use the validator below.

### Stream Validation

`utils/validate_stream.py` checks a file or a live stream for KLV integrity and TAK
client compatibility, and reports what to do about each problem it finds.

```bash
python utils/validate_stream.py /opt/app/streams/drone1/recording.ts
python utils/validate_stream.py rtsp://localhost:8554/drone1
python utils/validate_stream.py recording.ts --json --window 15
```

Checks performed:

| Check | Fails when |
|---|---|
| KLV track present | No data track — usually means it was published over RTSP |
| KLV timestamps monotonic | PTS runs backwards (breaks at the RTP hop) |
| Constant frame rate | Frame intervals vary (a known WinTAK failure) |
| TAK-safe video profile | High profile or B-frames present |
| KLV PES `stream_id` | Not `0xBD`; advisory only, FFmpeg always writes `0xFC` |
| ST 0601 decodes | Payload is not a parseable UAS Local Set |

Exit code is `0` when all checks pass, `1` when any check fails, `2` if the target
could not be read.

#### Validate a Stream or Recording
```http
POST /api/stream/validate
Content-Type: application/json

{
  "streamName": "drone1"
}
```

Or against a recording, relative to `STREAMS_DIR`:

```json
{
  "videoFile": "drone1/recording.ts",
  "window": 10
}
```

Provide exactly one of `streamName` or `videoFile`. `window` is the number of seconds
to sample (1–60, default 10).

Response:
```json
{
  "success": true,
  "report": {
    "target": "drone1/recording.ts",
    "ok": false,
    "has_warnings": true,
    "checks": [
      {
        "id": "klv_timestamps",
        "name": "KLV timestamps monotonic",
        "status": "fail",
        "summary": "KLV PTS runs backwards on 75/217 steps (34.6%).",
        "hint": "Repair with: -c:d copy -bsf:d \"setts=pts=DTS:dts=DTS\"",
        "detail": { "backward_steps": 75, "backward_pct": 34.6 }
      }
    ]
  }
}
```

Each check carries a `status` of `pass`, `warn`, `fail`, or `skip`. The web UI surfaces
failing and warning checks as a banner on the stream card (**🔎 Check KLV** on any live
stream).

### KLV Parsing Support

`shared/klv.py` decodes the MISB ST 0601.19 UAS Datalink Local Set. Reading KLV out of
a file or a binary dump is also available as a CLI:

```bash
python utils/read_klv.py recording.ts
python utils/read_klv.py metadata.klv.bin --json out.json
```

**MISB ST 0601.19 Tags Supported (All 89):**
- Tag 1: Checksum
- Tag 2: UNIX Timestamp (microseconds)
- Tag 3-4: Mission ID / Platform Designation
- Tag 5-7: Platform Heading/Pitch/Roll (corrected formulas)
- Tag 13-14: Sensor Lat/Lon (±90°/±180° with (2^31-1) divisor)
- Tag 15: Sensor True Altitude (-900 to +19000m)
- Tag 21: Slant Range (0-5000000m)
- Tag 23-24: Target Lat/Lon
- Tag 40-48: Target/Frame Corner Coordinates
- Tag 51: Platform Vertical Speed (±180 m/s)
- Tag 65: UAS LS Version Number
- Tag 79-80: Platform/Gimbal Velocities (±327 m/s)
- Tag 82-89: Full Precision Corners (8-byte double)
- Plus 60+ additional tags (strings, flags, angles, positions)

### Test Pattern Generator APIs

Generate synthetic video test patterns with 1kHz audio tone for testing stream ingestion.

**Supported parameters (all endpoints):**
- `streamName` (required): Target stream name
- `resolution` (optional, default: `"1280x720"`): Video resolution (e.g. `"1920x1080"`)
- `duration` (optional, default: `60`): Duration in seconds. `0` = continuous (runs until manually stopped). Range: 5–3600.
- `pattern` (optional, default: `"testsrc"`): FFmpeg lavfi source — `testsrc`, `testsrc2`, `smptebars`, `smptehdbars`, `color`
- `framerate` (optional, default: `30`): Frame rate — `24`, `25`, `29.97`, `30`, `60`

#### Start SRT Test Pattern
```http
POST /api/test/srt
Content-Type: application/json

{
  "streamName": "test-input",
  "resolution": "1280x720",
  "duration": 60,
  "pattern": "smptebars"
}
```

Publishes to: `srt://localhost:8890?streamid=publish:test-input`
View at: `srt://localhost:8890?streamid=read:test-input`

#### Start RTSP Test Pattern (TCP)
```http
POST /api/test/rtsp
Content-Type: application/json

{
  "streamName": "test-input",
  "resolution": "1920x1080",
  "duration": 60
}
```

Publishes to: `rtsp://localhost:8554/test-input` (TCP transport)

#### Start RTSPS Test Pattern (TLS Encrypted)
```http
POST /api/test/rtsps
Content-Type: application/json

{
  "streamName": "test-secure",
  "resolution": "1920x1080",
  "duration": 60
}
```

Publishes to: `rtsps://localhost:8555/test-secure` (requires TLS)

#### Start RTSP Test Pattern (UDP)
```http
POST /api/test/rtsp-udp
Content-Type: application/json

{
  "streamName": "test-input",
  "resolution": "1920x1080",
  "duration": 60
}
```

Publishes to: `rtsp://localhost:8554/test-input` (UDP transport)

**Response (All Endpoints):**
```json
{
  "success": true,
  "testId": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "streamName": "test-input",
  "message": "Test pattern started",
  "url": "rtsp://localhost:8554/test-input"
}
```

#### List Active Tests
```http
GET /api/test/list
```

List all active test pattern publishers. Used by the test page for restore-on-refresh.

**Response:**
```json
{
  "success": true,
  "tests": [
    {
      "testId": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
      "running": true,
      "protocol": "rtsp",
      "streamName": "test-input",
      "resolution": "1280x720",
      "duration": 60,
      "pattern": "testsrc",
      "framerate": 30,
      "elapsed": 42
    }
  ]
}
```

#### Get Test Status
```http
GET /api/test/{testId}/status
```

Check if test pattern is still running.

**Response:**
```json
{
  "testId": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "running": true,
  "status": "running",
  "protocol": "rtsp",
  "streamName": "test-input",
  "resolution": "1280x720",
  "duration": 60,
  "pattern": "testsrc",
  "framerate": 30,
  "elapsed": 42
}
```

#### Stop Test Pattern
```http
POST /api/test/{testId}/stop
```

Terminate a running test pattern.

**Response:**
```json
{
  "success": true,
  "message": "Test stopped"
}
```

### Authentication APIs

#### Login
```http
POST /api/auth/login
Content-Type: application/json

{
  "username": "admin",
  "password": "changeme"
}
```

Rate-limited to 5 requests per minute.

**Response:**
```json
{
  "success": true,
  "redirect": "/",
  "default_password": true
}
```

#### Logout
```http
POST /api/auth/logout
```

#### Check Auth Status (Public)
```http
GET /api/auth/status
```

**Response:**
```json
{
  "authenticated": true,
  "username": "admin",
  "default_password": false
}
```

#### Get Current User
```http
GET /api/auth/me
```

#### List API Keys
```http
GET /api/auth/keys
```

**Response:**
```json
[
  {
    "hash": "a1b2c3...",
    "name": "My Integration",
    "created": "2026-01-20T14:30:00Z"
  }
]
```

#### Create API Key
```http
POST /api/auth/keys
Content-Type: application/json

{
  "name": "My Integration"
}
```

**Response:**
```json
{
  "success": true,
  "key": "tvr_abc123...",
  "name": "My Integration",
  "hash": "a1b2c3..."
}
```

> **Note:** The raw `key` value is returned only once. Store it immediately.

#### Revoke API Key
```http
DELETE /api/auth/keys/{key_hash}
```

#### Get Audit Log
```http
GET /api/audit?lines=200
```

Returns the most recent audit log entries (default 200 lines).

### TLS / Certificate Management APIs

#### Get TLS Settings
```http
GET /api/tls/settings
```

**Response:**
```json
{
  "rtsps_enabled": true,
  "https_enabled": false,
  "cert_status": {
    "has_cert": true,
    "issuer": "Let's Encrypt Authority X3",
    "subject": "media.example.com",
    "expires": "2026-04-20T12:00:00Z",
    "self_signed": false
  }
}
```

#### Update TLS Settings
```http
POST /api/tls/settings
Content-Type: application/json

{
  "rtsps_enabled": true,
  "https_enabled": false
}
```

#### Generate Self-Signed Certificate
```http
POST /api/tls/self-signed
Content-Type: application/json

{
  "common_name": "localhost"
}
```

#### Request Let's Encrypt Certificate
```http
POST /api/tls/letsencrypt
Content-Type: application/json

{
  "domain": "media.example.com",
  "email": "admin@example.com"
}
```

Requires certbot installed in the container (included in Docker image) and **port 80 exposed and publicly reachable**. The endpoint runs `certbot --standalone` which binds port 80 inside the container to complete the ACME HTTP-01 challenge. Add `- "80:80"` to the `ports:` section in `docker-compose.yml` before calling this endpoint, then remove it afterwards.

#### Renew Let's Encrypt Certificate
```http
POST /api/tls/renew
```

#### Upload Custom Certificate
```http
POST /api/tls/upload
Content-Type: multipart/form-data

cert: (file) server.crt
key: (file) server.key
```

Accepted extensions: `.crt`, `.pem`, `.cer` for certificate; `.key`, `.pem` for private key.

#### Get Certificate Status
```http
GET /api/tls/cert-status
```

### ABR HLS APIs

#### Enable ABR for Stream
```http
POST /api/streams/{stream_name}/abr
```

Starts FFmpeg multi-rendition HLS transcoding for the stream.

**Response:**
```json
{
  "success": true,
  "message": "ABR started for drone1",
  "master_playlist": "/hls/drone1/master.m3u8"
}
```

#### Disable ABR for Stream
```http
DELETE /api/streams/{stream_name}/abr
```

#### Get ABR Status for Stream
```http
GET /api/streams/{stream_name}/abr/status
```

**Response:**
```json
{
  "stream": "drone1",
  "active": true,
  "master_playlist": "/hls/drone1/master.m3u8",
  "variants": [
    {"index": 0, "resolution": "1280x720", "bitrate": 2500000},
    {"index": 1, "resolution": "640x360", "bitrate": 800000}
  ]
}
```

#### List All Active ABR Processes
```http
GET /api/abr
```

#### Get ABR Settings
```http
GET /api/settings/abr
```

Returns current ABR rendition settings and dropdown options.

**Response:**
```json
{
  "high": {"resolution": "1280x720", "bitrate": 2500, "framerate": 30},
  "low": {"resolution": "640x360", "bitrate": 800, "framerate": 15},
  "options": {
    "resolutions": ["1920x1080", "1280x720", "854x480", "640x360", "426x240"],
    "bitrates": [4000, 2500, 1500, 1000, 800, 500, 300],
    "framerates": [30, 25, 15, 10]
  }
}
```

#### Update ABR Settings
```http
POST /api/settings/abr
Content-Type: application/json

{
  "high": {"resolution": "1920x1080", "bitrate": 4000, "framerate": 30},
  "low": {"resolution": "854x480", "bitrate": 1000, "framerate": 15}
}
```

### HLS CORS Proxy API

#### Proxy MediaMTX HLS (Public — No Auth)
```http
GET /api/hls/proxy/{stream_name}/{filename}
```

Proxies HLS content from MediaMTX with full CORS headers. Supports `GET`, `HEAD`, and `OPTIONS` methods.

- `.m3u8` playlists have segment URLs rewritten to go through the proxy
- Valid filenames: alphanumeric, dots, underscores, hyphens (`.m3u8`, `.ts`, `.m4s`, `.mp4`)
- Stream names: alphanumeric, underscores, dots, hyphens (max 128 chars)

**CORS Headers:**
```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, HEAD, OPTIONS
Access-Control-Allow-Headers: Content-Type, Range, X-API-Key
Access-Control-Max-Age: 86400
```

### Standby API

#### Remove Stream from Standby
```http
DELETE /api/streams/{stream_name}/standby
```

Immediately removes a stream from standby tracking.

**Response:**
```json
{
  "success": true,
  "message": "Removed drone1 from standby"
}
```

### System Status

#### Health Check
```http
GET /health
```

**Response:**
```json
{
  "status": "healthy",
  "timestamp": "2025-12-09T21:34:33.239891+00:00",
  "systemStartTime": "2025-12-09T20:00:00+00:00",
  "mediamtx": "up",
  "klvAvailable": true,
  "srtBufferAvailable": true,
  "activeRecordings": 2,
  "activePullStreams": 1,
  "pullStreamConfigs": 3,
  "autoRecordEnabled": false
}
```

The `status` field returns `"healthy"` when MediaMTX is reachable, or `"degraded"` when MediaMTX is down. The `mediamtx` field shows `"up"` or `"down"` explicitly.

#### Get Settings
```http
GET /api/settings
```

Get all system settings including disk space and configuration.

**Response:**
```json
{
  "autoRecord": false,
  "disk": {
    "total_gb": 943.72,
    "used_gb": 326.46,
    "free_gb": 617.26,
    "percent_used": 34.6
  },
  "settings": {
    "auto_cleanup_enabled": false,
    "cleanup_days": 30,
    "max_file_size_gb": 10,
    "min_free_space_gb": 10,
    "segment_duration": 600,
    "segmented_recording": false,
    "auto_reconnect": true,
    "max_reconnect_attempts": -1,
    "reconnect_delay": 5,
    "exponential_backoff": false,
    "max_backoff_delay": 60,
    "connection_timeout": 5000000,
    "enable_ffmpeg_reconnect": true,
    "repair_klv_timestamps": true,
    "rtsp_transport": "tcp",
    "srt_buffer_enabled": true,
    "srt_max_buffer_seconds": 30,
    "srt_auto_reconnect": true,
    "srt_reconnect_delay": 2,
    "health_check_enabled": true,
    "stall_detection_enabled": true,
    "stall_threshold_seconds": 30,
    "standby_enabled": true,
    "standby_timeout_minutes": 60
  }
}
```

#### Get Auto-Record Status
```http
GET /api/auto-record-status
```

Check if auto-record is enabled globally.

**Response:**
```json
{
  "enabled": false
}
```

#### Toggle Auto-Record
```http
POST /api/auto-record-toggle
Content-Type: application/json

{
  "enabled": true
}
```

Enable or disable auto-record globally. When enabled, all new streams automatically start recording.

**Response:**
```json
{
  "success": true,
  "enabled": true,
  "message": "Auto-record enabled"
}
```

#### Post-Processing Status
```http
GET /api/post-processing/status
```

Get status of background post-processing tasks.

**Response:**
```json
{
  "queue": [
    {
      "file": "recording.mov",
      "status": "processing",
      "progress": 45,
      "startTime": 1732220400.5
    }
  ]
}
```

### WebSocket Events

Connect to `ws://localhost:3000/socket.io/` for real-time updates.

**Events emitted:**

- `stream_created` - New stream path created
  ```json
  {"name": "drone1"}
  ```

- `stream_deleted` - Stream path deleted
  ```json
  {"name": "drone1"}
  ```

- `recording_started` - Recording started for stream
  ```json
  {"name": "drone1", "outputFile": "/opt/app/streams/drone1/recording.mov"}
  ```

- `recording_stopped` - Recording stopped for stream
  ```json
  {"name": "drone1", "duration": 120.5, "outputFile": "/opt/app/streams/drone1/recording.mov"}
  ```

- `pull_stream_started` - Pull stream connected
  ```json
  {"name": "camera1", "sourceUrl": "rtsp://192.168.1.100:554/stream"}
  ```

- `pull_stream_stopped` - Pull stream disconnected
  ```json
  {"name": "camera1"}
  ```

- `pull_stream_reconnected` - Pull stream auto-reconnected after failure
  ```json
  {"name": "camera1", "retryCount": 3}
  ```

- `transcode_complete` - Transcode operation finished
  ```json
  {
    "success": true,
    "inputFile": "/opt/app/streams/drone1/recording.mov",
    "outputFile": "/opt/app/streams/drone1/recording.ts",
    "option": 4
  }
  ```

- `test_complete` - Test pattern finished
  ```json
  {"testId": "srt_1732220400_1234", "streamName": "test-input"}
  ```

- `stream_standby_update` - Stream standby state changed
  ```json
  {
    "streams": [
      {"name": "drone1", "status": "standby", "disconnect_time": "2026-01-20T14:30:00Z"},
      {"name": "drone2", "status": "active", "last_seen": "2026-01-20T14:35:00Z"}
    ]
  }
  ```

- `abr_status` - ABR transcoding state changed
  ```json
  {"stream": "drone1", "active": true}
  ```

- `pull_stream` - Pull stream status update
  ```json
  {"name": "camera1", "active": true, "sourceUrl": "rtsp://..."}
  ```

- `stream_stopped` - Stream explicitly stopped
  ```json
  {"name": "drone1"}
  ```

- `codec_detecting` - Codec detection started for a recording
  ```json
  {"stream": "drone1", "file": "recording.mov"}
  ```

## Configuration

### Environment Variables
Edit `docker-compose.yml` environment variables:
```yaml
environment:
  - PORT=3000                    # Flask web UI port
  - MEDIAMTX_API_URL=http://localhost:8889  # MediaMTX API endpoint
  - MEDIAMTX_RTSP_URL=rtsp://127.0.0.1:8554  # RTSP source for ABR FFmpeg (use 127.0.0.1, not localhost)
  - MEDIAMTX_HLS_URL=http://127.0.0.1:8888   # Upstream HLS address for CORS proxy
  - STREAMS_DIR=/opt/app/streams # Recording storage path
  - DATA_DIR=/opt/app/data       # Application data directory
  - LOGS_DIR=/opt/app/logs       # Log directory (app logs, FFmpeg logs)
  - HLS_OUTPUT_DIR=/opt/app/hls  # ABR HLS segments/playlists directory
  - FFMPEG_LOG_DIR=/opt/app/logs/ffmpeg  # FFmpeg stderr log directory
  - ACTIVE_CERTS_DIR=/opt/app/certs  # TLS certificate directory
  - ADMIN_USERNAME=admin         # Login username
  - ADMIN_PASSWORD=changeme      # Login password (CHANGE THIS!)
  # - HTTPS_ENABLED=true         # Enable HTTPS for web UI
  # - SECRET_KEY=your-secret     # Flask session secret (auto-generated if omitted)
  - ENABLE_GPU_ENCODING=1        # Optional: Enable NVIDIA GPU encoding
  # - AUTO_GENERATE_CERTS=true   # Optional: auto-generate self-signed TLS certs on startup
  # - HLS_LOW_LATENCY_MODE=true  # Optional: enable low-latency HLS (shorter segments)
  - CORS_ORIGINS=http://localhost:3000  # Security: Restrict API access to specific domains
```

**Security Note - CORS Configuration:**

By default, the server allows API access from any origin (`*`), which is insecure for production deployments.

**For Production:** Set `CORS_ORIGINS` to restrict API access to trusted domains:
```yaml
environment:
  - CORS_ORIGINS=http://localhost:3000,https://yourdomain.com
```

**For Local Development Only:**
```yaml
environment:
  - CORS_ORIGINS=http://localhost:3000
```

**Why This Matters:**
- CORS (Cross-Origin Resource Sharing) controls which websites can access your API
- Without restrictions, malicious websites could interact with your media server
- In production, this could allow unauthorized access to streams, recordings, and settings
- Always restrict CORS origins when deploying to a public network

### Ports
The following ports are exposed by the container:
- **3000**: Flask Web UI and REST API (login required)
- **8554**: RTSP server (TCP/UDP)
- **8555**: RTSPS server (TLS encrypted)
- **8890**: SRT server (UDP)
- **8888**: HLS server (MediaMTX native)
- **8889**: MediaMTX API (bound to localhost only for security)

### Windows Firewall Configuration (Docker)

When running under Docker Desktop on Windows, the container processes are isolated inside a Linux VM. Windows Firewall sees traffic arriving from that VM on the host's network interfaces, so the ports must be explicitly opened before external devices (ATAK tablets, cameras, other computers) can reach the RTSP and SRT servers.

#### Open Ports via PowerShell (Run as Administrator)

```powershell
# RTSP TCP (port 8554)
New-NetFirewallRule -DisplayName "TAK Restreamer RTSP TCP" `
    -Direction Inbound -Protocol TCP -LocalPort 8554 `
    -Action Allow -Profile Any

# RTSP UDP (port 8554) — required for UDP transport
New-NetFirewallRule -DisplayName "TAK Restreamer RTSP UDP" `
    -Direction Inbound -Protocol UDP -LocalPort 8554 `
    -Action Allow -Profile Any

# RTSPS TCP (port 8555) — TLS-encrypted RTSP
New-NetFirewallRule -DisplayName "TAK Restreamer RTSPS TCP" `
    -Direction Inbound -Protocol TCP -LocalPort 8555 `
    -Action Allow -Profile Any

# SRT UDP (port 8890)
New-NetFirewallRule -DisplayName "TAK Restreamer SRT UDP" `
    -Direction Inbound -Protocol UDP -LocalPort 8890 `
    -Action Allow -Profile Any

# HLS HTTP (port 8888) — optional, only if clients connect directly to MediaMTX HLS
New-NetFirewallRule -DisplayName "TAK Restreamer HLS HTTP" `
    -Direction Inbound -Protocol TCP -LocalPort 8888 `
    -Action Allow -Profile Any

# Web UI / REST API (port 3000)
New-NetFirewallRule -DisplayName "TAK Restreamer Web UI" `
    -Direction Inbound -Protocol TCP -LocalPort 3000 `
    -Action Allow -Profile Any
```

#### Verify the Rules Were Created

```powershell
Get-NetFirewallRule -DisplayName "TAK Restreamer*" | Select-Object DisplayName, Enabled, Direction, Action
```

#### Remove the Rules (if needed)

```powershell
Get-NetFirewallRule -DisplayName "TAK Restreamer*" | Remove-NetFirewallRule
```

#### Windows Firewall via GUI

If you prefer the GUI:
1. Open **Windows Defender Firewall with Advanced Security** (`wf.msc`)
2. Click **Inbound Rules → New Rule**
3. Select **Port**, click Next
4. Enter the port number and select TCP or UDP as appropriate
5. Select **Allow the connection**, apply to all profiles, and give it a name

#### Notes

- **Docker Desktop networking**: Docker Desktop on Windows uses a virtual network adapter (`vEthernet (WSL)` or `vEthernet (Default Switch)`). Traffic from containers to the host arrives on this adapter, so the firewall rules above use **Profile Any** to cover all adapter types.
- **Port 8889** (MediaMTX API) is intentionally excluded — it is bound to `localhost` inside the container only and should not be exposed externally.
- If your machine is on a **domain network**, Windows may apply Domain profile rules separately. You can scope the rules with `-Profile Domain,Private,Public` explicitly.
- After adding rules, confirm connectivity from another machine with:
  ```bash
  # Test RTSP reachability
  ffplay rtsp://YOUR-WINDOWS-IP:8554/stream_name
  
  # Test SRT reachability
  ffplay "srt://YOUR-WINDOWS-IP:8890?streamid=read:stream_name"
  ```

### TLS Certificates (RTSPS)

RTSPS (RTSP over TLS) is available on port 8555 with flexible certificate management. Certificates can be managed through the **web UI**, the **TLS API**, or by placing files directly.

**Certificate Management Options:**

| Method | How | Details |
|--------|-----|---------|
| **Web UI** | Settings → TLS/RTSPS Settings | Upload, generate self-signed, or request Let's Encrypt |
| **TLS API** | `POST /api/tls/self-signed`, `/api/tls/letsencrypt`, `/api/tls/upload` | Programmatic certificate management |
| **File System** | Place `server.crt` + `server.key` in `data/certs/` | Manual placement, container restart required |

**Method 1: Upload via Web UI (Easiest)**
1. Navigate to Settings → TLS/RTSPS Settings
2. Upload your certificate (.crt, .pem) and private key (.key, .pem) files
3. Restart the container: `docker-compose restart`

**Method 2: Place Files Directly**
1. Place your certificate and key in `./data/certs/`:
   - `data/certs/server.crt` - Your TLS certificate
   - `data/certs/server.key` - Your private key
2. Restart the container: `docker-compose restart`

**Supported Certificate Sources:**
- **Let's Encrypt** (recommended - free, automated, trusted)
- **Internal Certificate Authority** (for private networks)
- **Commercial CA** (DigiCert, Sectigo, GlobalSign, etc.)
- Self-signed certificates (testing/development only)

**How It Works:**
1. Container checks `./data/certs/` for `server.crt` and `server.key` at startup
2. If custom certificates are found, they are used
3. If not found, falls back to auto-generated self-signed certificate (1-year validity)
4. Certificates are automatically set with proper permissions (600 for private key)
5. Web UI displays current certificate status, issuer, and expiration date

**Generate Self-Signed Certificate (Testing Only):**

If you need a self-signed certificate for testing:

*Via Web UI:*
- Use the "Generate Self-Signed Certificate" button in Settings
- Creates 10-year RSA-4096 certificate

*Via Command Line:*
```bash
openssl req -x509 -newkey rsa:4096 -keyout server.key \
  -out server.crt -days 3650 -nodes \
  -subj "/C=US/ST=State/L=City/O=Organization/CN=your-domain.com"
```

**⚠️ Security Warning:**
Self-signed certificates will trigger security warnings in RTSP clients and browsers. Certificates are generated with a Subject Alternative Name (SAN) matching the common name you provide — this is required by Chrome and modern ATAK builds, which reject certs that only have a CN. For production deployments, use a certificate from a trusted Certificate Authority.

### Let's Encrypt Certificates (Recommended for Production)

Let's Encrypt provides free, automated, and trusted SSL/TLS certificates. This is the **recommended approach** for production deployments.

**Requirements:**
- A public domain name (e.g., `media.example.com`)
- Port 80 accessible from the internet (for HTTP-01 challenge) — this means adding `- "80:80"` to `docker-compose.yml` and opening port 80 at your firewall
- OR DNS API access (for DNS-01 challenge — works behind NAT, no port 80 needed)

**Quick Setup (Automated Script):**

We provide a helper script that automates the entire process:

```bash
# Make script executable
chmod +x scripts/setup-letsencrypt.sh

# Run the setup script
sudo ./scripts/setup-letsencrypt.sh
```

The script will:
1. Check for certbot installation
2. Guide you through obtaining a certificate
3. Copy certificates to the correct location
4. Set proper permissions
5. Restart the container
6. Create an automatic renewal script

**Manual Setup Methods:**

**Method 1: Certbot (Standalone Mode)**

This method temporarily stops the container to obtain certificates.

1. **Install Certbot:**
```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install certbot

# CentOS/RHEL
sudo yum install certbot

# macOS
brew install certbot
```

2. **Stop the container (port 80 needed for verification):**
```bash
docker-compose down
```

3. **Obtain certificate:**
```bash
sudo certbot certonly --standalone \
  -d your-domain.com \
  --email your-email@example.com \
  --agree-tos \
  --non-interactive
```

4. **Copy certificates to your project:**
```bash
sudo cp /etc/letsencrypt/live/your-domain.com/fullchain.pem ./data/certs/server.crt
sudo cp /etc/letsencrypt/live/your-domain.com/privkey.pem ./data/certs/server.key
sudo chown $USER:$USER ./data/certs/server.*
chmod 644 ./data/certs/server.crt
chmod 600 ./data/certs/server.key
```

5. **Restart container:**
```bash
docker-compose up -d
```

6. **Set up automatic renewal:**
```bash
# Create renewal script
sudo tee /usr/local/bin/renew-media-certs.sh > /dev/null << 'EOF'
#!/bin/bash
certbot renew --quiet
if [ $? -eq 0 ]; then
    cp /etc/letsencrypt/live/your-domain.com/fullchain.pem /path/to/project/data/certs/server.crt
    cp /etc/letsencrypt/live/your-domain.com/privkey.pem /path/to/project/data/certs/server.key
    chmod 644 /path/to/project/data/certs/server.crt
    chmod 600 /path/to/project/data/certs/server.key
    cd /path/to/project && docker-compose restart
fi
EOF

sudo chmod +x /usr/local/bin/renew-media-certs.sh

# Add to crontab (runs daily, renews if needed)
sudo crontab -e
# Add this line:
0 2 * * * /usr/local/bin/renew-media-certs.sh
```

**Method 2: Certbot with Webroot (Container Running)**

This method works while the container is running if you expose port 80.

1. **Update docker-compose.yml to expose port 80:**
```yaml
services:
  media:
    ports:
      - "80:80"      # Add this line
      - "3000:3000"
      - "8554:8554"
      # ... rest of ports
```

2. **Create webroot directory:**
```bash
mkdir -p ./data/certbot-webroot
```

3. **Obtain certificate:**
```bash
sudo certbot certonly --webroot \
  -w ./data/certbot-webroot \
  -d your-domain.com \
  --email your-email@example.com \
  --agree-tos
```

4. **Copy certificates (same as Method 1 step 4)**

**Method 3: DNS Challenge (No Ports Required)**

Best for servers behind NAT/firewall or when ports 80/443 are unavailable.

1. **Install Certbot with DNS plugin:**
```bash
# Cloudflare example
sudo apt-get install python3-certbot-dns-cloudflare

# Other providers:
# python3-certbot-dns-route53 (AWS Route53)
# python3-certbot-dns-google (Google Cloud DNS)
# python3-certbot-dns-digitalocean
```

2. **Create credentials file:**
```bash
# For Cloudflare
mkdir -p ~/.secrets/certbot
tee ~/.secrets/certbot/cloudflare.ini > /dev/null << EOF
dns_cloudflare_api_token = your-api-token-here
EOF
chmod 600 ~/.secrets/certbot/cloudflare.ini
```

3. **Obtain certificate:**
```bash
sudo certbot certonly --dns-cloudflare \
  --dns-cloudflare-credentials ~/.secrets/certbot/cloudflare.ini \
  -d your-domain.com \
  -d *.your-domain.com \
  --email your-email@example.com \
  --agree-tos
```

4. **Copy certificates (same as Method 1 step 4)**

**Method 4: Docker Certbot (Fully Containerized)**

Manage certificates entirely within Docker.

1. **Create docker-compose override:**

Create `docker-compose.override.yml`:
```yaml
version: '3.8'

services:
  certbot:
    image: certbot/certbot:latest
    volumes:
      - ./data/certbot/conf:/etc/letsencrypt
      - ./data/certbot/www:/var/www/certbot
    entrypoint: "/bin/sh -c 'trap exit TERM; while :; do certbot renew; sleep 12h & wait $${!}; done;'"

  media:
    volumes:
      - ./data/certbot/conf/live/your-domain.com/fullchain.pem:/opt/app/external-certs/server.crt:ro
      - ./data/certbot/conf/live/your-domain.com/privkey.pem:/opt/app/external-certs/server.key:ro
```

2. **Obtain initial certificate:**
```bash
docker-compose run --rm certbot certonly --standalone \
  -d your-domain.com \
  --email your-email@example.com \
  --agree-tos \
  --non-interactive
```

3. **Start services:**
```bash
docker-compose up -d
```

**Verifying Let's Encrypt Certificates:**

After installation, verify in the Web UI:
1. Go to Settings → TLS/RTSPS Settings
2. Check certificate status shows:
   - ✓ Custom certificates installed
   - Issuer: Let's Encrypt Authority X3 (or similar)
   - Subject: your-domain.com
   - Expires: (90 days from issuance)

**Testing RTSPS with Let's Encrypt:**
```bash
# Should work without certificate warnings
ffplay rtsps://your-domain.com:8555/mystream

# VLC - no security warnings
vlc rtsps://your-domain.com:8555/mystream
```

**Important Notes:**
-  Let's Encrypt certificates expire every **90 days** - automation is critical
- Let's Encrypt will email you before expiration
- Certificates work for both RTSPS and HTTPS (if using reverse proxy)
- Must have a valid public domain name (no IP addresses)
- Rate limits: 50 certificates per registered domain per week

### HTTPS for Web UI

By default, the Flask web interface runs on HTTP (port 3000). For secure browser access, you can enable HTTPS using a reverse proxy.

**Option 1: Nginx Reverse Proxy (Recommended)**

1. **Install Nginx:**
```bash
# Ubuntu/Debian
sudo apt-get install nginx

# Windows - download from nginx.org
```

2. **Configure Nginx with SSL:**

Create `/etc/nginx/sites-available/tak-video-restreamer` (Linux) or `nginx.conf` (Windows):

```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.com;

    # SSL Certificate (same ones used for RTSPS)
    ssl_certificate /path/to/server.crt;
    ssl_certificate_key /path/to/server.key;

    # Modern SSL configuration
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # Proxy to Flask
    location / {
        proxy_pass http://localhost:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
    }

    # WebSocket support
    location /socket.io/ {
        proxy_pass http://localhost:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }
}

# Redirect HTTP to HTTPS
server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$server_name$request_uri;
}
```

3. **Enable and restart Nginx:**
```bash
# Linux
sudo ln -s /etc/nginx/sites-available/tak-video-restreamer /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx

# Windows
nginx -t
nginx -s reload
```

4. **Access via HTTPS:**
```
https://your-domain.com
```

**Option 2: Caddy (Automatic HTTPS)**

Caddy automatically obtains and renews Let's Encrypt certificates.

1. **Install Caddy:**
```bash
# Linux
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install caddy
```

2. **Create Caddyfile:**

`/etc/caddy/Caddyfile`:
```
your-domain.com {
    reverse_proxy localhost:3000
}
```

3. **Start Caddy:**
```bash
sudo systemctl start caddy
sudo systemctl enable caddy
```

Caddy automatically obtains a trusted Let's Encrypt certificate and handles renewals!

**Option 3: Traefik (Docker-native)**

If running in Docker, Traefik integrates seamlessly.

Update `docker-compose.yml`:
```yaml
version: '3.8'

services:
  traefik:
    image: traefik:v2.10
    container_name: traefik
    command:
      - "--providers.docker=true"
      - "--entrypoints.web.address=:80"
      - "--entrypoints.websecure.address=:443"
      - "--certificatesresolvers.letsencrypt.acme.email=your-email@example.com"
      - "--certificatesresolvers.letsencrypt.acme.storage=/acme.json"
      - "--certificatesresolvers.letsencrypt.acme.httpchallenge.entrypoint=web"
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./traefik-acme.json:/acme.json
    restart: unless-stopped

  media:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: tak-video-restreamer
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.media.rule=Host(`your-domain.com`)"
      - "traefik.http.routers.media.entrypoints=websecure"
      - "traefik.http.routers.media.tls.certresolver=letsencrypt"
      - "traefik.http.services.media.loadbalancer.server.port=3000"
    # Remove port 3000 exposure (Traefik handles it)
    ports:
      - "8554:8554"
      - "8555:8555"
      - "8890:8890/udp"
      - "8888:8888"
    # ... rest of your config
```

**Security Best Practices:**

- ✅ Use certificates from trusted CA (Let's Encrypt, commercial CA)
- ✅ Enable HTTP to HTTPS redirect (force SSL)
- ✅ Use modern TLS protocols (TLSv1.2, TLSv1.3)
- ✅ Enable HSTS headers for browsers
- ✅ Set up automatic certificate renewal
- ⚠️ Don't expose port 3000 directly if using reverse proxy
- ⚠️ Keep reverse proxy and certificates up to date

---

## File Structure

```
├── main.py                 # Flask application entry point
├── requirements.txt        # Python dependencies
├── Dockerfile             # Production container image (with certbot, HEALTHCHECK)
├── docker-compose.yml     # Container orchestration config
├── mediaMTX.yml          # MediaMTX streaming server config
├── app/                  # Modular Flask application
│   ├── __init__.py       # App factory, blueprint registration, auth middleware
│   ├── config.py         # Configuration management & settings schema
│   ├── state.py          # Application state management
│   ├── api/              # REST API blueprints
│   │   ├── health.py     # Health check endpoints
│   │   ├── streams.py    # Stream management & standby
│   │   ├── recordings.py # Recording operations
│   │   ├── settings.py   # Settings, SRT, ABR, and certificate APIs
│   │   ├── utils.py      # Utility operations (transcode, KLV)
│   │   ├── test.py       # Test pattern generation
│   │   ├── hls.py        # ABR HLS transcoding & CORS proxy
│   │   ├── auth_api.py   # Authentication, API keys, audit log
│   │   └── tls_api.py    # TLS certificate management
│   ├── auth.py           # Flask-Login setup, credentials, API key management
│   ├── services/         # Business logic services
│   │   ├── abr.py        # ABR HLS transcoding service
│   │   ├── mediamtx.py   # MediaMTX API client
│   │   ├── standby.py    # Stream standby manager
│   │   └── tls.py        # TLS certificate operations
│   ├── utils/            # Application utilities
│   │   ├── codec_detection.py  # Video codec detection
│   │   └── thumbnail.py        # Thumbnail generation
│   └── websocket/        # WebSocket event handling
│       ├── events.py     # Socket.IO event handlers
│       └── broadcast.py  # Real-time event broadcasting
├── shared/               # Shared libraries
│   ├── klv.py           # MISB ST 0601.19 KLV parser
│   └── srt_buffer.py    # SRT stream buffering
├── utils/               # Standalone utilities
│   ├── read_klv.py           # KLV reading utility
│   ├── validate_stream.py    # KLV / TAK-compatibility preflight validator
│   └── transcode_video.py    # Video transcoding script
├── scripts/             # Automation scripts
│   └── setup-letsencrypt.sh  # Let's Encrypt setup helper
├── web/static/          # Web UI files
│   ├── index.html       # Main dashboard
│   ├── recordings.html  # Recordings management
│   ├── settings.html    # Settings (auth, TLS, ABR, standby, API keys, audit)
│   ├── utils.html       # Utilities page
│   ├── test_video_input.html  # Test pattern generator
│   ├── videowall.html   # Multi-stream video wall
│   ├── login.html       # Login page
│   ├── client.js        # WebSocket client and UI logic
│   ├── styles.css       # Dark theme styling
│   ├── socket.io.min.js # Socket.IO 4.7.5 (self-hosted for offline use)
│   └── hls.min.js       # HLS.js 1.5.17 (self-hosted for offline use)
├── data/                # Runtime data (Docker volumes)
│   ├── streams/         # Recorded video files
│   ├── hls/             # ABR HLS segments and playlists
│   ├── logs/            # Application logs and audit log
│   └── certs/           # TLS certificates (server.crt, server.key)
```

----

## Testing

### Running Tests

The project includes a comprehensive test suite with 69 tests covering all API endpoints and web interfaces.

```bash
# Run all tests with verbose output
python test_app.py

# Run tests with pytest directly
pytest test_app.py -v

# Run specific test class
python test_app.py -k TestHealthEndpoint

# Run specific test
python test_app.py -k test_health_endpoint_exists

# Run with coverage report
pytest test_app.py --cov=app --cov-report=html
```

### Test Coverage

The test suite (`test_app.py`) includes:

**Health & Status** (7 tests)
- Health endpoint validation
- JSON response structure
- Status fields (activeRecordings, klvAvailable, srtBufferAvailable)
- Timestamp verification

**Stream Management** (12 tests)
- List streams endpoint
- Create/delete pull streams
- Stream validation
- Complete pull stream workflow

**Recording Control** (5 tests)
- Start/stop recording endpoints
- Stream name validation
- Recording workflow tests

**Recordings Management** (7 tests)
- List recordings with filtering
- Download recordings
- Delete recordings
- Recording metadata structure
- Video file filtering (excludes .bin, .json)

**Thumbnail Generation** (5 tests)
- Generate thumbnails for recordings
- Retrieve thumbnail images
- Content type validation
- Error handling for missing files

**Test Pattern Generator** (9 tests)
- SRT test pattern generation
- RTSP test patterns (TCP/UDP)
- RTSPS encrypted test patterns
- Test status monitoring
- Stop test patterns
- Complete test workflow

**Settings Management** (6 tests)
- Get/update settings
- Auto-record configuration
- Settings structure validation
- HTTP method validation

**Web Interface** (7 tests)
- Index page loading
- Recordings page
- Settings page
- Utils page
- Test pattern page
- Static assets (CSS, JS)

**Integration Tests** (3 tests)
- Multi-endpoint workflows
- API interaction patterns
- End-to-end scenarios

**Error Handling** (3 tests)
- 404 for invalid endpoints
- 405 for wrong HTTP methods
- 400 for malformed requests

**Utilities** (5 tests)
- Codec detection module
- Thumbnail utilities
- File validation
- Video extension filtering

### Prerequisites

Install pytest in your virtual environment:

```bash
# Activate virtual environment
.\venv_media\Scripts\Activate.ps1  # Windows
source venv_media/bin/activate      # Linux/Mac

# Install pytest
pip install pytest

# Optional: Install coverage tools
pip install pytest-cov
```

### Test Notes

- Test pattern generator tests may return 500 errors if FFmpeg is not available in the test environment
- Tests use Flask's test client for isolated endpoint testing
- No Docker container required for unit tests
- Integration tests verify multi-step workflows
- All tests use the same app configuration defined in `app/__init__.py`

----

## Troubleshooting

**Container won't start:**
```bash
docker logs media-server
```

**Recording corruption:**
Enable re-encoding when starting recording via UI or API.

**Port conflicts:**
Check ports 3000, 8554, 8890, 8888 are available.

**ABR streams freeze after ~200-230 seconds:**
This was caused by a `subprocess.PIPE` deadlock — FFmpeg's stderr output fills the 64KB OS pipe buffer and `write()` blocks forever. The fix: redirect stderr to a log file instead of PIPE, and use `-loglevel warning` to suppress progress output. If you encounter this in a fork, ensure `stderr` is **never** set to `subprocess.PIPE` without actively reading from it (e.g. via `communicate()` or a reader thread).

**ABR streams stall but FFmpeg process is still alive:**
Check the FFmpeg log at `data/logs/ffmpeg/{stream}.log`. Common causes:
- RTSP source disconnected (publisher crashed/restarted)
- FFmpeg entered D-state (uninterruptible sleep due to I/O hang) — the stall detector will orphan it and start a new process
- Network timeout — streams auto-recover within `STALL_TIMEOUT` (30s)

**HLS playback freezes in browser but server is fine:**
- Use the built-in player at `/hls/{stream}/player` which has full error recovery
- Chrome desktop cannot play `.m3u8` natively — you need hls.js or a browser extension
- Ensure your hls.js implementation includes `Hls.Events.ERROR` handling (see Embedding section above)

**ABR environment variables:**

| Variable | Default | Description |
|----------|---------|-------------|
| `MEDIAMTX_RTSP_URL` | `rtsp://127.0.0.1:8554` | RTSP source URL for FFmpeg ABR transcoding |
| `MEDIAMTX_HLS_URL` | `http://127.0.0.1:8888` | Upstream MediaMTX HLS for CORS proxy |
| `HLS_OUTPUT_DIR` | `/opt/app/hls` | Directory for HLS segments/playlists |
| `HLS_SEGMENT_DURATION` | `2` | Segment duration in seconds |
| `HLS_LIST_SIZE` | `10` | Number of segments in playlist |
| `HLS_LOW_LATENCY_MODE` | `false` | Enable low-latency HLS (shorter segments, faster start) |
| `FFMPEG_LOG_DIR` | `$LOGS_DIR/ffmpeg` | FFmpeg stderr log directory |
| `LOGS_DIR` | `/opt/app/logs` | Application log directory |

> **Note:** Use `127.0.0.1` rather than `localhost` for `MEDIAMTX_RTSP_URL` and `MEDIAMTX_HLS_URL`. On Windows, `localhost` may resolve to `::1` (IPv6) first, and if MediaMTX only binds IPv4, FFmpeg will hang waiting for the connection.

----

## Known Issues

### Docker + SRT Reliability

SRT streams published through Docker may experience intermittent drops or reconnection issues. This is caused by Docker's userland NAT proxy (`docker-proxy`) handling UDP traffic:

- Docker's port forwarding for UDP (`8890:8890/udp`) adds a NAT layer that can introduce packet loss and timing jitter
- SRT's built-in error correction (ARQ) relies on precise round-trip timing — the extra hop can cause retransmission storms
- Under high bitrate or bursty traffic, the `docker-proxy` process can lag behind, causing SRT receiver buffer underruns

**Workarounds:**

1. **Run locally without Docker** using `run-local.ps1` (Windows) or the manual setup (Linux/macOS) — this eliminates the NAT layer entirely and is the most reliable option for SRT
2. **Use `network_mode: host`** in `docker-compose.yml` (Linux only) — binds container ports directly to the host network stack, bypassing Docker's NAT:
   ```yaml
   services:
     media:
       network_mode: host
       # Remove the 'ports:' section when using host networking
   ```
3. **Increase SRT buffers** — in `mediaMTX.yml`, increase `writeQueueSize` (default 512) and consider tuning `readTimeout` / `writeTimeout`

**Symptoms to watch for:**
- SRT publisher shows connected but MediaMTX logs `[SRT] [conn ...] closed` shortly after
- HLS/RTSP viewers see the stream freeze and recover in bursts
- `srt-live-transmit` reports high packet loss when publishing through Docker but not when connecting directly

### ABR Stall Detection with Stale Playlists

If the server is restarted and old HLS segment files remain on disk from a previous run, the ABR stall detector could incorrectly flag the stream as stalled (the playlist mtime predates the new FFmpeg process). This has been fixed — the stall detector now ignores playlist files older than the current FFmpeg process start time.

### IPv6 Resolution on Windows

On Windows, `localhost` may resolve to `::1` (IPv6) before `127.0.0.1` (IPv4). If MediaMTX binds on `0.0.0.0` (IPv4 only), connections from FFmpeg or internal HTTP clients will hang indefinitely. All internal defaults now use `127.0.0.1` explicitly. If you override `MEDIAMTX_API_URL`, `MEDIAMTX_RTSP_URL`, or `MEDIAMTX_HLS_URL`, use `127.0.0.1` instead of `localhost`.

## License
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.
 
This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/
