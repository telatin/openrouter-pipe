#!/usr/bin/env python3
"""Print the cost ledger as CSV: one line per user for a month, or every request with --detail.

    python scripts/ledger_report.py --db-url sqlite:////path/webui.db --month 2026-10
    python scripts/ledger_report.py --db-url mysql+pymysql://u:p@host/db --month 2026-10 --detail
"""

from __future__ import annotations

import argparse
import csv
import datetime
import importlib.util
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine

_LEDGER_PATH = (
    Path(__file__).resolve().parent.parent
    / "open_webui_openrouter_pipe" / "plugins" / "cost_governor" / "ledger.py"
)
_spec = importlib.util.spec_from_file_location("cost_governor_ledger", _LEDGER_PATH)
assert _spec is not None and _spec.loader is not None
_ledger_module = importlib.util.module_from_spec(_spec)
sys.modules["cost_governor_ledger"] = _ledger_module
_spec.loader.exec_module(_ledger_module)
LEDGER_ROW_FIELDS = _ledger_module.LEDGER_ROW_FIELDS
CostLedger = _ledger_module.CostLedger


def month_bounds(month: str, tz_name: str) -> tuple[int, int]:
    year, mon = (int(part) for part in month.split("-"))
    zone = ZoneInfo(tz_name)
    start = datetime.datetime(year, mon, 1, tzinfo=zone)
    end = datetime.datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=zone)
    return int(start.timestamp()), int(end.timestamp())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-url", required=True)
    parser.add_argument("--month", default=datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m"), help="YYYY-MM")
    parser.add_argument("--tz", default="UTC", help="IANA zone deciding the month boundary")
    parser.add_argument("--detail", action="store_true", help="one line per request")
    parser.add_argument("--user-id", default=None)
    args = parser.parse_args()

    start, end = month_bounds(args.month, args.tz)
    ledger = CostLedger(create_engine(args.db_url))
    writer = csv.writer(sys.stdout)
    if args.detail:
        writer.writerow(LEDGER_ROW_FIELDS)
        for row in ledger.rows(start, end, args.user_id):
            writer.writerow([row[name] for name in LEDGER_ROW_FIELDS])
    else:
        writer.writerow(["user_id", "user_name", "user_email", "requests", "tokens_in", "tokens_out", "cost_usd"])
        for row in ledger.spend_by_user(start, end):
            if args.user_id and row["user_id"] != args.user_id:
                continue
            writer.writerow([
                row["user_id"], row["user_name"], row["user_email"], row["requests"],
                row["tokens_in"] or 0, row["tokens_out"] or 0, f"{row['cost'] or 0:.6f}",
            ])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
