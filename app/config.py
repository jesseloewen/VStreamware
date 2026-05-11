import os
import shlex
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


def _env_argv(key: str, default: list[str] | None = None) -> list[str]:
    value = os.getenv(key)
    if value is None:
        return list(default or [])

    normalized = value.strip()
    if not normalized:
        return list(default or [])

    try:
        return shlex.split(normalized, posix=(os.name != "nt"))
    except ValueError:
        return list(default or [])


class Config:
    BASE_DIR = Path(__file__).resolve().parent.parent
    DATA_DIR = BASE_DIR / "data"
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
    STREAM_DEFAULT_QUALITY = os.getenv("STREAM_DEFAULT_QUALITY", "best")
    STREAM_PROBE_TIMEOUT_SECONDS = max(5, _env_int("STREAM_PROBE_TIMEOUT_SECONDS", 20))
    STREAMLINK_EXTRA_ARGS = _env_argv("STREAMLINK_EXTRA_ARGS", [])
    RECORDING_BACKGROUND_PRIORITY = _env_bool("RECORDING_BACKGROUND_PRIORITY", True)
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
    AUTO_RECORD_MAX_PROBE_WORKERS = max(
        1,
        _env_int(
            "AUTO_RECORD_MAX_PROBE_WORKERS",
            max(2, min(8, os.cpu_count() or 2)),
        ),
    )
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
