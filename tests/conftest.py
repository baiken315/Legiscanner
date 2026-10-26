"""Shared pytest fixtures for the Legiscanner test suite."""

from __future__ import annotations

import sqlite3

import pytest

from legiscanner import db


@pytest.fixture
def conn() -> sqlite3.Connection:
    """A fresh in-memory database with the full schema + migrations applied."""
    c = db.get_connection(":memory:")
    db.init_db(c)
    yield c
    c.close()


def make_bill(
    conn: sqlite3.Connection,
    session_id: int,
    bill_number: str,
    *,
    title: str = "",
    description: str = "",
    legiscan_bill_id: int | None = None,
    sponsors: list[str] | None = None,
    texts: list[dict] | None = None,
    tracking: dict | None = None,
) -> int:
    """Insert a bill (with optional child rows) and return its id.

    Helper for building deterministic fixtures. Uses db.upsert_bill so the full
    write path (texts by content hash, sponsors, tracking, FTS) is exercised.
    """
    bill_dict = {
        "bill_number": bill_number,
        "bill_id": legiscan_bill_id or 0,
        "change_hash": f"hash-{bill_number}",
        "title": title,
        "description": description,
        "status": "Active",
        "introduced_date": "2026-01-15",
        "last_action_date": "2026-03-01",
        "last_action": "Referred to committee",
        "legiscan_url": "https://legiscan.com/VA/text/HB1/2026",
        "state_url": "https://lis.virginia.gov/",
        "retrieved_at": "2026-03-01T00:00:00",
        "texts": texts or [],
        "sponsors": [
            {"name": s, "party": "D", "role": "Sponsor", "district": "1"} for s in (sponsors or [])
        ],
        "history": [{"date": "2026-01-15", "action": "Introduced", "chamber": "House"}],
        "tracking_metadata": {
            "ai_tags": [],
            "ai_reason": "",
            "ai_impact": None,
            "watching": False,
            "user_tags": [],
            "notes": "",
            "user_impact_override": None,
            **(tracking or {}),
        },
    }
    return db.upsert_bill(conn, session_id, bill_dict)
