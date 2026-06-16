import json
import threading
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError


class SettingsStore:
    NOTIFICATION_DEFAULTS = {
        "stream_live": True,
        "stream_end": True,
        "recording_started": True,
        "recording_stopped": True,
    }
    DISPLAY_TIMEZONE_AUTO = "auto"

    def __init__(
        self,
        settings_file: str,
    ) -> None:
        self._settings_file = Path(settings_file)
        self._lock = threading.RLock()
        self._settings = self._load_settings()

    @staticmethod
    def _normalize_channel(channel: str) -> str:
        return channel.strip().lower().lstrip("@")

    @classmethod
    def _default_notifications(cls) -> dict[str, bool]:
        return {key: bool(value) for key, value in cls.NOTIFICATION_DEFAULTS.items()}

    @classmethod
    def _normalize_notifications(cls, raw: Any) -> dict[str, bool]:
        notifications = cls._default_notifications()
        if not isinstance(raw, dict):
            return notifications

        for key, default_value in cls.NOTIFICATION_DEFAULTS.items():
            notifications[key] = bool(raw.get(key, default_value))

        return notifications

    @staticmethod
    def _normalize_display_timezone(value: Any) -> str:
        if not isinstance(value, str):
            return SettingsStore.DISPLAY_TIMEZONE_AUTO

        normalized = value.strip()
        if not normalized:
            return SettingsStore.DISPLAY_TIMEZONE_AUTO

        if normalized.lower() == SettingsStore.DISPLAY_TIMEZONE_AUTO:
            return SettingsStore.DISPLAY_TIMEZONE_AUTO

        try:
            ZoneInfo(normalized)
        except (ZoneInfoNotFoundError, ValueError):
            return SettingsStore.DISPLAY_TIMEZONE_AUTO

        return normalized

    @staticmethod
    def _validate_display_timezone(value: Any) -> tuple[bool, str, str]:
        if not isinstance(value, str):
            return False, SettingsStore.DISPLAY_TIMEZONE_AUTO, "Display timezone is required."

        normalized = value.strip()
        if not normalized:
            return False, SettingsStore.DISPLAY_TIMEZONE_AUTO, "Display timezone is required."

        if normalized.lower() == SettingsStore.DISPLAY_TIMEZONE_AUTO:
            return True, SettingsStore.DISPLAY_TIMEZONE_AUTO, ""

        try:
            ZoneInfo(normalized)
        except (ZoneInfoNotFoundError, ValueError):
            return False, SettingsStore.DISPLAY_TIMEZONE_AUTO, f"Unsupported timezone: {normalized}."

        return True, normalized, ""

    @staticmethod
    def _normalize_quality(value: Any) -> str | None:
        """Return a stripped quality string, or None to use the global default."""
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized if normalized else None

    @staticmethod
    def _normalize_saved_channel(
        value: Any,
        default_auto_record: bool,
    ) -> dict[str, Any] | None:
        if isinstance(value, str):
            name = SettingsStore._normalize_channel(value)
            if not name:
                return None
            return {
                "name": name,
                "auto_record": default_auto_record,
                "quality": None,
                "notifications": SettingsStore._default_notifications(),
            }

        if not isinstance(value, dict):
            return None

        raw_name = value.get("name")
        if not isinstance(raw_name, str):
            return None

        name = SettingsStore._normalize_channel(raw_name)
        if not name:
            return None

        auto_record = bool(value.get("auto_record", False))
        return {
            "name": name,
            "auto_record": auto_record,
            "quality": SettingsStore._normalize_quality(value.get("quality")),
            "notifications": SettingsStore._normalize_notifications(value.get("notifications")),
        }

    @staticmethod
    def _copy_saved_channels(saved_channels: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "name": str(item["name"]),
                "auto_record": bool(item["auto_record"]),
                "quality": SettingsStore._normalize_quality(item.get("quality")),
                "notifications": SettingsStore._normalize_notifications(item.get("notifications")),
            }
            for item in saved_channels
        ]

    @staticmethod
    def _saved_channel_names(saved_channels: list[dict[str, Any]]) -> set[str]:
        return {str(item["name"]) for item in saved_channels}

    def _base_settings(self) -> dict[str, Any]:
        return {
            "saved_channels": [],
            "display_timezone": self.DISPLAY_TIMEZONE_AUTO,
        }

    def _normalize_settings(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        settings = self._base_settings()
        if not raw:
            return settings

        legacy_auto_record_raw = raw.get("auto_record", False)
        legacy_auto_record = legacy_auto_record_raw if isinstance(legacy_auto_record_raw, bool) else False

        raw_saved_channels = raw.get("saved_channels")
        if raw_saved_channels is None:
            raw_saved_channels = raw.get("channels", [])

        saved_channels: list[dict[str, Any]] = []
        seen = set()
        if isinstance(raw_saved_channels, list):
            for value in raw_saved_channels:
                normalized_item = self._normalize_saved_channel(
                    value,
                    legacy_auto_record,
                )
                if normalized_item is None:
                    continue

                name = str(normalized_item["name"])
                if name in seen:
                    continue

                saved_channels.append(normalized_item)
                seen.add(name)

        settings["saved_channels"] = saved_channels
        settings["display_timezone"] = self._normalize_display_timezone(raw.get("display_timezone"))

        return settings

    def _load_settings(self) -> dict[str, Any]:
        if not self._settings_file.exists():
            return self._base_settings()

        try:
            raw = json.loads(self._settings_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return self._base_settings()

        if not isinstance(raw, dict):
            return self._base_settings()

        return self._normalize_settings(raw)

    def _save_settings(self) -> None:
        self._settings_file.parent.mkdir(parents=True, exist_ok=True)
        self._settings_file.write_text(
            json.dumps(self._settings, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            return {
                "saved_channels": self._copy_saved_channels(self._settings["saved_channels"]),
                "display_timezone": str(self._settings.get("display_timezone", self.DISPLAY_TIMEZONE_AUTO)),
            }

    def add_channel(self, channel: str) -> tuple[bool, str]:
        normalized = self._normalize_channel(channel)
        if not normalized:
            return False, "Channel name is required."

        with self._lock:
            saved_channels: list[dict[str, Any]] = self._settings["saved_channels"]
            if normalized in self._saved_channel_names(saved_channels):
                return False, f"{normalized} is already in the channel list."

            saved_channels.append(
                {
                    "name": normalized,
                    "auto_record": False,
                    "quality": None,
                    "notifications": self._default_notifications(),
                }
            )
            self._save_settings()
            return True, f"Added {normalized}."

    def remove_channel(self, channel: str) -> tuple[bool, str]:
        normalized = self._normalize_channel(channel)
        with self._lock:
            saved_channels: list[dict[str, Any]] = self._settings["saved_channels"]
            names = self._saved_channel_names(saved_channels)
            if normalized not in names:
                return False, f"{normalized} is not in the channel list."

            self._settings["saved_channels"] = [
                item for item in saved_channels if str(item["name"]) != normalized
            ]
            self._save_settings()
            return True, f"Removed {normalized}."

    def set_channel_auto_record(self, channel: str, enabled: bool) -> tuple[bool, str]:
        normalized = self._normalize_channel(channel)
        if not normalized:
            return False, "Channel name is required."

        with self._lock:
            saved_channels: list[dict[str, Any]] = self._settings["saved_channels"]
            for item in saved_channels:
                if str(item["name"]) == normalized:
                    item["auto_record"] = bool(enabled)
                    self._save_settings()
                    state = "enabled" if enabled else "disabled"
                    return True, f"Auto record {state} for {normalized}."

            return False, f"{normalized} is not in the channel list."

    def set_channel_quality(self, channel: str, quality: str | None) -> tuple[bool, str]:
        normalized = self._normalize_channel(channel)
        if not normalized:
            return False, "Channel name is required."

        normalized_quality = self._normalize_quality(quality)

        with self._lock:
            saved_channels: list[dict[str, Any]] = self._settings["saved_channels"]
            for item in saved_channels:
                if str(item["name"]) == normalized:
                    item["quality"] = normalized_quality
                    self._save_settings()
                    if normalized_quality:
                        return True, f"Quality set to '{normalized_quality}' for {normalized}."
                    return True, f"Quality reset to global default for {normalized}."

            return False, f"{normalized} is not in the channel list."

    def set_channel_notifications(
        self,
        channel: str,
        notifications: dict[str, bool],
    ) -> tuple[bool, str]:
        normalized = self._normalize_channel(channel)
        if not normalized:
            return False, "Channel name is required."

        normalized_notifications = self._normalize_notifications(notifications)

        with self._lock:
            saved_channels: list[dict[str, Any]] = self._settings["saved_channels"]
            for item in saved_channels:
                if str(item["name"]) == normalized:
                    item["notifications"] = normalized_notifications
                    self._save_settings()
                    return True, f"Updated notification settings for {normalized}."

            return False, f"{normalized} is not in the channel list."

    def set_display_timezone(self, timezone_name: str) -> tuple[bool, str]:
        is_valid, normalized_timezone, error_message = self._validate_display_timezone(timezone_name)
        if not is_valid:
            return False, error_message

        with self._lock:
            self._settings["display_timezone"] = normalized_timezone
            self._save_settings()

        if normalized_timezone == self.DISPLAY_TIMEZONE_AUTO:
            return True, "Display timezone set to browser default (auto)."

        return True, f"Display timezone set to {normalized_timezone}."

    def is_saved_channel(self, channel: str) -> bool:
        normalized = self._normalize_channel(channel)
        with self._lock:
            saved_channels: list[dict[str, Any]] = self._settings["saved_channels"]
            return normalized in self._saved_channel_names(saved_channels)
