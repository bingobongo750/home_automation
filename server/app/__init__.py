"""Flask app factory. Starts the two background threads (serial reader,
plug poller) and serves both the JSON API and the static dashboard."""

import logging
from pathlib import Path

from flask import Flask

from . import config, db, poller, serial_reader
from .api import api

DASHBOARD_DIR = Path(__file__).resolve().parent.parent.parent / "dashboard"


def create_app() -> Flask:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    )
    log = logging.getLogger("app")

    db.init_db()
    log.info("Database ready at %s", config.DB_PATH)
    if config.MOCK_HARDWARE:
        log.warning("Running with MOCK_HARDWARE=1 — all hardware is simulated")

    serial_reader.start()
    poller.start()

    app = Flask(
        __name__,
        static_folder=str(DASHBOARD_DIR),
        static_url_path="",
    )
    app.register_blueprint(api)

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    return app
