import json
import random
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .settings_store import SettingsStore


def _utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _utc_iso_from_ms(value_ms: int) -> str:
    return datetime.fromtimestamp(value_ms / 1000, tz=timezone.utc).isoformat()


def _normalize_oauth_token(raw_token: str) -> str:
    token = raw_token.strip()
    if not token:
        return ""

    if token.lower().startswith("oauth:"):
        return token

    return f"oauth:{token}"


def _decode_irc_tag_value(value: str) -> str:
    replacements = {
        r"\\s": " ",
        r"\\:": ";",
        r"\\r": "\r",
        r"\\n": "\n",
        r"\\\\": r"\\",
    }

    result = value
    for encoded, decoded in replacements.items():
        result = result.replace(encoded, decoded)

    return result


def _parse_irc_tags(raw_tags: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for item in raw_tags.split(";"):
        if not item:
            continue

        key, _, value = item.partition("=")
        if not key:
            continue

        tags[key] = _decode_irc_tag_value(value)

    return tags


@dataclass
class _ParsedChatMessage:
    channel: str
    author: str
    text: str
    utc_ms: int
    tags: dict[str, str]


class _AuthError(RuntimeError):
    pass


def _parse_privmsg_line(line: str) -> _ParsedChatMessage | None:
    remaining = line.strip()
    tags: dict[str, str] = {}

    if remaining.startswith("@"):
        tags_block, separator, after = remaining.partition(" ")
        if not separator:
            return None

        tags = _parse_irc_tags(tags_block[1:])
        remaining = after

    prefix_block = ""
    if remaining.startswith(":"):
        prefix_block, separator, after = remaining.partition(" ")
        if not separator:
            return None

        remaining = after

    if not remaining.startswith("PRIVMSG "):
        return None

    _, _, after_command = remaining.partition("PRIVMSG ")
    target_block, separator, text_block = after_command.partition(" :")
    if not separator:
        return None

    channel = target_block.strip().lstrip("#").lower()
    if not channel:
        return None

    author = tags.get("display-name", "").strip()
    if not author and prefix_block.startswith(":"):
        author = prefix_block[1:].split("!", 1)[0].strip()

    if not author:
        author = "unknown"

    raw_sent_ts = tags.get("tmi-sent-ts", "").strip()
    if raw_sent_ts.isdigit():
        utc_ms = int(raw_sent_ts)
    else:
        utc_ms = _utc_now_ms()

    return _ParsedChatMessage(
        channel=channel,
        author=author,
        text=text_block,
        utc_ms=utc_ms,
        tags=tags,
    )


class _ChannelChatWorker:
    def __init__(
        self,
        channel: str,
        output_file: Path,
        host: str,
        port: int,
        bot_username: str,
        bot_oauth_token: str,
        anonymous_prefix: str,
        connect_timeout_seconds: int,
        receive_timeout_seconds: int,
        reconnect_initial_seconds: int,
        reconnect_max_seconds: int,
    ) -> None:
        self.channel = channel
        self.output_file = output_file
        self.sidecar_path = output_file.with_suffix(".chat.ndjson")
        self._host = host
        self._port = max(1, int(port))
        self._bot_username = bot_username.strip().lower()
        self._bot_oauth_token = _normalize_oauth_token(bot_oauth_token)
        self._anonymous_prefix = anonymous_prefix.strip() or "justinfan"
        self._connect_timeout_seconds = max(3, int(connect_timeout_seconds))
        self._receive_timeout_seconds = max(5, int(receive_timeout_seconds))
        self._reconnect_initial_seconds = max(1, int(reconnect_initial_seconds))
        self._reconnect_max_seconds = max(self._reconnect_initial_seconds, int(reconnect_max_seconds))

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"chat-capture-{self.channel}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _build_connection_modes(self) -> list[str]:
        if self._bot_username and self._bot_oauth_token:
            return ["bot", "anonymous"]

        return ["anonymous"]

    def _run(self) -> None:
        reconnect_delay_seconds = self._reconnect_initial_seconds

        while not self._stop_event.is_set():
            connected = False
            modes = self._build_connection_modes()
            for mode in modes:
                if self._stop_event.is_set():
                    break

                try:
                    self._run_connection(mode)
                    connected = True
                    reconnect_delay_seconds = self._reconnect_initial_seconds
                    break
                except _AuthError:
                    continue
                except Exception:
                    continue

            if self._stop_event.is_set():
                break

            if connected:
                continue

            self._stop_event.wait(timeout=reconnect_delay_seconds)
            reconnect_delay_seconds = min(reconnect_delay_seconds * 2, self._reconnect_max_seconds)

    def _send_irc_command(self, sock: socket.socket, command: str) -> None:
        payload = f"{command}\r\n".encode("utf-8", errors="replace")
        sock.sendall(payload)

    def _handle_irc_line(self, line: str, writer: Any, sock: socket.socket) -> None:
        if line.startswith("PING"):
            ping_payload = line[5:].strip() if len(line) > 5 else ":tmi.twitch.tv"
            self._send_irc_command(sock, f"PONG {ping_payload}")
            return

        if "Login authentication failed" in line or "Improperly formatted auth" in line:
            raise _AuthError("Chat authentication failed")

        message = _parse_privmsg_line(line)
        if message is None or message.channel != self.channel:
            return

        payload = {
            "utc_ms": message.utc_ms,
            "utc_iso": _utc_iso_from_ms(message.utc_ms),
            "channel": message.channel,
            "author": message.author,
            "text": message.text,
            "tags": message.tags,
        }
        writer.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        writer.write("\n")
        writer.flush()

    def _run_connection(self, mode: str) -> None:
        nick = ""
        if mode == "bot":
            nick = self._bot_username
            if not nick or not self._bot_oauth_token:
                raise _AuthError("Missing bot credentials")
        else:
            nick = f"{self._anonymous_prefix}{random.randint(10000, 99999)}"

        with socket.create_connection(
            (self._host, self._port),
            timeout=self._connect_timeout_seconds,
        ) as sock:
            sock.settimeout(self._receive_timeout_seconds)

            if mode == "bot":
                self._send_irc_command(sock, f"PASS {self._bot_oauth_token}")

            self._send_irc_command(sock, f"NICK {nick}")
            self._send_irc_command(sock, "CAP REQ :twitch.tv/tags twitch.tv/commands")
            self._send_irc_command(sock, f"JOIN #{self.channel}")

            self.sidecar_path.parent.mkdir(parents=True, exist_ok=True)
            with self.sidecar_path.open("a", encoding="utf-8") as writer:
                buffer = b""
                while not self._stop_event.is_set():
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue

                    if not chunk:
                        raise ConnectionError("Chat socket closed")

                    buffer += chunk
                    while b"\r\n" in buffer:
                        raw_line, buffer = buffer.split(b"\r\n", 1)
                        line = raw_line.decode("utf-8", errors="replace")
                        self._handle_irc_line(line, writer, sock)


class TwitchChatCaptureService:
    def __init__(
        self,
        settings_store: SettingsStore,
        capture_enabled: bool,
        host: str,
        port: int,
        bot_username: str,
        bot_oauth_token: str,
        anonymous_prefix: str,
        connect_timeout_seconds: int,
        receive_timeout_seconds: int,
        reconnect_initial_seconds: int,
        reconnect_max_seconds: int,
    ) -> None:
        self._settings_store = settings_store
        self._capture_enabled = bool(capture_enabled)
        self._host = host
        self._port = int(port)
        self._bot_username = bot_username
        self._bot_oauth_token = bot_oauth_token
        self._anonymous_prefix = anonymous_prefix
        self._connect_timeout_seconds = int(connect_timeout_seconds)
        self._receive_timeout_seconds = int(receive_timeout_seconds)
        self._reconnect_initial_seconds = int(reconnect_initial_seconds)
        self._reconnect_max_seconds = int(reconnect_max_seconds)

        self._workers: dict[str, _ChannelChatWorker] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _normalize_channel(channel: str) -> str:
        return channel.strip().lower().lstrip("@")

    def _chat_enabled_for_channel(self, channel: str) -> bool:
        settings = self._settings_store.get_settings()
        saved_channels = settings.get("saved_channels", [])
        if not isinstance(saved_channels, list):
            return True

        normalized = self._normalize_channel(channel)
        for item in saved_channels:
            if not isinstance(item, dict):
                continue

            if self._normalize_channel(str(item.get("name", ""))) == normalized:
                return bool(item.get("chat_record", True))

        return True

    def _new_worker(self, channel: str, output_file: str) -> _ChannelChatWorker:
        return _ChannelChatWorker(
            channel=channel,
            output_file=Path(output_file),
            host=self._host,
            port=self._port,
            bot_username=self._bot_username,
            bot_oauth_token=self._bot_oauth_token,
            anonymous_prefix=self._anonymous_prefix,
            connect_timeout_seconds=self._connect_timeout_seconds,
            receive_timeout_seconds=self._receive_timeout_seconds,
            reconnect_initial_seconds=self._reconnect_initial_seconds,
            reconnect_max_seconds=self._reconnect_max_seconds,
        )

    def handle_recording_event(self, event: dict[str, Any]) -> None:
        if not self._capture_enabled:
            return

        event_name = str(event.get("event", "")).strip().lower()
        channel = self._normalize_channel(str(event.get("channel", "")))
        if not channel:
            return

        if event_name == "recording_started":
            if not self._chat_enabled_for_channel(channel):
                return

            output_file = str(event.get("output_file", "")).strip()
            if not output_file:
                return

            replacement_worker = self._new_worker(channel, output_file)
            existing_worker: _ChannelChatWorker | None = None
            with self._lock:
                existing_worker = self._workers.pop(channel, None)
                self._workers[channel] = replacement_worker

            if existing_worker is not None:
                existing_worker.stop()

            replacement_worker.start()
            return

        if event_name == "recording_stopped":
            worker: _ChannelChatWorker | None = None
            with self._lock:
                worker = self._workers.pop(channel, None)

            if worker is not None:
                worker.stop()

    def stop_all(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()

        for worker in workers:
            worker.stop()
