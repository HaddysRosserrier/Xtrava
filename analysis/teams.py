import re
import pandas as pd

DISTANCE_RE = re.compile(r"([\d,\.]+)\s*(km|mi|m)\b", re.IGNORECASE)
ELEV_RE = re.compile(r"([\d,\.]+)\s*(m|ft)\b", re.IGNORECASE)
TIME_RE = re.compile(r"(?:(\d+)h)?\s*(?:(\d+)m)?")

KM_FACTORS = {"km": 1.0, "mi": 1.60934, "m": 0.001}
ELEV_FACTORS = {"m": 1.0, "ft": 0.3048}


def parse_distance_km(raw: str) -> float:
    match = DISTANCE_RE.search(raw)
    if not match:
        return 0.0
    value = float(match.group(1).replace(",", ""))
    unit = match.group(2).lower()
    return round(value * KM_FACTORS.get(unit, 1.0), 2)


def parse_elev_m(raw: str) -> float:
    match = ELEV_RE.search(raw)
    if not match:
        return 0.0
    value = float(match.group(1).replace(",", ""))
    unit = match.group(2).lower()
    return round(value * ELEV_FACTORS.get(unit, 1.0), 1)


def parse_activities(raw: str) -> int:
    digits = re.sub(r"[^\d]", "", raw)
    return int(digits) if digits else 0


def parse_time_minutes(raw: str) -> int:
    """Convert '14h 1m' or '2h 30m' or '45m' into total minutes."""
    match = TIME_RE.search(raw)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    mins = int(match.group(2) or 0)
    return hours * 60 + mins


def minutes_to_hm(total_minutes: int) -> str:
    h, m = divmod(int(total_minutes), 60)
    return f"{h}h {m}m"


def build_athletes_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["distance_km"] = df["distance_raw"].apply(parse_distance_km)
    df["activities"] = df["activities_raw"].apply(parse_activities)
    df["elev_gain_m"] = df["elev_gain_raw"].apply(parse_elev_m)
    df["time_minutes"] = df["time_raw"].apply(parse_time_minutes)
    df["time"] = df["time_minutes"].apply(minutes_to_hm)
    df = df.drop(columns=["raw_name", "distance_raw", "activities_raw", "elev_gain_raw", "time_raw", "time_minutes"], errors="ignore")
    # athlete_url is optional (older scrapes won't have it); keep it when present.
    cols = ["rank", "name", "team", "distance_km", "activities", "elev_gain_m", "time"]
    if "athlete_url" in df.columns:
        cols.insert(3, "athlete_url")
    return df[cols]


def build_teams_df(athletes_df: pd.DataFrame) -> pd.DataFrame:
    grouped = athletes_df.copy()
    grouped["team"] = grouped["team"].str.title()
    grouped["time_minutes"] = grouped["time"].apply(parse_time_minutes)

    teams = (
        grouped.groupby("team", as_index=False)
        .agg(
            total_distance_km=("distance_km", "sum"),
            total_activities=("activities", "sum"),
            total_elev_gain_m=("elev_gain_m", "sum"),
            total_time_minutes=("time_minutes", "sum"),
            athlete_count=("name", "count"),
            members=("name", lambda x: ", ".join(x)),
        )
        .sort_values("total_distance_km", ascending=False)
        .reset_index(drop=True)
    )

    teams.insert(0, "rank", range(1, len(teams) + 1))
    teams["total_distance_km"] = teams["total_distance_km"].round(2)
    teams["total_elev_gain_m"] = teams["total_elev_gain_m"].round(1)
    teams["total_time"] = teams["total_time_minutes"].apply(minutes_to_hm)
    teams = teams.drop(columns=["total_time_minutes"])

    return teams[["rank", "team", "total_distance_km", "total_activities", "total_elev_gain_m", "total_time", "athlete_count", "members"]]
