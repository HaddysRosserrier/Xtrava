import re
import json
import calendar
from datetime import datetime, date, timedelta

from playwright.sync_api import sync_playwright, BrowserContext, Page

from scrapers.strava_scraper import (
    SESSION_FILE,
    _extract_team,
    _load_session,
    _do_login,
)
from settings import STRAVA_BASE_URL, RECENT_ACTIVITY_URL

# RECENT_ACTIVITY_URL (the club's recent-activity feed) comes from settings/env.
# We read the feed to discover that day's activities, then open each activity
# page to pull the full stat set (steps / calories / heart-rate / cadence only
# exist on the detail page).
ACTIVITY_HREF_RE = re.compile(r"/activities/(\d+)")

# Month name/abbr -> number, for parsing absolute feed dates ("June 15, 2026").
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})

# Clock time in a feed timestamp; absent when the athlete hides their start time.
FEED_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(am|pm)?")

# HR/cadence values are JS-rendered into the analysis chart, never in the static
# HTML. The chart-builder config does declare which streams the activity exposes,
# so we read visibility from there instead.
SHOW_STATS_RE = re.compile(r"showStats\((\{[^}]*\})\)")
HAS_CADENCE_RE = re.compile(r"hasCadenceOrTemp\(true\)")

# Stat labels as they appear on an activity page -> our output keys. Matching is
# case-insensitive substring, so "Avg Pace" still maps to "pace".
STAT_LABELS = {
    "distance": "distance",
    "steps": "steps",
    "elapsed time": "elapsed_time",
    "moving time": "moving_time",
    "elevation": "elevation",
    "elev gain": "elevation",
    "calories": "calories",
    "pace": "pace",
    "heart rate": "heart_rate",
    "cadence": "cadence",
}


def _text(el) -> str:
    return " ".join(el.inner_text().split()) if el else ""


def _parse_feed_datetime(text: str, today: date) -> datetime | None:
    """Parse Strava feed timestamps like 'Today at 12:00 PM', 'Yesterday at
    6:30 AM', or 'June 15, 2026 at 7:00 AM' into a datetime."""
    low = " ".join(text.split()).lower()

    hh, mm = 0, 0
    tm = FEED_TIME_RE.search(low)
    if tm:
        hh, mm = int(tm.group(1)), int(tm.group(2))
        ap = tm.group(3)
        if ap == "pm" and hh != 12:
            hh += 12
        elif ap == "am" and hh == 12:
            hh = 0

    if "today" in low:
        day = today
    elif "yesterday" in low:
        day = today - timedelta(days=1)
    else:
        dm = re.search(r"([a-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?", low)
        month = MONTHS.get(dm.group(1)) if dm else None
        if not month:
            return None
        year = int(dm.group(3)) if dm.group(3) else today.year
        try:
            day = date(year, month, int(dm.group(2)))
        except ValueError:
            return None

    return datetime(day.year, day.month, day.day, hh, mm)


def _icon_title(icon) -> str:
    """Activity type lives in the feed icon's SVG <title> (e.g. 'Walk')."""
    if not icon:
        return ""
    title = icon.query_selector("title")
    return title.text_content().strip() if title else ""


def _entry_date_text(entry) -> str:
    el = entry.query_selector("[data-testid='date_at_time']")
    return _text(el) if el else ""


def _entry_activities(entry, now: date) -> list[dict]:
    """Parse one feed entry into its activities. A grouped post can hold several,
    so this returns a list of {name, team, raw_name, activity_type, activity_url,
    activity_id, activity_dt, time_visible}; `activity_dt` may be None when the
    date label can't be parsed. No date filtering — callers decide what to keep.

    The athlete-profile "Recent Activities" section reuses the same feed-entry
    component as the club feed, so this is shared by both scrapers."""
    date_text = _entry_date_text(entry)
    activity_dt = _parse_feed_datetime(date_text, now) if date_text else None
    time_visible = bool(FEED_TIME_RE.search(date_text.lower()))

    name_el = entry.query_selector("[data-testid='owners-name']")
    raw_name = _text(name_el)
    clean_name, team = _extract_team(raw_name)

    # One entry can hold several activities (grouped post); pair each activity
    # link with the type icon at the same index.
    links = entry.query_selector_all("[data-testid='activity_name']")
    icons = entry.query_selector_all("[data-testid='activity-icon']")
    out: list[dict] = []
    for i, link in enumerate(links):
        match = ACTIVITY_HREF_RE.search(link.get_attribute("href") or "")
        if not match:
            continue
        icon = icons[i] if i < len(icons) else None
        out.append({
            "name": clean_name,
            "team": team,
            "raw_name": raw_name,
            "activity_type": _icon_title(icon),
            "activity_url": f"{STRAVA_BASE_URL}/activities/{match.group(1)}",
            "activity_id": match.group(1),
            "activity_dt": activity_dt,
            "time_visible": time_visible,
        })
    return out


def _lazy_load_feed(page: Page, cutoff: date, now: date) -> None:
    """Scroll the lazy-loaded, newest-first feed until the oldest loaded entry
    predates `cutoff` (so everything on/after it is loaded), or the feed stops
    growing after several no-growth passes. Bringing the last entry into view is
    what fires the next batch; the first batch lags, so be patient."""
    stale = 0
    for _ in range(120):
        entries = page.query_selector_all("[data-testid='web-feed-entry']")
        oldest = _parse_feed_datetime(_entry_date_text(entries[-1]), now) if entries else None
        if oldest and oldest.date() < cutoff:
            break

        if entries:
            try:
                entries[-1].scroll_into_view_if_needed(timeout=4000)
            except Exception:
                pass
        page.wait_for_timeout(1800)

        # Async loads (plus an initial lag); only give up after several stale passes.
        if len(page.query_selector_all("[data-testid='web-feed-entry']")) == len(entries):
            stale += 1
            if stale >= 6:
                break
        else:
            stale = 0


def _collect_feed_entries(page: Page, target: date, now: date) -> list[dict]:
    """Return the activities in the feed posted on `target`. `now` is the real
    current date, used to resolve relative labels like 'Today' / 'Yesterday'."""
    page.wait_for_selector("[data-testid='web-feed-entry']", timeout=15_000)
    _lazy_load_feed(page, target, now)

    found: list[dict] = []
    seen_ids: set[str] = set()
    for entry in page.query_selector_all("[data-testid='web-feed-entry']"):
        for act in _entry_activities(entry, now):
            if act["activity_dt"] is None or act["activity_dt"].date() != target:
                continue
            if act["activity_id"] in seen_ids:
                continue
            seen_ids.add(act["activity_id"])
            found.append(act)
    return found


def _stats_map(page: Page) -> dict[str, str]:
    """Scrape every label/value pair from an activity page into a flat map."""
    stats: dict[str, str] = {}

    # Inline stats: value in <strong>, label in <div class="label">.
    for li in page.query_selector_all("ul.inline-stats li"):
        value = _text(li.query_selector("strong"))
        label = _text(li.query_selector(".label")).lower()
        if value and label:
            stats[label] = value

    # "More stats": a <div> grid of alternating label / value cells.
    more = page.query_selector("div.more-stats")
    if more:
        for row in more.query_selector_all(".row"):
            label = ""
            for cell in row.query_selector_all(":scope > div"):
                strong = cell.query_selector("strong")
                if strong:
                    if label:
                        stats[label] = _text(strong)
                else:
                    label = _text(cell).lower()

    return stats


def _match_stat(stats: dict[str, str], needle: str) -> str:
    for label, value in stats.items():
        if needle in label:
            return value
    return ""


def _activity_type_from_title(page: Page) -> str:
    """Detail page title is '<Name> | <Type> | Strava'; pull the type segment."""
    parts = [p.strip() for p in page.title().split("|")]
    return parts[-2] if len(parts) >= 3 else ""


def _parse_activity_detail(page: Page) -> dict:
    """Pull the stat/visibility detail out of an already-loaded activity page."""
    stats = _stats_map(page)

    detail: dict = {"activity_type": _activity_type_from_title(page)}
    for needle, key in STAT_LABELS.items():
        if not detail.get(key):
            detail[key] = _match_stat(stats, needle)

    # A stat/map is "visible" when it's actually rendered on the page.
    detail["is_map_visible"] = bool(
        page.query_selector("#map-canvas, .activity-map, [data-testid='map']")
    )
    detail["is_pace_visible"] = bool(detail.get("pace"))

    # HR/cadence aren't in the static stats blocks; read the chart-builder flags.
    content = page.content()
    show_stats: dict = {}
    m = SHOW_STATS_RE.search(content)
    if m:
        try:
            show_stats = json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    detail["is_heart_rate_visible"] = bool(show_stats.get("heartrate"))
    detail["is_cadence_visible"] = bool(HAS_CADENCE_RE.search(content))
    return detail


def _scrape_activity_detail(page: Page, url: str) -> dict:
    """Navigate to an activity page and parse its stat/visibility detail."""
    page.goto(url, wait_until="networkidle")
    return _parse_activity_detail(page)


def scrape_single_activity(url: str) -> dict | None:
    """Re-fetch one activity's detail straight from its URL (no feed crawl).

    Returns one of:
      - {"deleted": True}            if the activity no longer exists (HTTP 4xx),
      - the stat/visibility detail   (distance, steps, moving_time, elevation,
                                      calories, elapsed_time, pace, activity_type
                                      and the four is_*_visible flags),
      - None                         if the page couldn't be loaded at all.
    Re-auths and retries once if the saved session has expired."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=SESSION_FILE.exists())
        context: BrowserContext = browser.new_context()

        if SESSION_FILE.exists():
            _load_session(context)

        page = context.new_page()

        if not SESSION_FILE.exists():
            _do_login(page, context)

        response = page.goto(url, wait_until="networkidle")

        # Session expired mid-request: re-authenticate and load once more.
        if "login" in page.url:
            SESSION_FILE.unlink(missing_ok=True)
            _do_login(page, context)
            response = page.goto(url, wait_until="networkidle")

        # A deleted (or otherwise gone) activity answers with a 4xx — most often
        # 404. Treat that as "deleted" rather than a transient scrape failure.
        if response is not None and response.status >= 400:
            browser.close()
            return {"deleted": True, "status": response.status}

        detail = _parse_activity_detail(page)
        browser.close()

    return detail or None


def _build_row(item: dict, detail: dict) -> dict:
    """Combine a feed item with its scraped detail into one raw activity row, in
    the shape build_activities_df() expects. Shared by the club-feed and
    athlete-profile scrapers so both produce identical workbook rows."""
    return {
        "name": item["name"],
        "group": item["team"],
        "activity_date": item["activity_dt"].date().isoformat(),
        "activity_time": item["activity_dt"].strftime("%H:%M"),
        "activity_type": item["activity_type"] or detail.get("activity_type", ""),
        "distance": detail.get("distance", ""),
        "steps": detail.get("steps", ""),
        "time": detail.get("moving_time", ""),
        "elevation": detail.get("elevation", ""),
        "calories": detail.get("calories", ""),
        "elapsed_time": detail.get("elapsed_time", ""),
        "moving_time": detail.get("moving_time", ""),
        "pace": detail.get("pace", ""),
        "is_time_visible": item["time_visible"],
        "is_map_visible": detail["is_map_visible"],
        "is_heart_rate_visible": detail["is_heart_rate_visible"],
        "is_pace_visible": detail["is_pace_visible"],
        "is_cadence_visible": detail["is_cadence_visible"],
        "activity_url": item["activity_url"],
    }


def scrape_activities(url: str = RECENT_ACTIVITY_URL, target: date | None = None) -> list[dict]:
    """Scrape all club activities posted on `target` (defaults to today)."""
    now = datetime.now().date()
    target = target or now

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=SESSION_FILE.exists())
        context: BrowserContext = browser.new_context()

        if SESSION_FILE.exists():
            _load_session(context)

        page = context.new_page()

        if not SESSION_FILE.exists():
            _do_login(page, context)

        print(f"Loading recent activity: {url}")
        page.goto(url, wait_until="networkidle")

        if "login" in page.url:
            print("Session expired. Re-authenticating...")
            SESSION_FILE.unlink(missing_ok=True)
            _do_login(page, context)
            page.goto(url, wait_until="networkidle")

        feed = _collect_feed_entries(page, target, now)
        print(f"Found {len(feed)} activities for {target.isoformat()}.")

        rows: list[dict] = []
        for item in feed:
            print(f"  -> {item['name']} ({item['activity_type']}): {item['activity_url']}")
            detail = _scrape_activity_detail(page, item["activity_url"])
            rows.append(_build_row(item, detail))

        browser.close()

    return rows


def scrape_athlete_week(
    athlete_url: str, week_monday: date, skip_urls: set[str] | None = None
) -> list[dict]:
    """Scrape a single athlete's *public* recent activities that fall within the
    week starting `week_monday` (Mon..Sun), returning raw rows in the same shape
    as scrape_activities(). `skip_urls` are activity URLs we already have, so we
    don't re-scrape them.

    Only activities the saved session is allowed to see are returned — i.e. the
    athlete's visibility is "Everyone", or you follow them. A private/hidden
    profile (no visible recent activity) yields an empty list rather than error.

    NOTE: this reads the profile's "Recent Activities" section, which reuses the
    club feed's entry component. If Strava changes that layout, the selectors in
    _entry_activities / _lazy_load_feed are the place to adjust.
    """
    skip = set(skip_urls or ())
    now = datetime.now().date()
    monday = week_monday - timedelta(days=week_monday.weekday())  # normalise
    sunday = monday + timedelta(days=6)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=SESSION_FILE.exists())
        context: BrowserContext = browser.new_context()

        if SESSION_FILE.exists():
            _load_session(context)

        page = context.new_page()

        if not SESSION_FILE.exists():
            _do_login(page, context)

        print(f"Loading athlete profile: {athlete_url}")
        page.goto(athlete_url, wait_until="networkidle")

        if "login" in page.url:
            print("Session expired. Re-authenticating...")
            SESSION_FILE.unlink(missing_ok=True)
            _do_login(page, context)
            page.goto(athlete_url, wait_until="networkidle")

        # No visible recent-activity feed → private/hidden profile (or none this
        # week). Treat as "nothing to add" rather than a scrape failure.
        try:
            page.wait_for_selector("[data-testid='web-feed-entry']", timeout=15_000)
        except Exception:
            print("No visible recent activities (profile may be private).")
            browser.close()
            return []

        _lazy_load_feed(page, monday, now)

        wanted: list[dict] = []
        seen_ids: set[str] = set()
        for entry in page.query_selector_all("[data-testid='web-feed-entry']"):
            for act in _entry_activities(entry, now):
                day = act["activity_dt"].date() if act["activity_dt"] else None
                if day is None or not (monday <= day <= sunday):
                    continue
                if act["activity_id"] in seen_ids or act["activity_url"] in skip:
                    continue
                seen_ids.add(act["activity_id"])
                wanted.append(act)

        print(f"Found {len(wanted)} new activities for week of {monday.isoformat()}.")

        rows: list[dict] = []
        for item in wanted:
            print(f"  -> {item['activity_type']}: {item['activity_url']}")
            detail = _scrape_activity_detail(page, item["activity_url"])
            rows.append(_build_row(item, detail))

        browser.close()

    return rows


if __name__ == "__main__":
    import json

    activities = scrape_activities()
    print(json.dumps(activities, indent=2, ensure_ascii=False))
