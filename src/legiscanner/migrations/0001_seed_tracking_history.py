"""Migration 1: seed tracking_history from the current tracking table.

The tracking_history table (and its AFTER UPDATE trigger) are created by
schema.sql. This migration records a single baseline snapshot of every bill's
current tracking state so the audit trail has a starting point on a
pre-existing database. Fresh installs get no rows here (there is no prior
state to preserve), which is correct.

Idempotent: guarded so re-running never duplicates the baseline row for a
bill.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT bill_id, watching, notes, ai_reason, ai_impact, user_impact_override "
        "FROM tracking"
    ).fetchall()
    for r in rows:
        conn.execute(
            "INSERT INTO tracking_history "
            "(bill_id, watching, notes, ai_reason, ai_impact, user_impact_override, changed_at) "
            "SELECT ?, ?, ?, ?, ?, ?, (SELECT updated_at FROM tracking WHERE bill_id = ?) "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM tracking_history th WHERE th.bill_id = ?)",
            (
                r["bill_id"],
                r["watching"],
                r["notes"],
                r["ai_reason"],
                r["ai_impact"],
                r["user_impact_override"],
                r["bill_id"],
                r["bill_id"],
            ),
        )
    conn.commit()
