import os
from pathlib import Path


def _env_int(key: str, default: int) -> int:
    value = os.getenv(key)
    if value is None:
        return default

    try:
        return int(value)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_str(key: str, default: str) -> str:
    value = os.getenv(key)
    if value is None:
        return default

    normalized = value.strip()
    if not normalized:
        return default

    return normalized


class Config:
    BASE_DIR = Path(__file__).resolve().parent.parent
    DATA_DIR = BASE_DIR / "data"
    FLASK_RUN_HOST = _env_str("FLASK_RUN_HOST", "0.0.0.0")
    FLASK_RUN_PORT = _env_int("FLASK_RUN_PORT", 8523)
    FLASK_DEBUG = _env_bool("FLASK_DEBUG", False)
    FLASK_USE_RELOADER = _env_bool("FLASK_USE_RELOADER", False)
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
    STREAMLINK_COMMAND = os.getenv("STREAMLINK_COMMAND", "streamlink")
    FFMPEG_COMMAND = os.getenv("FFMPEG_COMMAND", "ffmpeg")
    CACHE_DIR = _env_str("CACHE_DIR", str(DATA_DIR / "cache"))
    VIDEO_THUMBNAIL_CACHE_DIR = _env_str(
        "VIDEO_THUMBNAIL_CACHE_DIR",
        str(Path(CACHE_DIR) / "video-thumbnails"),
    )
    VIDEO_TRANSCODE_CACHE_DIR = _env_str(
        "VIDEO_TRANSCODE_CACHE_DIR",
        str(Path(CACHE_DIR) / "video-transcodes"),
    )
    VIDEO_THUMBNAIL_WIDTH = _env_int("VIDEO_THUMBNAIL_WIDTH", 480)
    VIDEO_THUMBNAIL_HEIGHT = _env_int("VIDEO_THUMBNAIL_HEIGHT", 270)
    LIVE_EDGE_OFFSET_SECONDS = _env_int("LIVE_EDGE_OFFSET_SECONDS", 60)
    LIVE_BUFFER_MIN_SECONDS = _env_int("LIVE_BUFFER_MIN_SECONDS", 5)
    LIVE_BUFFER_MAX_SECONDS = _env_int("LIVE_BUFFER_MAX_SECONDS", 90)
    LIVE_DIRECT_START_FROM_END_SECONDS = _env_int("LIVE_DIRECT_START_FROM_END_SECONDS", 30)
    LIVE_INCREMENTAL_DVR_ENABLED = _env_bool("LIVE_INCREMENTAL_DVR_ENABLED", True)
    LIVE_INCREMENTAL_DVR_CHUNK_SECONDS = _env_int("LIVE_INCREMENTAL_DVR_CHUNK_SECONDS", 300)
    LIVE_INCREMENTAL_DVR_POLL_SECONDS = _env_int("LIVE_INCREMENTAL_DVR_POLL_SECONDS", 300)
    LIVE_INCREMENTAL_DVR_SAFETY_SECONDS = _env_int("LIVE_INCREMENTAL_DVR_SAFETY_SECONDS", 20)
    LIVE_INCREMENTAL_DVR_KEEP_CHUNKS = _env_bool("LIVE_INCREMENTAL_DVR_KEEP_CHUNKS", False)
    STREAM_DEFAULT_QUALITY = os.getenv("STREAM_DEFAULT_QUALITY", "best")
    PUSHOVER_APP_TOKEN = os.getenv("PUSHOVER_APP_TOKEN", "")
    PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "")
    PUSHOVER_API_URL = os.getenv(
        "PUSHOVER_API_URL",
        "https://api.pushover.net/1/messages.json",
    )
    PUSHOVER_TIMEOUT_SECONDS = _env_int("PUSHOVER_TIMEOUT_SECONDS", 5)
    RECORDINGS_DIR = _env_str("RECORDINGS_DIR", str(DATA_DIR / "recordings"))
    STREAM_SETTINGS_FILE = _env_str(
        "STREAM_SETTINGS_FILE",
        str(DATA_DIR / "recording_settings.json"),
    )
    AUTO_RECORD_POLL_SECONDS = _env_int("AUTO_RECORD_POLL_SECONDS", 30)
    TWITCH_CHAT_CAPTURE_ENABLED = _env_bool("TWITCH_CHAT_CAPTURE_ENABLED", True)
    TWITCH_CHAT_HOST = os.getenv("TWITCH_CHAT_HOST", "irc.chat.twitch.tv")
    TWITCH_CHAT_PORT = _env_int("TWITCH_CHAT_PORT", 6667)
    TWITCH_CHAT_BOT_USERNAME = os.getenv("TWITCH_CHAT_BOT_USERNAME", "")
    TWITCH_CHAT_BOT_OAUTH_TOKEN = os.getenv("TWITCH_CHAT_BOT_OAUTH_TOKEN", "")
    TWITCH_CHAT_ANON_PREFIX = os.getenv("TWITCH_CHAT_ANON_PREFIX", "justinfan")
    TWITCH_CHAT_CONNECT_TIMEOUT_SECONDS = _env_int("TWITCH_CHAT_CONNECT_TIMEOUT_SECONDS", 12)
    TWITCH_CHAT_RECEIVE_TIMEOUT_SECONDS = _env_int("TWITCH_CHAT_RECEIVE_TIMEOUT_SECONDS", 30)
    TWITCH_CHAT_RECONNECT_INITIAL_SECONDS = _env_int("TWITCH_CHAT_RECONNECT_INITIAL_SECONDS", 2)
    TWITCH_CHAT_RECONNECT_MAX_SECONDS = _env_int("TWITCH_CHAT_RECONNECT_MAX_SECONDS", 45)
