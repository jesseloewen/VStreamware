from collections.abc import Mapping
from typing import Any, Callable

from flask import Flask

from .notification_dispatcher import NotificationDispatcher
from .auto_recorder import AutoRecorder
from .pushover_notifier import PushoverNotifier
from .recording_manager import RecordingManager
from .settings_store import SettingsStore
from .live_incremental_dvr import LiveIncrementalDvrService
from .transcode_queue import TranscodeQueueService
from .twitch_chat_capture import TwitchChatCaptureService

SERVICES_EXTENSION_KEY = "vstreamware_services"


def _build_recording_event_callback(
	notification_dispatcher: NotificationDispatcher,
	chat_capture_service: TwitchChatCaptureService,
	live_dvr_provider: Callable[[], LiveIncrementalDvrService | None],
	transcode_queue_provider: Callable[[], TranscodeQueueService | None],
) -> Any:
	def handle_event(event: dict[str, Any]) -> None:
		notification_dispatcher.handle_event(event)
		chat_capture_service.handle_recording_event(event)
		live_dvr_service = live_dvr_provider()
		if live_dvr_service is not None:
			live_dvr_service.handle_recording_event(event)
		transcode_queue = transcode_queue_provider()
		if transcode_queue is not None:
			transcode_queue.handle_recording_event(event)

	return handle_event


def init_services(app: Flask) -> None:
	settings_store = SettingsStore(
		settings_file=app.config["STREAM_SETTINGS_FILE"],
	)

	pushover_notifier = PushoverNotifier(
		app_token=app.config["PUSHOVER_APP_TOKEN"],
		user_key=app.config["PUSHOVER_USER_KEY"],
		api_url=app.config["PUSHOVER_API_URL"],
		timeout_seconds=app.config["PUSHOVER_TIMEOUT_SECONDS"],
	)

	notification_dispatcher = NotificationDispatcher(
		settings_store=settings_store,
		notifier=pushover_notifier,
	)

	chat_capture_service = TwitchChatCaptureService(
		settings_store=settings_store,
		capture_enabled=app.config["TWITCH_CHAT_CAPTURE_ENABLED"],
		host=app.config["TWITCH_CHAT_HOST"],
		port=app.config["TWITCH_CHAT_PORT"],
		bot_username=app.config["TWITCH_CHAT_BOT_USERNAME"],
		bot_oauth_token=app.config["TWITCH_CHAT_BOT_OAUTH_TOKEN"],
		anonymous_prefix=app.config["TWITCH_CHAT_ANON_PREFIX"],
		connect_timeout_seconds=app.config["TWITCH_CHAT_CONNECT_TIMEOUT_SECONDS"],
		receive_timeout_seconds=app.config["TWITCH_CHAT_RECEIVE_TIMEOUT_SECONDS"],
		reconnect_initial_seconds=app.config["TWITCH_CHAT_RECONNECT_INITIAL_SECONDS"],
		reconnect_max_seconds=app.config["TWITCH_CHAT_RECONNECT_MAX_SECONDS"],
	)

	live_dvr_service: LiveIncrementalDvrService | None = None
	transcode_queue_service: TranscodeQueueService | None = None

	def _get_live_dvr_service() -> LiveIncrementalDvrService | None:
		return live_dvr_service

	def _get_transcode_queue() -> TranscodeQueueService | None:
		return transcode_queue_service

	recording_event_callback = _build_recording_event_callback(
		notification_dispatcher=notification_dispatcher,
		chat_capture_service=chat_capture_service,
		live_dvr_provider=_get_live_dvr_service,
		transcode_queue_provider=_get_transcode_queue,
	)

	recording_manager = RecordingManager(
		streamlink_command=app.config["STREAMLINK_COMMAND"],
		default_quality=app.config["STREAM_DEFAULT_QUALITY"],
		default_output_path=app.config["RECORDINGS_DIR"],
		event_callback=recording_event_callback,
	)

	live_dvr_service = LiveIncrementalDvrService(
		recordings_dir=app.config["RECORDINGS_DIR"],
		ffmpeg_command=app.config["FFMPEG_COMMAND"],
		recording_manager=recording_manager,
		enabled=app.config["LIVE_INCREMENTAL_DVR_ENABLED"],
		chunk_seconds=app.config["LIVE_INCREMENTAL_DVR_CHUNK_SECONDS"],
		poll_seconds=app.config["LIVE_INCREMENTAL_DVR_POLL_SECONDS"],
		safety_seconds=app.config["LIVE_INCREMENTAL_DVR_SAFETY_SECONDS"],
		keep_chunks=app.config["LIVE_INCREMENTAL_DVR_KEEP_CHUNKS"],
	)
	live_dvr_service.start()

	transcode_queue_service = TranscodeQueueService(
		recordings_dir=app.config["RECORDINGS_DIR"],
		ffmpeg_command=app.config["FFMPEG_COMMAND"],
		recording_manager=recording_manager,
		startup_backfill=True,
	)
	transcode_queue_service.start()

	auto_recorder = AutoRecorder(
		settings_store=settings_store,
		recording_manager=recording_manager,
		poll_seconds=app.config["AUTO_RECORD_POLL_SECONDS"],
		stream_event_callback=notification_dispatcher.handle_event,
	)
	auto_recorder.start()

	app.extensions[SERVICES_EXTENSION_KEY] = {
		"settings_store": settings_store,
		"recording_manager": recording_manager,
		"live_incremental_dvr": live_dvr_service,
		"transcode_queue": transcode_queue_service,
		"auto_recorder": auto_recorder,
		"notification_dispatcher": notification_dispatcher,
		"chat_capture_service": chat_capture_service,
	}


def get_services(app: Flask) -> Mapping[str, Any]:
	services = app.extensions.get(SERVICES_EXTENSION_KEY)
	if services is None:
		raise RuntimeError("Services are not initialized.")

	if not isinstance(services, Mapping):
		raise RuntimeError("Services extension has invalid state.")

	return services


def shutdown_services(app: Flask) -> None:
	services = app.extensions.get(SERVICES_EXTENSION_KEY)
	if not isinstance(services, Mapping):
		return

	auto_recorder = services.get("auto_recorder")
	recording_manager = services.get("recording_manager")
	live_dvr_service = services.get("live_incremental_dvr")
	transcode_queue = services.get("transcode_queue")
	chat_capture_service = services.get("chat_capture_service")

	if isinstance(auto_recorder, AutoRecorder):
		auto_recorder.stop()

	if isinstance(live_dvr_service, LiveIncrementalDvrService):
		live_dvr_service.stop()

	if isinstance(transcode_queue, TranscodeQueueService):
		transcode_queue.stop()

	if isinstance(recording_manager, RecordingManager):
		recording_manager.stop_all()

	if isinstance(chat_capture_service, TwitchChatCaptureService):
		chat_capture_service.stop_all()
