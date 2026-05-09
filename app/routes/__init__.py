from flask import Flask

from .dashboard import dashboard_bp
from .health import health_bp



def register_blueprints(app: Flask) -> None:
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(health_bp)
