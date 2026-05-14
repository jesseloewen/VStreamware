# VStreamware

<p align="center">
  <img src="app/static/icons/icon-64.png" alt="VStreamware icon" width="96" height="96">
</p>

<p align="center">
  Twitch recording dashboard with channel watchlists, automatic recording, chat capture, and browser playback.
</p>

<p align="center">
  Created by <a href="https://jesseloewen.com">jesseloewen.com</a>
</p>

## What This App Does

VStreamware is a Flask-based web app for tracking Twitch channels and recording streams with Streamlink.

It provides:

- Saved channel management
- Per-channel auto recording toggles
- Manual start/stop recording
- Per-channel notification preferences
- Optional Pushover notifications
- Optional Twitch chat capture per recording
- Video browser with thumbnails and filters
- In-browser playback with live DVR behavior for active `.ts` recordings
- Background TS-to-MP4 conversion queue for completed recordings
- Cache management for thumbnails and live DVR snapshot/transient media

## Core Features

- Dashboard and settings UI for channel operations
- Background auto-recorder worker that polls live state
- Recording file naming with sanitized stream titles and UTC timestamps
- Automatic in-place conversion of completed `.ts` recordings to `.mp4` (with source cleanup)
- Playback API that keeps active live `.ts` playback browser-friendly via live DVR snapshot/transmux behavior
- Chat sidecar files (`.chat.ndjson`) with timeline replay in the video detail view
- Health endpoint for uptime checks

## Tech Stack

- Python 3.10+
- Flask
- Streamlink
- FFmpeg
- python-dotenv
- tzdata (IANA timezone database for Python zoneinfo)

## Quick Start

### 1. Clone

```bash
git clone https://github.com/<your-account>/VStreamware.git
cd VStreamware
```

### 2. Create and activate a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

`tzdata` is included in `requirements.txt` so timezone selections like `America/New_York`
validate consistently across platforms, including Windows.

### 4. Configure environment variables

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

macOS/Linux:

```bash
cp .env.example .env
```

Edit `.env` and set the values you want.

### 5. Install system dependencies

You need both Streamlink and FFmpeg available in your PATH.

Windows (examples):

```powershell
winget install Streamlink.Streamlink
winget install Gyan.FFmpeg
```

macOS (Homebrew):

```bash
brew install streamlink ffmpeg
```

Ubuntu/Debian (example):

```bash
sudo apt update
sudo apt install -y streamlink ffmpeg
```

### 6. Run the app

```bash
python app.py
```

By default, the app runs on:

- http://localhost:8523

Health check:

- http://localhost:8523/health

## Configuration Reference

Default values are loaded from `.env.example` and `app/config.py`.

| Variable | Default | Purpose |
|---|---|---|
| `FLASK_RUN_HOST` | `0.0.0.0` | Host to bind the Flask server |
| `FLASK_RUN_PORT` | `8523` | Port to run the app |
| `FLASK_DEBUG` | `1` in example | Enable Flask debug mode |
| `FLASK_USE_RELOADER` | `0` | Flask reloader toggle |
| `SECRET_KEY` | `change-me-in-production` | Flask session/flash security |
| `STREAMLINK_COMMAND` | `streamlink` | Streamlink executable path/command |
| `STREAM_DEFAULT_QUALITY` | `best` | Default quality for recording starts |
| `RECORDINGS_DIR` | `data/recordings` | Root folder for recorded videos |
| `STREAM_SETTINGS_FILE` | `data/recording_settings.json` | Settings persistence file |
| `CACHE_DIR` | `data/cache` | Base folder for thumbnail/live snapshot cache outputs |
| `AUTO_RECORD_POLL_SECONDS` | `30` | Auto-recorder poll interval |
| `LIVE_EDGE_OFFSET_SECONDS` | `60` | Default live playback offset |
| `LIVE_BUFFER_MIN_SECONDS` | `5` | Minimum buffer in live playback UI |
| `LIVE_BUFFER_MAX_SECONDS` | `90` | Maximum buffer in live playback UI |
| `LIVE_DIRECT_START_FROM_END_SECONDS` | `30` | Fallback direct-live start position |
| `FFMPEG_COMMAND` | `ffmpeg` | FFmpeg executable path/command |
| `VIDEO_THUMBNAIL_CACHE_DIR` | `data/cache/video-thumbnails` | Thumbnail cache location (derived from `CACHE_DIR`) |
| `VIDEO_TRANSCODE_CACHE_DIR` | `data/cache/video-transcodes` | Live DVR snapshot/transient cache location (derived from `CACHE_DIR`) |
| `VIDEO_THUMBNAIL_WIDTH` | `480` | Thumbnail width |
| `VIDEO_THUMBNAIL_HEIGHT` | `270` | Thumbnail height |
| `TWITCH_CHAT_CAPTURE_ENABLED` | `1` | Global toggle for chat capture |
| `TWITCH_CHAT_HOST` | `irc.chat.twitch.tv` | Twitch IRC host |
| `TWITCH_CHAT_PORT` | `6667` | Twitch IRC port |
| `TWITCH_CHAT_BOT_USERNAME` | empty | Optional bot username |
| `TWITCH_CHAT_BOT_OAUTH_TOKEN` | empty | Optional bot OAuth token |
| `TWITCH_CHAT_ANON_PREFIX` | `justinfan` | Anonymous username prefix fallback |
| `TWITCH_CHAT_CONNECT_TIMEOUT_SECONDS` | `12` | IRC connect timeout |
| `TWITCH_CHAT_RECEIVE_TIMEOUT_SECONDS` | `30` | IRC read timeout |
| `TWITCH_CHAT_RECONNECT_INITIAL_SECONDS` | `2` | Initial reconnect delay |
| `TWITCH_CHAT_RECONNECT_MAX_SECONDS` | `45` | Max reconnect delay |
| `PUSHOVER_APP_TOKEN` | empty | Pushover app token |
| `PUSHOVER_USER_KEY` | empty | Pushover user key |
| `PUSHOVER_API_URL` | `https://api.pushover.net/1/messages.json` | Pushover endpoint |
| `PUSHOVER_TIMEOUT_SECONDS` | `5` | Notification API timeout |

## Quick Key Setup Links

Use these links to quickly generate the credentials needed for notifications and Twitch integrations.

- Pushover home (push notifications): https://pushover.net/
- Pushover app token creation: https://pushover.net/apps/build
- Twitch Developer Console (create/manage apps): https://dev.twitch.tv/console/apps
- Twitch token generator (quick OAuth token helper): https://twitchtokengenerator.com/

Recommended flow:

1. Create a Twitch app in the Twitch Developer Console.
2. Generate your Twitch OAuth token.
3. Create a Pushover app token and get your user key.
4. Place values in `.env` (`TWITCH_CHAT_BOT_OAUTH_TOKEN`, `PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY`).

## Data and File Layout

Generated/Runtime files:

- `data/recordings/<channel>/<YYYY-MM-DD>/<title>_<YYYYMMDD_HHMMSS>.ts` while active or pending conversion
- `data/recordings/<channel>/<YYYY-MM-DD>/<title>_<YYYYMMDD_HHMMSS>.mp4` finalized completed recording
- `data/recordings/.../<file>.chat.ndjson` for captured chat
- `data/recording_settings.json` for saved channels and preferences
- `data/cache/video-thumbnails` and `data/cache/video-transcodes` (live DVR snapshots/transient files)

Ignored by git:

- `.env`
- `.venv/`
- `data/recordings/`
- `data/recording_settings.json`
- `data/cache/`

## HTTP Endpoints

Primary endpoints:

- `GET /` main browser view
- `GET /Settings` dashboard/settings screen
- `GET /status` aggregated dashboard state (JSON)
- `GET /transcode/status` transcode queue status for header indicator (JSON)
- `GET /recordings/index` recording catalog (JSON)
- `GET /recordings/view/<path>` single recording page
- `GET /recordings/media/<path>` media stream/download source
- `GET /recordings/thumb/<path>` thumbnail generator/serve
- `GET /recordings/chat/<path>` chat replay payload (JSON)
- `POST /channels/add` add saved channel
- `POST /channels/remove` remove saved channel
- `POST /channels/auto` toggle per-channel auto-record
- `POST /channels/chat` toggle per-channel chat capture
- `POST /channels/notifications` update per-channel notifications
- `POST /notifications/test` send notification test message
- `POST /recording/start` manual recording start
- `POST /recording/stop` manual recording stop
- `POST /transcode/backfill` queue existing eligible TS recordings for background conversion
- `POST /cache/clear` clear thumbnail/live snapshot caches
- `GET /health` health check

## Typical Workflow

1. Open the app in a browser.
2. Add channels in the saved channels panel.
3. Enable auto-record for channels you want monitored continuously.
4. Use manual start/stop when needed.
5. Browse videos from the main page and open single-video view for playback/chat replay.

## Troubleshooting

- Error: Streamlink command was not found.
  - Install Streamlink or set `STREAMLINK_COMMAND` to the full executable path.
- Thumbnails or playback conversion not working.
  - Install FFmpeg and verify `FFMPEG_COMMAND`.
- Completed recordings still showing as `.ts` for a while.
  - The transcode queue runs in the background. Use the dashboard backfill action to enqueue remaining eligible TS files.
- No notifications received.
  - Set valid `PUSHOVER_APP_TOKEN` and `PUSHOVER_USER_KEY`, then use the test notification action.
- Chat replay is empty.
  - Ensure `TWITCH_CHAT_CAPTURE_ENABLED=1`, channel chat capture is enabled, and recording was started after chat capture was active.

## Project Structure

```text
app.py
requirements.txt
app/
  config.py
  routes/
    dashboard.py
    health.py
  services/
    auto_recorder.py
    recording_manager.py
    settings_store.py
    twitch_chat_capture.py
    notification_dispatcher.py
    pushover_notifier.py
  templates/
  static/
```

## Website

- https://jesseloewen.com
