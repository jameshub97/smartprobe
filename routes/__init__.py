"""Route blueprints for the simulation service.

Each module in this package is a Flask Blueprint owning one logical group of
routes. Register all blueprints onto the app via ``register_blueprints()``.

Usage in simulation_service.py (app-factory style)::

    from routes import register_blueprints
    register_blueprints(app)
"""

from .dashboard import dashboard_bp
from .preflight import preflight_bp
from .cleanup import cleanup_bp
from .simulation import simulation_bp
from .diagnostics import diagnostics_bp


def register_blueprints(app):
    """Register all route blueprints onto a Flask app instance."""
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(preflight_bp)
    app.register_blueprint(cleanup_bp)
    app.register_blueprint(simulation_bp)
    app.register_blueprint(diagnostics_bp)
