import json
import hashlib
import math
import mimetypes
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse
from typing import Any
from zoneinfo import available_timezones

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
_TRANSIENT_TRANSCODE_JOBS_GUARD = threading.Lock()
_TRANSIENT_TRANSCODE_JOBS: dict[str, dict[str, object]] = {}
_MEDIA_DURATION_CACHE_GUARD = threading.Lock()
_MEDIA_DURATION_CACHE: dict[str, int] = {}
_MEDIA_DURATION_CACHE_MAX_ENTRIES = 6000

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


@lru_cache(maxsize=1)
def _display_timezone_options() -> tuple[str, ...]:
    try:
        discovered = available_timezones()
    except Exception:
        discovered = {"UTC"}

    if not isinstance(discovered, set):
        discovered = set(discovered)

    discovered.add("UTC")
    return tuple(sorted(str(name) for name in discovered if isinstance(name, str) and name.strip()))


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


def _format_duration_label(total_seconds_value: object) -> str:
    try:
        total_seconds = float(total_seconds_value)
    except (TypeError, ValueError):
        return "n/a"

    if not math.isfinite(total_seconds) or total_seconds <= 0:
        return "n/a"

    rounded = max(1, int(math.floor(total_seconds)))
    hours = rounded // 3600
    minutes = (rounded % 3600) // 60
    seconds = rounded % 60

    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    return f"{minutes}:{seconds:02d}"


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


def _is_transcode_output_locked(output_path: Path) -> bool:
    cache_key = str(output_path.resolve())
    with _TRANSCODE_LOCKS_GUARD:
        lock = _TRANSCODE_LOCKS.get(cache_key)

    return bool(lock is not None and lock.locked())


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_file_size(path: Path) -> int:
    try:
        if path.exists() and path.is_file():
            return int(path.stat().st_size)
    except OSError:
        return 0

    return 0


def _estimate_progress_percent(source_size_bytes: int, output_size_bytes: int) -> int:
    if source_size_bytes <= 0:
        return 1 if output_size_bytes > 0 else 0

    if output_size_bytes <= 0:
        return 0

    return max(1, min(99, int((output_size_bytes / source_size_bytes) * 100)))


def _transient_transcode_key(output_path: Path) -> str:
    try:
        return str(output_path.resolve())
    except OSError:
        return str(output_path)


def _register_transient_transcode_job(
    source_path: Path,
    output_path: Path,
    transient_cache_id: str | None = None,
    live_dvr_key: str | None = None,
) -> None:
    source_size_bytes = _safe_file_size(source_path)
    job_key = _transient_transcode_key(output_path)

    with _TRANSIENT_TRANSCODE_JOBS_GUARD:
        _TRANSIENT_TRANSCODE_JOBS[job_key] = {
            "source_path": source_path,
            "output_path": output_path,
            "source_size_bytes": source_size_bytes,
            "transient_cache_id": transient_cache_id or "",
            "live_dvr_key": live_dvr_key or "",
            "started_at": _utc_now_iso(),
            "cancel_requested": False,
            "process": None,
        }


def _unregister_transient_transcode_job(output_path: Path) -> None:
    job_key = _transient_transcode_key(output_path)
    with _TRANSIENT_TRANSCODE_JOBS_GUARD:
        _TRANSIENT_TRANSCODE_JOBS.pop(job_key, None)


def _set_transient_transcode_process(output_path: Path, process: subprocess.Popen[bytes] | None) -> None:
    job_key = _transient_transcode_key(output_path)
    with _TRANSIENT_TRANSCODE_JOBS_GUARD:
        job = _TRANSIENT_TRANSCODE_JOBS.get(job_key)
        if isinstance(job, dict):
            job["process"] = process


def _is_transient_transcode_cancel_requested(output_path: Path) -> bool:
    job_key = _transient_transcode_key(output_path)
    with _TRANSIENT_TRANSCODE_JOBS_GUARD:
        job = _TRANSIENT_TRANSCODE_JOBS.get(job_key)
        if not isinstance(job, dict):
            return False

        return bool(job.get("cancel_requested"))


def _terminate_subprocess(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return

    try:
        process.terminate()
    except OSError:
        pass

    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass

        try:
            process.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass


def _request_cancel_transient_jobs(transient_cache_id: str) -> int:
    if not transient_cache_id:
        return 0

    active_processes: list[subprocess.Popen[bytes]] = []
    with _TRANSIENT_TRANSCODE_JOBS_GUARD:
        for job in _TRANSIENT_TRANSCODE_JOBS.values():
            if not isinstance(job, dict):
                continue

            if str(job.get("transient_cache_id", "")).strip() != transient_cache_id:
                continue

            job["cancel_requested"] = True
            process = job.get("process")
            if isinstance(process, subprocess.Popen):
                active_processes.append(process)

    for process in active_processes:
        _terminate_subprocess(process)

    return len(active_processes)


def _snapshot_active_transient_transcode(root: Path) -> dict[str, object] | None:
    with _TRANSIENT_TRANSCODE_JOBS_GUARD:
        jobs = list(_TRANSIENT_TRANSCODE_JOBS.values())

    active_job: dict[str, object] | None = None
    for job in jobs:
        if not isinstance(job, dict):
            continue

        output_path = job.get("output_path")
        source_path = job.get("source_path")
        if not isinstance(output_path, Path) or not isinstance(source_path, Path):
            continue

        if not _is_transcode_output_locked(output_path):
            continue

        active_job = job

    if active_job is None:
        return None

    source_path = active_job["source_path"]
    output_path = active_job["output_path"]

    source_size_bytes = int(active_job.get("source_size_bytes", 0) or 0)
    if source_size_bytes <= 0:
        source_size_bytes = _safe_file_size(source_path)

    output_size_bytes = _safe_file_size(output_path)
    progress_percent = _estimate_progress_percent(source_size_bytes, output_size_bytes)

    try:
        relative_path = source_path.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        relative_path = source_path.name

    return {
        "relative_path": relative_path,
        "file_name": source_path.name,
        "progress_percent": progress_percent,
        "started_at": str(active_job.get("started_at", "") or ""),
        "source_size_bytes": max(source_size_bytes, 0),
        "output_size_bytes": max(output_size_bytes, 0),
        "kind": "live_dvr_transient",
    }


def _build_live_dvr_transcode_status(output_path: Path, source_size_bytes: int) -> dict[str, object]:
    source_size = max(int(source_size_bytes), 0)
    cached_size_bytes = _safe_file_size(output_path)
    is_locked = _is_transcode_output_locked(output_path)

    if cached_size_bytes > 0 and not is_locked:
        return {
            "available": True,
            "is_transcoding": False,
            "progress_percent": 100,
            "progress_label": "Ready",
            "source_size_bytes": source_size,
            "cached_size_bytes": cached_size_bytes,
            "is_live_source": True,
            "state": "ready",
            "error": None,
        }

    if is_locked:
        progress_percent = _estimate_progress_percent(source_size, cached_size_bytes)
        return {
            "available": False,
            "is_transcoding": True,
            "progress_percent": progress_percent,
            "progress_label": "Transcoding",
            "source_size_bytes": source_size,
            "cached_size_bytes": cached_size_bytes,
            "is_live_source": True,
            "state": "transcoding",
            "error": None,
        }

    # If a DVR request was created but ffmpeg has not yet locked the output path,
    # keep the UI in a waiting state so the overlay remains visible.
    return {
        "available": False,
        "is_transcoding": False,
        "progress_percent": 0,
        "progress_label": "Queued",
        "source_size_bytes": source_size,
        "cached_size_bytes": cached_size_bytes,
        "is_live_source": True,
        "state": "queued",
        "error": None,
    }


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
        "transcode_status_url": url_for("dashboard.recording_transcode_status", recording_path=relative_path),
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


def _recording_navigation_payload(item: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(item, dict):
        return None

    view_slug = str(item.get("view_slug", "")).strip()
    if not view_slug:
        return None

    size_label = str(item.get("size_label", "")).strip()
    size_bytes = int(item.get("size_bytes", 0) or 0)
    duration_seconds = item.get("media_duration_seconds")

    started_at_value = item.get("started_at")
    started_at = str(started_at_value).strip() if isinstance(started_at_value, str) else ""
    recorded_at_value = item.get("recorded_at")
    recorded_at = str(recorded_at_value).strip() if isinstance(recorded_at_value, str) else ""

    return {
        "view_slug": view_slug,
        "view_url": url_for("dashboard.view_recording", recording_slug=view_slug),
        "display_title": str(item.get("display_title", "")).strip() or str(item.get("file_name", "")).strip(),
        "channel": str(item.get("channel", "")).strip(),
        "day": str(item.get("day", "")).strip(),
        "thumbnail_url": str(item.get("thumbnail_url", "")).strip(),
        "is_live": bool(item.get("is_live", False)),
        "quality": str(item.get("quality", "")).strip(),
        "started_at": started_at,
        "recorded_at": recorded_at,
        "recorded_at_unix": int(item.get("recorded_at_unix", 0) or 0),
        "size_label": size_label or _format_size_label(size_bytes),
        "size_bytes": size_bytes,
        "media_duration_seconds": duration_seconds,
        "duration_label": _format_duration_label(duration_seconds),
    }


def _channel_recording_neighbors(
    catalog: dict[str, object],
    current_record: dict[str, object],
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    channel_name = str(current_record.get("channel", "")).strip()
    current_relative_path = str(current_record.get("relative_path", "")).strip()
    current_view_slug = str(current_record.get("view_slug", "")).strip()
    if not current_view_slug and current_relative_path:
        current_view_slug = _recording_view_slug(current_relative_path)
    if not channel_name or not current_relative_path:
        return None, None

    recordings = catalog.get("recordings", [])
    if not isinstance(recordings, list):
        return None, None

    channel_key = channel_name.lower()
    filtered: list[dict[str, object]] = []
    for item in recordings:
        if not isinstance(item, dict):
            continue

        item_channel = str(item.get("channel", "")).strip().lower()
        if item_channel != channel_key:
            continue

        item_relative_path = str(item.get("relative_path", "")).strip()
        if not item_relative_path:
            continue

        item_can_play = bool(item.get("can_play", False))
        if not item_can_play and item_relative_path != current_relative_path:
            continue

        filtered.append(item)

    if not filtered:
        return None, None

    current_index = -1
    for index, item in enumerate(filtered):
        if str(item.get("relative_path", "")).strip() == current_relative_path:
            current_index = index
            break

    if current_index < 0:
        return None, None

    def _find_neighbor_item(start_index: int, step: int) -> dict[str, object] | None:
        index = start_index
        while 0 <= index < len(filtered):
            candidate = filtered[index]
            candidate_relative_path = str(candidate.get("relative_path", "")).strip()
            candidate_view_slug = str(candidate.get("view_slug", "")).strip()

            if not candidate_relative_path or candidate_relative_path == current_relative_path:
                index += step
                continue

            # Live .ts and incremental .mp4 can share a stem and therefore slug.
            # Skip same-slug siblings so previous/next always moves to another recording.
            if current_view_slug and candidate_view_slug and candidate_view_slug == current_view_slug:
                index += step
                continue

            return candidate

        return None

    # Catalog order is newest-first. Previous should go older; Next should go newer.
    previous_item = _find_neighbor_item(current_index + 1, 1)
    next_item = _find_neighbor_item(current_index - 1, -1)

    return _recording_navigation_payload(previous_item), _recording_navigation_payload(next_item)


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


def _ffprobe_command_candidates() -> list[str]:
    candidates: list[str] = []

    for command in _ffmpeg_command_candidates():
        ffmpeg_binary = str(command[0]).strip()
        if not ffmpeg_binary:
            continue

        ffmpeg_path = Path(ffmpeg_binary)
        ffmpeg_name = ffmpeg_path.name
        if "ffprobe" in ffmpeg_name.lower():
            candidates.append(str(ffmpeg_path))
            continue

        if "ffmpeg" in ffmpeg_name.lower():
            probe_name = re.sub("ffmpeg", "ffprobe", ffmpeg_name, flags=re.IGNORECASE)
            probe_candidate = str(ffmpeg_path.with_name(probe_name))
            candidates.append(probe_candidate)

    candidates.append("ffprobe")

    unique: list[str] = []
    seen = set()
    for candidate in candidates:
        normalized = str(candidate).strip()
        if not normalized or normalized in seen:
            continue

        seen.add(normalized)
        unique.append(normalized)

    return unique


@lru_cache(maxsize=1)
def _resolve_ffprobe_binary() -> str | None:
    for binary in _ffprobe_command_candidates():
        path_candidate = Path(binary)
        if path_candidate.exists():
            return str(path_candidate)

        if shutil.which(binary) is not None:
            return binary

    return None


def _duration_cache_key(media_path: Path, file_size: int, file_mtime_ns: int) -> str:
    try:
        resolved = str(media_path.resolve())
    except OSError:
        resolved = str(media_path)

    return f"{resolved}|{int(file_size)}|{int(file_mtime_ns)}"


def _probe_media_duration_seconds(
    media_path: Path,
    *,
    file_size: int | None = None,
    file_mtime_ns: int | None = None,
) -> int | None:
    if not media_path.exists() or not media_path.is_file():
        return None

    if file_size is None or file_mtime_ns is None:
        try:
            file_stat = media_path.stat()
        except OSError:
            return None

        file_size = int(file_stat.st_size)
        file_mtime_ns = int(file_stat.st_mtime_ns)

    cache_key = _duration_cache_key(media_path, int(file_size), int(file_mtime_ns))
    with _MEDIA_DURATION_CACHE_GUARD:
        cached = _MEDIA_DURATION_CACHE.get(cache_key)
    if isinstance(cached, int):
        return cached

    ffprobe_binary = _resolve_ffprobe_binary()
    if not ffprobe_binary:
        return None

    command = [
        ffprobe_binary,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(media_path),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    raw_duration = str(result.stdout).strip()
    if not raw_duration:
        return None

    try:
        parsed_duration = float(raw_duration)
    except ValueError:
        return None

    if parsed_duration <= 0:
        return None

    duration_seconds = max(1, int(round(parsed_duration)))
    with _MEDIA_DURATION_CACHE_GUARD:
        if len(_MEDIA_DURATION_CACHE) >= _MEDIA_DURATION_CACHE_MAX_ENTRIES:
            _MEDIA_DURATION_CACHE.clear()
        _MEDIA_DURATION_CACHE[cache_key] = duration_seconds

    return duration_seconds


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


def _materialize_ts_mp4_file(
    ts_path: Path,
    output_path: Path | None = None,
    transient_cache_id: str | None = None,
) -> Path | None:
    ffmpeg_binary = _resolve_ffmpeg_binary()
    if ffmpeg_binary is None:
        return None

    if output_path is None:
        return None

    cache_path = output_path
    cancellation_enabled = bool(
        transient_cache_id
        and _is_valid_transient_cache_id(str(transient_cache_id).strip())
    )

    transcode_lock = _transcode_output_lock(cache_path)
    with transcode_lock:
        if cancellation_enabled and _is_transient_transcode_cancel_requested(cache_path):
            try:
                if cache_path.exists():
                    cache_path.unlink()
            except OSError:
                pass

            return None

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
            if cancellation_enabled and _is_transient_transcode_cancel_requested(cache_path):
                break

            try:
                if cache_path.exists():
                    cache_path.unlink()
            except OSError:
                pass

            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
            except OSError:
                continue

            _set_transient_transcode_process(cache_path, process)
            try:
                started_at = time.monotonic()
                command_timed_out = False
                command_cancelled = False
                while True:
                    return_code = process.poll()
                    if return_code is not None:
                        break

                    if cancellation_enabled and _is_transient_transcode_cancel_requested(cache_path):
                        command_cancelled = True
                        _terminate_subprocess(process)
                        break

                    if (time.monotonic() - started_at) >= (60 * 30):
                        command_timed_out = True
                        _terminate_subprocess(process)
                        break

                    time.sleep(0.25)

                if command_cancelled:
                    break

                if command_timed_out:
                    continue

                if process.returncode != 0:
                    continue

            except OSError:
                continue
            finally:
                _set_transient_transcode_process(cache_path, None)

            if cache_path.exists() and cache_path.stat().st_size > 0:
                return cache_path

        if cancellation_enabled and _is_transient_transcode_cancel_requested(cache_path):
            try:
                if cache_path.exists():
                    cache_path.unlink()
            except OSError:
                pass

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
                day_files = [
                    path
                    for path in sorted(day_dir.iterdir(), key=lambda path: path.name.lower())
                    if path.is_file()
                ]
                mp4_stems = {
                    path.stem
                    for path in day_files
                    if path.suffix.lower() == ".mp4"
                }

                for recording_file in day_files:

                    suffix = recording_file.suffix.lower()
                    if suffix and suffix not in VIDEO_EXTENSIONS:
                        continue

                    try:
                        file_stat = recording_file.stat()
                        resolved_file = recording_file.resolve()
                    except OSError:
                        continue

                    # Prefer finalized MP4 when both MP4 and TS are present for the same recording stem,
                    # but keep the TS entry if it is currently active.
                    if (
                        suffix == ".ts"
                        and recording_file.stem in mp4_stems
                        and resolved_file not in active_by_path
                    ):
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
                    media_duration_seconds = _probe_media_duration_seconds(
                        recording_file,
                        file_size=int(file_stat.st_size),
                        file_mtime_ns=int(file_stat.st_mtime_ns),
                    )

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
                        "media_duration_seconds": media_duration_seconds,
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
        size_mtime_ns = 0
        recording_end_utc_ms = recorded_at_utc_ms
        try:
            active_stat = active_path.stat()
            size_bytes = int(active_stat.st_size)
            size_mtime_ns = int(active_stat.st_mtime_ns)
            recording_end_utc_ms = int(active_stat.st_mtime * 1000)
        except OSError:
            pass

        media_duration_seconds = _probe_media_duration_seconds(
            active_path,
            file_size=size_bytes,
            file_mtime_ns=size_mtime_ns,
        )

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
                "media_duration_seconds": media_duration_seconds,
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
        timezone_options=_display_timezone_options(),
        dashboard_signature=dashboard_state["dashboard_signature"],
        pushover_configured=pushover_configured,
        cache_summary=cache_summary,
    )


@dashboard_bp.get("/status")
def status() -> object:
    services = get_services(current_app)
    dashboard_state = _build_dashboard_state(services)
    return jsonify(dashboard_state)


@dashboard_bp.get("/transcode/status")
def transcode_queue_status() -> object:
    services = get_services(current_app)
    transcode_queue = services.get("transcode_queue")
    recordings_root = _recordings_root_path()
    snapshot = {
        "running": False,
        "is_working": False,
        "queued_count": 0,
        "failed_count": 0,
        "active": None,
        "last_failed": None,
        "indicator_text": "",
    }

    get_snapshot = getattr(transcode_queue, "snapshot", None)
    if callable(get_snapshot):
        try:
            payload = get_snapshot()
            if isinstance(payload, dict):
                snapshot = payload
        except Exception:
            pass

    active_job = snapshot.get("active")
    has_persistent_active_job = isinstance(active_job, dict) and bool(active_job)
    if not has_persistent_active_job:
        live_dvr_service = services.get("live_incremental_dvr")
        get_live_snapshot = getattr(live_dvr_service, "snapshot", None)
        if callable(get_live_snapshot):
            try:
                live_snapshot = get_live_snapshot()
            except Exception:
                live_snapshot = None

            if isinstance(live_snapshot, dict) and bool(live_snapshot.get("is_working")):
                active_payload = live_snapshot.get("active")
                indicator_text = str(live_snapshot.get("indicator_text", "")).strip()
                if isinstance(active_payload, dict):
                    snapshot["active"] = {
                        "relative_path": str(active_payload.get("relative_path", "")),
                        "file_name": str(active_payload.get("file_name", "")),
                        "progress_percent": int(active_payload.get("progress_percent", 0) or 0),
                        "started_at": str(active_payload.get("started_at", "")),
                    }
                    snapshot["is_working"] = True
                    snapshot["indicator_text"] = indicator_text

    active_job = snapshot.get("active")
    has_persistent_active_job = isinstance(active_job, dict) and bool(active_job)
    if not has_persistent_active_job:
        transient_active = _snapshot_active_transient_transcode(recordings_root)
        if transient_active is not None:
            progress_percent = int(transient_active.get("progress_percent", 0) or 0)
            display_name = str(
                transient_active.get("file_name")
                or transient_active.get("relative_path")
                or "live dvr"
            ).strip()
            snapshot["active"] = {
                "relative_path": str(transient_active.get("relative_path", "")),
                "file_name": str(transient_active.get("file_name", "")),
                "progress_percent": progress_percent,
                "started_at": str(transient_active.get("started_at", "")),
            }
            snapshot["is_working"] = True
            snapshot["indicator_text"] = f"{display_name} ({progress_percent}%)"

    return jsonify(snapshot)


@dashboard_bp.post("/transcode/backfill")
def transcode_backfill() -> object:
    services = get_services(current_app)
    transcode_queue = services.get("transcode_queue")
    enqueue_existing = getattr(transcode_queue, "enqueue_existing_recordings", None)
    queued_count = 0

    if callable(enqueue_existing):
        try:
            queued_count = int(enqueue_existing())
        except Exception:
            queued_count = 0

    if queued_count > 0:
        flash(f"Queued {queued_count} recording transcode jobs.", "success")
    else:
        flash("No additional TS recordings were eligible for backfill.", "success")

    return _redirect_back("dashboard.settings")


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

    previous_recording, next_recording = _channel_recording_neighbors(catalog, record)

    # Visiting a completed TS recording should enqueue the same persistent queue job
    # that continues even if the client leaves the page.
    if not bool(record.get("is_live", False)):
        relative_path = str(record.get("relative_path", "")).strip()
        if relative_path.lower().endswith(".ts"):
            resolved_path = _resolve_recording_path(_recordings_root_path(), relative_path)
            if resolved_path is not None and resolved_path.exists() and resolved_path.is_file():
                transcode_queue = services.get("transcode_queue")
                enqueue = getattr(transcode_queue, "enqueue_file", None)
                if callable(enqueue):
                    try:
                        enqueue(resolved_path, reason="view-recording")
                    except Exception:
                        pass

    live_buffer_default = _live_buffer_default_seconds()
    live_buffer_min, live_buffer_max = _live_buffer_bounds()

    return render_template(
        "video_detail.html",
        recording=record,
        previous_recording=previous_recording,
        next_recording=next_recording,
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
    requested_suffix = Path(recording_path).suffix.lower()
    resolved_path = _resolve_recording_path(root, recording_path)
    if resolved_path is None:
        abort(404)

    if not resolved_path.exists() or not resolved_path.is_file():
        if requested_suffix == ".ts":
            mp4_fallback = _resolve_recording_path(root, str(Path(recording_path).with_suffix(".mp4")))
            if mp4_fallback is not None and mp4_fallback.exists() and mp4_fallback.is_file():
                resolved_path = mp4_fallback
            else:
                abort(404)
        else:
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
                live_dvr_service = services.get("live_incremental_dvr")
                get_dvr_snapshot_path = getattr(live_dvr_service, "get_dvr_snapshot_path", None)
                if callable(get_dvr_snapshot_path):
                    try:
                        snapshot_path = get_dvr_snapshot_path(resolved_path)
                    except Exception:
                        snapshot_path = None

                    if isinstance(snapshot_path, Path):
                        snapshot_size = _safe_file_size(snapshot_path)
                        if snapshot_size > 0:
                            response = send_file(
                                snapshot_path,
                                mimetype="video/mp4",
                                conditional=True,
                                as_attachment=False,
                                max_age=0,
                            )
                            response.headers["Accept-Ranges"] = "bytes"
                            response.headers["Cache-Control"] = "no-store"
                            response.headers["X-Playback-Transcoded-File"] = "true"
                            response.headers["X-Playback-Live-DVR"] = "true"
                            response.headers["X-Playback-Live-DVR-Incremental"] = "true"
                            return response

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
                    _register_transient_transcode_job(
                        resolved_path,
                        dvr_snapshot_path,
                        transient_cache_id=transient_cache_id or None,
                        live_dvr_key=live_dvr_key,
                    )
                    try:
                        converted_mp4 = _materialize_ts_mp4_file(
                            resolved_path,
                            output_path=dvr_snapshot_path,
                            transient_cache_id=transient_cache_id or None,
                        )
                    finally:
                        _unregister_transient_transcode_job(dvr_snapshot_path)

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

        finalized_mp4 = resolved_path.with_suffix(".mp4")
        try:
            finalized_size = int(finalized_mp4.stat().st_size) if finalized_mp4.exists() else 0
        except OSError:
            finalized_size = 0

        if finalized_size > 0:
            response = send_file(
                finalized_mp4,
                mimetype="video/mp4",
                conditional=True,
                as_attachment=False,
                max_age=600,
            )
            response.headers["Accept-Ranges"] = "bytes"
            response.headers["Cache-Control"] = "public, max-age=600"
            response.headers["X-Playback-Transcoded-File"] = "true"
            return response

        transcode_queue = services.get("transcode_queue")
        enqueue = getattr(transcode_queue, "enqueue_file", None)
        if callable(enqueue):
            try:
                enqueue(resolved_path, reason="playback-request")
            except Exception:
                pass

        return Response(
            "Recording transcode is still in progress. Please retry shortly.",
            status=425,
            mimetype="text/plain",
            headers={
                "Cache-Control": "no-store",
                "Retry-After": "2",
                "X-Playback-Transcode-Pending": "true",
            },
        )

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


@dashboard_bp.get("/recordings/transcode-status/<path:recording_path>")
def recording_transcode_status(recording_path: str) -> object:
    playback_mode = str(request.args.get("mode", "")).strip().lower()
    prefer_recorded_playback = playback_mode == "recorded"

    root = _recordings_root_path()
    requested_path = Path(recording_path)
    requested_suffix = requested_path.suffix.lower()
    resolved_path = _resolve_recording_path(root, recording_path)
    if resolved_path is None:
        abort(404)

    if (not resolved_path.exists() or not resolved_path.is_file()) and requested_suffix == ".ts":
        mp4_fallback = _resolve_recording_path(root, str(requested_path.with_suffix(".mp4")))
        if mp4_fallback is not None and mp4_fallback.exists() and mp4_fallback.is_file():
            source_size_bytes = 0
            try:
                source_size_bytes = int(mp4_fallback.stat().st_size)
            except OSError:
                source_size_bytes = 0

            return jsonify(
                {
                    "available": True,
                    "is_transcoding": False,
                    "progress_percent": 100,
                    "progress_label": "Ready",
                    "source_size_bytes": source_size_bytes,
                    "cached_size_bytes": source_size_bytes,
                    "is_live_source": False,
                    "state": "ready",
                }
            )

    if not resolved_path.exists() or not resolved_path.is_file():
        abort(404)

    suffix = resolved_path.suffix.lower()
    source_size_bytes = 0
    try:
        source_size_bytes = int(resolved_path.stat().st_size)
    except OSError:
        source_size_bytes = 0

    # Non-TS files are directly streamable and do not require a transcode wait state.
    if suffix != ".ts":
        return jsonify(
            {
                "available": True,
                "is_transcoding": False,
                "progress_percent": 100,
                "progress_label": "Ready",
                "source_size_bytes": source_size_bytes,
                "cached_size_bytes": source_size_bytes,
                "is_live_source": False,
                "state": "ready",
            }
        )

    services = get_services(current_app)
    recording_manager = services["recording_manager"]
    if _is_live_recording_path(recording_manager, resolved_path):
        if prefer_recorded_playback:
            live_dvr_service = services.get("live_incremental_dvr")
            get_live_status = getattr(live_dvr_service, "get_live_status", None)
            if callable(get_live_status):
                try:
                    live_status = get_live_status(resolved_path)
                except Exception:
                    live_status = None

                if isinstance(live_status, dict):
                    live_state = str(live_status.get("state", "queued")).strip().lower()
                    available_now = bool(live_status.get("available"))
                    progress_percent = int(live_status.get("progress_percent", 0) or 0)
                    source_size = int(live_status.get("source_size_bytes", source_size_bytes) or source_size_bytes)
                    cached_size = int(live_status.get("cached_size_bytes", 0) or 0)
                    live_error = live_status.get("error")

                    if available_now:
                        return jsonify(
                            {
                                "available": True,
                                "is_transcoding": False,
                                "progress_percent": 100,
                                "progress_label": "Ready",
                                "source_size_bytes": source_size,
                                "cached_size_bytes": cached_size,
                                "is_live_source": True,
                                "state": "ready",
                                "error": None,
                            }
                        )

                    progress_label = "Queued"
                    is_transcoding = False
                    if live_state == "catching_up":
                        progress_label = "Catching up"
                        is_transcoding = True
                    elif live_state == "finalizing_tail":
                        progress_label = "Finalizing tail"
                        is_transcoding = True
                    elif live_state == "failed":
                        progress_label = "Failed"
                        is_transcoding = False
                    elif live_state == "pending":
                        progress_label = "Preparing"

                    return jsonify(
                        {
                            "available": False,
                            "is_transcoding": bool(is_transcoding),
                            "progress_percent": max(0, min(99, progress_percent)),
                            "progress_label": progress_label,
                            "source_size_bytes": source_size,
                            "cached_size_bytes": cached_size,
                            "is_live_source": True,
                            "state": live_state,
                            "error": live_error if live_state == "failed" else None,
                        }
                    )

            transient_cache_id = request.args.get("transient_id", "").strip()
            live_dvr_key = request.args.get("live_dvr", "").strip()
            has_transient_id = _is_valid_transient_cache_id(transient_cache_id)
            has_live_dvr_key = _is_valid_live_dvr_key(live_dvr_key)
            if has_transient_id and has_live_dvr_key:
                try:
                    dvr_snapshot_path = _live_dvr_snapshot_path(
                        root,
                        resolved_path,
                        live_dvr_key,
                        transient_cache_id=transient_cache_id,
                    )
                except (OSError, ValueError):
                    dvr_snapshot_path = None

                if dvr_snapshot_path is not None:
                    live_dvr_status = _build_live_dvr_transcode_status(
                        dvr_snapshot_path,
                        source_size_bytes,
                    )
                    return jsonify(live_dvr_status)

            return jsonify(
                {
                    "available": False,
                    "is_transcoding": False,
                    "progress_percent": 0,
                    "progress_label": "Live",
                    "source_size_bytes": source_size_bytes,
                    "cached_size_bytes": 0,
                    "is_live_source": True,
                    "state": "live_wait",
                }
            )

        return jsonify(
            {
                "available": True,
                "is_transcoding": False,
                "progress_percent": 100,
                "progress_label": "Live",
                "source_size_bytes": source_size_bytes,
                "cached_size_bytes": source_size_bytes,
                "is_live_source": True,
                "state": "live",
            }
        )

    finalized_mp4 = resolved_path.with_suffix(".mp4")
    finalized_size_bytes = 0
    try:
        if finalized_mp4.exists() and finalized_mp4.is_file():
            finalized_size_bytes = int(finalized_mp4.stat().st_size)
    except OSError:
        finalized_size_bytes = 0

    if finalized_size_bytes > 0:
        return jsonify(
            {
                "available": True,
                "is_transcoding": False,
                "progress_percent": 100,
                "progress_label": "Ready",
                "source_size_bytes": source_size_bytes,
                "cached_size_bytes": finalized_size_bytes,
                "is_live_source": False,
                "state": "ready",
            }
        )

    transcode_queue = services.get("transcode_queue")
    file_status: dict[str, object] = {"state": "pending", "progress_percent": 0, "output_size_bytes": 0}
    get_file_status = getattr(transcode_queue, "get_file_status", None)
    if callable(get_file_status):
        try:
            status_payload = get_file_status(resolved_path)
            if isinstance(status_payload, dict):
                file_status = status_payload
        except Exception:
            file_status = {"state": "pending", "progress_percent": 0, "output_size_bytes": 0}

    state = str(file_status.get("state", "pending")).strip().lower()
    progress_percent = int(file_status.get("progress_percent", 0) or 0)
    cached_size_bytes = int(file_status.get("output_size_bytes", 0) or 0)

    if state in {"pending", "missing"}:
        enqueue = getattr(transcode_queue, "enqueue_file", None)
        if callable(enqueue):
            try:
                did_queue = bool(enqueue(resolved_path, reason="status-request"))
                if did_queue:
                    state = "queued"
            except Exception:
                pass

    if state == "ready":
        progress_label = "Ready"
        progress_percent = 100
        is_transcoding = False
        available = True
    elif state == "transcoding":
        progress_label = "Transcoding"
        progress_percent = max(1, min(99, progress_percent))
        is_transcoding = True
        available = False
    elif state == "queued":
        progress_label = "Queued"
        progress_percent = 0
        is_transcoding = False
        available = False
    elif state == "failed":
        progress_label = "Failed"
        progress_percent = 0
        is_transcoding = False
        available = False
    else:
        progress_label = "Preparing"
        progress_percent = 0
        is_transcoding = False
        available = False

    return jsonify(
        {
            "available": available,
            "is_transcoding": bool(is_transcoding),
            "progress_percent": progress_percent,
            "progress_label": progress_label,
            "source_size_bytes": source_size_bytes,
            "cached_size_bytes": cached_size_bytes,
            "is_live_source": False,
            "state": state,
            "error": file_status.get("error") if state == "failed" else None,
        }
    )


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
            (
                "Live/thumbnail cache cleared. "
                f"Removed {removed_files} files and {removed_dirs} directories."
            ),
            "success",
        )
    else:
        flash("Live and thumbnail caches were already empty.", "success")

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

    cancelled_processes = _request_cancel_transient_jobs(transient_cache_id)

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
            "cancelled_processes": cancelled_processes,
            "removed_files": removed_files,
            "removed_size_bytes": removed_size_bytes,
        }
    )
