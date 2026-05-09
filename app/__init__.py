from flask import Flask, redirect, render_template, url_for

from .config import Config
from .routes import register_blueprints
from .services import init_services
from .services import get_services
from .services.dashboard_state import build_saved_channels_status



def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    init_services(app)
    register_blueprints(app)

    @app.context_processor
    def inject_saved_channels() -> dict[str, object]:
        try:
            services = get_services(app)
            settings_store = services["settings_store"]
            recording_manager = services["recording_manager"]
            auto_recorder = services["auto_recorder"]
            settings = settings_store.get_settings()
            saved_channels = settings.get("saved_channels", [])
            display_timezone = str(settings.get("display_timezone", "auto"))

            if not isinstance(saved_channels, list):
                saved_channels = []

            saved_channels_status = build_saved_channels_status(
                saved_channels=saved_channels,
                recording_manager=recording_manager,
                auto_recorder=auto_recorder,
            )
        except (KeyError, RuntimeError, AttributeError, TypeError):
            saved_channels_status = []
            display_timezone = "auto"

        return {
            "saved_channels": saved_channels_status,
            "display_timezone": display_timezone,
        }

    @app.get("/favicon.ico")
    def favicon() -> object:
        return redirect(url_for("static", filename="icons/favicon.ico"), code=302)

    @app.get("/apple-touch-icon.png")
    def apple_touch_icon() -> object:
        return redirect(url_for("static", filename="icons/apple-touch-icon.png"), code=302)

    @app.get("/site.webmanifest")
    def webmanifest() -> object:
        return redirect(url_for("static", filename="site.webmanifest"), code=302)

    @app.errorhandler(404)
    def page_not_found(_error: object) -> tuple[str, int]:
        return render_template("404.html"), 404

    return app
