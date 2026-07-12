"""Flask app factory. Starts the background threads (serial reader, plug
poller, auto-lighting job), restores any pending scene wake timer, and serves
both the JSON API and the static dashboard."""

import logging
from pathlib import Path

from flask import Flask

from . import config, db, health, lighting, planner, poller, scenes, serial_reader
from .api import api

DASHBOARD_DIR = Path(__file__).resolve().parent.parent.parent / "dashboard"


def create_app() -> Flask:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    )
    log = logging.getLogger("app")

    db.init_db()
    planner.init_db()  # planner owns its own tables (see app/planner.py)
    health.init_db()   # health too (see app/health.py)
    log.info("Database ready at %s", config.DB_PATH)
    if config.MOCK_HARDWARE:
        log.warning("Running with MOCK_HARDWARE=1 — all hardware is simulated")

    serial_reader.start()
    poller.start()
    lighting.start()
    # After the device clients exist: re-arm (or fire, if overdue) a pending
    # Sleeping->Day wake persisted before the last shutdown.
    scenes.init()

    app = Flask(
        __name__,
        static_folder=str(DASHBOARD_DIR),
        static_url_path="",
    )
    app.register_blueprint(api)
    app.register_blueprint(planner.bp)
    app.register_blueprint(health.bp)

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    return app
