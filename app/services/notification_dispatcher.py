from __future__ import annotations

import threading
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
        self._pending_events: dict[str, dict[str, Any]] = {}
        self._pending_timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

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

        if event_name in ("recording_started", "recording_stopped", "stream_live", "stream_end"):
            self._handle_pairwise_event(event_name, channel, event, notifications)
            return

        if not bool(notifications.get(event_name, False)):
            return

        title, message = self._build_message(event_name, channel, event)
        self._notifier.send_message(title=title, message=message)

    def _handle_pairwise_event(
        self,
        event_name: str,
        channel: str,
        event: dict[str, Any],
        notifications: dict[str, Any],
    ) -> None:
        with self._lock:
            old_timer = self._pending_timers.pop(channel, None)
            if old_timer is not None:
                old_timer.cancel()

            other_event = self._pending_events.pop(channel, None)
            if other_event is not None:
                self._dispatch_matched_pair(event_name, channel, event, other_event, notifications)
                return

            self._pending_events[channel] = event
            timer = threading.Timer(3.0, self._send_pending_alone, args=[channel])
            self._pending_timers[channel] = timer
            timer.start()

    def _dispatch_matched_pair(
        self,
        event_name: str,
        channel: str,
        event: dict[str, Any],
        other_event: dict[str, Any],
        notifications: dict[str, Any],
    ) -> None:
        other_name = str(other_event.get("event", ""))

        stream_evt: dict[str, Any] | None = None
        rec_evt: dict[str, Any] | None = None

        if event_name in ("stream_live", "stream_end"):
            stream_evt = event
            rec_evt = other_event if other_name in ("recording_started", "recording_stopped") else None
        elif other_name in ("stream_live", "stream_end"):
            stream_evt = other_event
            rec_evt = event

        if stream_evt is None or rec_evt is None:
            for evt in (event, other_event):
                en = str(evt.get("event", ""))
                if bool(notifications.get(en, False)):
                    title, message = self._build_message(en, channel, evt)
                    self._notifier.send_message(title=title, message=message)
            return

        stream_name = str(stream_evt.get("event", ""))
        rec_name = str(rec_evt.get("event", ""))

        is_live = stream_name == "stream_live"
        is_recording = rec_name == "recording_started"

        if is_live and is_recording:
            stream_enabled = bool(notifications.get("stream_live", False))
            rec_enabled = bool(notifications.get("recording_started", False))

            if stream_enabled and rec_enabled:
                combined = {**stream_evt, **rec_evt}
                title, message = self._build_message("stream_live_and_recording", channel, combined)
                self._notifier.send_message(title=title, message=message)
            elif stream_enabled:
                title, message = self._build_message("stream_live", channel, stream_evt)
                self._notifier.send_message(title=title, message=message)
            elif rec_enabled:
                title, message = self._build_message("recording_started", channel, rec_evt)
                self._notifier.send_message(title=title, message=message)
        elif not is_live and not is_recording:
            stream_enabled = bool(notifications.get("stream_end", False))
            rec_enabled = bool(notifications.get("recording_stopped", False))

            if stream_enabled and rec_enabled:
                combined = {**stream_evt, **rec_evt}
                title, message = self._build_message("stream_end_and_recording", channel, combined)
                self._notifier.send_message(title=title, message=message)
            elif stream_enabled:
                title, message = self._build_message("stream_end", channel, stream_evt)
                self._notifier.send_message(title=title, message=message)
            elif rec_enabled:
                title, message = self._build_message("recording_stopped", channel, rec_evt)
                self._notifier.send_message(title=title, message=message)
        else:
            for evt in (event, other_event):
                en = str(evt.get("event", ""))
                if bool(notifications.get(en, False)):
                    title, message = self._build_message(en, channel, evt)
                    self._notifier.send_message(title=title, message=message)

    def _send_pending_alone(self, channel: str) -> None:
        with self._lock:
            pending = self._pending_events.pop(channel, None)
            self._pending_timers.pop(channel, None)

        if pending is None:
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
        event_name = str(pending.get("event", ""))
        if bool(notifications.get(event_name, False)):
            title, message = self._build_message(event_name, channel, pending)
            self._notifier.send_message(title=title, message=message)

    @staticmethod
    def _build_message(
        event_name: str,
        channel: str,
        event: dict[str, Any],
    ) -> tuple[str, str]:
        if event_name == "stream_live":
            stream_title = event.get("title")
            if isinstance(stream_title, str) and stream_title.strip():
                return (
                    "Streamer Live",
                    f"{channel} is live on Twitch.\n{stream_title.strip()}",
                )
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

        if event_name == "stream_live_and_recording":
            stream_title = event.get("title")
            output_file = str(event.get("output_file", "")).strip()
            lines: list[str] = []
            if isinstance(stream_title, str) and stream_title.strip():
                lines.append(stream_title.strip())
            if output_file:
                filename = Path(output_file).name
                lines.append(f"File: {filename}")
            extra = "\n" + "\n".join(lines) if lines else ""
            return (
                "Streamer Live & Recording Started",
                f"{channel} is live on Twitch and recording has started.{extra}",
            )

        if event_name == "stream_end_and_recording":
            output_file = str(event.get("output_file", "")).strip()
            if output_file:
                return (
                    "Stream Ended & Recording Stopped",
                    f"{channel} has gone offline and recording has stopped.\nFile: {output_file}",
                )
            return (
                "Stream Ended & Recording Stopped",
                f"{channel} has gone offline and recording has stopped.",
            )

        now = datetime.now(timezone.utc).isoformat()
        return (
            "VStreamware",
            f"Event '{event_name}' for {channel} at {now}.",
        )