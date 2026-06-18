import sys
from pathlib import Path
from datetime import datetime, date, timedelta

import pandas as pd

from scrapers.strava_scraper import scrape_leaderboard
from scrapers.activities_scraper import scrape_activities
from analysis.teams import build_athletes_df, build_teams_df
from analysis.activities import build_activities_df
from analysis.audit import format_report
from audit_service import audit_url, AUDIT_LOG_PATH
from settings import LEADERBOARD_URL, RECENT_ACTIVITY_URL

OUTPUT_DIR = Path("output")


def save_output(
    athletes_df: pd.DataFrame,
    teams_df: pd.DataFrame,
    last_athletes_df: pd.DataFrame,
    last_teams_df: pd.DataFrame,
    target: date,
) -> None:
    # One workbook per week (anchored on Monday). The leaderboard is weekly-
    # cumulative, so each run just refreshes the four snapshot sheets in place.
    monday = target - timedelta(days=target.weekday())
    xlsx_path = OUTPUT_DIR / f"leaderboard_week_{monday.isoformat()}.xlsx"

    if xlsx_path.exists():
        writer = pd.ExcelWriter(
            xlsx_path, engine="openpyxl", mode="a", if_sheet_exists="replace"
        )
    else:
        writer = pd.ExcelWriter(xlsx_path, engine="openpyxl", mode="w")
    with writer:
        athletes_df.to_excel(writer, sheet_name="This Week - Athletes", index=False)
        teams_df.to_excel(writer, sheet_name="This Week - Teams", index=False)
        last_athletes_df.to_excel(writer, sheet_name="Last Week - Athletes", index=False)
        last_teams_df.to_excel(writer, sheet_name="Last Week - Teams", index=False)

    print(f"\nSaved:\n  {xlsx_path}")


def save_activities(activities_df: pd.DataFrame, target: date) -> None:
    # One workbook per week (anchored on Monday), one sheet per day. Re-running
    # for a day replaces just that day's sheet, leaving the rest of the week intact.
    monday = target - timedelta(days=target.weekday())
    xlsx_path = OUTPUT_DIR / f"activities_week_{monday.isoformat()}.xlsx"
    sheet_name = target.strftime("%a %Y-%m-%d")

    if xlsx_path.exists():
        writer = pd.ExcelWriter(
            xlsx_path, engine="openpyxl", mode="a", if_sheet_exists="replace"
        )
    else:
        writer = pd.ExcelWriter(xlsx_path, engine="openpyxl", mode="w")
    with writer:
        activities_df.to_excel(writer, sheet_name=sheet_name, index=False)

    print(f"\nSaved:\n  {xlsx_path} (sheet '{sheet_name}')")


def run_activities(target: date | None = None) -> None:
    target = target or datetime.now().date()
    activities = scrape_activities(RECENT_ACTIVITY_URL, target)
    if not activities:
        print(f"No activities scraped for {target.isoformat()}. Check selectors, login state, or that the day is recent enough to still be in the feed.")
        return

    activities_df = build_activities_df(activities)
    print(f"\n=== {target.isoformat()} — ACTIVITIES ===")
    print(activities_df.to_string(index=False))
    save_activities(activities_df, target)


def run_audit(url: str) -> None:
    out = audit_url(url)
    print("\n" + format_report(out["activity"], out["result"]))
    print(f"\nLogged audit to {AUDIT_LOG_PATH}")


def print_last_week(rows: list[dict]) -> None:
    if not rows:
        print("  (no data)")
        return

    athletes_df = build_athletes_df(rows)
    teams_df = build_teams_df(athletes_df)

    print("\n=== LAST WEEK — ATHLETES ===")
    print(athletes_df.to_string(index=False))
    print("\n=== LAST WEEK — TEAM TOTALS ===")
    print(teams_df.to_string(index=False))


def run_leaderboard() -> None:
    this_week, last_week = scrape_leaderboard(LEADERBOARD_URL)

    if not this_week:
        print("No data scraped. Check selectors or login state.")
        return

    athletes_df = build_athletes_df(this_week)
    teams_df = build_teams_df(athletes_df)

    last_athletes_df = build_athletes_df(last_week) if last_week else pd.DataFrame()
    last_teams_df = build_teams_df(last_athletes_df) if not last_athletes_df.empty else pd.DataFrame()
    print_last_week(last_week)

    
    print("\n=== THIS WEEK — ATHLETES ===")
    print(athletes_df.to_string(index=False))
    print("\n=== THIS WEEK — TEAM TOTALS ===")
    print(teams_df.to_string(index=False))

    save_output(athletes_df, teams_df, last_athletes_df, last_teams_df, datetime.now().date())


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "leaderboard"

    if mode not in ("leaderboard", "activities", "audit", "all"):
        print(f"Unknown mode '{mode}'. Use: leaderboard | activities [YYYY-MM-DD] | audit <activity_url> | all")
        return

    # Audit one activity: `python main.py audit https://www.strava.com/activities/123`.
    if mode == "audit":
        if len(sys.argv) < 3:
            print("Usage: python main.py audit <activity_url>")
            return
        run_audit(sys.argv[2])
        return

    # Optional day for activities scraping, e.g. `python main.py activities 2026-06-14`.
    target: date | None = None
    if len(sys.argv) > 2:
        try:
            target = date.fromisoformat(sys.argv[2])
        except ValueError:
            print(f"Invalid date '{sys.argv[2]}'. Use YYYY-MM-DD.")
            return

    if mode in ("leaderboard", "all"):
        run_leaderboard()
    if mode in ("activities", "all"):
        run_activities(target)


if __name__ == "__main__":
    main()
