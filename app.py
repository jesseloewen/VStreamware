from dotenv import load_dotenv

load_dotenv()

from app import create_app
from app.services import shutdown_services

app = create_app()


if __name__ == "__main__":
    host = str(app.config["FLASK_RUN_HOST"])
    port = int(app.config["FLASK_RUN_PORT"])
    debug = bool(app.config["FLASK_DEBUG"])
    use_reloader = bool(app.config["FLASK_USE_RELOADER"])

    try:
        app.run(host=host, port=port, debug=debug, use_reloader=use_reloader)
    finally:
        shutdown_services(app)
