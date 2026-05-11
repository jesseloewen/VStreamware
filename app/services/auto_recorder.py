from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from .recording_manager import RecordingManager
from .settings_store import SettingsStore


class AutoRecorder:
    def __init__(
        self,
        settings_store: SettingsStore,
        recording_manager: RecordingManager,
        poll_seconds: int,
        max_probe_workers: int,
        stream_event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._settings_store = settings_store
        self._recording_manager = recording_manager
        self._poll_seconds = max(5, int(poll_seconds))
        self._max_probe_workers = max(1, int(max_probe_workers))
        self._stream_event_callback = stream_event_callback
        self._channel_live_states: dict[str, dict[str, str | bool | None]] = {}
        self._last_refresh_at: str | None = None
        self._stop_event = threading.Event()
        self._wait_condition = threading.Condition()
        self._probe_executor: ThreadPoolExecutor | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    def _build_probe_executor(self) -> ThreadPoolExecutor:
        return ThreadPoolExecutor(
            max_workers=self._max_probe_workers,
            thread_name_prefix="stream-probe",
        )

    @staticmethod
    def _normalize_channel(channel: str) -> str:
        return channel.strip().lower().lstrip("@")

    @staticmethod
    def _normalize_optional_iso(value: object) -> str | None:
        if not isinstance(value, str):
            return None

        normalized = value.strip()
        return normalized or None

    def _emit_stream_event(self, event: dict[str, Any]) -> None:
        if self._stream_event_callback is None:
            return

        try:
            self._stream_event_callback(event)
        except Exception:
            pass

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return

            self._stop_event.clear()
            if self._probe_executor is None:
                self._probe_executor = self._build_probe_executor()
            self._thread = threading.Thread(
                target=self._run,
                name="auto-recorder",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._stop_event.set()

        with self._wait_condition:
            self._wait_condition.notify_all()

        if thread and thread.is_alive():
            thread.join(timeout=5)

        with self._lock:
            probe_executor = self._probe_executor
            self._probe_executor = None

        if probe_executor is not None:
            probe_executor.shutdown(wait=False, cancel_futures=True)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            running = bool(self._thread and self._thread.is_alive())
            last_refresh_at = self._last_refresh_at

        settings = self._settings_store.get_settings()
        saved_channels = settings["saved_channels"]
        enabled_channels = sum(1 for item in saved_channels if bool(item["auto_record"]))
        return {
            "running": running,
            "enabled_channels": enabled_channels,
            "total_channels": len(saved_channels),
            "poll_seconds": self._poll_seconds,
            "max_probe_workers": self._max_probe_workers,
            "last_refresh_at": last_refresh_at,
        }

    def get_live_state(self, channel: str) -> bool | None:
        live_info = self.get_live_info(channel)
        if live_info is None:
            return None

        return bool(live_info.get("is_live", False))

    def get_live_info(self, channel: str) -> dict[str, str | bool | None] | None:
        normalized = self._normalize_channel(channel)
        if not normalized:
            return None

        with self._lock:
            current = self._channel_live_states.get(normalized)

        if current is None:
            return None

        return dict(current)

    def request_refresh(self) -> None:
        with self._wait_condition:
            self._wait_condition.notify()

    @staticmethod
    def _normalize_stream_info(info: object) -> dict[str, str | bool | None]:
        if not isinstance(info, dict):
            return {"is_live": False, "title": None}

        is_live = bool(info.get("is_live", False))
        raw_title = info.get("title")
        title: str | None = None
        if isinstance(raw_title, str):
            title = raw_title.strip() or None

        return {"is_live": is_live, "title": title}

    def _probe_stream_infos(self, channels: list[str]) -> dict[str, dict[str, str | bool | None]]:
        if not channels:
            return {}

        worker_count = min(self._max_probe_workers, len(channels))
        if worker_count <= 1:
            return {
                channel: self._normalize_stream_info(
                    self._recording_manager.get_channel_stream_info(channel)
                )
                for channel in channels
            }

        with self._lock:
            probe_executor = self._probe_executor

        if probe_executor is None:
            return {
                channel: self._normalize_stream_info(
                    self._recording_manager.get_channel_stream_info(channel)
                )
                for channel in channels
            }

        stream_infos: dict[str, dict[str, str | bool | None]] = {}
        futures: dict[Future[dict[str, str | bool | None]], str] = {}
        for channel in channels:
            try:
                future: Future[dict[str, str | bool | None]] = probe_executor.submit(
                    self._recording_manager.get_channel_stream_info,
                    channel,
                )
            except RuntimeError:
                return {
                    fallback_channel: self._normalize_stream_info(
                        self._recording_manager.get_channel_stream_info(fallback_channel)
                    )
                    for fallback_channel in channels
                }
            futures[future] = channel

        for future in as_completed(futures):
            channel = futures[future]
            try:
                stream_infos[channel] = self._normalize_stream_info(future.result())
            except Exception:
                stream_infos[channel] = {"is_live": False, "title": None}

        return stream_infos

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                settings = self._settings_store.get_settings()
                with self._lock:
                    previous_live_states = dict(self._channel_live_states)
                observed_at = datetime.now(timezone.utc).isoformat()

                saved_channels_by_name: dict[str, dict[str, object]] = {}
                for item in settings["saved_channels"]:
                    channel_name = self._normalize_channel(str(item["name"]))
                    if not channel_name:
                        continue
                    saved_channels_by_name[channel_name] = item

                active_recordings = self._recording_manager.get_active_recordings()
                active_recording_channels: set[str] = set()
                active_recording_started_at: dict[str, str | None] = {}
                for recording in active_recordings:
                    channel_name = self._normalize_channel(str(recording.get("channel", "")))
                    if not channel_name:
                        continue

                    active_recording_channels.add(channel_name)
                    active_recording_started_at[channel_name] = self._normalize_optional_iso(
                        recording.get("started_at")
                    )

                channels_to_check = set(saved_channels_by_name.keys()) | active_recording_channels
                sorted_channels = sorted(channels_to_check)
                stream_info_by_channel = self._probe_stream_infos(sorted_channels)
                next_live_states: dict[str, dict[str, str | bool | None]] = {}
                for channel in sorted_channels:
                    saved_item = saved_channels_by_name.get(channel)
                    auto_record = bool(saved_item["auto_record"]) if saved_item is not None else False

                    is_recording = self._recording_manager.is_recording(channel)
                    recording_started_at = active_recording_started_at.get(channel)
                    stream_info = dict(
                        stream_info_by_channel.get(channel, {"is_live": False, "title": None})
                    )
                    stream_is_live = bool(stream_info["is_live"])
                    is_live = is_recording or stream_is_live

                    previous_live_info = previous_live_states.get(channel, {})
                    previous_last_live_at = self._normalize_optional_iso(previous_live_info.get("last_live_at"))
                    previous_last_recording_at = self._normalize_optional_iso(previous_live_info.get("last_recording_at"))

                    last_live_at = observed_at if is_live else previous_last_live_at
                    last_recording_at = recording_started_at if is_recording and recording_started_at else previous_last_recording_at

                    if is_recording and not stream_info["is_live"]:
                        stream_info["title"] = previous_live_info.get("title")

                    if is_recording and stream_is_live:
                        stream_title = stream_info.get("title")
                        if not isinstance(stream_title, str):
                            stream_title = None

                        self._recording_manager.rotate_recording_if_needed(
                            channel,
                            stream_title=stream_title,
                            stream_is_live=stream_is_live,
                        )
                        is_recording = self._recording_manager.is_recording(channel)

                    stream_info["is_live"] = is_live

                    previous_live_state = None
                    if previous_live_info is not None:
                        previous_live_state = bool(previous_live_info.get("is_live", False))

                    should_emit_live_change = previous_live_state is not None and previous_live_state != is_live
                    should_emit_startup_live = previous_live_state is None and is_live
                    if should_emit_live_change or should_emit_startup_live:
                        event: dict[str, Any] = {
                            "event": "stream_live" if is_live else "stream_end",
                            "channel": channel,
                        }
                        stream_title = stream_info.get("title")
                        if is_live and isinstance(stream_title, str) and stream_title.strip():
                            event["title"] = stream_title.strip()

                        self._emit_stream_event(
                            event
                        )

                    stream_info["last_live_at"] = last_live_at
                    stream_info["recording_started_at"] = recording_started_at
                    stream_info["last_recording_at"] = last_recording_at
                    next_live_states[channel] = stream_info

                    if auto_record and is_live and not is_recording:
                        self._recording_manager.start_recording(
                            channel,
                        )

                with self._lock:
                    self._channel_live_states = next_live_states
            except Exception:
                # Keep the monitor alive on transient errors.
                pass
            finally:
                with self._lock:
                    self._last_refresh_at = datetime.now(timezone.utc).isoformat()

            if self._stop_event.is_set():
                break

            with self._wait_condition:
                self._wait_condition.wait(timeout=self._poll_seconds)
