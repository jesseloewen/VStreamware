from flask import Blueprint

health_bp = Blueprint("health", __name__)


@health_bp.get("/health")
def health() -> tuple[dict[str, str], int]:
    return {"status": "ok"}, 200
