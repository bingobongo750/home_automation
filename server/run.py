"""Entry point: python run.py"""

from app import config
from app import create_app

app = create_app()

if __name__ == "__main__":
    # Flask's built-in server is fine here: single user on the LAN/Tailscale,
    # tiny footprint. use_reloader=False so background threads start once.
    app.run(host=config.HOST, port=config.PORT, debug=False, use_reloader=False)
