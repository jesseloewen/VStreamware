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


class Config:
    BASE_DIR = Path(__file__).resolve().parent.parent
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
    STREAMLINK_COMMAND = os.getenv("STREAMLINK_COMMAND", "streamlink")
    FFMPEG_COMMAND = os.getenv("FFMPEG_COMMAND", "ffmpeg")
    VIDEO_THUMBNAIL_CACHE_DIR = os.getenv(
        "VIDEO_THUMBNAIL_CACHE_DIR",
        str(BASE_DIR / ".cache" / "video-thumbnails"),
    )
    VIDEO_TRANSCODE_CACHE_DIR = os.getenv(
        "VIDEO_TRANSCODE_CACHE_DIR",
        str(BASE_DIR / ".cache" / "video-transcodes"),
    )
    VIDEO_THUMBNAIL_WIDTH = _env_int("VIDEO_THUMBNAIL_WIDTH", 480)
    VIDEO_THUMBNAIL_HEIGHT = _env_int("VIDEO_THUMBNAIL_HEIGHT", 270)
    LIVE_EDGE_OFFSET_SECONDS = _env_int("LIVE_EDGE_OFFSET_SECONDS", 60)
    LIVE_BUFFER_MIN_SECONDS = _env_int("LIVE_BUFFER_MIN_SECONDS", 5)
    LIVE_BUFFER_MAX_SECONDS = _env_int("LIVE_BUFFER_MAX_SECONDS", 90)
    LIVE_DIRECT_START_FROM_END_SECONDS = _env_int("LIVE_DIRECT_START_FROM_END_SECONDS", 30)
    STREAM_DEFAULT_QUALITY = os.getenv("STREAM_DEFAULT_QUALITY", "best")
    PUSHOVER_APP_TOKEN = os.getenv("PUSHOVER_APP_TOKEN", "")
    PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "")
    PUSHOVER_API_URL = os.getenv(
        "PUSHOVER_API_URL",
        "https://api.pushover.net/1/messages.json",
    )
    PUSHOVER_TIMEOUT_SECONDS = _env_int("PUSHOVER_TIMEOUT_SECONDS", 5)
    RECORDINGS_DIR = os.getenv("RECORDINGS_DIR", str(BASE_DIR / "recordings"))
    STREAM_SETTINGS_FILE = os.getenv(
        "STREAM_SETTINGS_FILE",
        str(BASE_DIR / "recording_settings.json"),
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
