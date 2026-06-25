"""Lightweight Flask GUI for browsing the scraped Strava data.

Reads the most recent weekly Excel workbooks produced by main.py:
  - leaderboard_week_<monday>.xlsx  -> dashboard (this week's leaderboard)
  - activities_week_<monday>.xlsx   -> activities (one sheet per day)

Run with:  python -m webapp.app   (from the project root)
"""
from __future__ import annotations

import hmac
import os
import threading
from datetime import datetime, date
from functools import wraps
from pathlib import Path

import pandas as pd
from flask import Flask, render_template, request, abort, jsonify, Response

# Importing settings loads ".env" so the login config below is populated even
# when running the dev server directly (python -m webapp.app), not just serve.py.
import settings  # noqa: F401

from analysis.teams import parse_time_minutes, minutes_to_hm, parse_distance_km, parse_elev_m
from analysis.activities import parse_int, parse_duration_s

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

# Auditors tick off activities they've verified. State lives in its own workbook
# keyed by the activity URL (globally unique), so it survives re-scrapes and is
# shared across weekly workbooks.
AUDIT_PATH = OUTPUT_DIR / "audits.xlsx"
AUDIT_SHEET = "Audits"

# Single-activity integrity audits logged by `python main.py audit <url>`.
# One workbook, one row per audited activity (re-audits replace the row).
AUDIT_LOG_PATH = OUTPUT_DIR / "activity_audits.xlsx"
AUDIT_LOG_SHEET = "Audits"

# Activities whose URL has gone 404 (deleted) are archived here on re-fetch,
# one row each, keyed by activity_url (re-detections replace the row).
DELETED_PATH = OUTPUT_DIR / "deleted_activities.xlsx"
DELETED_SHEET = "Deleted"

# Verdict -> traffic-light colour, reusing the .dot classes from the CSS.
VERDICT_DOT = {"FLAGGED": "red", "REVIEW": "yellow", "MINOR": "yellow", "CLEAN": "green"}

# Columns shown in the audit-log table, in order, with friendly headers.
AUDIT_LOG_COLUMNS = [
    "verdict", "name", "team", "activity_type", "device",
    "distance_km", "avg_speed_kmh", "max_speed_kmh", "flag_count", "details", "audited_at",
]
AUDIT_LOG_HEADERS = {
    "verdict": "Verdict",
    "activity_type": "Activity",
    "device": "Device",
    "distance_km": "Distance Km",
    "avg_speed_kmh": "Avg Km/h",
    "max_speed_kmh": "Max Km/h",
    "elev_per_km": "Elev M/km",
    "flag_count": "Flags",
    "audited_at": "Audited",
}

# Friendly headers for the Team Standings table ("Total" prefix is redundant).
TEAM_HEADERS = {
    "team": "Team",
    "total_distance_km": "Distance Km",
    "total_activities": "Activities",
    "total_elev_gain_m": "Elev Gain M",
    "total_time": "Time",
    "athlete_count": "Athletes",
    "kinematic_gain": "Kinematic Gain",
}

# Team-standings columns whose top value across teams gets highlighted.
TEAM_HIGHLIGHT_COLS = [
    "total_distance_km", "total_activities", "total_elev_gain_m",
    "total_time", "athlete_count",
]

# Activity columns hidden from the table (redundant with the day/group selectors
# or with their human-readable twin column).
HIDDEN_COLS = {
    "group", "activity_date", "distance_km", "elevation_m",
    "moving_time_s", "elapsed_time_s", "is_time_visible", "calories",
    # is_pace_visible is redundant on screen (the pace value already shows it),
    # but stays in VALID_FLAGS so it still counts toward the "Valid" signal.
    "is_pace_visible",
    # activity_url is kept in the row data but not shown as its own column —
    # the "Valid" dot links to it instead.
    "activity_url",
    # auto_audit_details backs the Auto Audit tooltip, not its own column.
    "auto_audit_details",
}

# Friendly headers for the activities table (key -> label). Anything not listed
# falls back to a title-cased version of the column name.
ACTIVITY_HEADERS = {
    "valid": "Flag",
    "audit": "Verified",
    "auto_audit": "Scan",
    "activity_time": "At",
    "activity_type": "Activity",
    "is_map_visible": "Map",
    "is_heart_rate_visible": "HR",
    "is_cadence_visible": "Cadence",
    "update_date": "Updated",
    "update": "Update",
}

# Priority order for the activities table. Columns not listed here (e.g.
# activity_time, calories) keep their original order and trail the rest.
COLUMN_ORDER = [
    "valid", "audit", "auto_audit", "name", "activity_type", "distance", "elevation", "pace",
    "is_map_visible", "is_heart_rate_visible", "is_cadence_visible",
    "steps", "moving_time", "elapsed_time", "activity_time",
    # "update_date" (last re-fetch time) and the "update" action button trail last.
    "update_date", "update",
]

# Activity fields refreshed from a single-activity re-fetch, mapped to how the
# scraped detail value is normalised before it's written back to the workbook.
def _refresh_updates(detail: dict) -> dict:
    return {
        "distance": detail.get("distance", ""),
        "elevation": detail.get("elevation", ""),
        "moving_time": detail.get("moving_time", ""),
        "elapsed_time": detail.get("elapsed_time", ""),
        "pace": detail.get("pace", ""),
        "steps": parse_int(detail.get("steps", "")),
        "calories": parse_int(detail.get("calories", "")),
        "distance_km": parse_distance_km(detail.get("distance", "")),
        "elevation_m": parse_elev_m(detail.get("elevation", "")),
        "moving_time_s": parse_duration_s(detail.get("moving_time", "")),
        "elapsed_time_s": parse_duration_s(detail.get("elapsed_time", "")),
        "is_map_visible": bool(detail.get("is_map_visible")),
        "is_heart_rate_visible": bool(detail.get("is_heart_rate_visible")),
        "is_pace_visible": bool(detail.get("is_pace_visible")),
        "is_cadence_visible": bool(detail.get("is_cadence_visible")),
    }


def _record_deleted(row: dict, when: str) -> None:
    """Append a deleted activity's last-known data to the deleted-activities
    workbook, keyed by activity_url (a re-detection replaces the prior row).
    Drops the display-only synthetic columns."""
    record = {k: v for k, v in row.items() if k not in ("valid", "audit", "update")}
    record["deleted_at"] = when

    existing = _read_sheet(DELETED_PATH, DELETED_SHEET) if DELETED_PATH.exists() else pd.DataFrame()
    out = pd.concat([existing, pd.DataFrame([record])], ignore_index=True)
    if "activity_url" in out.columns:
        out = out.drop_duplicates(subset="activity_url", keep="last")

    OUTPUT_DIR.mkdir(exist_ok=True)
    with pd.ExcelWriter(DELETED_PATH, engine="openpyxl", mode="w") as writer:
        out.to_excel(writer, sheet_name=DELETED_SHEET, index=False)

# The four visibility flags that decide an activity's "Valid" traffic light.
VALID_FLAGS = ["is_map_visible", "is_heart_rate_visible", "is_pace_visible", "is_cadence_visible"]

# A day's activities can only be (re-)scraped while it's still in Strava's recent
# feed. Older days have scrolled off, so the per-day scrape button is limited to
# today and the previous SCRAPE_MAX_AGE_DAYS days.
SCRAPE_MAX_AGE_DAYS = 2

app = Flask(__name__)


# --- Access control -------------------------------------------------------
# HTTP Basic Auth on every route, including the scrape buttons (which act on the
# configured Strava account). Credentials come from the environment so they're
# never committed. With no password set the app refuses to serve anything, so it
# can't be hosted wide open on the network by accident.
AUTH_USER = os.environ.get("XTRAVA_USER", "admin")
AUTH_PASSWORD = os.environ.get("XTRAVA_PASSWORD", "")


@app.before_request
def _require_login() -> Response | None:
    if not AUTH_PASSWORD:
        return Response(
            "Xtrava is not configured for hosting: set the XTRAVA_PASSWORD "
            "environment variable (and optionally XTRAVA_USER) before serving.",
            503,
        )
    auth = request.authorization
    if (
        auth is not None
        and hmac.compare_digest(auth.username or "", AUTH_USER)
        and hmac.compare_digest(auth.password or "", AUTH_PASSWORD)
    ):
        return None
    return Response(
        "Authentication required.",
        401,
        {"WWW-Authenticate": 'Basic realm="Xtrava"'},
    )


# --- Scrape concurrency ---------------------------------------------------
# Browser-driven scrapes are serialized: only one may run at a time, both to
# avoid launching several (sometimes headed) browsers at once and to stop two
# writers corrupting the same workbook. The decorated endpoints try to acquire
# without blocking and return 409 if a scrape is already underway.
_scrape_lock = threading.Lock()


def _scrape_endpoint(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not _scrape_lock.acquire(blocking=False):
            return jsonify({
                "ok": False,
                "error": "another scrape is already running — try again in a moment",
            }), 409
        try:
            return view(*args, **kwargs)
        finally:
            _scrape_lock.release()

    return wrapper


def _latest(prefix: str) -> Path | None:
    """Newest output/<prefix>_week_*.xlsx by the Monday date in its name."""
    files = sorted(OUTPUT_DIR.glob(f"{prefix}_week_*.xlsx"))
    return files[-1] if files else None


def _week_options(prefix: str) -> list[str]:
    """Week labels (Monday dates) we have <prefix> data for, newest first."""
    files = sorted(OUTPUT_DIR.glob(f"{prefix}_week_*.xlsx"))
    return [p.stem.replace(f"{prefix}_week_", "") for p in files][::-1]


def _week_path(prefix: str, week: str | None) -> Path | None:
    """Path to a specific week's workbook, or the newest when `week` is falsy.
    `week` is validated as an ISO date (which also blocks path traversal)."""
    if not week:
        return _latest(prefix)
    try:
        date.fromisoformat(week)
    except ValueError:
        return None
    path = OUTPUT_DIR / f"{prefix}_week_{week}.xlsx"
    return path if path.exists() else None


def _consistency(distance_km, minutes) -> float:
    """Average kilometric speed: distance squared over time (km²/min) — i.e.
    distance × average speed, so it rewards covering more distance in less time.
    Returns 0.0 when there's no active time (minutes == 0) to avoid dividing by zero.
    """
    distance = float(distance_km or 0)
    minutes = float(minutes or 0)
    if minutes <= 0:
        return 0.0
    return round(distance ** 2 / minutes, 2)


def _add_consistency(df: pd.DataFrame, distance_col: str, time_col: str) -> None:
    """Append a `kinematic_gain` column computed per row, in place. No-op if either
    source column is missing. `time_col` holds an 'Xh Ym' string."""
    if not {distance_col, time_col} <= set(df.columns):
        return
    df["kinematic_gain"] = [
        _consistency(d, parse_time_minutes(str(t)))
        for d, t in zip(df[distance_col], df[time_col])
    ]


def _add_team_consistency(teams: pd.DataFrame, athletes: pd.DataFrame) -> None:
    """Append each team's `kinematic_gain` as the sum of its athletes' scores, in
    place. Teams are matched to athletes by case-insensitive team name (the Teams
    sheet title-cases the name, the athlete sheet keeps the raw value). No-op if
    athlete kinematic_gain hasn't been computed or the team key is missing."""
    if "kinematic_gain" not in athletes.columns or "team" not in athletes.columns:
        return
    if "team" not in teams.columns:
        return
    sums = (
        athletes.assign(_k=athletes["team"].astype(str).str.strip().str.casefold())
        .groupby("_k")["kinematic_gain"].sum()
    )
    teams["kinematic_gain"] = (
        teams["team"].astype(str).str.strip().str.casefold().map(sums).fillna(0.0).round(2)
    )


def _read_sheet(path: Path, sheet: str) -> pd.DataFrame:
    try:
        return pd.read_excel(path, sheet_name=sheet)
    except ValueError:
        # Sheet not present in this workbook.
        return pd.DataFrame()


def _records(df: pd.DataFrame) -> list[dict]:
    return df.fillna("").to_dict(orient="records")


def _load_audits() -> dict[str, dict]:
    """Map of activity_url -> {"verified_at": <iso str>} for verified activities."""
    if not AUDIT_PATH.exists():
        return {}
    df = _read_sheet(AUDIT_PATH, AUDIT_SHEET)
    if "activity_url" not in df.columns:
        return {}
    audits = {}
    for _, row in df.iterrows():
        url = str(row["activity_url"]).strip()
        if url:
            audits[url] = {"verified_at": str(row.get("verified_at", "")).strip()}
    return audits


def _load_audit_results() -> dict[str, dict]:
    """Map of activity_url -> {"verdict", "details"} from the auto-audit log
    workbook. Used to pre-fill the Activities page's Auto Audit column — including
    the flag details in the tooltip — with prior results, without re-auditing."""
    if not AUDIT_LOG_PATH.exists():
        return {}
    df = _read_sheet(AUDIT_LOG_PATH, AUDIT_LOG_SHEET)
    if not {"activity_url", "verdict"} <= set(df.columns):
        return {}
    has_details = "details" in df.columns
    results = {}
    for _, row in df.iterrows():
        url = str(row["activity_url"]).strip()
        if url:
            details = str(row.get("details", "")).strip() if has_details else ""
            results[url] = {
                "verdict": str(row.get("verdict", "")).strip(),
                "details": "" if details.lower() == "nan" else details,
            }
    return results


def _set_audit(activity_url: str, verified: bool) -> None:
    """Record (or clear) an auditor's verification for one activity, then persist
    the whole audit workbook. Only verified activities are stored; unchecking
    drops the row."""
    audits = _load_audits()
    if verified:
        audits[activity_url] = {"verified_at": datetime.now().isoformat(timespec="seconds")}
    else:
        audits.pop(activity_url, None)

    OUTPUT_DIR.mkdir(exist_ok=True)
    df = pd.DataFrame(
        [{"activity_url": url, "verified_at": meta["verified_at"]} for url, meta in audits.items()],
        columns=["activity_url", "verified_at"],
    )
    with pd.ExcelWriter(AUDIT_PATH, engine="openpyxl", mode="w") as writer:
        df.to_excel(writer, sheet_name=AUDIT_SHEET, index=False)


def _mark_team_leaders(teams: pd.DataFrame, records: list[dict]) -> None:
    """Tag each record with `_leaders`: the set of TEAM_HIGHLIGHT_COLS in which
    that team holds the top value (ties highlight every leader)."""
    for rec in records:
        rec["_leaders"] = []
    for col in TEAM_HIGHLIGHT_COLS:
        if col not in teams.columns:
            continue
        # total_time is a "14h 1m" string; rank it by minutes. Others are numeric.
        if col == "total_time":
            values = teams[col].map(parse_time_minutes)
        else:
            values = pd.to_numeric(teams[col], errors="coerce")
        top = values.max()
        if pd.isna(top):
            continue
        for rec, val in zip(records, values):
            if pd.notna(val) and val == top:
                rec["_leaders"].append(col)


def _team_kpis(teams: pd.DataFrame) -> list[dict]:
    """Club-wide totals for the summary cards above Team Standings."""
    if teams.empty:
        return []

    def _sum(col: str) -> float:
        return float(pd.to_numeric(teams[col], errors="coerce").sum()) if col in teams else 0.0

    minutes = int(teams["total_time"].map(parse_time_minutes).sum()) if "total_time" in teams else 0
    return [
        {"label": "Distance", "value": f"{_sum('total_distance_km'):,.0f}", "unit": "km"},
        {"label": "Activities", "value": f"{_sum('total_activities'):,.0f}", "unit": ""},
        {"label": "Elevation", "value": f"{_sum('total_elev_gain_m'):,.0f}", "unit": "m"},
        {"label": "Active Time", "value": minutes_to_hm(minutes), "unit": ""},
        {"label": "Athletes", "value": f"{_sum('athlete_count'):,.0f}", "unit": ""},
    ]


@app.route("/")
def dashboard():
    weeks = _week_options("leaderboard")
    selected = request.args.get("week")
    if selected and selected not in weeks:
        abort(404)
    path = _week_path("leaderboard", selected)
    if path is None:
        return render_template("dashboard.html", week=None, weeks=weeks)

    teams = _read_sheet(path, "This Week - Teams")
    teams = teams.drop(columns=["members"], errors="ignore")
    athletes = _read_sheet(path, "This Week - Athletes")

    _add_consistency(athletes, "distance_km", "time")
    _add_team_consistency(teams, athletes)

    team_records = _records(teams)
    _mark_team_leaders(teams, team_records)
    kpis = _team_kpis(teams)

    # Filename is leaderboard_week_<YYYY-MM-DD>.xlsx — surface the week label.
    week = path.stem.replace("leaderboard_week_", "")

    return render_template(
        "dashboard.html",
        week=week,
        weeks=weeks,
        kpis=kpis,
        team_cols=list(teams.columns),
        team_headers=TEAM_HEADERS,
        teams=team_records,
        # athlete_url backs the profile link on the coverage page; it isn't a
        # column anyone wants to read here, so keep it out of the displayed set.
        athlete_cols=[c for c in athletes.columns if c != "athlete_url"],
        athletes=_records(athletes),
    )


def _weekly_coverage(xl: pd.ExcelFile, week: str | None = None) -> list[dict]:
    """Per group: how many activities we extracted across the whole week vs. the
    leaderboard's weekly total, and how many are still missing.

    `xl` is the activities workbook (one sheet per day). Expected totals come
    from the same week's leaderboard "This Week - Teams" sheet (newest when
    `week` is None).
    """
    frames = [xl.parse(s) for s in xl.sheet_names]
    acts = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if "group" not in acts.columns:
        return []
    extracted = acts["group"].value_counts().to_dict()

    expected: dict[str, int] = {}
    lb_path = _week_path("leaderboard", week)
    if lb_path is not None:
        teams = _read_sheet(lb_path, "This Week - Teams")
        if {"team", "total_activities"} <= set(teams.columns):
            totals = pd.to_numeric(teams["total_activities"], errors="coerce").fillna(0).astype(int)
            expected = dict(zip(teams["team"], totals))

    rows = []
    for group in sorted(set(extracted) | set(expected)):
        ext = int(extracted.get(group, 0))
        exp = int(expected.get(group, 0))
        rows.append({
            "group": group,
            "extracted": ext,
            "expected": exp,
            # diff: <0 missing, 0 exact, >0 surplus.
            "diff": ext - exp,
            "pct": round(min(ext / exp, 1) * 100) if exp else 0,
        })
    return rows


@app.route("/refresh", methods=["POST"])
@_scrape_endpoint
def refresh_leaderboard():
    """Re-scrape this week's leaderboard from Strava and rewrite the weekly
    workbook, so the dashboard reflects the latest standings. Launches a browser
    (slow); imported lazily so the web app still runs without Playwright."""
    try:
        from main import run_leaderboard
        run_leaderboard()
    except Exception as exc:  # noqa: BLE001 — surface any scrape failure to the UI
        return jsonify({"ok": False, "error": f"refresh failed: {exc}"}), 502
    return jsonify({"ok": True})


@app.route("/scrape-day", methods=["POST"])
@_scrape_endpoint
def scrape_day():
    """Scrape one day's club activities from Strava's recent feed and save them to
    that day's sheet. Limited to the past SCRAPE_MAX_AGE_DAYS days, since older days
    have scrolled out of the feed. Opens a browser (slow)."""
    data = request.get_json(silent=True) or {}
    try:
        target = date.fromisoformat(str(data.get("date", "")).strip())
    except ValueError:
        return jsonify({"ok": False, "error": "bad or missing date"}), 400

    age = (date.today() - target).days
    if not 0 <= age <= SCRAPE_MAX_AGE_DAYS:
        return jsonify({
            "ok": False,
            "error": f"can only scrape today and the past {SCRAPE_MAX_AGE_DAYS} days",
        }), 400

    try:
        from main import run_activities
        run_activities(target)
    except Exception as exc:  # noqa: BLE001 — surface any scrape failure to the UI
        return jsonify({"ok": False, "error": f"scrape failed: {exc}"}), 502
    return jsonify({"ok": True, "date": target.isoformat()})


@app.route("/activities")
def activities():
    weeks = _week_options("activities")
    selected = request.args.get("week")
    if selected and selected not in weeks:
        abort(404)
    path = _week_path("activities", selected)
    if path is None:
        return render_template("activities.html", week=None, weeks=weeks, days=[])

    xl = pd.ExcelFile(path)
    days = xl.sheet_names  # one sheet per day, e.g. "Tue 2026-06-16"
    week = path.stem.replace("activities_week_", "")
    coverage = _weekly_coverage(xl, week)

    # Default to the most recent day (sheets are appended chronologically). A
    # stale day from a previous week's selection falls back to the latest day.
    day = request.args.get("day")
    if not day or day not in days:
        day = days[-1] if days else None

    df = xl.parse(day) if day else pd.DataFrame()

    # "Valid" traffic light from the four visibility flags. Count how many are
    # NOT visible: 0 -> green (valid), 1-2 -> yellow (grey area), 3+ -> red (flagged).
    if not df.empty and all(f in df.columns for f in VALID_FLAGS):
        missing = (~df[VALID_FLAGS].astype(bool)).sum(axis=1)
        df.insert(0, "valid", missing.map(
            lambda m: "green" if m == 0 else ("yellow" if m <= 2 else "red")
        ))

    # Auditor "Verified" checkbox, pre-ticked from the saved audit workbook.
    if not df.empty and "activity_url" in df.columns:
        audits = _load_audits()
        df.insert(0, "audit", df["activity_url"].map(
            lambda u: str(u).strip() in audits
        ))

    # "Auto Audit" column: the saved verdict for this activity (blank if never
    # audited). The cell renders a colored verdict plus a run/re-run button.
    if not df.empty and "activity_url" in df.columns:
        results = _load_audit_results()
        df["auto_audit"] = df["activity_url"].map(
            lambda u: results.get(str(u).strip(), {}).get("verdict", "")
        )
        # Flag details for the tooltip; hidden as its own column (see HIDDEN_COLS).
        df["auto_audit_details"] = df["activity_url"].map(
            lambda u: results.get(str(u).strip(), {}).get("details", "")
        )

    # "Updated" timestamp (always present so the column shows) and the synthetic
    # "update" action column that renders the re-fetch button.
    if not df.empty:
        if "update_date" not in df.columns:
            df["update_date"] = ""
        df["update"] = ""

    # "group" becomes the section heading; the rest are dropped as redundant.
    # Remaining columns are ordered by COLUMN_ORDER (unlisted ones trail, in
    # their original order — sorted() is stable).
    visible = [c for c in df.columns if c not in HIDDEN_COLS]
    columns = sorted(
        visible,
        key=lambda c: COLUMN_ORDER.index(c) if c in COLUMN_ORDER else len(COLUMN_ORDER),
    )
    # Records keep every field (incl. activity_url for the "Valid" link);
    # `columns` controls only which ones get their own table column.
    groups = []
    if "group" in df.columns:
        for name, sub in df.groupby("group", sort=True):
            groups.append({"name": name or "—", "rows": _records(sub)})
    elif not df.empty:
        groups.append({"name": "All", "rows": _records(df)})

    # The per-day scrape button: derive the selected day's ISO date (the trailing
    # token of a "Mon 2026-06-15" sheet name) and whether it's recent enough to
    # still be in Strava's feed.
    day_iso = day.split()[-1] if day else None
    day_scrapeable = False
    if day_iso:
        try:
            day_scrapeable = 0 <= (date.today() - date.fromisoformat(day_iso)).days <= SCRAPE_MAX_AGE_DAYS
        except ValueError:
            day_iso = None

    return render_template(
        "activities.html",
        week=week,
        weeks=weeks,
        days=days,
        day=day,
        day_iso=day_iso,
        day_scrapeable=day_scrapeable,
        scrape_max_age=SCRAPE_MAX_AGE_DAYS,
        coverage=coverage,
        columns=columns,
        headers=ACTIVITY_HEADERS,
        verdict_dot=VERDICT_DOT,
        groups=groups,
    )


@app.route("/audit", methods=["POST"])
def audit():
    """Persist an auditor's verify/unverify of a single activity (by URL)."""
    data = request.get_json(silent=True) or {}
    activity_url = str(data.get("activity_url", "")).strip()
    verified = bool(data.get("verified"))
    if not activity_url:
        abort(400)
    _set_audit(activity_url, verified)
    return jsonify({"ok": True, "activity_url": activity_url, "verified": verified})


@app.route("/update", methods=["POST"])
@_scrape_endpoint
def update_activity():
    """Re-fetch one activity straight from its URL and rewrite that row in the
    activities workbook, stamping `update_date`. Returns the refreshed values so
    the page can be reloaded to reflect them."""
    data = request.get_json(silent=True) or {}
    activity_url = str(data.get("activity_url", "")).strip()
    if not activity_url:
        abort(400)

    path = _latest("activities")
    if path is None:
        return jsonify({"ok": False, "error": "no activities workbook"}), 404

    # Load every day-sheet; find the one holding this activity.
    xl = pd.ExcelFile(path)
    sheets = {name: xl.parse(name) for name in xl.sheet_names}
    target_sheet = next(
        (name for name, sdf in sheets.items()
         if "activity_url" in sdf.columns
         and (sdf["activity_url"].astype(str).str.strip() == activity_url).any()),
        None,
    )
    if target_sheet is None:
        return jsonify({"ok": False, "error": "activity not found"}), 404

    # Re-scrape (launches a browser — slow). Import lazily so the web app still
    # runs if Playwright/browser binaries are unavailable.
    try:
        from scrapers.activities_scraper import scrape_single_activity
        detail = scrape_single_activity(activity_url)
    except Exception as exc:  # noqa: BLE001 — surface any scrape failure to the UI
        return jsonify({"ok": False, "error": f"scrape failed: {exc}"}), 502
    if not detail:
        return jsonify({"ok": False, "error": "could not load activity"}), 502

    sdf = sheets[target_sheet]
    idx = sdf.index[sdf["activity_url"].astype(str).str.strip() == activity_url][0]
    when = datetime.now().isoformat(timespec="seconds")

    if detail.get("deleted"):
        # Activity is gone: archive its last-known data, then drop it from the
        # weekly workbook entirely.
        _record_deleted(sdf.loc[idx].to_dict(), when)
        sheets[target_sheet] = sdf.drop(index=idx).reset_index(drop=True)
    else:
        updates = _refresh_updates(detail)
        # Only fill activity_type if it's currently blank (the feed type is preferred).
        if detail.get("activity_type") and not str(sdf.at[idx, "activity_type"]).strip():
            updates["activity_type"] = detail["activity_type"]
        updates["update_date"] = when
        for col, val in updates.items():
            if col not in sdf.columns:
                sdf[col] = ""
            # object dtype keeps mixed string/number/bool values intact on write.
            if sdf[col].dtype != object:
                sdf[col] = sdf[col].astype(object)
            sdf.at[idx, col] = val
        sheets[target_sheet] = sdf

    # Rewrite the whole workbook to preserve every other day's sheet.
    with pd.ExcelWriter(path, engine="openpyxl", mode="w") as writer:
        for name, out in sheets.items():
            out.to_excel(writer, sheet_name=name, index=False)

    return jsonify({
        "ok": True,
        "activity_url": activity_url,
        "update_date": when,
        "deleted": bool(detail.get("deleted")),
    })


@app.route("/backfill-athlete", methods=["POST"])
@_scrape_endpoint
def backfill_athlete():
    """Scrape one athlete's public profile and add any activities we're missing
    for the given week to the activities workbook. Used by the coverage page to
    close gaps the club feed didn't surface. Opens a browser (slow)."""
    data = request.get_json(silent=True) or {}
    athlete_url = str(data.get("athlete_url", "")).strip()
    week = str(data.get("week", "")).strip() or None
    if not athlete_url:
        abort(400)

    path = _week_path("activities", week)
    if path is None:
        return jsonify({"ok": False, "error": "no activities workbook for that week"}), 404
    try:
        monday = date.fromisoformat(path.stem.replace("activities_week_", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "bad week"}), 400

    # Every activity URL we already hold for the week, so the scrape skips them.
    xl = pd.ExcelFile(path)
    sheets = {name: xl.parse(name) for name in xl.sheet_names}
    existing_urls: set[str] = set()
    for sdf in sheets.values():
        if "activity_url" in sdf.columns:
            existing_urls |= set(sdf["activity_url"].astype(str).str.strip())

    # Re-scrape (launches a browser — slow). Import lazily so the web app still
    # runs if Playwright/browser binaries are unavailable.
    try:
        from scrapers.activities_scraper import scrape_athlete_week
        from analysis.activities import build_activities_df
        raw = scrape_athlete_week(athlete_url, monday, skip_urls=existing_urls)
    except Exception as exc:  # noqa: BLE001 — surface any scrape failure to the UI
        return jsonify({"ok": False, "error": f"scrape failed: {exc}"}), 502

    if not raw:
        return jsonify({"ok": True, "added": 0})

    # Normalise to the workbook schema and append each new activity to its day
    # sheet (creating the sheet if that day wasn't scraped before).
    new_df = build_activities_df(raw)
    added = 0
    for _, row in new_df.iterrows():
        try:
            day = date.fromisoformat(str(row["activity_date"]))
        except ValueError:
            continue
        sheet = day.strftime("%a %Y-%m-%d")
        row_df = pd.DataFrame([row])
        if sheet in sheets:
            sheets[sheet] = pd.concat([sheets[sheet], row_df], ignore_index=True)
        else:
            sheets[sheet] = row_df
        added += 1

    # Rewrite the workbook with sheets in chronological order (the trailing
    # YYYY-MM-DD in each sheet name sorts correctly).
    ordered = dict(sorted(sheets.items(), key=lambda kv: kv[0].split()[-1]))
    with pd.ExcelWriter(path, engine="openpyxl", mode="w") as writer:
        for name, out in ordered.items():
            out.to_excel(writer, sheet_name=name, index=False)

    return jsonify({"ok": True, "added": added})


@app.route("/auto-audit", methods=["POST"])
@_scrape_endpoint
def auto_audit():
    """Scrape + audit one activity on demand, persist it to the audit log, and
    return the verdict so the Activities page can update the row in place. Opens a
    headed browser (needed for the speed stream), so it takes several seconds."""
    data = request.get_json(silent=True) or {}
    activity_url = str(data.get("activity_url", "")).strip()
    if not activity_url:
        abort(400)

    # Import lazily so the web app still starts without Playwright installed.
    try:
        from audit_service import audit_url
        out = audit_url(activity_url)
    except Exception as exc:  # noqa: BLE001 — surface any scrape/audit failure to the UI
        return jsonify({"ok": False, "error": f"audit failed: {exc}"}), 502

    result = out["result"]
    return jsonify({
        "ok": True,
        "activity_url": activity_url,
        "verdict": result["verdict"],
        "flag_count": len(result["flags"]),
        "details": "; ".join(f["message"] for f in result["flags"]),
        "max_speed_kmh": result["metrics"].get("max_speed_kmh", ""),
    })


@app.route("/audit-log")
def audit_log():
    """Browse the single-activity integrity audits from `main.py audit`,
    newest first, with an optional ?verdict= filter."""
    df = _read_sheet(AUDIT_LOG_PATH, AUDIT_LOG_SHEET) if AUDIT_LOG_PATH.exists() else pd.DataFrame()

    if df.empty or "verdict" not in df.columns:
        return render_template(
            "audit.html", groups=[], columns=AUDIT_LOG_COLUMNS,
            headers=AUDIT_LOG_HEADERS, verdict_dot=VERDICT_DOT,
            counts={}, active=None, total=0,
        )

    # Newest audits first (audited_at is an ISO timestamp string, so lexical sort
    # matches chronological order).
    if "audited_at" in df.columns:
        df = df.sort_values("audited_at", ascending=False)

    counts = df["verdict"].value_counts().to_dict()

    active = request.args.get("verdict")
    if active:
        df = df[df["verdict"] == active]

    # Separate audits by team into tabs, mirroring the Activities page. Blank or
    # missing team falls under "—". Rows stay newest-first within each team.
    groups = []
    if "team" in df.columns:
        team = df["team"].fillna("").astype(str).str.strip()
        df = df.assign(_team=team.where(team != "", "—"))
        for name, sub in df.groupby("_team", sort=True):
            groups.append({"name": name, "rows": _records(sub.drop(columns="_team"))})
    elif not df.empty:
        groups.append({"name": "All", "rows": _records(df)})

    return render_template(
        "audit.html",
        groups=groups,
        # team is the tab heading now, so don't repeat it as a column.
        columns=[c for c in AUDIT_LOG_COLUMNS if c in df.columns and c != "team"],
        headers=AUDIT_LOG_HEADERS,
        verdict_dot=VERDICT_DOT,
        counts=counts,
        active=active,
        total=int(sum(counts.values())),
    )


def _athlete_discrepancies(group: str, week: str | None = None) -> list[dict]:
    """Per-athlete mismatch between activities logged on the leaderboard and the
    activities we actually extracted, for everyone connected to `group`.

    Expected counts come from the leaderboard's "This Week - Athletes" sheet
    (`activities` column); extracted counts come from the activities workbook
    (all day sheets), filtered to `group`. Each returned row carries a signed
    `diff = extracted - expected`: negative means missing activities, positive
    means extra ones we extracted beyond the logged total (including athletes in
    the feed who aren't on the leaderboard at all). Rows with diff == 0 are
    omitted. Sorted most-missing first, then most-extra.
    """
    act_path = _week_path("activities", week)
    lb_path = _week_path("leaderboard", week)
    if act_path is None or lb_path is None:
        return []

    target = group.strip().casefold()

    # Extracted activities per athlete within this group, across the whole week.
    xl = pd.ExcelFile(act_path)
    frames = [xl.parse(s) for s in xl.sheet_names]
    acts = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    extracted_counts: dict[str, int] = {}
    if {"name", "group"} <= set(acts.columns):
        in_group = acts[acts["group"].astype(str).str.strip().str.casefold() == target]
        extracted_counts = (
            in_group["name"].astype(str).str.strip().value_counts().to_dict()
        )

    # Expected per-athlete counts from the leaderboard's athlete sheet. The team
    # name is matched case-insensitively (the Teams sheet title-cases it, the
    # athlete sheet keeps the raw value).
    expected_counts: dict[str, int] = {}
    # name -> Strava profile URL, for linking the athlete on the coverage page.
    # Only present when the leaderboard was scraped with athlete_url support.
    athlete_urls: dict[str, str] = {}
    athletes_sheet = _read_sheet(lb_path, "This Week - Athletes")
    if {"name", "team", "activities"} <= set(athletes_sheet.columns):
        has_url = "athlete_url" in athletes_sheet.columns
        team_match = athletes_sheet["team"].astype(str).str.strip().str.casefold() == target
        for _, a in athletes_sheet[team_match].iterrows():
            name = str(a["name"]).strip()
            expected_counts[name] = int(pd.to_numeric(a["activities"], errors="coerce") or 0)
            if has_url:
                url = str(a["athlete_url"]).strip()
                if url and url.lower() != "nan":
                    athlete_urls[name] = url

    rows = []
    for name in set(extracted_counts) | set(expected_counts):
        expected = expected_counts.get(name, 0)
        extracted = extracted_counts.get(name, 0)
        diff = extracted - expected
        if diff != 0:
            rows.append({
                "name": name,
                "expected": expected,
                "extracted": extracted,
                "diff": diff,
                "athlete_url": athlete_urls.get(name, ""),
            })
    rows.sort(key=lambda r: (r["diff"], r["name"]))
    return rows


@app.route("/coverage")
def coverage_detail():
    group = request.args.get("group")
    if not group:
        abort(404)

    week = request.args.get("week")
    athletes = _athlete_discrepancies(group, week)
    act_path = _week_path("activities", week)
    week = act_path.stem.replace("activities_week_", "") if act_path else week
    missing = [a for a in athletes if a["diff"] < 0]
    extra = [a for a in athletes if a["diff"] > 0]

    return render_template(
        "coverage.html",
        group=group,
        week=week,
        missing=missing,
        extra=extra,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
