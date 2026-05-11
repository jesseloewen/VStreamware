import json
import hashlib
import mimetypes
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, stream_with_context, url_for

from app.services import get_services
from app.services.dashboard_state import build_saved_channels_status

dashboard_bp = Blueprint("dashboard", __name__)

NOTIFICATION_KEYS = [
    "stream_live",
    "stream_end",
    "recording_started",
    "recording_stopped",
]

TRUTHY_VALUES = {
    "1",
    "true",
    "yes",
    "on",
}

VIDEO_EXTENSIONS = {
    ".ts",
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".m4v",
    ".avi",
    ".flv",
}

FORCED_VIDEO_MIME_TYPES = {
    ".ts": "video/mp2t",
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".avi": "video/x-msvideo",
    ".flv": "video/x-flv",
}

_TIMESTAMP_SUFFIX_PATTERN = re.compile(r"^(?P<title>.+?)_(?P<stamp>\d{8}_\d{6})$")
_LIVE_DVR_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_TRANSIENT_CACHE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,96}$")
_TRANSCODE_LOCKS_GUARD = threading.Lock()
_TRANSCODE_LOCKS: dict[str, threading.Lock] = {}

try:
    import imageio_ffmpeg  # type: ignore[import-not-found]
except Exception:
    imageio_ffmpeg = None


def _recordings_root_path() -> Path:
    configured_path = str(current_app.config["RECORDINGS_DIR"])
    return Path(configured_path).expanduser().resolve()


def _resolve_recording_path(root: Path, recording_path: str) -> Path | None:
    candidate = (root / recording_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None

    return candidate


def _display_title_from_stem(stem: str) -> str:
    match = _TIMESTAMP_SUFFIX_PATTERN.match(stem)
    if match is None:
        return stem

    title = match.group("title").strip()
    return title or stem


def _derive_recorded_datetime(day_name: str, file_stem: str) -> datetime | None:
    match = _TIMESTAMP_SUFFIX_PATTERN.match(file_stem)
    if match is not None:
        raw_stamp = match.group("stamp")
        try:
            parsed = datetime.strptime(raw_stamp, "%Y%m%d_%H%M%S")
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    try:
        parsed_day = datetime.strptime(day_name, "%Y-%m-%d")
        return parsed_day.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _recording_sort_key(item: dict[str, object]) -> tuple[int, str]:
    timestamp = int(item.get("recorded_at_unix", 0) or 0)
    relative_path = str(item.get("relative_path", ""))
    return timestamp, relative_path


def _thumbnail_cache_dir() -> Path:
    configured_path = str(current_app.config.get("VIDEO_THUMBNAIL_CACHE_DIR", "")).strip()
    if configured_path:
        cache_dir = Path(configured_path).expanduser().resolve()
    else:
        cache_dir = _recordings_root_path() / ".thumbnail-cache"

    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _video_cache_dir() -> Path:
    configured_path = str(current_app.config.get("VIDEO_TRANSCODE_CACHE_DIR", "")).strip()
    if configured_path:
        cache_dir = Path(configured_path).expanduser().resolve()
    else:
        cache_dir = _recordings_root_path() / ".video-cache"

    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _clear_cache_directory(cache_dir: Path) -> tuple[int, int]:
    removed_files = 0
    removed_dirs = 0

    if not cache_dir.exists() or not cache_dir.is_dir():
        return removed_files, removed_dirs

    for item in cache_dir.iterdir():
        try:
            if item.is_dir():
                shutil.rmtree(item)
                removed_dirs += 1
            else:
                item.unlink()
                removed_files += 1
        except OSError:
            continue

    return removed_files, removed_dirs


def _cache_directory_stats(cache_dir: Path) -> tuple[int, int]:
    total_files = 0
    total_bytes = 0

    if not cache_dir.exists() or not cache_dir.is_dir():
        return total_files, total_bytes

    for item in cache_dir.rglob("*"):
        if not item.is_file():
            continue

        try:
            total_bytes += int(item.stat().st_size)
            total_files += 1
        except OSError:
            continue

    return total_files, total_bytes


def _format_size_label(size_bytes: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(max(size_bytes, 0))
    unit_index = 0

    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1

    if unit_index == 0:
        return f"{int(value)} {units[unit_index]}"

    return f"{value:.1f} {units[unit_index]}"


def _build_cache_summary() -> dict[str, object]:
    thumbnail_cache_dir = _thumbnail_cache_dir()
    video_cache_dir = _video_cache_dir()
    thumbnail_files, thumbnail_bytes = _cache_directory_stats(thumbnail_cache_dir)
    video_files, video_bytes = _cache_directory_stats(video_cache_dir)

    total_files = thumbnail_files + video_files
    total_size_bytes = thumbnail_bytes + video_bytes
    return {
        "total_files": total_files,
        "total_size_bytes": total_size_bytes,
        "total_size_label": _format_size_label(total_size_bytes),
    }


def _thumbnail_cache_path(root: Path, recording_file: Path) -> Path:
    relative_path = recording_file.relative_to(root).as_posix()
    stat = recording_file.stat()
    cache_key = f"{relative_path}|{stat.st_size}|{stat.st_mtime_ns}"
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    return _thumbnail_cache_dir() / f"{digest}.jpg"


def _live_thumbnail_cache_prefix(root: Path, recording_file: Path) -> str:
    relative_path = recording_file.relative_to(root).as_posix()
    recording_digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:20]
    return f"live-{recording_digest}-"


def _live_thumbnail_cache_path(root: Path, recording_file: Path) -> Path:
    cache_prefix = _live_thumbnail_cache_prefix(root, recording_file)
    stat = recording_file.stat()
    return _thumbnail_cache_dir() / f"{cache_prefix}{stat.st_mtime_ns}.jpg"


def _prune_live_thumbnail_cache(root: Path, recording_file: Path, keep_path: Path) -> None:
    cache_prefix = _live_thumbnail_cache_prefix(root, recording_file)
    cache_dir = _thumbnail_cache_dir()

    for candidate in cache_dir.glob(f"{cache_prefix}*.jpg"):
        if candidate == keep_path or not candidate.is_file():
            continue

        try:
            candidate.unlink()
        except OSError:
            continue


def _video_cache_path(root: Path, recording_file: Path) -> Path:
    relative_path = recording_file.relative_to(root).as_posix()
    stat = recording_file.stat()
    cache_key = f"{relative_path}|{stat.st_size}|{stat.st_mtime_ns}|mp4"
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    return _video_cache_dir() / f"{digest}.mp4"


def _is_valid_live_dvr_key(key: str) -> bool:
    return bool(_LIVE_DVR_KEY_PATTERN.fullmatch(key))


def _is_valid_transient_cache_id(cache_id: str) -> bool:
    return bool(_TRANSIENT_CACHE_ID_PATTERN.fullmatch(cache_id))


def _live_dvr_snapshot_path(
    root: Path,
    recording_file: Path,
    dvr_key: str,
    transient_cache_id: str | None = None,
) -> Path:
    relative_path = recording_file.relative_to(root).as_posix()
    recording_digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:20]
    transient_prefix = ""
    if transient_cache_id and _is_valid_transient_cache_id(transient_cache_id):
        transient_prefix = f"transient-{transient_cache_id}-"

    return _video_cache_dir() / f"{transient_prefix}live-{recording_digest}-{dvr_key}.mp4"


def _transient_transcode_path(root: Path, recording_file: Path, transient_cache_id: str) -> Path:
    relative_path = recording_file.relative_to(root).as_posix()
    recording_digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:20]
    return _video_cache_dir() / f"transient-{transient_cache_id}-{recording_digest}.mp4"


def _transcode_output_lock(output_path: Path) -> threading.Lock:
    cache_key = str(output_path.resolve())
    with _TRANSCODE_LOCKS_GUARD:
        lock = _TRANSCODE_LOCKS.get(cache_key)
        if lock is None:
            lock = threading.Lock()
            _TRANSCODE_LOCKS[cache_key] = lock

    return lock


def _live_buffer_bounds() -> tuple[int, int]:
    minimum = int(current_app.config.get("LIVE_BUFFER_MIN_SECONDS", 5) or 5)
    maximum = int(current_app.config.get("LIVE_BUFFER_MAX_SECONDS", 90) or 90)

    minimum = max(minimum, 1)
    maximum = max(maximum, minimum)
    return minimum, maximum


def _clamp_live_buffer_seconds(value: int) -> int:
    minimum, maximum = _live_buffer_bounds()
    return max(minimum, min(maximum, int(value)))


def _live_buffer_default_seconds() -> int:
    default_offset = int(current_app.config.get("LIVE_EDGE_OFFSET_SECONDS", 30) or 30)
    if default_offset <= 0:
        default_offset = int(current_app.config.get("LIVE_DIRECT_START_FROM_END_SECONDS", 30) or 30)
    return _clamp_live_buffer_seconds(max(default_offset, 1))


def _live_buffer_seconds_from_request() -> int:
    raw_value = request.args.get("live_offset", "").strip()
    if not raw_value:
        return _live_buffer_default_seconds()

    try:
        parsed = int(raw_value)
    except ValueError:
        return _live_buffer_default_seconds()

    return _clamp_live_buffer_seconds(parsed)


def _thumbnail_fallback_path() -> Path:
    static_folder = current_app.static_folder
    if not static_folder:
        raise RuntimeError("Static folder is not configured.")

    return Path(static_folder) / "icons" / "icon-64.png"


def _generate_thumbnail(source_file: Path, thumbnail_file: Path) -> bool:
    ffmpeg_binary = _resolve_ffmpeg_binary()
    if ffmpeg_binary is None:
        return False

    scale_width = int(current_app.config.get("VIDEO_THUMBNAIL_WIDTH", 480) or 480)
    scale_height = int(current_app.config.get("VIDEO_THUMBNAIL_HEIGHT", 270) or 270)
    filter_graph = (
        f"scale={scale_width}:{scale_height}:force_original_aspect_ratio=decrease,"
        f"pad={scale_width}:{scale_height}:(ow-iw)/2:(oh-ih)/2:black"
    )

    command_candidates = [
        [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-sseof",
            "-2",
            "-i",
            str(source_file),
            "-frames:v",
            "1",
            "-vf",
            filter_graph,
            "-q:v",
            "3",
            "-y",
            str(thumbnail_file),
        ],
        [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "00:00:05",
            "-i",
            str(source_file),
            "-frames:v",
            "1",
            "-vf",
            filter_graph,
            "-q:v",
            "3",
            "-y",
            str(thumbnail_file),
        ],
    ]

    thumbnail_file.parent.mkdir(parents=True, exist_ok=True)
    for command in command_candidates:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue

        if result.returncode == 0 and thumbnail_file.exists() and thumbnail_file.stat().st_size > 0:
            return True

    return False


def _recording_urls(relative_path: str) -> dict[str, str]:
    view_slug = _recording_view_slug(relative_path)
    return {
        "media_url": url_for("dashboard.recording_media", recording_path=relative_path),
        "thumbnail_url": url_for("dashboard.recording_thumbnail", recording_path=relative_path),
        "view_url": url_for("dashboard.view_recording", recording_slug=view_slug),
        "chat_url": url_for("dashboard.recording_chat", recording_path=relative_path),
    }


def _recording_view_slug(relative_path: str) -> str:
    normalized = Path(relative_path).as_posix().strip("/")
    if not normalized:
        return ""

    relative = Path(normalized)
    parent = relative.parent.as_posix()
    stem = relative.stem
    if parent in {"", "."}:
        return stem

    return f"{parent}/{stem}"


def _find_recording_entry(catalog: dict[str, object], recording_path: str) -> dict[str, object] | None:
    recordings = catalog.get("recordings", [])
    if not isinstance(recordings, list):
        return None

    normalized_path = Path(recording_path).as_posix().strip("/")
    for item in recordings:
        if not isinstance(item, dict):
            continue

        relative_path = Path(str(item.get("relative_path", ""))).as_posix().strip("/")
        view_slug = Path(str(item.get("view_slug", ""))).as_posix().strip("/")
        if normalized_path == relative_path or normalized_path == view_slug:
            return item

    return None


def _chat_sidecar_path_for_recording(recording_file: Path) -> Path:
    return recording_file.with_suffix(".chat.ndjson")


def _chat_sidecar_has_messages(sidecar_path: Path) -> bool:
    try:
        return sidecar_path.exists() and sidecar_path.stat().st_size > 0
    except OSError:
        return False


def _iso_to_utc_ms(value: str | None) -> int | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)

    return int(parsed.timestamp() * 1000)


def _ffmpeg_command_candidates() -> list[list[str]]:
    configured = str(current_app.config.get("FFMPEG_COMMAND", "")).strip()
    candidates: list[list[str]] = []
    if configured:
        candidates.append([configured])

    candidates.append(["ffmpeg"])

    if imageio_ffmpeg is not None:
        try:
            candidates.append([str(imageio_ffmpeg.get_ffmpeg_exe())])
        except Exception:
            pass

    unique_candidates: list[list[str]] = []
    seen = set()
    for command in candidates:
        binary = command[0]
        if binary in seen:
            continue

        seen.add(binary)
        unique_candidates.append(command)

    return unique_candidates


def _resolve_ffmpeg_binary() -> str | None:
    for command in _ffmpeg_command_candidates():
        binary = command[0]
        if Path(binary).exists():
            return binary

        if shutil.which(binary) is not None:
            return binary

    return None


def _is_live_recording_path(recording_manager: Any, file_path: Path) -> bool:
    try:
        normalized_target = file_path.resolve()
    except OSError:
        return False

    active = recording_manager.get_active_recordings()
    for item in active:
        output_file = item.get("output_file")
        if not isinstance(output_file, str) or not output_file.strip():
            continue

        try:
            normalized_output = Path(output_file).resolve()
        except OSError:
            continue

        if normalized_output == normalized_target:
            return True

    return False


def _materialize_ts_mp4_file(ts_path: Path, output_path: Path | None = None) -> Path | None:
    ffmpeg_binary = _resolve_ffmpeg_binary()
    if ffmpeg_binary is None:
        return None

    root = _recordings_root_path()
    if output_path is not None:
        cache_path = output_path
    else:
        try:
            cache_path = _video_cache_path(root, ts_path)
        except (OSError, ValueError):
            return None

    transcode_lock = _transcode_output_lock(cache_path)
    with transcode_lock:
        if cache_path.exists() and cache_path.stat().st_size > 0:
            return cache_path

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        command_candidates = [
            [
                ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(ts_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-y",
                str(cache_path),
            ],
            [
                ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(ts_path),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-y",
                str(cache_path),
            ],
        ]

        for command in command_candidates:
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=60 * 30,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue

            if result.returncode == 0 and cache_path.exists() and cache_path.stat().st_size > 0:
                return cache_path

    return None


def _stream_ts_as_mp4(ts_path: Path, start_from_end_seconds: int | None = None) -> Response:
    ffmpeg_binary = _resolve_ffmpeg_binary()
    if ffmpeg_binary is None:
        abort(500, description="FFmpeg is required for TS playback but was not found.")

    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
    ]

    if start_from_end_seconds is not None:
        start_offset = max(int(start_from_end_seconds), 1)
        # Starting near the tail keeps live clients close to real time on first load.
        command.extend(["-sseof", f"-{start_offset}"])

    command.extend(
        [
            "-i",
            str(ts_path),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            "-f",
            "mp4",
            "pipe:1",
        ]
    )

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except OSError as exc:
        abort(500, description=f"Failed to start FFmpeg for TS playback: {exc}")

    def generate() -> Any:
        try:
            if process.stdout is None:
                return

            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                process.kill()
            except OSError:
                pass
            process.wait(timeout=5)

    response = Response(stream_with_context(generate()), mimetype="video/mp4")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Playback-Transmuxed"] = "true"
    return response


def _build_recordings_catalog(recording_manager: Any, settings_store: Any) -> dict[str, object]:
    root = _recordings_root_path()
    active_recordings = recording_manager.get_active_recordings()
    active_by_path: dict[Path, dict[str, object]] = {}

    for item in active_recordings:
        output_file = item.get("output_file")
        if not isinstance(output_file, str) or not output_file.strip():
            continue

        try:
            resolved = Path(output_file).resolve()
        except OSError:
            continue

        active_by_path[resolved] = {
            "channel": str(item.get("channel", "")).strip().lower(),
            "quality": str(item.get("quality", "")).strip(),
            "started_at": str(item.get("started_at", "")).strip() or None,
            "pid": item.get("pid"),
        }

    recordings: list[dict[str, object]] = []
    channels: set[str] = set()
    channel_days: dict[str, set[str]] = {}
    seen_relative_paths: set[str] = set()

    if root.exists() and root.is_dir():
        for channel_dir in sorted(root.iterdir(), key=lambda path: path.name.lower()):
            if not channel_dir.is_dir():
                continue

            channel_name = channel_dir.name
            for day_dir in sorted(channel_dir.iterdir(), key=lambda path: path.name, reverse=True):
                if not day_dir.is_dir():
                    continue

                day_name = day_dir.name
                for recording_file in sorted(day_dir.iterdir(), key=lambda path: path.name.lower()):
                    if not recording_file.is_file():
                        continue

                    suffix = recording_file.suffix.lower()
                    if suffix and suffix not in VIDEO_EXTENSIONS:
                        continue

                    try:
                        file_stat = recording_file.stat()
                        resolved_file = recording_file.resolve()
                    except OSError:
                        continue

                    recorded_dt = _derive_recorded_datetime(day_name, recording_file.stem)
                    recorded_at = recorded_dt.isoformat() if recorded_dt is not None else None
                    recorded_at_unix = int(recorded_dt.timestamp()) if recorded_dt is not None else 0
                    recorded_at_utc_ms = int(recorded_dt.timestamp() * 1000) if recorded_dt is not None else 0

                    relative_path = recording_file.relative_to(root).as_posix()
                    seen_relative_paths.add(relative_path)
                    active_info = active_by_path.get(resolved_file)
                    channel_key = channel_name.strip().lower()
                    chat_enabled = True
                    chat_sidecar_path = _chat_sidecar_path_for_recording(recording_file)

                    started_at_value = None if active_info is None else active_info.get("started_at")
                    started_at = (
                        str(started_at_value)
                        if isinstance(started_at_value, str) and started_at_value.strip()
                        else None
                    )
                    recording_start_utc_ms = _iso_to_utc_ms(started_at)
                    if recording_start_utc_ms is None:
                        recording_start_utc_ms = recorded_at_utc_ms if recorded_at_utc_ms > 0 else None
                    recording_end_utc_ms = int(file_stat.st_mtime * 1000)

                    record = {
                        "channel": channel_name,
                        "day": day_name,
                        "file_name": recording_file.name,
                        "display_title": _display_title_from_stem(recording_file.stem),
                        "relative_path": relative_path,
                        "view_slug": _recording_view_slug(relative_path),
                        "recorded_at": recorded_at,
                        "recorded_at_unix": recorded_at_unix,
                        "size_bytes": int(file_stat.st_size),
                        "size_label": _format_size_label(int(file_stat.st_size)),
                        "is_live": active_info is not None,
                        "quality": None if active_info is None else active_info.get("quality"),
                        "started_at": started_at,
                        "recording_start_utc_ms": recording_start_utc_ms,
                        "recording_end_utc_ms": recording_end_utc_ms,
                        "pid": None if active_info is None else active_info.get("pid"),
                        "chat_enabled": chat_enabled,
                        "chat_available": _chat_sidecar_has_messages(chat_sidecar_path),
                        "can_play": True,
                        **_recording_urls(relative_path),
                    }
                    recordings.append(record)

                    channels.add(channel_name)
                    channel_days.setdefault(channel_name, set()).add(day_name)

    for active_path, active_info in active_by_path.items():
        try:
            relative_path = active_path.relative_to(root).as_posix()
        except ValueError:
            continue

        if relative_path in seen_relative_paths:
            continue

        active_channel = str(active_info.get("channel") or "").strip().lower()
        if not active_channel:
            active_channel = active_path.parts[-3] if len(active_path.parts) >= 3 else "unknown"

        active_day = active_path.parts[-2] if len(active_path.parts) >= 2 else "unknown"
        started_at = active_info.get("started_at")
        recorded_dt = _derive_recorded_datetime(active_day, active_path.stem)
        if recorded_dt is None and isinstance(started_at, str):
            try:
                recorded_dt = datetime.fromisoformat(started_at)
            except ValueError:
                recorded_dt = None

        if recorded_dt is None:
            recorded_dt = datetime.now(tz=timezone.utc)

        recorded_at_unix = int(recorded_dt.timestamp())
        recorded_at_utc_ms = int(recorded_dt.timestamp() * 1000)
        started_at_iso = (
            str(started_at)
            if isinstance(started_at, str) and started_at.strip()
            else None
        )
        size_bytes = 0
        recording_end_utc_ms = recorded_at_utc_ms
        try:
            active_stat = active_path.stat()
            size_bytes = int(active_stat.st_size)
            recording_end_utc_ms = int(active_stat.st_mtime * 1000)
        except OSError:
            pass

        recording_start_utc_ms = _iso_to_utc_ms(started_at_iso)
        if recording_start_utc_ms is None:
            recording_start_utc_ms = recorded_at_utc_ms

        channel_key = active_channel.strip().lower()
        chat_enabled = True
        chat_sidecar_path = _chat_sidecar_path_for_recording(active_path)

        recordings.append(
            {
                "channel": active_channel,
                "day": active_day,
                "file_name": active_path.name,
                "display_title": _display_title_from_stem(active_path.stem),
                "relative_path": relative_path,
                "view_slug": _recording_view_slug(relative_path),
                "recorded_at": recorded_dt.isoformat(),
                "recorded_at_unix": recorded_at_unix,
                "size_bytes": size_bytes,
                "size_label": _format_size_label(size_bytes),
                "is_live": True,
                "quality": active_info.get("quality"),
                "started_at": started_at_iso,
                "recording_start_utc_ms": recording_start_utc_ms,
                "recording_end_utc_ms": recording_end_utc_ms,
                "pid": active_info.get("pid"),
                "chat_enabled": chat_enabled,
                "chat_available": _chat_sidecar_has_messages(chat_sidecar_path),
                "can_play": active_path.exists() and active_path.is_file(),
                **_recording_urls(relative_path),
            }
        )
        channels.add(active_channel)
        channel_days.setdefault(active_channel, set()).add(active_day)

    recordings.sort(key=_recording_sort_key, reverse=True)

    channel_days_payload = {
        channel: sorted(days, reverse=True)
        for channel, days in sorted(channel_days.items(), key=lambda item: item[0].lower())
    }
    signature_payload = [
        {
            "relative_path": str(item["relative_path"]),
            "recorded_at_unix": int(item["recorded_at_unix"]),
            "size_bytes": int(item["size_bytes"]),
            "is_live": bool(item["is_live"]),
        }
        for item in recordings
    ]

    return {
        "recordings": recordings,
        "channels": sorted(channels, key=str.lower),
        "channel_days": channel_days_payload,
        "signature": json.dumps(signature_payload, sort_keys=True, separators=(",", ":")),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def _request_int(
    arg_name: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw_value = request.args.get(arg_name, "").strip()
    if not raw_value:
        value = default
    else:
        try:
            value = int(raw_value)
        except ValueError:
            value = default

    if minimum is not None:
        value = max(minimum, value)

    if maximum is not None:
        value = min(maximum, value)

    return value


def _request_optional_int(arg_name: str) -> int | None:
    raw_value = request.args.get(arg_name, "").strip()
    if not raw_value:
        return None

    try:
        return int(raw_value)
    except ValueError:
        return None


def _filter_chat_messages(
    sidecar_path: Path,
    *,
    limit: int,
    target_ms: int | None,
    since_ms: int | None,
    window_ms: int,
) -> tuple[list[dict[str, object]], int | None]:
    selected: list[dict[str, object]] = []
    latest_utc_ms: int | None = None

    if not sidecar_path.exists() or not sidecar_path.is_file():
        return selected, latest_utc_ms

    try:
        chat_file = sidecar_path.open("r", encoding="utf-8")
    except OSError:
        return selected, latest_utc_ms

    with chat_file:
        for line in chat_file:
            line = line.strip()
            if not line:
                continue

            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(payload, dict):
                continue

            raw_utc_ms = payload.get("utc_ms")
            if not isinstance(raw_utc_ms, int):
                continue

            if latest_utc_ms is None or raw_utc_ms > latest_utc_ms:
                latest_utc_ms = raw_utc_ms

            if since_ms is not None and raw_utc_ms <= since_ms:
                continue

            if target_ms is not None:
                # Replay should show only messages at or before the playback target.
                # This prevents future chat from lingering when scrubbing backward.
                lower_bound = target_ms - window_ms
                if raw_utc_ms < lower_bound or raw_utc_ms > target_ms:
                    continue

            selected.append(
                {
                    "utc_ms": raw_utc_ms,
                    "utc_iso": str(payload.get("utc_iso", "")),
                    "author": str(payload.get("author", "unknown")),
                    "text": str(payload.get("text", "")),
                }
            )

    if len(selected) > limit:
        selected = selected[-limit:]

    return selected, latest_utc_ms


def _redirect_back(default_endpoint: str = "dashboard.settings") -> object:
    referrer = request.referrer or ""
    if referrer:
        parsed = urlparse(referrer)
        if parsed.scheme in {"http", "https"} and parsed.netloc == request.host and parsed.path.startswith("/"):
            target = parsed.path
            if parsed.query:
                target = f"{target}?{parsed.query}"
            return redirect(target)

    return redirect(url_for(default_endpoint))


def _build_dashboard_signature(
    saved_channels_status: list[dict[str, object]],
    active_recordings: list[dict[str, object]],
    auto_status: dict[str, object],
    display_timezone: str,
) -> str:
    saved_channels_signature = [
        {
            "name": str(item["name"]),
            "auto_record": bool(item["auto_record"]),
            "is_live": bool(item["is_live"]),
            "live_state": str(item["live_state"]),
            "is_recording": bool(item["is_recording"]),
            "stream_title": item["stream_title"],
        }
        for item in saved_channels_status
    ]
    active_recordings_signature = [
        {
            "channel": str(item.get("channel", "")),
            "quality": str(item.get("quality", "")),
            "pid": item.get("pid"),
            "started_at": str(item.get("started_at", "")),
            "output_file": str(item.get("output_file", "")),
        }
        for item in active_recordings
    ]
    auto_status_signature = {
        "running": bool(auto_status.get("running")),
        "enabled_channels": int(auto_status.get("enabled_channels", 0)),
        "total_channels": int(auto_status.get("total_channels", 0)),
        "poll_seconds": int(auto_status.get("poll_seconds", 0)),
    }
    payload = {
        "saved_channels": saved_channels_signature,
        "active_recordings": active_recordings_signature,
        "auto_status": auto_status_signature,
        "display_timezone": display_timezone,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _build_dashboard_state(services: dict[str, Any]) -> dict[str, object]:
    settings_store = services["settings_store"]
    recording_manager = services["recording_manager"]
    auto_recorder = services["auto_recorder"]

    settings = settings_store.get_settings()
    saved_channels = settings["saved_channels"]
    display_timezone = str(settings.get("display_timezone", "auto"))
    saved_channels_status = build_saved_channels_status(
        saved_channels=saved_channels,
        recording_manager=recording_manager,
        auto_recorder=auto_recorder,
    )

    active_recordings = recording_manager.get_active_recordings()
    auto_status = auto_recorder.snapshot()

    return {
        "saved_channels": saved_channels_status,
        "active_recordings": active_recordings,
        "auto_status": auto_status,
        "display_timezone": display_timezone,
        "dashboard_signature": _build_dashboard_signature(
            saved_channels_status=saved_channels_status,
            active_recordings=active_recordings,
            auto_status=auto_status,
            display_timezone=display_timezone,
        ),
    }


@dashboard_bp.get("/")
def index() -> str:
    services = get_services(current_app)
    auto_recorder = services["auto_recorder"]
    auto_recorder.request_refresh()

    return render_template("main.html")


@dashboard_bp.get("/Settings")
def settings() -> str:
    services = get_services(current_app)
    auto_recorder = services["auto_recorder"]
    auto_recorder.request_refresh()
    dashboard_state = _build_dashboard_state(services)
    pushover_configured = bool(services["notification_dispatcher"].is_configured())
    cache_summary = _build_cache_summary()

    return render_template(
        "dashboard.html",
        active_recordings=dashboard_state["active_recordings"],
        auto_status=dashboard_state["auto_status"],
        display_timezone=dashboard_state["display_timezone"],
        dashboard_signature=dashboard_state["dashboard_signature"],
        pushover_configured=pushover_configured,
        cache_summary=cache_summary,
    )


@dashboard_bp.get("/status")
def status() -> object:
    services = get_services(current_app)
    dashboard_state = _build_dashboard_state(services)
    return jsonify(dashboard_state)


@dashboard_bp.get("/channels/panel")
def saved_channels_panel_partial() -> str:
    services = get_services(current_app)
    services["auto_recorder"].request_refresh()
    dashboard_state = _build_dashboard_state(services)
    return render_template(
        "partials/saved_channels_panel.html",
        saved_channels=dashboard_state["saved_channels"],
    )


@dashboard_bp.get("/recordings/index")
def recordings_index() -> object:
    services = get_services(current_app)
    payload = _build_recordings_catalog(
        services["recording_manager"],
        services["settings_store"],
    )
    return jsonify(payload)


@dashboard_bp.get("/recordings/view/<path:recording_slug>")
def view_recording(recording_slug: str) -> object:
    services = get_services(current_app)
    catalog = _build_recordings_catalog(
        services["recording_manager"],
        services["settings_store"],
    )
    record = _find_recording_entry(catalog, recording_slug)
    if record is None:
        abort(404)

    canonical_slug = str(record.get("view_slug", "")).strip()
    requested_slug = Path(recording_slug).as_posix().strip("/")
    if canonical_slug and requested_slug != canonical_slug:
        return redirect(url_for("dashboard.view_recording", recording_slug=canonical_slug), code=302)

    live_buffer_default = _live_buffer_default_seconds()
    live_buffer_min, live_buffer_max = _live_buffer_bounds()

    return render_template(
        "video_detail.html",
        recording=record,
        live_edge_offset_seconds=live_buffer_default,
        live_buffer_default_seconds=live_buffer_default,
        live_buffer_min_seconds=live_buffer_min,
        live_buffer_max_seconds=live_buffer_max,
    )


@dashboard_bp.get("/recordings/view/<path:recording_slug>/size")
def recording_size(recording_slug: str) -> object:
    services = get_services(current_app)
    catalog = _build_recordings_catalog(
        services["recording_manager"],
        services["settings_store"],
    )
    record = _find_recording_entry(catalog, recording_slug)
    if record is None:
        abort(404)

    size_bytes = int(record.get("size_bytes", 0) or 0)
    return jsonify(
        {
            "size_bytes": size_bytes,
            "size_label": _format_size_label(size_bytes),
            "is_live": bool(record.get("is_live")),
        }
    )


@dashboard_bp.get("/recordings/thumb/<path:recording_path>")
def recording_thumbnail(recording_path: str) -> object:
    root = _recordings_root_path()
    resolved_path = _resolve_recording_path(root, recording_path)
    if resolved_path is None or not resolved_path.exists() or not resolved_path.is_file():
        abort(404)

    services = get_services(current_app)
    recording_manager = services["recording_manager"]
    is_live_recording = _is_live_recording_path(recording_manager, resolved_path)

    try:
        if is_live_recording:
            thumbnail_path = _live_thumbnail_cache_path(root, resolved_path)
        else:
            thumbnail_path = _thumbnail_cache_path(root, resolved_path)
    except (OSError, ValueError):
        thumbnail_path = _thumbnail_cache_dir() / "fallback.jpg"

    if not thumbnail_path.exists() or thumbnail_path.stat().st_size <= 0:
        generated = _generate_thumbnail(resolved_path, thumbnail_path)
        if not generated:
            fallback_path = _thumbnail_fallback_path()
            return send_file(
                fallback_path,
                mimetype="image/png",
                conditional=True,
                max_age=300,
            )

    if is_live_recording and thumbnail_path.exists() and thumbnail_path.stat().st_size > 0:
        try:
            _prune_live_thumbnail_cache(root, resolved_path, keep_path=thumbnail_path)
        except (OSError, ValueError):
            pass

    return send_file(
        thumbnail_path,
        mimetype="image/jpeg",
        conditional=True,
        max_age=300,
    )


@dashboard_bp.get("/recordings/media/<path:recording_path>")
def recording_media(recording_path: str) -> object:
    root = _recordings_root_path()
    resolved_path = _resolve_recording_path(root, recording_path)
    if resolved_path is None or not resolved_path.exists() or not resolved_path.is_file():
        abort(404)

    transient_cache_id = request.args.get("transient_id", "").strip()
    if not _is_valid_transient_cache_id(transient_cache_id):
        transient_cache_id = ""

    suffix = resolved_path.suffix.lower()
    if suffix == ".ts":
        services = get_services(current_app)
        recording_manager = services["recording_manager"]
        is_live = _is_live_recording_path(recording_manager, resolved_path)
        if is_live:
            live_buffer_seconds = _live_buffer_seconds_from_request()
            live_dvr_key = request.args.get("live_dvr", "").strip()
            if _is_valid_live_dvr_key(live_dvr_key):
                try:
                    dvr_snapshot_path = _live_dvr_snapshot_path(
                        root,
                        resolved_path,
                        live_dvr_key,
                        transient_cache_id=transient_cache_id or None,
                    )
                except (OSError, ValueError):
                    dvr_snapshot_path = None

                if dvr_snapshot_path is not None:
                    converted_mp4 = _materialize_ts_mp4_file(
                        resolved_path,
                        output_path=dvr_snapshot_path,
                    )
                    if converted_mp4 is not None:
                        response = send_file(
                            converted_mp4,
                            mimetype="video/mp4",
                            conditional=True,
                            as_attachment=False,
                            max_age=0,
                        )
                        response.headers["Accept-Ranges"] = "bytes"
                        response.headers["Cache-Control"] = "no-store"
                        response.headers["X-Playback-Transcoded-File"] = "true"
                        response.headers["X-Playback-Live-DVR"] = "true"
                        return response

            # Fallback preserves live playback even when stable DVR snapshot is unavailable.
            return _stream_ts_as_mp4(
                resolved_path,
                start_from_end_seconds=live_buffer_seconds,
            )

        converted_mp4 = _materialize_ts_mp4_file(resolved_path)

        if converted_mp4 is not None:
            response = send_file(
                converted_mp4,
                mimetype="video/mp4",
                conditional=True,
                as_attachment=False,
                max_age=600,
            )
            response.headers["Accept-Ranges"] = "bytes"
            response.headers["Cache-Control"] = "public, max-age=600"
            response.headers["X-Playback-Transcoded-File"] = "true"
            return response

        return _stream_ts_as_mp4(resolved_path)

    forced_mime_type = FORCED_VIDEO_MIME_TYPES.get(suffix)
    guessed_mime_type, _ = mimetypes.guess_type(str(resolved_path))
    mime_type = forced_mime_type or guessed_mime_type or "application/octet-stream"
    response = send_file(
        resolved_path,
        mimetype=mime_type,
        conditional=True,
        as_attachment=False,
        max_age=600,
    )
    response.headers["Accept-Ranges"] = "bytes"
    services = get_services(current_app)
    if _is_live_recording_path(services["recording_manager"], resolved_path):
        response.headers["Cache-Control"] = "no-store"
    else:
        response.headers["Cache-Control"] = "public, max-age=600"
    return response


@dashboard_bp.get("/recordings/chat/<path:recording_path>")
def recording_chat(recording_path: str) -> object:
    services = get_services(current_app)
    catalog = _build_recordings_catalog(
        services["recording_manager"],
        services["settings_store"],
    )
    record = _find_recording_entry(catalog, recording_path)
    if record is None:
        abort(404)

    root = _recordings_root_path()
    resolved_path = _resolve_recording_path(root, recording_path)
    if resolved_path is None:
        abort(404)

    sidecar_path = _chat_sidecar_path_for_recording(resolved_path)
    limit = _request_int("limit", default=150, minimum=10, maximum=600)
    target_ms = _request_optional_int("target_ms")
    since_ms = _request_optional_int("since_ms")
    window_ms = _request_int("window_ms", default=120000, minimum=15000, maximum=600000)

    messages, latest_utc_ms = _filter_chat_messages(
        sidecar_path,
        limit=limit,
        target_ms=target_ms,
        since_ms=since_ms,
        window_ms=window_ms,
    )

    recording_start_utc_ms = record.get("recording_start_utc_ms")
    if not isinstance(recording_start_utc_ms, int):
        recording_start_utc_ms = None

    return jsonify(
        {
            "available": _chat_sidecar_has_messages(sidecar_path),
            "chat_enabled": bool(record.get("chat_enabled", True)),
            "recording_start_utc_ms": recording_start_utc_ms,
            "is_live": bool(record.get("is_live", False)),
            "messages": messages,
            "latest_utc_ms": latest_utc_ms,
        }
    )


@dashboard_bp.post("/channels/add")
def add_channel() -> object:
    services = get_services(current_app)
    channel = request.form.get("channel", "")
    ok, message = services["settings_store"].add_channel(channel)
    services["auto_recorder"].request_refresh()
    flash(message, "success" if ok else "error")
    return _redirect_back()


@dashboard_bp.post("/channels/remove")
def remove_channel() -> object:
    services = get_services(current_app)
    channel = request.form.get("channel", "")
    ok, message = services["settings_store"].remove_channel(channel)
    services["auto_recorder"].request_refresh()
    flash(message, "success" if ok else "error")
    return _redirect_back()


@dashboard_bp.post("/channels/auto")
def set_channel_auto_recording() -> object:
    services = get_services(current_app)
    channel = request.form.get("channel", "")
    enabled = request.form.get("enabled", "false").strip().lower() in TRUTHY_VALUES

    ok, message = services["settings_store"].set_channel_auto_record(channel, enabled)
    services["auto_recorder"].request_refresh()
    flash(message, "success" if ok else "error")
    return _redirect_back()


@dashboard_bp.post("/settings/timezone")
def set_display_timezone() -> object:
    services = get_services(current_app)
    timezone_name = request.form.get("display_timezone", "auto")
    ok, message = services["settings_store"].set_display_timezone(timezone_name)
    flash(message, "success" if ok else "error")
    return _redirect_back()


@dashboard_bp.post("/channels/notifications")
def set_channel_notifications() -> object:
    services = get_services(current_app)
    channel = request.form.get("channel", "")
    notifications = {
        key: request.form.get(key, "false").strip().lower() in TRUTHY_VALUES
        for key in NOTIFICATION_KEYS
    }

    ok, message = services["settings_store"].set_channel_notifications(channel, notifications)
    flash(message, "success" if ok else "error")
    return _redirect_back()


@dashboard_bp.post("/notifications/test")
def test_notifications() -> object:
    services = get_services(current_app)
    ok, message = services["notification_dispatcher"].send_test_notification()
    flash(message, "success" if ok else "error")
    return _redirect_back()


@dashboard_bp.post("/recording/start")
def start_recording() -> object:
    services = get_services(current_app)
    channel = request.form.get("channel", "")
    if not services["settings_store"].is_saved_channel(channel):
        flash("Only saved channels can be recorded.", "error")
        return _redirect_back()

    ok, message = services["recording_manager"].start_recording(
        channel,
    )
    services["auto_recorder"].request_refresh()
    flash(message, "success" if ok else "error")
    return _redirect_back()


@dashboard_bp.post("/recording/stop")
def stop_recording() -> object:
    services = get_services(current_app)
    channel = request.form.get("channel", "")
    ok, message = services["recording_manager"].stop_recording(channel)
    services["auto_recorder"].request_refresh()
    flash(message, "success" if ok else "error")
    return _redirect_back()


@dashboard_bp.post("/cache/clear")
def clear_cache() -> object:
    thumbnail_cache_dir = _thumbnail_cache_dir()
    video_cache_dir = _video_cache_dir()

    removed_files = 0
    removed_dirs = 0
    removed_anything = False

    for cache_dir in (thumbnail_cache_dir, video_cache_dir):
        files_count, dirs_count = _clear_cache_directory(cache_dir)
        removed_files += files_count
        removed_dirs += dirs_count
        if files_count > 0 or dirs_count > 0:
            removed_anything = True

    if removed_anything:
        flash(
            f"Cache cleared. Removed {removed_files} files and {removed_dirs} directories.",
            "success",
        )
    else:
        flash("Cache was already empty.", "success")

    return _redirect_back("dashboard.index")


@dashboard_bp.post("/cache/transcode/release")
def release_transcode_cache() -> object:
    transient_cache_id = ""

    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        transient_cache_id = str(payload.get("transient_id", "")).strip()

    if not transient_cache_id:
        transient_cache_id = request.form.get("transient_id", "").strip()

    if not _is_valid_transient_cache_id(transient_cache_id):
        return jsonify({"ok": False, "error": "Invalid transient cache id."}), 400

    cache_dir = _video_cache_dir()
    removed_files = 0
    removed_size_bytes = 0
    pattern = f"transient-{transient_cache_id}-*.mp4"

    for path in cache_dir.glob(pattern):
        if not path.is_file():
            continue

        file_size = 0
        try:
            file_size = int(path.stat().st_size)
        except OSError:
            file_size = 0

        try:
            path.unlink()
            removed_files += 1
            removed_size_bytes += max(file_size, 0)
        except OSError:
            continue

    return jsonify(
        {
            "ok": True,
            "removed_files": removed_files,
            "removed_size_bytes": removed_size_bytes,
        }
    )
