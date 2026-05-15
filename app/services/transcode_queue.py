import shutil
import subprocess
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import imageio_ffmpeg  # type: ignore[import-not-found]
except Exception:
    imageio_ffmpeg = None


class TranscodeQueueService:
    def __init__(
        self,
        recordings_dir: str,
        ffmpeg_command: str,
        recording_manager: Any,
        startup_backfill: bool = True,
        failed_history_limit: int = 100,
    ) -> None:
        self._recordings_root = Path(recordings_dir).expanduser().resolve()
        self._ffmpeg_command = (ffmpeg_command or "ffmpeg").strip() or "ffmpeg"
        self._recording_manager = recording_manager
        self._startup_backfill = bool(startup_backfill)
        self._failed_history_limit = max(10, int(failed_history_limit))

        self._queue: deque[Path] = deque()
        self._queued_keys: set[str] = set()
        self._failed_jobs: dict[str, dict[str, object]] = {}

        self._active_job: dict[str, object] | None = None
        self._active_job_key: str | None = None
        self._active_process: subprocess.Popen[bytes] | None = None

        self._stop_event = threading.Event()
        self._wait_condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._accept_new_jobs = True
        self._lock = threading.RLock()

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _path_key(self, path: Path) -> str:
        try:
            return str(path.resolve())
        except OSError:
            return str(path)

    def _relative_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self._recordings_root).as_posix()
        except (OSError, ValueError):
            try:
                return path.relative_to(self._recordings_root).as_posix()
            except ValueError:
                return path.name

    def _active_recording_keys(self) -> set[str]:
        active_keys: set[str] = set()
        try:
            active_recordings = self._recording_manager.get_active_recordings()
        except Exception:
            return active_keys

        if not isinstance(active_recordings, list):
            return active_keys

        for item in active_recordings:
            if not isinstance(item, dict):
                continue

            output_file = item.get("output_file")
            if not isinstance(output_file, str) or not output_file.strip():
                continue

            try:
                active_path = Path(output_file).expanduser().resolve()
            except OSError:
                continue

            active_keys.add(self._path_key(active_path))

        return active_keys

    @staticmethod
    def _safe_file_size(path: Path) -> int:
        try:
            if path.exists() and path.is_file():
                return int(path.stat().st_size)
        except OSError:
            return 0

        return 0

    def _is_ready_mp4(self, path: Path) -> bool:
        return self._safe_file_size(path) > 0

    def _is_transcode_candidate(self, source_path: Path) -> bool:
        if source_path.suffix.lower() != ".ts":
            return False

        try:
            resolved_source = source_path.expanduser().resolve()
        except OSError:
            return False

        if not resolved_source.exists() or not resolved_source.is_file():
            return False

        source_key = self._path_key(resolved_source)
        if source_key in self._active_recording_keys():
            return False

        target_mp4 = resolved_source.with_suffix(".mp4")
        if self._is_ready_mp4(target_mp4):
            # Conversion is already complete; best effort cleanup of stale source.
            try:
                resolved_source.unlink()
            except OSError:
                pass
            return False

        return True

    def _ffmpeg_command_candidates(self) -> list[str]:
        candidates: list[str] = []
        if self._ffmpeg_command:
            candidates.append(self._ffmpeg_command)

        candidates.append("ffmpeg")

        if imageio_ffmpeg is not None:
            try:
                candidates.append(str(imageio_ffmpeg.get_ffmpeg_exe()))
            except Exception:
                pass

        unique: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            normalized = candidate.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique.append(normalized)

        return unique

    def _resolve_ffmpeg_binary(self) -> str | None:
        for candidate in self._ffmpeg_command_candidates():
            if Path(candidate).exists():
                return candidate

            if shutil.which(candidate) is not None:
                return candidate

        return None

    def _mark_failed(self, source_path: Path, reason: str) -> None:
        key = self._path_key(source_path)
        relative_path = self._relative_path(source_path)
        failed_entry = {
            "relative_path": relative_path,
            "file_name": source_path.name,
            "failed_at": self._utc_now_iso(),
            "error": reason,
        }

        with self._lock:
            self._failed_jobs[key] = failed_entry
            if len(self._failed_jobs) > self._failed_history_limit:
                oldest_key = next(iter(self._failed_jobs))
                self._failed_jobs.pop(oldest_key, None)

    def _clear_failed(self, source_path: Path) -> None:
        key = self._path_key(source_path)
        with self._lock:
            self._failed_jobs.pop(key, None)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return

            self._accept_new_jobs = True
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="transcode-queue",
                daemon=True,
            )
            self._thread.start()

        if self._startup_backfill:
            self.enqueue_existing_recordings()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._accept_new_jobs = False
            self._stop_event.set()
            active_process = self._active_process

        with self._wait_condition:
            self._wait_condition.notify_all()

        if active_process is not None and active_process.poll() is None:
            try:
                active_process.terminate()
            except OSError:
                pass

        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

    def enqueue_file(self, source_file: str | Path, reason: str | None = None) -> bool:
        _ = reason  # reserved for future diagnostics

        source_path = Path(source_file)
        if not self._is_transcode_candidate(source_path):
            return False

        try:
            resolved_source = source_path.expanduser().resolve()
        except OSError:
            return False

        source_key = self._path_key(resolved_source)

        with self._lock:
            if not self._accept_new_jobs:
                return False

            if source_key in self._queued_keys or source_key == self._active_job_key:
                return False

            self._queue.append(resolved_source)
            self._queued_keys.add(source_key)

        with self._wait_condition:
            self._wait_condition.notify()

        return True

    def enqueue_existing_recordings(self) -> int:
        if not self._recordings_root.exists() or not self._recordings_root.is_dir():
            return 0

        queued_count = 0
        for source_path in sorted(self._recordings_root.rglob("*.ts"), key=lambda path: str(path).lower()):
            if self.enqueue_file(source_path, reason="backfill"):
                queued_count += 1

        return queued_count

    def handle_recording_event(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return

        if bool(event.get("skip_transcode_queue")):
            return

        event_name = str(event.get("event", "")).strip().lower()
        if event_name != "recording_stopped":
            return

        output_file = event.get("output_file")
        if not isinstance(output_file, str) or not output_file.strip():
            return

        self.enqueue_file(output_file, reason="recording-stopped")

    def _build_ffmpeg_commands(self, ffmpeg_binary: str, source_path: Path, output_path: Path) -> list[list[str]]:
        return [
            [
                ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                "-y",
                str(output_path),
            ],
            [
                ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source_path),
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
                "-f",
                "mp4",
                "-y",
                str(output_path),
            ],
        ]

    @staticmethod
    def _estimate_progress_percent(source_size_bytes: int, output_size_bytes: int) -> int:
        if source_size_bytes <= 0:
            return 1 if output_size_bytes > 0 else 0

        if output_size_bytes <= 0:
            return 0

        return max(1, min(99, int((output_size_bytes / source_size_bytes) * 100)))

    def _set_active_job_progress(self, job_key: str, source_size: int, output_size: int) -> None:
        progress = self._estimate_progress_percent(source_size, output_size)

        with self._lock:
            if self._active_job_key != job_key or self._active_job is None:
                return

            self._active_job["progress_percent"] = progress
            self._active_job["output_size_bytes"] = max(output_size, 0)

    def _run_ffmpeg_command(
        self,
        command: list[str],
        source_size_bytes: int,
        temp_output_path: Path,
        job_key: str,
    ) -> bool:
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError:
            return False

        with self._lock:
            self._active_process = process

        try:
            while True:
                if self._stop_event.is_set():
                    try:
                        process.terminate()
                    except OSError:
                        pass
                    return False

                return_code = process.poll()
                output_size = self._safe_file_size(temp_output_path)
                self._set_active_job_progress(job_key, source_size_bytes, output_size)

                if return_code is not None:
                    return return_code == 0

                self._stop_event.wait(0.8)
        finally:
            with self._lock:
                if self._active_process is process:
                    self._active_process = None

    def _process_source_file(self, source_path: Path) -> None:
        source_key = self._path_key(source_path)
        relative_path = self._relative_path(source_path)
        target_mp4 = source_path.with_suffix(".mp4")
        temp_output = target_mp4.with_name(f"{target_mp4.stem}.part{target_mp4.suffix}")

        source_size_bytes = self._safe_file_size(source_path)

        with self._lock:
            self._active_job = {
                "relative_path": relative_path,
                "file_name": source_path.name,
                "source_size_bytes": source_size_bytes,
                "output_size_bytes": 0,
                "progress_percent": 0,
                "started_at": self._utc_now_iso(),
            }
            self._active_job_key = source_key

        if self._is_ready_mp4(target_mp4):
            try:
                source_path.unlink()
            except OSError:
                pass
            self._clear_failed(source_path)
            with self._lock:
                if self._active_job is not None:
                    self._active_job["progress_percent"] = 100
            return

        ffmpeg_binary = self._resolve_ffmpeg_binary()
        if ffmpeg_binary is None:
            self._mark_failed(source_path, "ffmpeg_not_found")
            return

        commands = self._build_ffmpeg_commands(ffmpeg_binary, source_path, temp_output)
        transcode_succeeded = False

        for command in commands:
            try:
                if temp_output.exists():
                    temp_output.unlink()
            except OSError:
                pass

            if not self._run_ffmpeg_command(command, source_size_bytes, temp_output, source_key):
                continue

            if self._safe_file_size(temp_output) <= 0:
                continue

            try:
                target_mp4.parent.mkdir(parents=True, exist_ok=True)
                if target_mp4.exists():
                    target_mp4.unlink()
                temp_output.replace(target_mp4)
            except OSError:
                continue

            if self._safe_file_size(target_mp4) <= 0:
                continue

            try:
                source_path.unlink()
            except OSError:
                self._mark_failed(source_path, "transcode_complete_but_ts_cleanup_failed")
                return

            transcode_succeeded = True
            self._clear_failed(source_path)
            with self._lock:
                if self._active_job is not None:
                    self._active_job["progress_percent"] = 100
                    self._active_job["output_size_bytes"] = self._safe_file_size(target_mp4)
            break

        if not transcode_succeeded:
            self._mark_failed(source_path, "ffmpeg_transcode_failed")

        try:
            if temp_output.exists():
                temp_output.unlink()
        except OSError:
            pass

    def _dequeue_next(self) -> Path | None:
        with self._lock:
            if not self._queue:
                return None

            source_path = self._queue.popleft()
            source_key = self._path_key(source_path)
            self._queued_keys.discard(source_key)
            return source_path

    def _run(self) -> None:
        while not self._stop_event.is_set():
            next_source = self._dequeue_next()
            if next_source is None:
                with self._wait_condition:
                    self._wait_condition.wait(timeout=2)
                continue

            try:
                if self._is_transcode_candidate(next_source):
                    self._process_source_file(next_source)
            except Exception:
                self._mark_failed(next_source, "unexpected_worker_error")
            finally:
                with self._lock:
                    self._active_job = None
                    self._active_job_key = None

    def get_file_status(self, source_file: str | Path) -> dict[str, object]:
        source_path = Path(source_file)
        try:
            resolved_source = source_path.expanduser().resolve()
        except OSError:
            return {"state": "missing", "progress_percent": 0}

        target_mp4 = resolved_source.with_suffix(".mp4")
        if self._is_ready_mp4(target_mp4):
            return {
                "state": "ready",
                "progress_percent": 100,
                "output_size_bytes": self._safe_file_size(target_mp4),
            }

        source_key = self._path_key(resolved_source)

        with self._lock:
            if self._active_job_key == source_key and self._active_job is not None:
                return {
                    "state": "transcoding",
                    "progress_percent": int(self._active_job.get("progress_percent", 0) or 0),
                    "output_size_bytes": int(self._active_job.get("output_size_bytes", 0) or 0),
                }

            if source_key in self._queued_keys:
                return {
                    "state": "queued",
                    "progress_percent": 0,
                    "output_size_bytes": 0,
                }

            failed_entry = self._failed_jobs.get(source_key)
            if isinstance(failed_entry, dict):
                return {
                    "state": "failed",
                    "progress_percent": 0,
                    "output_size_bytes": 0,
                    "error": str(failed_entry.get("error", "transcode_failed")),
                }

        return {
            "state": "pending",
            "progress_percent": 0,
            "output_size_bytes": 0,
        }

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            running = bool(self._thread is not None and self._thread.is_alive())
            queued_count = len(self._queue)
            failed_count = len(self._failed_jobs)
            next_queued_relative_path = ""
            next_queued_file_name = ""
            if self._queue:
                next_queued_path = self._queue[0]
                next_queued_relative_path = self._relative_path(next_queued_path)
                next_queued_file_name = next_queued_path.name

            active_payload: dict[str, object] | None = None
            if self._active_job is not None:
                active_payload = {
                    "relative_path": str(self._active_job.get("relative_path", "")),
                    "file_name": str(self._active_job.get("file_name", "")),
                    "progress_percent": int(self._active_job.get("progress_percent", 0) or 0),
                    "started_at": str(self._active_job.get("started_at", "")),
                }

            last_failed: dict[str, object] | None = None
            if self._failed_jobs:
                last_failed = list(self._failed_jobs.values())[-1]

        indicator_text = ""
        is_working = active_payload is not None
        if active_payload is not None:
            file_name = str(active_payload.get("file_name", "")).strip()
            relative_path = str(active_payload.get("relative_path", "")).strip()
            progress_percent = int(active_payload.get("progress_percent", 0) or 0)
            display_name = file_name or relative_path or "recording"
            indicator_text = f"{display_name} ({progress_percent}%)"

        return {
            "running": running,
            "is_working": is_working,
            "queued_count": queued_count,
            "failed_count": failed_count,
            "active": active_payload,
            "next_queued": {
                "relative_path": next_queued_relative_path,
                "file_name": next_queued_file_name,
            },
            "last_failed": last_failed,
            "indicator_text": indicator_text,
        }
