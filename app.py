import os

from dotenv import load_dotenv

load_dotenv()

from app import create_app
from app.services import shutdown_services

app = create_app()


if __name__ == "__main__":
    host = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_RUN_PORT", "8523"))
    debug = os.getenv("FLASK_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}
    use_reloader = os.getenv("FLASK_USE_RELOADER", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    try:
        app.run(host=host, port=port, debug=debug, use_reloader=use_reloader)
    finally:
        shutdown_services(app)
