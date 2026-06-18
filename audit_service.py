"""Shared audit orchestration: scrape one activity, run the rules, persist the
result. Used by both the CLI (`main.py audit`) and the webapp's Auto Audit button
so the two stay in lockstep."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from scrapers.audit_scraper import scrape_single_activity
from analysis.audit import audit_activity, audit_to_row

# Absolute so it resolves to the same workbook regardless of the caller's cwd.
OUTPUT_DIR = Path(__file__).resolve().parent / "output"
AUDIT_LOG_PATH = OUTPUT_DIR / "activity_audits.xlsx"
AUDIT_LOG_SHEET = "Audits"


def save_audit_log(row: dict) -> Path:
    """Upsert one audit row into the audit workbook, keyed by activity_url, so
    re-auditing the same activity replaces its prior row. Returns the file path."""
    OUTPUT_DIR.mkdir(exist_ok=True)

    existing = pd.DataFrame()
    if AUDIT_LOG_PATH.exists():
        try:
            existing = pd.read_excel(AUDIT_LOG_PATH, sheet_name=AUDIT_LOG_SHEET)
        except ValueError:
            existing = pd.DataFrame()
        if "activity_url" in existing.columns:
            existing = existing[existing["activity_url"] != row["activity_url"]]

    df = pd.concat([existing, pd.DataFrame([row])], ignore_index=True)
    with pd.ExcelWriter(AUDIT_LOG_PATH, engine="openpyxl", mode="w") as writer:
        df.to_excel(writer, sheet_name=AUDIT_LOG_SHEET, index=False)
    return AUDIT_LOG_PATH


def audit_url(url: str) -> dict:
    """Scrape, audit and log one activity. Returns {activity, result, row}."""
    activity = scrape_single_activity(url)
    result = audit_activity(activity)
    row = audit_to_row(activity, result)
    row["audited_at"] = datetime.now().isoformat(timespec="seconds")
    save_audit_log(row)
    return {"activity": activity, "result": result, "row": row}
