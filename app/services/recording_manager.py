import subprocess
import sys
import threading
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass
class RecordingJob:
    channel: str
    quality: str
    output_file: str
    output_day: str
    output_title: str
    started_at: str
    process: subprocess.Popen


class RecordingManager:
    _MAX_TITLE_FILENAME_LENGTH = 80
    _WINDOWS_RESERVED_FILENAMES = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }

    def __init__(
        self,
        streamlink_command: str,
        default_quality: str,
        default_output_path: str,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        stream_probe_timeout_seconds: int = 20,
        streamlink_extra_args: list[str] | None = None,
        background_priority: bool = True,
    ) -> None:
        self._streamlink_command = streamlink_command
        self._default_quality = default_quality
        self._default_output_path = default_output_path
        self._event_callback = event_callback
        self._stream_probe_timeout_seconds = max(5, int(stream_probe_timeout_seconds))
        self._streamlink_extra_args = list(streamlink_extra_args or [])
        self._background_priority = bool(background_priority)
        self._jobs: dict[str, RecordingJob] = {}
        self._lock = threading.RLock()

    def _streamlink_commands(self, args: list[str]) -> list[list[str]]:
        full_args = [*self._streamlink_extra_args, *args]
        primary = [self._streamlink_command, *full_args]
        fallback = [sys.executable, "-m", "streamlink", *full_args]

        if self._streamlink_command == sys.executable:
            return [primary]

        return [primary, fallback]

    @staticmethod
    def _normalize_channel(channel: str) -> str:
        return channel.strip().lower().lstrip("@")

    def _cleanup_finished_locked(self) -> list[RecordingJob]:
        finished_jobs: list[RecordingJob] = []
        to_remove = [
            channel for channel, job in self._jobs.items() if job.process.poll() is not None
        ]
        for channel in to_remove:
            job = self._jobs.pop(channel, None)
            if job is not None:
                finished_jobs.append(job)

        return finished_jobs

    def _emit_event(self, event: dict[str, Any]) -> None:
        if self._event_callback is None:
            return

        try:
            self._event_callback(event)
        except Exception:
            pass

    def _emit_finished_jobs(self, jobs: list[RecordingJob]) -> None:
        if not jobs:
            return

        stopped_at = datetime.now(timezone.utc).isoformat()
        for job in jobs:
            self._emit_event(
                {
                    "event": "recording_stopped",
                    "channel": job.channel,
                    "quality": job.quality,
                    "output_file": job.output_file,
                    "started_at": job.started_at,
                    "stopped_at": stopped_at,
                    "reason": "process_exited",
                }
            )

    @staticmethod
    def _current_utc_day(now: datetime | None = None) -> str:
        current_time = now or datetime.now(timezone.utc)
        return current_time.strftime("%Y-%m-%d")

    @classmethod
    def _sanitize_filename_title(cls, stream_title: str | None) -> str:
        raw_title = (stream_title or "").strip()
        if not raw_title:
            return "stream"

        cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "", raw_title)
        cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(". ")
        if len(cleaned) > cls._MAX_TITLE_FILENAME_LENGTH:
            cleaned = cleaned[: cls._MAX_TITLE_FILENAME_LENGTH].rstrip(". ")

        if not cleaned:
            return "stream"

        if cleaned.upper() in cls._WINDOWS_RESERVED_FILENAMES:
            cleaned = f"{cleaned}_"

        return cleaned

    def _build_output_file(
        self,
        channel: str,
        output_path: str,
        stream_title: str | None = None,
        now: datetime | None = None,
    ) -> tuple[Path, str, str]:
        current_time = now or datetime.now(timezone.utc)
        timestamp = current_time.strftime("%Y%m%d_%H%M%S")
        day_folder = self._current_utc_day(current_time)
        output_dir = Path(output_path) / channel / day_folder
        output_dir.mkdir(parents=True, exist_ok=True)
        safe_title = self._sanitize_filename_title(stream_title)
        return output_dir / f"{safe_title}_{timestamp}.ts", day_folder, safe_title

    def is_recording(self, channel: str) -> bool:
        normalized = self._normalize_channel(channel)
        with self._lock:
            finished_jobs = self._cleanup_finished_locked()
            is_active = normalized in self._jobs

        self._emit_finished_jobs(finished_jobs)
        return is_active

    def get_active_recordings(self) -> list[dict[str, Any]]:
        with self._lock:
            finished_jobs = self._cleanup_finished_locked()
            result: list[dict[str, Any]] = []
            for channel, job in sorted(self._jobs.items()):
                result.append(
                    {
                        "channel": channel,
                        "quality": job.quality,
                        "output_file": job.output_file,
                        "started_at": job.started_at,
                        "pid": job.process.pid,
                    }
                )

        self._emit_finished_jobs(finished_jobs)
        return result

    def check_channel_live(self, channel: str, quality: str | None = None) -> bool:
        stream_info = self.get_channel_stream_info(channel, quality=quality)
        return bool(stream_info["is_live"])

    @staticmethod
    def _parse_stream_info_payload(payload: str) -> tuple[bool, str | None]:
        if not payload.strip():
            return False, None

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return False, None

        stream_url = data.get("url")
        is_live = isinstance(stream_url, str) and bool(stream_url.strip())

        title: str | None = None
        metadata = data.get("metadata")
        if isinstance(metadata, dict):
            raw_title = metadata.get("title")
            if isinstance(raw_title, str):
                title = raw_title.strip() or None

        return is_live, title

    def get_channel_stream_info(
        self,
        channel: str,
        quality: str | None = None,
    ) -> dict[str, str | bool | None]:
        normalized = self._normalize_channel(channel)
        if not normalized:
            return {"is_live": False, "title": None}

        url = f"https://twitch.tv/{normalized}"

        # Live detection should not depend on an exact quality being available.
        probe_qualities = ["best"]
        requested_quality = (quality or "").strip().lower()
        if requested_quality in {"best", "worst"} and requested_quality not in probe_qualities:
            probe_qualities.append(requested_quality)

        for probe_quality in probe_qualities:
            args = [
                "--json",
                url,
                probe_quality,
            ]

            for command in self._streamlink_commands(args):
                try:
                    result = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        timeout=self._stream_probe_timeout_seconds,
                        check=False,
                    )
                except FileNotFoundError:
                    continue
                except (OSError, subprocess.TimeoutExpired):
                    continue

                if result.returncode != 0:
                    continue

                is_live, title = self._parse_stream_info_payload(result.stdout)
                if is_live:
                    return {"is_live": True, "title": title}

        return {"is_live": False, "title": None}

    def _build_streamlink_popen_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }

        if self._background_priority and sys.platform.startswith("win"):
            create_no_window = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            below_normal = int(getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
            creationflags = create_no_window | below_normal
            if creationflags:
                kwargs["creationflags"] = creationflags

        return kwargs

    def start_recording(
        self,
        channel: str,
        quality: str | None = None,
        output_path: str | None = None,
    ) -> tuple[bool, str]:
        normalized = self._normalize_channel(channel)
        if not normalized:
            return False, "Channel name is required."

        stream_quality = (quality or self._default_quality).strip() or self._default_quality
        destination = (output_path or self._default_output_path).strip() or self._default_output_path
        stream_info = self.get_channel_stream_info(normalized, quality=stream_quality)
        stream_title = str(stream_info.get("title") or "").strip() or None
        output_file: Path | None = None
        output_day = ""
        output_title = ""
        started_at = ""
        process: subprocess.Popen | None = None
        last_error: OSError | None = None
        already_recording = False

        with self._lock:
            finished_jobs = self._cleanup_finished_locked()
            if normalized in self._jobs:
                already_recording = True
            else:
                start_time = datetime.now(timezone.utc)
                output_file, output_day, output_title = self._build_output_file(
                    normalized,
                    destination,
                    stream_title=stream_title,
                    now=start_time,
                )
                url = f"https://twitch.tv/{normalized}"
                args = [
                    "--output",
                    str(output_file),
                    url,
                    stream_quality,
                ]

                for command in self._streamlink_commands(args):
                    try:
                        popen_kwargs = self._build_streamlink_popen_kwargs()
                        process = subprocess.Popen(
                            command,
                            **popen_kwargs,
                        )
                        break
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        last_error = exc
                        break

                if process is not None:
                    started_at = start_time.isoformat()
                    self._jobs[normalized] = RecordingJob(
                        channel=normalized,
                        quality=stream_quality,
                        output_file=str(output_file),
                        output_day=output_day,
                        output_title=output_title,
                        started_at=started_at,
                        process=process,
                    )

        self._emit_finished_jobs(finished_jobs)
        if already_recording:
            return False, f"{normalized} is already recording."

        if process is None:
            if last_error is not None:
                return False, f"Failed to start recording: {last_error}"
            return False, "Streamlink command was not found. Install Streamlink first."

        self._emit_event(
            {
                "event": "recording_started",
                "channel": normalized,
                "quality": stream_quality,
                "output_file": str(output_file),
                "started_at": started_at,
                "pid": process.pid,
            }
        )
        return True, f"Started recording {normalized}."

    def rotate_recording_if_needed(
        self,
        channel: str,
        stream_title: str | None = None,
        stream_is_live: bool = True,
    ) -> tuple[bool, str]:
        normalized = self._normalize_channel(channel)
        if not normalized:
            return False, "Channel name is required."

        if not stream_is_live:
            return False, "No rotation needed."

        with self._lock:
            finished_jobs = self._cleanup_finished_locked()
            job = self._jobs.get(normalized)

        self._emit_finished_jobs(finished_jobs)
        if job is None:
            return False, f"{normalized} is not recording."

        expected_day = self._current_utc_day()
        day_changed = job.output_day != expected_day

        title_changed = False
        raw_stream_title = (stream_title or "").strip()
        if raw_stream_title:
            current_title = self._sanitize_filename_title(raw_stream_title)
            title_changed = current_title != job.output_title

        if not day_changed and not title_changed:
            return False, "No rotation needed."

        self.stop_recording(normalized)
        started, start_message = self.start_recording(normalized, quality=job.quality)
        if not started:
            return False, f"Rotation failed: {start_message}"

        reason = "day and title changed" if day_changed and title_changed else (
            "day changed" if day_changed else "title changed"
        )
        return True, f"Rotated recording for {normalized}: {reason}."

    def stop_recording(self, channel: str) -> tuple[bool, str]:
        normalized = self._normalize_channel(channel)
        stopped_job: RecordingJob | None = None
        is_not_recording = False

        with self._lock:
            finished_jobs = self._cleanup_finished_locked()
            job = self._jobs.get(normalized)
            if job is None:
                is_not_recording = True
            else:
                process = job.process
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

                stopped_job = self._jobs.pop(normalized, None)

        self._emit_finished_jobs(finished_jobs)
        if is_not_recording:
            return False, f"{normalized} is not recording."

        if stopped_job is not None:
            self._emit_event(
                {
                    "event": "recording_stopped",
                    "channel": stopped_job.channel,
                    "quality": stopped_job.quality,
                    "output_file": stopped_job.output_file,
                    "started_at": stopped_job.started_at,
                    "stopped_at": datetime.now(timezone.utc).isoformat(),
                    "reason": "manual_stop",
                }
            )
        return True, f"Stopped recording {normalized}."

    def stop_all(self) -> None:
        with self._lock:
            channels = list(self._jobs.keys())

        for channel in channels:
            self.stop_recording(channel)
