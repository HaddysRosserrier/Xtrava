import re
import pandas as pd

from analysis.teams import parse_distance_km, parse_elev_m

# Output column order for the activities sheet.
COLUMNS = [
    "name", "group", "activity_date", "activity_time", "activity_type",
    "distance_km", "distance", "steps", "elevation_m", "elevation",
    "calories", "moving_time", "elapsed_time", "moving_time_s", "elapsed_time_s",
    "pace", "is_time_visible", "is_map_visible", "is_heart_rate_visible",
    "is_pace_visible", "is_cadence_visible", "activity_url",
    # When this row was last re-fetched from its URL via the web GUI. Empty for
    # rows that have only ever been populated by the bulk feed scrape.
    "update_date",
]


def parse_int(raw: str) -> int:
    """Pull an integer out of strings like '8,432' or '512 cal'."""
    digits = re.sub(r"[^\d]", "", str(raw))
    return int(digits) if digits else 0


def parse_duration_s(raw: str) -> int:
    """Convert 'H:MM:SS', 'MM:SS', or '45m' style durations into seconds."""
    raw = str(raw).strip()
    if not raw:
        return 0
    if ":" in raw:
        parts = [int(p) for p in raw.split(":") if p.isdigit()]
        seconds = 0
        for part in parts:  # most-significant first: h, m, s
            seconds = seconds * 60 + part
        return seconds
    # Fallback for 'Hh Mm' / '45m' forms.
    hours = int((re.search(r"(\d+)\s*h", raw) or [0, 0])[1]) if re.search(r"(\d+)\s*h", raw) else 0
    mins = int((re.search(r"(\d+)\s*m", raw) or [0, 0])[1]) if re.search(r"(\d+)\s*m", raw) else 0
    return hours * 3600 + mins * 60


def build_activities_df(rows: list[dict]) -> pd.DataFrame:
    """Normalize raw scraped activity strings into typed columns."""
    if not rows:
        return pd.DataFrame(columns=COLUMNS)

    df = pd.DataFrame(rows)

    df["distance_km"] = df["distance"].apply(parse_distance_km)
    df["elevation_m"] = df["elevation"].apply(parse_elev_m)
    df["steps"] = df["steps"].apply(parse_int)
    df["calories"] = df["calories"].apply(parse_int)
    df["moving_time_s"] = df["moving_time"].apply(parse_duration_s)
    df["elapsed_time_s"] = df["elapsed_time"].apply(parse_duration_s)

    # The bulk scrape never sets an update date; the web GUI fills it on re-fetch.
    if "update_date" not in df.columns:
        df["update_date"] = ""

    # Keep only the columns we know about, preserving order; tolerate missing ones.
    present = [c for c in COLUMNS if c in df.columns]
    return df[present].sort_values(["group", "name"]).reset_index(drop=True)
