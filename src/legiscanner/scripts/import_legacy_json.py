"""One-time import of a legacy combined.json file into the SQLite database.

Supersedes migrate.py: there is no more "JSON schema" to migrate between, so
this script's job is a one-time format change (JSON file -> SQL database),
not an ongoing concern. Safe to re-run (upserts are idempotent, keyed on
(session_id, bill_number)).
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .. import db

logger = logging.getLogger(__name__)

# Old flat schema (priority/tags/reason) -> ai_impact, for defensive fallback only.
# All current data is already on the new schema (verified against output/combined.json),
# but this keeps a bad/older input file from crashing the import.
PRIORITY_TO_IMPACT = {"high": 5, "medium": 3, "low": 2, "none": 1}


def _normalize_tracking_metadata(raw: dict) -> dict:
    if "ai_tags" in raw:
        return raw
    # Old schema fallback.
    old_tags = raw.get("tags", [])
    impact = PRIORITY_TO_IMPACT.get(raw.get("priority", "medium"), 3)
    if "non-land-use" in old_tags:
        impact = 1
    return {
        "ai_tags": old_tags,
        "ai_reason": raw.get("reason", ""),
        "ai_impact": impact,
        "watching": False,
        "user_tags": [],
        "notes": "",
        "user_impact_override": None,
    }


def import_combined_json(
    conn,
    combined_path: Path,
    *,
    state: str,
    session_name: str,
    year: int,
    batch_size: int = 100,
) -> dict:
    with open(combined_path) as f:
        data = json.load(f)

    bills = data.get("bills", [])
    session_id = db.get_or_create_session(conn, state=state, name=session_name, year=year)
    conn.commit()

    stats = {
        "bills": 0,
        "texts": 0,
        "sponsors": 0,
        "history": 0,
        "topics_created": set(),
        "watching": 0,
        "errors": [],
    }

    for i, bill in enumerate(bills):
        bill_number = bill.get("bill_number", "unknown")
        try:
            bill = dict(bill)  # shallow copy, don't mutate caller's data
            bill["tracking_metadata"] = _normalize_tracking_metadata(bill.get("tracking_metadata", {}))

            db.upsert_bill(conn, session_id, bill)

            stats["bills"] += 1
            stats["texts"] += len(bill.get("texts", []))
            stats["sponsors"] += len(bill.get("sponsors", []))
            stats["history"] += len(bill.get("history", []))
            stats["topics_created"].update(bill["tracking_metadata"].get("ai_tags", []))
            stats["topics_created"].update(bill["tracking_metadata"].get("user_tags", []))
            if bill["tracking_metadata"].get("watching"):
                stats["watching"] += 1
        except Exception as e:
            stats["errors"].append(f"{bill_number}: {e}")
            logger.error(f"Failed to import {bill_number}: {e}")

        if (i + 1) % batch_size == 0:
            conn.commit()
            logger.info(f"  ...{i + 1}/{len(bills)} bills imported")

    conn.commit()

    stats["topics_created"] = len(stats["topics_created"])
    stats["session_id"] = session_id
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a legacy combined.json into the Legiscanner SQLite DB")
    parser.add_argument("combined_json", type=Path, nargs="?", default=Path("output/combined.json"))
    parser.add_argument("--db", type=Path, default=Path("legiscanner.db"))
    parser.add_argument("--state", default="VA")
    parser.add_argument("--session-name", default="2026 Regular Session")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")

    if not args.combined_json.exists():
        print(f"Error: file not found: {args.combined_json}")
        return 1

    conn = db.get_connection(args.db)
    db.init_db(conn)

    print(f"Importing {args.combined_json} -> {args.db}")
    print(f"Session: {args.state} / {args.session_name} ({args.year})")
    print()

    stats = import_combined_json(
        conn,
        args.combined_json,
        state=args.state,
        session_name=args.session_name,
        year=args.year,
    )

    print()
    print(f"Bills imported:    {stats['bills']}")
    print(f"Texts inserted:    {stats['texts']}")
    print(f"Sponsors inserted: {stats['sponsors']}")
    print(f"History inserted:  {stats['history']}")
    print(f"Topics seeded:     {stats['topics_created']}")
    print(f"Watching:          {stats['watching']}")
    if stats["errors"]:
        print(f"Errors: {len(stats['errors'])}")
        for err in stats["errors"][:20]:
            print(f"  - {err}")

    conn.close()
    return 0 if not stats["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
