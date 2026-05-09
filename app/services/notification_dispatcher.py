from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .pushover_notifier import PushoverNotifier
from .settings_store import SettingsStore


class NotificationDispatcher:
    def __init__(
        self,
        settings_store: SettingsStore,
        notifier: PushoverNotifier,
    ) -> None:
        self._settings_store = settings_store
        self._notifier = notifier

    def is_configured(self) -> bool:
        return self._notifier.is_configured()

    def send_test_notification(self) -> tuple[bool, str]:
        if not self._notifier.is_configured():
            return False, "Pushover is not configured."

        now = datetime.now(timezone.utc).isoformat()
        return self._notifier.send_message(
            title="VStreamware Test",
            message=f"Test notification sent at {now}.",
        )

    def handle_event(self, event: dict[str, Any]) -> None:
        event_name = str(event.get("event", "")).strip()
        channel = str(event.get("channel", "")).strip().lower().lstrip("@")
        if not event_name or not channel:
            return

        if not self._notifier.is_configured():
            return

        settings = self._settings_store.get_settings()
        saved_channel = None
        for item in settings["saved_channels"]:
            if str(item["name"]) == channel:
                saved_channel = item
                break

        if saved_channel is None:
            return

        notifications = saved_channel.get("notifications", {})
        if not bool(notifications.get(event_name, False)):
            return

        title, message = self._build_message(event_name, channel, event)
        self._notifier.send_message(title=title, message=message)

    @staticmethod
    def _build_message(
        event_name: str,
        channel: str,
        event: dict[str, Any],
    ) -> tuple[str, str]:
        if event_name == "stream_live":
            return (
                "Streamer Live",
                f"{channel} is live on Twitch.",
            )

        if event_name == "stream_end":
            return (
                "Stream Ended",
                f"{channel} has gone offline.",
            )

        if event_name == "recording_started":
            output_file = str(event.get("output_file", "")).strip()
            if output_file:
                filename = Path(output_file).name
                return (
                    "Recording Started",
                    f"Recording started for {channel}. File: {filename}",
                )
            return (
                "Recording Started",
                f"Recording started for {channel}.",
            )

        if event_name == "recording_stopped":
            output_file = str(event.get("output_file", "")).strip()
            if output_file:
                return (
                    "Recording Stopped",
                    f"Recording stopped for {channel}. File: {output_file}",
                )
            return (
                "Recording Stopped",
                f"Recording stopped for {channel}.",
            )

        now = datetime.now(timezone.utc).isoformat()
        return (
            "VStreamware",
            f"Event '{event_name}' for {channel} at {now}.",
        )
