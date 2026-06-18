"""Production entry point: serve the Xtrava web app on the local network.

Uses waitress (a production WSGI server) instead of Flask's debug dev server,
which must never be exposed. Run from the project root so the relative `output/`
and `session.json` paths resolve:

    python serve.py

Configuration is read from a ".env" file next to this script (copy ".env.example"
to ".env" and edit it). The app then listens on http://<this-machine-LAN-ip>:5000
for everyone on your network; log in with the username/password from ".env".

Configuration keys (set in .env):
    XTRAVA_PASSWORD   required — the login password (no default)
    XTRAVA_USER       login username (default "admin")
    XTRAVA_HOST       bind address (default "0.0.0.0" = all interfaces)
    XTRAVA_PORT       port (default 5000)
"""
from __future__ import annotations

import os

# Importing settings loads ".env" into the environment; do it first, before
# webapp.app reads its login config at import time.
import settings  # noqa: F401

from waitress import serve  # noqa: E402 — imported after .env is loaded
from webapp.app import app  # noqa: E402 — reads login config at import time

if __name__ == "__main__":
    if not os.environ.get("XTRAVA_PASSWORD"):
        raise SystemExit(
            "Refusing to start without a login password.\n"
            "Set XTRAVA_PASSWORD in your .env file (copy .env.example to .env "
            "and edit it) so the app isn't exposed on the network wide open."
        )

    host = os.environ.get("XTRAVA_HOST", "0.0.0.0")
    port = int(os.environ.get("XTRAVA_PORT", "5000"))
    print(f"Xtrava serving on http://{host}:{port}  (Ctrl+C to stop)")
    # Threaded so a slow ~30s scrape doesn't block other viewers; the scrape
    # lock in webapp.app still serializes the browser work itself.
    serve(app, host=host, port=port, threads=8)
