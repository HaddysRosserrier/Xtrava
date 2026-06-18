"""Central configuration, loaded once at import.

Reads a project-root ".env" file (if present) into the environment, then exposes
the Strava URLs the scrapers use. Importing this module is what makes ".env"
available to the CLI (`python main.py ...`) as well as the web server. Every
value falls back to the current club's default, so nothing breaks without a
".env".
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def load_env_file(path: Path = PROJECT_ROOT / ".env") -> None:
    """Populate os.environ from a simple KEY=value ".env" file, if it exists.
    Supports `export KEY=value`, `#` comments and quoted values. Variables already
    set in the real environment win, so a terminal value still overrides ".env"."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


# Load .env before reading any setting below.
load_env_file()

# --- Strava URLs ----------------------------------------------------------
# The base host and club id drive the two club URLs; each full URL can also be
# overridden outright via its own variable (STRAVA_LEADERBOARD_URL etc.).
STRAVA_BASE_URL = os.environ.get("STRAVA_BASE_URL", "https://www.strava.com").rstrip("/")
STRAVA_CLUB_ID = os.environ.get("STRAVA_CLUB_ID", "")

LEADERBOARD_URL = os.environ.get(
    "STRAVA_LEADERBOARD_URL", f"{STRAVA_BASE_URL}/clubs/{STRAVA_CLUB_ID}/leaderboard"
)
RECENT_ACTIVITY_URL = os.environ.get(
    "STRAVA_RECENT_ACTIVITY_URL", f"{STRAVA_BASE_URL}/clubs/{STRAVA_CLUB_ID}/recent_activity"
)
LOGIN_URL = os.environ.get("STRAVA_LOGIN_URL", f"{STRAVA_BASE_URL}/login")
