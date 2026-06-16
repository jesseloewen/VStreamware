from datetime import datetime, timezone
from typing import Any


def _normalize_channel_name(channel: str) -> str:
    return channel.strip().lower().lstrip("@")


def _normalize_optional_iso(value: object) -> str | None:
    if not isinstance(value, str):
        return None

    normalized = value.strip()
    return normalized or None


def _parse_iso_to_unix(value: str | None) -> int:
    if not value:
        return 0

    normalized = value.strip()
    if not normalized:
        return 0

    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return 0

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return int(parsed.timestamp())


def _build_activity_sort_fields(
    is_recording: bool,
    is_live: bool,
    recording_started_at: str | None,
    last_live_at: str | None,
    last_recording_at: str | None,
) -> tuple[int, str | None, int]:
    sort_priority = 0 if is_recording else (1 if is_live else 2)
    candidates = [recording_started_at, last_recording_at, last_live_at]

    last_activity_at: str | None = None
    last_activity_unix = 0
    for candidate in candidates:
        candidate_unix = _parse_iso_to_unix(candidate)
        if candidate_unix > last_activity_unix:
            last_activity_unix = candidate_unix
            last_activity_at = candidate

    return sort_priority, last_activity_at, last_activity_unix


def _sort_saved_channels_recent(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(
        rows,
        key=lambda row: (
            int(row.get("sort_priority", 2)),
            -int(row.get("last_activity_unix", 0)),
            str(row.get("name", "")).lower(),
        ),
    )


def build_saved_channels_status(
    saved_channels: list[dict[str, object]],
    recording_manager: Any,
    auto_recorder: Any,
) -> list[dict[str, object]]:
    active_recordings = recording_manager.get_active_recordings()
    active_recording_channels: set[str] = set()
    active_recording_started_at: dict[str, str | None] = {}
    for recording in active_recordings:
        channel_name = _normalize_channel_name(str(recording.get("channel", "")))
        if not channel_name:
            continue

        active_recording_channels.add(channel_name)
        active_recording_started_at[channel_name] = _normalize_optional_iso(recording.get("started_at"))

    rows: list[dict[str, object]] = []
    for item in saved_channels:
        channel = str(item["name"])
        channel_key = _normalize_channel_name(channel)
        auto_record = bool(item["auto_record"])
        is_recording = channel_key in active_recording_channels
        recording_started_at = active_recording_started_at.get(channel_key)
        cached_live_info = auto_recorder.get_live_info(channel)
        cached_live_state = None
        stream_title: str | None = None
        last_live_at: str | None = None
        last_recording_at: str | None = None
        if cached_live_info is not None:
            cached_live_state = bool(cached_live_info.get("is_live", False))
            raw_title = cached_live_info.get("title")
            if isinstance(raw_title, str):
                stream_title = raw_title.strip() or None
            last_live_at = _normalize_optional_iso(cached_live_info.get("last_live_at"))
            last_recording_at = _normalize_optional_iso(cached_live_info.get("last_recording_at"))

        if recording_started_at:
            last_recording_at = recording_started_at

        is_live = is_recording or cached_live_state is True
        live_state = "live" if is_live else ("offline" if cached_live_state is False else "checking")
        sort_priority, last_activity_at, last_activity_unix = _build_activity_sort_fields(
            is_recording=is_recording,
            is_live=is_live,
            recording_started_at=recording_started_at,
            last_live_at=last_live_at,
            last_recording_at=last_recording_at,
        )

        channel_quality = item.get("quality")
        quality: str | None = channel_quality if isinstance(channel_quality, str) and channel_quality.strip() else None

        rows.append(
            {
                "name": channel,
                "auto_record": auto_record,
                "quality": quality,
                "notifications": dict(item.get("notifications", {})),
                "is_live": is_live,
                "live_state": live_state,
                "is_recording": is_recording,
                "stream_title": stream_title,
                "recording_started_at": recording_started_at,
                "last_live_at": last_live_at,
                "last_recording_at": last_recording_at,
                "last_activity_at": last_activity_at,
                "last_activity_unix": last_activity_unix,
                "sort_priority": sort_priority,
            }
        )

    return _sort_saved_channels_recent(rows)
