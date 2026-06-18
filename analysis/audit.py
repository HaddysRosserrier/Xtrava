"""Rule-based integrity check for a single Strava activity.

Turns one scraped activity (see scrapers.audit_scraper) into a list of flags,
each with a severity, and an overall verdict. The goal is to surface activities
that break the club rules or can't be trusted.

Club rules this encodes:
  1. Allowed activity types are running, walking and hiking only — anything else
     (a ride, swim, etc.) is a violation.
  2. There is no speed limit, but the effort must be *humanly possible* — i.e. no
     vehicle use. We flag an activity only when its average moving speed exceeds
     what a human can sustain on foot over that distance.

Everything else (hidden map / HR / cadence, odd timing) is raised as softer
"can't fully verify this" notes, not rule violations. A flag means "a human
should look at this", not "guilty". Real, fast athletes should pass cleanly.
"""
from __future__ import annotations

from analysis.activities import parse_int, parse_duration_s
from analysis.teams import parse_distance_km, parse_elev_m

# Activity types the club allows. Normalised (lower-case, single-spaced) before
# matching. "trail run" / "virtual run" are still running; treadmill runs report
# as "Run" or "Virtual Run" and legitimately have no GPS map (see GPS_TYPES).
ALLOWED_TYPES = {"run", "trail run", "virtual run", "walk", "hike"}

# Allowed types where we expect a GPS route, so a missing map is a real
# verifiability gap. Virtual/treadmill runs are deliberately excluded.
GPS_TYPES = {"run", "trail run", "walk", "hike"}

# Types where the device should record steps, so a walk/hike logging distance
# with zero steps is suspicious (often an imported/fabricated GPS route, not an
# actual walk). Runs are excluded — many running watches report no step count.
STEP_EXPECTED_TYPES = {"walk", "hike"}

# Distance (km) -> a generous ceiling on average moving speed (km/h) a human can
# sustain on foot over that distance. Anchored ~15% above running world-record
# average speeds, so only clearly non-human (vehicle) efforts trip it. A human
# can average sprint pace for 200 m but not for 10 km, hence the curve. Beyond
# the longest anchor the ceiling holds flat.
HUMAN_FOOT_SPEED_CURVE: list[tuple[float, float]] = [
    (0.1, 42.0),   # ~100 m; Bolt peaked ~37 km/h
    (0.4, 38.0),
    (1.0, 31.0),
    (5.0, 27.0),
    (10.0, 25.0),
    (21.1, 24.0),  # half marathon
    (42.2, 23.0),  # marathon; WR avg ~20.5 km/h
]

# Per-second speed-stream thresholds (vehicle detection). On foot, sustained time
# above ~25 km/h isn't possible — that's a vehicle. Calibrated against real club
# data: compliant run/walk activities spent 0 s above 25 km/h (fastest peak was a
# 20 km/h sprint for ~2 s), while motorcycle-assisted "walks" peaked ~46 km/h and
# spent 18–22 s above 25 km/h.
VEHICLE_SPEED_KMH = 25.0   # instantaneous speed above this on foot = vehicle
BURST_SPEED_KMH = 20.0     # used to measure a sustained fast stretch
VEHICLE_SECONDS_MIN = 5    # total seconds above VEHICLE_SPEED_KMH to flag
BURST_SECONDS_MIN = 8      # OR one unbroken stretch this long above BURST_SPEED_KMH

SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}


def _speed_summary(velocity_kmh: list[float], times: list[float] | None) -> dict | None:
    """Summarise a per-second speed stream: peak speed, seconds spent at vehicle
    speed, and the longest unbroken fast burst. Uses the time stream for accurate
    per-sample durations when present, else assumes 1 s per sample. Returns None
    when there's no stream."""
    if not velocity_kmh:
        return None
    n = len(velocity_kmh)
    durs = [1.0] * n
    if times and len(times) == n:
        for i in range(n - 1):
            dt = times[i + 1] - times[i]
            durs[i] = dt if 0 < dt < 60 else 1.0  # ignore long pause gaps

    vehicle_s = sum(d for v, d in zip(velocity_kmh, durs) if v > VEHICLE_SPEED_KMH)
    longest = cur = 0.0
    for v, d in zip(velocity_kmh, durs):
        cur = cur + d if v > BURST_SPEED_KMH else 0.0
        longest = max(longest, cur)

    return {
        "max_speed_kmh": round(max(velocity_kmh), 1),
        "vehicle_speed_s": round(vehicle_s),
        "longest_burst_s": round(longest),
    }


def _norm_type(activity_type: str) -> str:
    return " ".join(str(activity_type).split()).strip().lower()


def human_foot_speed_ceiling(distance_km: float) -> float:
    """Max average moving speed (km/h) a human could plausibly hold on foot over
    `distance_km`, via linear interpolation across HUMAN_FOOT_SPEED_CURVE."""
    pts = HUMAN_FOOT_SPEED_CURVE
    if distance_km <= pts[0][0]:
        return pts[0][1]
    if distance_km >= pts[-1][0]:
        return pts[-1][1]
    for (d0, s0), (d1, s1) in zip(pts, pts[1:]):
        if d0 <= distance_km <= d1:
            t = (distance_km - d0) / (d1 - d0)
            return round(s0 + t * (s1 - s0), 1)
    return pts[-1][1]


def _flag(code: str, severity: str, message: str) -> dict:
    return {"code": code, "severity": severity, "message": message}


def audit_activity(activity: dict) -> dict:
    """Return {metrics, flags, verdict} for one scraped activity dict."""
    atype = activity.get("activity_type", "")
    norm = _norm_type(atype)
    distance_km = parse_distance_km(activity.get("distance", ""))
    elevation_m = parse_elev_m(activity.get("elevation", ""))
    steps = parse_int(activity.get("steps", ""))
    moving_s = parse_duration_s(activity.get("moving_time", ""))
    elapsed_s = parse_duration_s(activity.get("elapsed_time", ""))

    speed_kmh = round(distance_km / (moving_s / 3600), 2) if moving_s > 0 else 0.0
    ceiling = human_foot_speed_ceiling(distance_km) if distance_km > 0 else 0.0
    elev_per_km = round(elevation_m / distance_km, 1) if distance_km > 0 else 0.0

    metrics = {
        "distance_km": distance_km,
        "elevation_m": elevation_m,
        "elev_per_km": elev_per_km,
        "steps": steps,
        "moving_time_s": moving_s,
        "elapsed_time_s": elapsed_s,
        "avg_speed_kmh": speed_kmh,
        "human_ceiling_kmh": ceiling,
    }

    flags: list[dict] = []

    # --- Already flagged on Strava --------------------------------------------
    if activity.get("is_flagged"):
        flags.append(_flag(
            "community_flagged", "high",
            "Strava already shows a 'this activity has been flagged' banner — the "
            "community or staff has flagged it as suspect.",
        ))

    # --- Recording device / upload source -------------------------------------
    # A live GPS recording leaves a device fingerprint; its absence means a
    # manual or imported (GPX) entry that can't be tied to a real recording.
    device = str(activity.get("device", "")).strip()
    if not device:
        flags.append(_flag(
            "no_device", "medium",
            "No recording device — likely a manual or imported entry, not a live "
            "GPS recording, so the route can't be trusted.",
        ))

    # --- Rule 1: allowed activity type ----------------------------------------
    if norm and norm not in ALLOWED_TYPES:
        flags.append(_flag(
            "disallowed_type", "high",
            f"Activity type '{atype}' is not allowed — only running, walking and "
            "hiking count.",
        ))
        is_foot = False
    else:
        is_foot = True  # allowed type, or blank (give benefit of the doubt)
        if not norm:
            flags.append(_flag(
                "unknown_type", "low",
                "Activity type couldn't be read from the page — can't confirm it's "
                "an allowed type.",
            ))

    # --- Rule 2: humanly possible (no vehicle) --------------------------------
    if is_foot and moving_s > 0 and distance_km > 0:
        if speed_kmh > ceiling:
            flags.append(_flag(
                "impossible_speed", "high",
                f"Avg speed {speed_kmh} km/h over {distance_km} km exceeds the "
                f"human ceiling on foot (~{ceiling} km/h) — consistent with a "
                "vehicle or GPS spoof, not a real effort.",
            ))
    elif is_foot and distance_km > 0 and moving_s == 0:
        flags.append(_flag(
            "no_moving_time", "medium",
            f"Logged {distance_km} km with no moving time — speed can't be "
            "checked; possible manual/edited entry.",
        ))

    # Per-second speed stream: catches vehicle use over part of the route, which a
    # low *average* speed (dragged down by stops) would otherwise hide.
    speed = _speed_summary(activity.get("velocity_kmh"), activity.get("stream_time"))
    if speed:
        metrics["max_speed_kmh"] = speed["max_speed_kmh"]
        metrics["vehicle_speed_s"] = speed["vehicle_speed_s"]
        metrics["longest_burst_s"] = speed["longest_burst_s"]
        if is_foot and (speed["vehicle_speed_s"] >= VEHICLE_SECONDS_MIN
                        or speed["longest_burst_s"] >= BURST_SECONDS_MIN):
            flags.append(_flag(
                "vehicle_speed", "high",
                f"Peaked at {speed['max_speed_kmh']} km/h and spent "
                f"{speed['vehicle_speed_s']}s above {int(VEHICLE_SPEED_KMH)} km/h — "
                "speeds only reachable in a vehicle, not on foot.",
            ))

    # Elapsed should never be shorter than moving time; if it is, the file was
    # likely edited.
    if elapsed_s and moving_s and elapsed_s < moving_s:
        flags.append(_flag(
            "elapsed_lt_moving", "medium",
            f"Elapsed time ({elapsed_s}s) is shorter than moving time "
            f"({moving_s}s) — the activity file may have been edited.",
        ))

    # Long stretches of "stopped" time inside a short effort: the athlete only
    # moved for a fraction of a much longer window. Big gaps can hide travel
    # between points (e.g. a lift between two walked segments).
    if moving_s > 0 and elapsed_s >= moving_s * 3 and (elapsed_s - moving_s) >= 1800:
        paused_min = round((elapsed_s - moving_s) / 60)
        moving_min = round(moving_s / 60)
        flags.append(_flag(
            "long_pause", "medium",
            f"Only {moving_min} min moving inside a {round(elapsed_s/60)} min window "
            f"({paused_min} min stopped) — long gaps can conceal travel between points.",
        ))

    # --- Verifiability (softer notes, not rule violations) --------------------
    if norm in GPS_TYPES and not activity.get("is_map_visible"):
        flags.append(_flag(
            "no_map", "medium",
            "No route map is visible — can't confirm the route wasn't covered by "
            "vehicle; may also be a manual entry.",
        ))
    if not activity.get("is_pace_visible"):
        flags.append(_flag("no_pace", "low", "Pace/speed is hidden."))
    if not activity.get("is_heart_rate_visible"):
        flags.append(_flag("no_heart_rate", "low", "Heart-rate data is hidden or absent."))
    if not activity.get("is_cadence_visible"):
        flags.append(_flag("no_cadence", "low", "Cadence data is hidden or absent."))

    if norm in STEP_EXPECTED_TYPES and distance_km >= 1 and steps == 0:
        flags.append(_flag(
            "walk_no_steps", "medium",
            f"{distance_km} km {norm} logged with zero steps — a real walk/hike "
            "records steps; zero suggests an imported or fabricated GPS route.",
        ))

    verdict = _verdict(flags)
    return {"metrics": metrics, "flags": flags, "verdict": verdict}


def _verdict(flags: list[dict]) -> str:
    """Roll the flags up into one label.
    FLAGGED (a high-severity flag), REVIEW (a medium), MINOR (only low), CLEAN."""
    if not flags:
        return "CLEAN"
    worst = max(SEVERITY_RANK[f["severity"]] for f in flags)
    return {3: "FLAGGED", 2: "REVIEW", 1: "MINOR"}[worst]


def format_report(activity: dict, result: dict) -> str:
    """Human-readable audit report for the console."""
    m = result["metrics"]
    lines = [
        "=" * 60,
        f"ACTIVITY AUDIT — {result['verdict']}",
        "=" * 60,
        f"Athlete : {activity.get('name', '?')}  (team: {activity.get('team', '?')})",
        f"Type    : {activity.get('activity_type') or '?'}",
        f"Date    : {activity.get('activity_date') or '?'}",
        f"Device  : {activity.get('device') or '(none)'}"
        f"{'   ⚑ FLAGGED ON STRAVA' if activity.get('is_flagged') else ''}",
        f"URL     : {activity.get('activity_url', '')}",
        "-" * 60,
        f"Distance: {m['distance_km']} km   Avg speed: {m['avg_speed_kmh']} km/h "
        f"(human ceiling ~{m['human_ceiling_kmh']} km/h)",
        (f"Max spd : {m['max_speed_kmh']} km/h   "
         f"{m['vehicle_speed_s']}s above {int(VEHICLE_SPEED_KMH)} km/h   "
         f"(longest burst {m['longest_burst_s']}s)"
         if "max_speed_kmh" in m else "Max spd : (speed stream unavailable)"),
        f"Moving  : {m['moving_time_s']}s   Elapsed: {m['elapsed_time_s']}s",
        f"Elev    : {m['elevation_m']} m ({m['elev_per_km']} m/km)   Steps: {m['steps']}",
        "-" * 60,
    ]
    if not result["flags"]:
        lines.append("No issues detected. Activity looks rule-abiding.")
    else:
        lines.append(f"{len(result['flags'])} flag(s):")
        for f in sorted(result["flags"], key=lambda x: -SEVERITY_RANK[x["severity"]]):
            lines.append(f"  [{f['severity'].upper():6}] {f['message']}")
    lines.append("=" * 60)
    return "\n".join(lines)


def audit_to_row(activity: dict, result: dict) -> dict:
    """Flatten an audit into one record for the audit log spreadsheet."""
    m = result["metrics"]
    return {
        "audited_at": None,  # filled by the caller (keeps this module import-light)
        "name": activity.get("name", ""),
        "team": activity.get("team", ""),
        "athlete_url": activity.get("athlete_url", ""),
        "activity_type": activity.get("activity_type", ""),
        "activity_date": activity.get("activity_date", ""),
        "device": activity.get("device", ""),
        "is_flagged": bool(activity.get("is_flagged")),
        "verdict": result["verdict"],
        "distance_km": m["distance_km"],
        "avg_speed_kmh": m["avg_speed_kmh"],
        "max_speed_kmh": m.get("max_speed_kmh", ""),
        "vehicle_speed_s": m.get("vehicle_speed_s", ""),
        "human_ceiling_kmh": m["human_ceiling_kmh"],
        "elev_per_km": m["elev_per_km"],
        "moving_time_s": m["moving_time_s"],
        "elapsed_time_s": m["elapsed_time_s"],
        "steps": m["steps"],
        "flag_count": len(result["flags"]),
        "flags": "; ".join(f"{f['severity']}:{f['code']}" for f in result["flags"]),
        "details": " | ".join(f["message"] for f in result["flags"]),
        "activity_url": activity.get("activity_url", ""),
    }
