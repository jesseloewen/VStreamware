import json
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import imageio_ffmpeg  # type: ignore[import-not-found]
except Exception:
    imageio_ffmpeg = None


class LiveIncrementalDvrService:
    """Builds a seekable MP4 progressively while a TS recording is still active.

    The service transcodes stable TS windows into chunk MP4 files every few minutes,
    then remuxes those chunks into the target recording-folder MP4. When recording
    stops, it flushes the remaining tail and finalizes immediately.
    """

    def __init__(
        self,
        recordings_dir: str,
        ffmpeg_command: str,
        recording_manager: Any,
        enabled: bool = True,
        chunk_seconds: int = 300,
        poll_seconds: int = 12,
        safety_seconds: int = 20,
        keep_chunks: bool = False,
    ) -> None:
        self._recordings_root = Path(recordings_dir).expanduser().resolve()
        self._ffmpeg_command = (ffmpeg_command or "ffmpeg").strip() or "ffmpeg"
        self._recording_manager = recording_manager
        self._enabled = bool(enabled)
        self._chunk_seconds = max(30, int(chunk_seconds or 300))
        self._poll_seconds = max(2, int(poll_seconds or 12))
        self._safety_seconds = max(0, int(safety_seconds or 20))
        self._keep_chunks = bool(keep_chunks)

        self._jobs: dict[str, dict[str, object]] = {}
        self._active_job_key: str | None = None
        self._stop_event = threading.Event()
        self._wait_condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _path_key(self, path: Path) -> str:
        try:
            return str(path.resolve())
        except OSError:
            return str(path)

    @staticmethod
    def _safe_size(path: Path) -> int:
        try:
            if path.exists() and path.is_file():
                return int(path.stat().st_size)
        except OSError:
            return 0

        return 0

    @staticmethod
    def _parse_duration(value: str) -> float:
        raw = (value or "").strip()
        if not raw:
            return 0.0

        try:
            parsed = float(raw)
        except ValueError:
            return 0.0

        if parsed <= 0:
            return 0.0

        return parsed

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

    @staticmethod
    def _candidate_ffprobe_paths(ffmpeg_binary: str) -> list[str]:
        ffmpeg_path = Path(ffmpeg_binary)
        stem = ffmpeg_path.stem.lower()
        candidates: list[str] = []

        if ffmpeg_path.exists():
            if stem == "ffmpeg":
                candidates.append(str(ffmpeg_path.with_name("ffprobe" + ffmpeg_path.suffix)))
            candidates.append(str(ffmpeg_path.parent / "ffprobe"))
            candidates.append(str(ffmpeg_path.parent / "ffprobe.exe"))

        candidates.append("ffprobe")
        return candidates

    def _resolve_ffprobe_binary(self) -> str | None:
        ffmpeg_binary = self._resolve_ffmpeg_binary()
        if ffmpeg_binary is None:
            return None

        for candidate in self._candidate_ffprobe_paths(ffmpeg_binary):
            path = Path(candidate)
            if path.exists():
                return str(path)

            if shutil.which(candidate) is not None:
                return candidate

        return None

    def _probe_duration_seconds(self, source_path: Path) -> float:
        ffprobe_binary = self._resolve_ffprobe_binary()
        if ffprobe_binary is None:
            return 0.0

        command = [
            ffprobe_binary,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(source_path),
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
            return 0.0

        if result.returncode != 0:
            return 0.0

        return self._parse_duration(result.stdout)

    def _build_chunk_command_candidates(
        self,
        ffmpeg_binary: str,
        source_path: Path,
        output_path: Path,
        start_seconds: float,
        duration_seconds: float,
    ) -> list[list[str]]:
        start_arg = f"{max(start_seconds, 0.0):.3f}"
        duration_arg = f"{max(duration_seconds, 0.001):.3f}"

        return [
            [
                ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                start_arg,
                "-t",
                duration_arg,
                "-i",
                str(source_path),
                "-c",
                "copy",
                "-movflags",
                "+frag_keyframe+empty_moov+default_base_moof",
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
                "-ss",
                start_arg,
                "-t",
                duration_arg,
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
                "+frag_keyframe+empty_moov+default_base_moof",
                "-f",
                "mp4",
                "-y",
                str(output_path),
            ],
        ]

    def _run_command(self, command: list[str], timeout_seconds: int = 1200) -> bool:
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False

        return result.returncode == 0

    def _relative_source(self, source_path: Path) -> str:
        try:
            return source_path.resolve().relative_to(self._recordings_root).as_posix()
        except (OSError, ValueError):
            return source_path.name

    def _state_file_path(self, source_path: Path) -> Path:
        return source_path.with_name(f"{source_path.stem}.live_dvr_state.json")

    def _chunks_dir_path(self, source_path: Path) -> Path:
        return source_path.with_name(f".{source_path.stem}.live_dvr_chunks")

    def _job_from_source(self, source_path: Path) -> dict[str, object]:
        target_mp4 = source_path.with_suffix(".mp4")
        chunks_dir = self._chunks_dir_path(source_path)
        state_file = self._state_file_path(source_path)

        return {
            "source_path": source_path,
            "target_mp4": target_mp4,
            "chunks_dir": chunks_dir,
            "state_file": state_file,
            "relative_path": self._relative_source(source_path),
            "file_name": source_path.name,
            "started_at": self._utc_now_iso(),
            "updated_at": self._utc_now_iso(),
            "next_start_seconds": 0.0,
            "chunk_index": 0,
            "chunks": [],
            "is_active": True,
            "is_processing": False,
            "is_finalizing": False,
            "last_error": "",
            "finalized_at": "",
        }

    def _write_job_state(self, job: dict[str, object]) -> None:
        source_path = job.get("source_path")
        state_file = job.get("state_file")
        target_mp4 = job.get("target_mp4")
        chunks_dir = job.get("chunks_dir")
        if not isinstance(source_path, Path):
            return
        if not isinstance(state_file, Path):
            return
        if not isinstance(target_mp4, Path):
            return
        if not isinstance(chunks_dir, Path):
            return

        payload = {
            "source_path": str(source_path),
            "target_mp4": str(target_mp4),
            "chunks_dir": str(chunks_dir),
            "relative_path": str(job.get("relative_path", "")),
            "started_at": str(job.get("started_at", "")),
            "updated_at": str(job.get("updated_at", "")),
            "next_start_seconds": float(job.get("next_start_seconds", 0.0) or 0.0),
            "chunk_index": int(job.get("chunk_index", 0) or 0),
            "chunks": job.get("chunks", []),
            "is_active": bool(job.get("is_active", False)),
            "is_finalizing": bool(job.get("is_finalizing", False)),
            "last_error": str(job.get("last_error", "")),
            "finalized_at": str(job.get("finalized_at", "")),
        }

        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _register_source(self, source_path: Path) -> None:
        if source_path.suffix.lower() != ".ts":
            return

        try:
            resolved = source_path.expanduser().resolve()
        except OSError:
            return

        if not resolved.exists() or not resolved.is_file():
            return

        source_key = self._path_key(resolved)

        with self._lock:
            job = self._jobs.get(source_key)
            if isinstance(job, dict):
                job["is_active"] = True
                job["updated_at"] = self._utc_now_iso()
                return

            new_job = self._job_from_source(resolved)
            self._jobs[source_key] = new_job
            self._write_job_state(new_job)

        with self._wait_condition:
            self._wait_condition.notify_all()

    def _is_recording_active(self, source_path: Path) -> bool:
        try:
            source_key = self._path_key(source_path.resolve())
        except OSError:
            source_key = self._path_key(source_path)

        try:
            active = self._recording_manager.get_active_recordings()
        except Exception:
            return False

        if not isinstance(active, list):
            return False

        for item in active:
            if not isinstance(item, dict):
                continue

            output_file = item.get("output_file")
            if not isinstance(output_file, str) or not output_file.strip():
                continue

            try:
                active_key = self._path_key(Path(output_file).expanduser().resolve())
            except OSError:
                continue

            if active_key == source_key:
                return True

        return False

    def _collect_active_sources(self) -> list[Path]:
        try:
            active = self._recording_manager.get_active_recordings()
        except Exception:
            return []

        if not isinstance(active, list):
            return []

        sources: list[Path] = []
        for item in active:
            if not isinstance(item, dict):
                continue

            output_file = item.get("output_file")
            if not isinstance(output_file, str) or not output_file.strip():
                continue

            try:
                source_path = Path(output_file).expanduser().resolve()
            except OSError:
                continue

            if source_path.suffix.lower() != ".ts":
                continue

            sources.append(source_path)

        return sources

    def start(self) -> None:
        if not self._enabled:
            return

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return

            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="live-incremental-dvr",
                daemon=True,
            )
            self._thread.start()

        for source_path in self._collect_active_sources():
            self._register_source(source_path)

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._stop_event.set()

        with self._wait_condition:
            self._wait_condition.notify_all()

        if thread is not None and thread.is_alive():
            thread.join(timeout=6)

    def handle_recording_event(self, event: dict[str, Any]) -> None:
        if not self._enabled:
            return

        if not isinstance(event, dict):
            return

        event_name = str(event.get("event", "")).strip().lower()
        output_file = event.get("output_file")
        if not isinstance(output_file, str) or not output_file.strip():
            return

        try:
            source_path = Path(output_file).expanduser().resolve()
        except OSError:
            return

        if event_name == "recording_started":
            self._register_source(source_path)
            return

        if event_name != "recording_stopped":
            return

        finalized = self.finalize_recording(source_path)
        if finalized:
            event["skip_transcode_queue"] = True
            event["mp4_ready"] = True

    def _render_chunk(self, job_key: str, job: dict[str, object], flush: bool) -> bool:
        source_path = job.get("source_path")
        target_mp4 = job.get("target_mp4")
        chunks_dir = job.get("chunks_dir")
        if not isinstance(source_path, Path):
            return False
        if not isinstance(target_mp4, Path):
            return False
        if not isinstance(chunks_dir, Path):
            return False

        if not source_path.exists() or not source_path.is_file():
            return False

        ffmpeg_binary = self._resolve_ffmpeg_binary()
        if ffmpeg_binary is None:
            with self._lock:
                current = self._jobs.get(job_key)
                if isinstance(current, dict):
                    current["last_error"] = "ffmpeg_not_found"
                    current["updated_at"] = self._utc_now_iso()
            return False

        duration_seconds = self._probe_duration_seconds(source_path)
        if duration_seconds <= 0:
            return False

        with self._lock:
            current = self._jobs.get(job_key)
            if not isinstance(current, dict):
                return False
            next_start = float(current.get("next_start_seconds", 0.0) or 0.0)
            chunk_index = int(current.get("chunk_index", 0) or 0)

        safety = 0.0 if flush else float(self._safety_seconds)
        safe_end = max(0.0, duration_seconds - safety)
        remaining = safe_end - next_start
        if remaining < 2.0:
            return False

        chunk_duration = min(float(self._chunk_seconds), remaining)
        chunk_index += 1

        try:
            chunks_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False

        chunk_file = chunks_dir / f"chunk_{chunk_index:06d}.mp4"

        commands = self._build_chunk_command_candidates(
            ffmpeg_binary,
            source_path,
            chunk_file,
            start_seconds=next_start,
            duration_seconds=chunk_duration,
        )

        rendered = False
        for command in commands:
            try:
                if chunk_file.exists():
                    chunk_file.unlink()
            except OSError:
                pass

            if not self._run_command(command):
                continue

            if self._safe_size(chunk_file) <= 0:
                continue

            rendered = True
            break

        if not rendered:
            with self._lock:
                current = self._jobs.get(job_key)
                if isinstance(current, dict):
                    current["last_error"] = "chunk_render_failed"
                    current["updated_at"] = self._utc_now_iso()
            return False

        append_ok = self._append_chunk_to_target(ffmpeg_binary, target_mp4, chunk_file)
        if not append_ok:
            with self._lock:
                current = self._jobs.get(job_key)
                if isinstance(current, dict):
                    current["last_error"] = "chunk_append_failed"
                    current["updated_at"] = self._utc_now_iso()
            return False

        chunk_meta = {
            "index": chunk_index,
            "path": str(chunk_file),
            "duration_seconds": round(chunk_duration, 3),
            "start_seconds": round(next_start, 3),
        }

        with self._lock:
            current = self._jobs.get(job_key)
            if not isinstance(current, dict):
                return True

            chunks = current.get("chunks")
            if not isinstance(chunks, list):
                chunks = []
                current["chunks"] = chunks
            chunks.append(chunk_meta)
            current["chunk_index"] = chunk_index
            current["next_start_seconds"] = next_start + chunk_duration
            current["last_error"] = ""
            current["updated_at"] = self._utc_now_iso()
            self._write_job_state(current)

        return True

    def _append_chunk_to_target(self, ffmpeg_binary: str, target_mp4: Path, chunk_file: Path) -> bool:
        target_mp4.parent.mkdir(parents=True, exist_ok=True)

        def _concat_escape(path: Path) -> str:
            return str(path).replace("'", "'\\''")

        if self._safe_size(target_mp4) <= 0:
            temp_target = target_mp4.with_name(f"{target_mp4.stem}.part{target_mp4.suffix}")
            command = [
                ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(chunk_file),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                "-y",
                str(temp_target),
            ]
            if not self._run_command(command):
                return False
            if self._safe_size(temp_target) <= 0:
                return False
            try:
                if target_mp4.exists():
                    target_mp4.unlink()
                temp_target.replace(target_mp4)
            except OSError:
                return False
            return self._safe_size(target_mp4) > 0

        concat_list = target_mp4.with_name(f".{target_mp4.stem}.concat.txt")
        temp_target = target_mp4.with_name(f"{target_mp4.stem}.part{target_mp4.suffix}")

        try:
            concat_list.write_text(
                "\n".join(
                    [
                        f"file '{_concat_escape(target_mp4)}'",
                        f"file '{_concat_escape(chunk_file)}'",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            return False

        try:
            command = [
                ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                "-y",
                str(temp_target),
            ]
            if not self._run_command(command):
                return False
            if self._safe_size(temp_target) <= 0:
                return False
            temp_target.replace(target_mp4)
            return self._safe_size(target_mp4) > 0
        except OSError:
            return False
        finally:
            try:
                if concat_list.exists():
                    concat_list.unlink()
            except OSError:
                pass
            try:
                if temp_target.exists():
                    temp_target.unlink()
            except OSError:
                pass

    def _process_job(self, job_key: str, flush: bool = False) -> None:
        with self._lock:
            job = self._jobs.get(job_key)
            if not isinstance(job, dict):
                return
            if bool(job.get("is_processing")):
                return
            job["is_processing"] = True
            job["is_finalizing"] = bool(flush)
            job["updated_at"] = self._utc_now_iso()

        try:
            processed_any = True
            while processed_any and not self._stop_event.is_set():
                processed_any = self._render_chunk(job_key, job, flush=flush)
                if not flush:
                    break
        finally:
            with self._lock:
                current = self._jobs.get(job_key)
                if isinstance(current, dict):
                    current["is_processing"] = False
                    current["is_finalizing"] = False
                    current["updated_at"] = self._utc_now_iso()
                    self._write_job_state(current)

    def _cleanup_chunks_if_needed(self, job: dict[str, object]) -> None:
        if self._keep_chunks:
            return

        chunks_dir = job.get("chunks_dir")
        state_file = job.get("state_file")
        if isinstance(chunks_dir, Path):
            try:
                if chunks_dir.exists() and chunks_dir.is_dir():
                    shutil.rmtree(chunks_dir, ignore_errors=True)
            except OSError:
                pass

        if isinstance(state_file, Path):
            try:
                if state_file.exists():
                    state_file.unlink()
            except OSError:
                pass

    def finalize_recording(self, source_file: str | Path) -> bool:
        if not self._enabled:
            return False

        source_path = Path(source_file)
        try:
            resolved = source_path.expanduser().resolve()
        except OSError:
            return False

        source_key = self._path_key(resolved)
        self._register_source(resolved)
        self._process_job(source_key, flush=True)

        with self._lock:
            job = self._jobs.get(source_key)
            if not isinstance(job, dict):
                return False

            target_mp4 = job.get("target_mp4")
            if not isinstance(target_mp4, Path):
                return False

            mp4_ready = self._safe_size(target_mp4) > 0
            if not mp4_ready:
                job["last_error"] = "finalize_no_mp4"
                job["updated_at"] = self._utc_now_iso()
                self._write_job_state(job)
                return False

            job["is_active"] = False
            job["finalized_at"] = self._utc_now_iso()
            job["updated_at"] = self._utc_now_iso()
            self._write_job_state(job)

        try:
            if resolved.exists() and resolved.is_file():
                resolved.unlink()
        except OSError:
            return False

        with self._lock:
            job = self._jobs.get(source_key)
            if isinstance(job, dict):
                self._cleanup_chunks_if_needed(job)

        return True

    def get_dvr_snapshot_path(self, source_file: str | Path) -> Path | None:
        source_path = Path(source_file)
        try:
            resolved = source_path.expanduser().resolve()
        except OSError:
            return None

        source_key = self._path_key(resolved)
        with self._lock:
            job = self._jobs.get(source_key)
            if isinstance(job, dict):
                target_mp4 = job.get("target_mp4")
                if isinstance(target_mp4, Path) and self._safe_size(target_mp4) > 0:
                    return target_mp4

        fallback_path = resolved.with_suffix(".mp4")
        if self._safe_size(fallback_path) > 0:
            return fallback_path

        return None

    def get_live_status(self, source_file: str | Path) -> dict[str, object]:
        source_path = Path(source_file)
        try:
            resolved = source_path.expanduser().resolve()
        except OSError:
            return {
                "state": "missing",
                "available": False,
                "progress_percent": 0,
                "source_size_bytes": 0,
                "cached_size_bytes": 0,
            }

        source_key = self._path_key(resolved)
        source_size = self._safe_size(resolved)

        with self._lock:
            job = self._jobs.get(source_key)
            if not isinstance(job, dict):
                fallback_mp4 = resolved.with_suffix(".mp4")
                fallback_size = self._safe_size(fallback_mp4)
                if fallback_size > 0:
                    return {
                        "state": "ready",
                        "available": True,
                        "progress_percent": 100,
                        "source_size_bytes": source_size,
                        "cached_size_bytes": fallback_size,
                    }
                return {
                    "state": "pending",
                    "available": False,
                    "progress_percent": 0,
                    "source_size_bytes": source_size,
                    "cached_size_bytes": 0,
                }

            target_mp4 = job.get("target_mp4")
            if not isinstance(target_mp4, Path):
                return {
                    "state": "pending",
                    "available": False,
                    "progress_percent": 0,
                    "source_size_bytes": source_size,
                    "cached_size_bytes": 0,
                }

            cached_size = self._safe_size(target_mp4)
            progress = 0
            if source_size > 0 and cached_size > 0:
                progress = max(1, min(99, int((cached_size / source_size) * 100)))
            elif cached_size > 0:
                progress = 1

            is_finalizing = bool(job.get("is_finalizing"))
            is_processing = bool(job.get("is_processing"))
            is_active = bool(job.get("is_active"))
            last_error = str(job.get("last_error", "")).strip()

            if cached_size > 0 and not is_processing and not is_finalizing and not is_active:
                state = "ready"
                progress = 100
            elif cached_size > 0 and (is_processing or is_finalizing):
                state = "catching_up"
            elif cached_size > 0:
                state = "ready"
                progress = max(progress, 100 if not self._is_recording_active(resolved) else progress)
            elif is_finalizing:
                state = "finalizing_tail"
            elif is_processing:
                state = "catching_up"
            elif last_error:
                state = "failed"
            else:
                state = "queued"

            return {
                "state": state,
                "available": cached_size > 0,
                "progress_percent": progress,
                "source_size_bytes": source_size,
                "cached_size_bytes": cached_size,
                "error": last_error or None,
            }

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            running = bool(self._thread is not None and self._thread.is_alive())
            active_key = self._active_job_key
            active_job = self._jobs.get(active_key or "") if active_key else None

            if not isinstance(active_job, dict):
                for candidate_key, candidate in self._jobs.items():
                    if not isinstance(candidate, dict):
                        continue
                    if bool(candidate.get("is_processing")) or bool(candidate.get("is_finalizing")):
                        active_job = candidate
                        self._active_job_key = candidate_key
                        break

            if not isinstance(active_job, dict):
                return {
                    "running": running,
                    "is_working": False,
                    "active": None,
                    "indicator_text": "",
                }

            source_path = active_job.get("source_path")
            target_mp4 = active_job.get("target_mp4")
            if not isinstance(source_path, Path) or not isinstance(target_mp4, Path):
                return {
                    "running": running,
                    "is_working": False,
                    "active": None,
                    "indicator_text": "",
                }

            source_size = self._safe_size(source_path)
            cached_size = self._safe_size(target_mp4)
            progress = 0
            if source_size > 0 and cached_size > 0:
                progress = max(1, min(99, int((cached_size / source_size) * 100)))
            elif cached_size > 0:
                progress = 1

            display_name = str(active_job.get("file_name", "recording")).strip() or "recording"

            return {
                "running": running,
                "is_working": True,
                "active": {
                    "relative_path": str(active_job.get("relative_path", "")),
                    "file_name": display_name,
                    "progress_percent": progress,
                    "started_at": str(active_job.get("started_at", "")),
                },
                "indicator_text": f"{display_name} ({progress}%)",
            }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            for source_path in self._collect_active_sources():
                self._register_source(source_path)

            with self._lock:
                pending_keys = [
                    key
                    for key, job in self._jobs.items()
                    if isinstance(job, dict)
                    and bool(job.get("is_active"))
                    and not bool(job.get("is_processing"))
                    and not bool(job.get("is_finalizing"))
                ]

            for job_key in pending_keys:
                if self._stop_event.is_set():
                    break
                self._active_job_key = job_key
                self._process_job(job_key, flush=False)

            self._active_job_key = None

            with self._wait_condition:
                self._wait_condition.wait(timeout=self._poll_seconds)
