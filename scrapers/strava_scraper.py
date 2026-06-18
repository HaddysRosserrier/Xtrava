import re
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, BrowserContext, Page

from settings import STRAVA_BASE_URL, LOGIN_URL

SESSION_FILE = Path("session.json")

TEAM_PATTERN = re.compile(r"#(.+)$")

EMOJI_TO_TEAM = {
    "🔴": "Red", "❤️": "Red", "♥️": "Red", "♥": "Red", "👹": "Red", "❤️":"Red", "🟠": "Orange", "🟡": "Yellow", "🟢": "Green",
    "🔵": "Blue", "💙": "Blue", "🟣": "Purple", "💜": "Purple",
    "💚": "Green", "💛": "Yellow",
    "⚫": "Black", "⚪": "White", "🟤": "Brown",
}


def _extract_team(name: str) -> tuple[str, str]:
    """Return (clean_name, team) by parsing '#<emoji>' suffix from athlete name."""
    match = TEAM_PATTERN.search(name)
    if match:
        tag = match.group(1).strip()
        clean = TEAM_PATTERN.sub("", name).strip()
        # Substring match so multi-char emoji (e.g. ❤️ = U+2764 + U+FE0F) are found whole
        for emoji, team in EMOJI_TO_TEAM.items():
            if emoji in tag:
                return clean, team
        return clean, tag.split()[0].title()  # fallback: first word only, normalised case
    return name.strip(), "No Team"


def _save_session(context: BrowserContext) -> None:
    SESSION_FILE.write_text(json.dumps(context.storage_state()))


def _load_session(context: BrowserContext) -> None:
    state = json.loads(SESSION_FILE.read_text())
    context.add_cookies(state.get("cookies", []))


def _do_login(page: Page, context: BrowserContext) -> None:
    print("Session not found. Opening browser for manual login...")
    print("Please log in to Strava in the browser window, then wait.")
    page.goto(LOGIN_URL)
    page.wait_for_url(re.compile(r"strava\.com/(dashboard|athlete|feed|clubs)"), timeout=120_000)
    _save_session(context)
    print("Session saved. Future runs will skip login.")


def scrape_leaderboard(url: str) -> tuple[list[dict], list[dict]]:
    """Return (this_week_rows, last_week_rows)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=SESSION_FILE.exists())
        context = browser.new_context()

        if SESSION_FILE.exists():
            _load_session(context)

        page = context.new_page()

        if not SESSION_FILE.exists():
            _do_login(page, context)

        print(f"Loading leaderboard: {url}")
        page.goto(url, wait_until="networkidle")

        if "login" in page.url:
            print("Session expired. Re-authenticating...")
            SESSION_FILE.unlink(missing_ok=True)
            _do_login(page, context)
            page.goto(url, wait_until="networkidle")

        this_week = _parse_this_week(page)
        last_week = _parse_last_week(page)
        browser.close()

    return this_week, last_week


def _cell_text(el) -> str:
    return " ".join(el.inner_text().split()) if el else ""


def _parse_this_week(page: Page) -> list[dict]:
    page.wait_for_selector("div.leaderboard table.dense", timeout=15_000)

    athletes = []

    for row in page.query_selector_all("div.leaderboard table.dense tbody tr"):
        rank_el = row.query_selector("td.rank")
        name_el = row.query_selector("td.athlete a.athlete-name")

        if not name_el:
            continue

        raw_name = name_el.inner_text().strip()
        clean_name, team = _extract_team(raw_name)
        rank = int(rank_el.inner_text().strip()) if rank_el else 0

        # The athlete-name link points at the Strava profile (e.g. /athletes/123).
        # Make it absolute so the dashboard can link straight to it.
        href = name_el.get_attribute("href") or ""
        athlete_url = f"{STRAVA_BASE_URL}{href}" if href.startswith("/") else href

        athletes.append({
            "rank": rank,
            "name": clean_name,
            "team": team,
            "athlete_url": athlete_url,
            "raw_name": raw_name,
            "distance_raw": _cell_text(row.query_selector("td.distance")),
            "activities_raw": _cell_text(row.query_selector("td.num_activities")),
            "elev_gain_raw": _cell_text(row.query_selector("td.elev_gain")),
            "time_raw": _cell_text(row.query_selector("td.moving_time")),
        })

    return athletes


def _parse_last_week(page: Page) -> list[dict]:
    # Click the "Last Week" tab to swap the full table to last week's data
    tab = page.query_selector("span.button.last-week")
    if not tab:
        return []

    tab.click()

    # Wait for heading to say "Last Week"
    page.wait_for_function(
        "document.querySelector('h2#leaderboard-heading')?.textContent?.includes('Last Week')",
        timeout=10_000,
    )
    # Wait for network to settle (XHR table reload)
    page.wait_for_load_state("networkidle", timeout=10_000)

    # If the top athlete hasn't changed (same people both weeks), skip the name check
    # Either way, scrape whatever the table now shows
    return _parse_this_week(page)
