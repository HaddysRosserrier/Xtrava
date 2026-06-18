import re
from playwright.sync_api import sync_playwright, BrowserContext

from scrapers.strava_scraper import (
    SESSION_FILE,
    _extract_team,
    _load_session,
    _do_login,
)
from scrapers.activities_scraper import (
    _parse_activity_detail,
    _text,
)
from settings import STRAVA_BASE_URL

# A single activity URL, e.g. https://www.strava.com/activities/1234567890.
ACTIVITY_URL_RE = re.compile(r"strava\.com/activities/(\d+)")

# A bare athlete-profile link, /athletes/<id> (no /training, /posts, etc. subpath).
ATHLETE_HREF_RE = re.compile(r"/athletes/(\d+)(?:[/?#]|$)")

# Strava's internal per-second data streams. Reachable only from within a loaded
# activity page in a *headed* browser — the data endpoint is bot-blocked (HTTP
# 403) for headless Chromium even though the page HTML loads fine. We pull the
# speed stream (m/s) + time stream to detect vehicle-speed segments that a low
# overall average hides.
STREAMS_JS = """
async (aid) => {
  const q = 'stream_types[]=velocity_smooth&stream_types[]=time';
  try {
    const r = await fetch('/activities/' + aid + '/streams?' + q, {
      headers: {'Accept': 'text/javascript, application/javascript',
                'X-Requested-With': 'XMLHttpRequest'},
      credentials: 'include'
    });
    if (!r.ok) return {ok: false, status: r.status};
    const j = await r.json();
    return {ok: true, velocity_smooth: j.velocity_smooth || null, time: j.time || null};
  } catch (e) {
    return {ok: false, error: String(e)};
  }
}
"""


def _fetch_speed_stream(page, activity_id: str) -> tuple[list[float], list[float], bool]:
    """Return (velocity_kmh, time_s, available). Empty + False when the streams
    endpoint is blocked or the activity has no speed stream."""
    try:
        res = page.evaluate(STREAMS_JS, activity_id)
    except Exception:
        return [], [], False
    if not res.get("ok") or not res.get("velocity_smooth"):
        if res.get("status") == 403:
            print("  (streams blocked — run with a headed browser to enable "
                  "vehicle-speed detection)")
        return [], [], False
    velocity_kmh = [v * 3.6 for v in res["velocity_smooth"]]
    return velocity_kmh, res.get("time") or [], True


def _goto_activity(page, url: str) -> None:
    """Load an activity page tolerantly. A headed browser's activity page never
    reaches 'networkidle' (the chart keeps polling), so we wait for the load
    event plus a short settle for the JS-injected stat flags to render."""
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    try:
        page.wait_for_load_state("load", timeout=15_000)
    except Exception:
        pass
    page.wait_for_timeout(2_500)


def _athlete(page) -> tuple[str, str]:
    """Return (owner_name, profile_url) for the activity.

    The owner is the athlete link inside the activity header (h2), NOT the page
    <h1>/title (that's the activity *title*, which athletes often set freely) and
    NOT the global nav (which links to the logged-in user's own profile). We scope
    to the activity-detail containers and require a bare /athletes/<id> href to
    skip nav links like '.../training/log'."""
    for sel in (
        "h2 a[href*='/athletes/']",
        ".details-container a[href*='/athletes/']",
        ".activity-summary a[href*='/athletes/']",
    ):
        for el in page.query_selector_all(sel):
            href = el.get_attribute("href") or ""
            name = _text(el)
            m = ATHLETE_HREF_RE.search(href)
            if m and name and "/training" not in href and "/posts" not in href:
                return name, f"{STRAVA_BASE_URL}/athletes/{m.group(1)}"
    # Fallback: title is "<Activity title> | <Type> | Strava" — not the owner, but
    # better than nothing when the header markup shifts.
    return page.title().split("|")[0].strip(), ""


def _device(page) -> str:
    """Recording device / upload source, e.g. 'Garmin fēnix 7' or 'Strava App'.
    Empty when the activity has no device metadata (often a manual/imported entry)."""
    el = page.query_selector(".device")
    if not el:
        return ""
    # The clean .device node is just the device name, but guard against markup that
    # folds gear in ('… Shoes: —').
    return _text(el).split("Shoes:")[0].strip()


def _is_flagged(page) -> bool:
    """True when Strava shows the 'This activity has been flagged' banner — i.e.
    the community or staff has already flagged it as suspect."""
    return "has been flagged" in (page.content() or "").lower()


def _activity_date(page) -> str:
    """Start date/time as Strava shows it on the activity page (free-form text)."""
    for sel in (".activity-summary time", "time[datetime]", "time"):
        el = page.query_selector(sel)
        if el:
            dt = el.get_attribute("datetime")
            if dt:
                return dt.strip()
            txt = _text(el)
            if txt:
                return txt
    return ""


def scrape_single_activity(url: str) -> dict:
    """Scrape one activity page into a flat dict of stats + visibility flags,
    ready for the audit rules. Reuses the club-feed detail scraper so the stat
    extraction stays in one place; adds the athlete name and date that the feed
    path already had from elsewhere."""
    match = ACTIVITY_URL_RE.search(url)
    if not match:
        raise ValueError(
            f"Not a Strava activity URL: {url!r}\n"
            "Expected something like https://www.strava.com/activities/1234567890"
        )
    activity_id = match.group(1)
    canonical = f"{STRAVA_BASE_URL}/activities/{activity_id}"

    with sync_playwright() as p:
        # Headed: the per-second streams endpoint (vehicle-speed detection) is
        # bot-blocked for headless Chromium. A window will briefly open.
        browser = p.chromium.launch(headless=False)
        context: BrowserContext = browser.new_context()

        if SESSION_FILE.exists():
            _load_session(context)

        page = context.new_page()

        if not SESSION_FILE.exists():
            _do_login(page, context)

        print(f"Loading activity: {canonical}")
        _goto_activity(page, canonical)

        if "login" in page.url:
            print("Session expired. Re-authenticating...")
            SESSION_FILE.unlink(missing_ok=True)
            _do_login(page, context)
            _goto_activity(page, canonical)

        detail = _parse_activity_detail(page)

        raw_name, athlete_url = _athlete(page)
        clean_name, team = _extract_team(raw_name)
        activity_date = _activity_date(page)
        device = _device(page)
        is_flagged = _is_flagged(page)
        velocity_kmh, stream_time, stream_available = _fetch_speed_stream(page, activity_id)

        browser.close()

    return {
        "activity_id": activity_id,
        "activity_url": canonical,
        "athlete_url": athlete_url,
        "name": clean_name,
        "team": team,
        "raw_name": raw_name,
        "activity_date": activity_date,
        "device": device,
        "is_flagged": is_flagged,
        "activity_type": detail.get("activity_type", ""),
        "distance": detail.get("distance", ""),
        "steps": detail.get("steps", ""),
        "elevation": detail.get("elevation", ""),
        "calories": detail.get("calories", ""),
        "moving_time": detail.get("moving_time", ""),
        "elapsed_time": detail.get("elapsed_time", ""),
        "pace": detail.get("pace", ""),
        "is_map_visible": detail["is_map_visible"],
        "is_pace_visible": detail["is_pace_visible"],
        "is_heart_rate_visible": detail["is_heart_rate_visible"],
        "is_cadence_visible": detail["is_cadence_visible"],
        # Per-second speed stream (km/h) + timestamps, for vehicle-speed analysis.
        # Empty when streams were blocked; the analyzer falls back to summary rules.
        "stream_available": stream_available,
        "velocity_kmh": velocity_kmh,
        "stream_time": stream_time,
    }


if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2:
        print("Usage: python -m scrapers.audit_scraper <activity_url>")
        raise SystemExit(1)

    print(json.dumps(scrape_single_activity(sys.argv[1]), indent=2, ensure_ascii=False))
