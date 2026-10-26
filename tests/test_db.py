"""Tests for db.py: sessions, upsert idempotency, filters, FTS, outcome triggers,
companions, lineage, and tracking history."""

from __future__ import annotations

import sqlite3

from legiscanner import db
from tests.conftest import make_bill


def test_init_db_is_idempotent(conn: sqlite3.Connection):
    """Running init_db twice must not error and must not duplicate data."""
    sid = db.get_or_create_session(conn, state="VA", name="2026 Regular Session", year=2026)
    make_bill(conn, sid, "HB1", title="A bill")
    conn.commit()

    db.init_db(conn)  # second run
    rows = conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
    assert rows == 1
    # schema_version is recorded (migration system ran)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 1


def test_get_or_create_session_make_active(conn: sqlite3.Connection):
    s1 = db.get_or_create_session(conn, state="VA", name="2025", year=2025)
    s2 = db.get_or_create_session(conn, state="VA", name="2026", year=2026)
    active = db.get_active_session(conn)
    assert active["id"] == s2
    # Both sessions exist
    assert db.get_session(conn, s1) is not None
    assert db.get_session(conn, s2) is not None


def test_set_active_session(conn: sqlite3.Connection):
    s1 = db.get_or_create_session(conn, state="VA", name="2025", year=2025, make_active=False)
    db.get_or_create_session(conn, state="VA", name="2026", year=2026)
    db.set_active_session(conn, s1)
    assert db.get_active_session(conn)["id"] == s1
    # Only one active at a time
    n_active = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE active = 1"
    ).fetchone()[0]
    assert n_active == 1


def test_upsert_bill_idempotent(conn: sqlite3.Connection):
    sid = db.get_or_create_session(conn, state="VA", name="2026", year=2026)
    bid1 = make_bill(
        conn, sid, "HB1", title="First title",
        texts=[{"version": "Introduced", "content": "v1"}],
    )
    bid2 = make_bill(
        conn, sid, "HB1", title="Updated title",
        texts=[{"version": "Introduced", "content": "v1"}],
    )
    conn.commit()
    # Same (session, bill_number) -> same row id
    assert bid1 == bid2
    assert conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0] == 1
    # Title updated
    assert (
        conn.execute("SELECT title FROM bills WHERE id = ?", (bid1,)).fetchone()["title"]
        == "Updated title"
    )


def test_upsert_bill_text_dedup_by_content_hash(conn: sqlite3.Connection):
    sid = db.get_or_create_session(conn, state="VA", name="2026", year=2026)
    bid = make_bill(
        conn, sid, "HB1",
        texts=[
            {"version": "Introduced", "content": "AAA"},
            {"version": "Amended", "content": "BBB"},
        ],
    )
    # Re-scrape with one identical + one new text
    make_bill(
        conn, sid, "HB1",
        texts=[
            {"version": "Introduced", "content": "AAA"},
            {"version": "Enrolled", "content": "CCC"},
        ],
    )
    conn.commit()
    # BBB was dropped (no longer published), AAA stable, CCC added
    rows = conn.execute(
        "SELECT content FROM bill_texts WHERE bill_id = ?", (bid,)
    ).fetchall()
    hashes = {r["content"] for r in rows}
    assert hashes == {"AAA", "CCC"}
    # AAA kept a row (stable identity via content hash)
    aaa_row = conn.execute(
        "SELECT id FROM bill_texts WHERE bill_id=? AND content='AAA'", (bid,)
    ).fetchone()
    assert aaa_row is not None


def test_list_bills_filters(conn: sqlite3.Connection):
    sid = db.get_or_create_session(conn, state="VA", name="2026", year=2026)
    make_bill(
        conn, sid, "HB1", title="Housing reform",
        tracking={"ai_impact": 5, "watching": True},
    )
    make_bill(conn, sid, "HB2", title="Taxes on carbon", tracking={"ai_impact": 2})
    make_bill(conn, sid, "SB1", title="Housing density", tracking={"ai_impact": 1})
    conn.commit()

    # watching_only
    watched = db.list_bills(conn, sid, watching_only=True)
    assert {b["bill_number"] for b in watched} == {"HB1"}

    # impact_levels
    high = db.list_bills(conn, sid, impact_levels={5})
    assert {b["bill_number"] for b in high} == {"HB1"}

    # hide_low_relevance (excludes impact 1, keeps null)
    no_low = db.list_bills(conn, sid, hide_low_relevance=True)
    no_low_numbers = {b["bill_number"] for b in no_low}
    assert "SB1" not in no_low_numbers
    assert "HB1" in no_low_numbers

    # sort by impact desc
    by_impact = db.list_bills(conn, sid, sort="impact")
    assert by_impact[0]["bill_number"] == "HB1"


def test_fts_search(conn: sqlite3.Connection):
    sid = db.get_or_create_session(conn, state="VA", name="2026", year=2026)
    make_bill(conn, sid, "HB1", title="Housing reform and rental standards")
    make_bill(conn, sid, "HB2", title="Corporate tax credits")
    conn.commit()

    results = db.list_bills(conn, sid, search="housing")
    assert {b["bill_number"] for b in results} == {"HB1"}

    # multi-term AND search
    results2 = db.list_bills(conn, sid, search="housing rental")
    assert {b["bill_number"] for b in results2} == {"HB1"}
    # a bill must match ALL terms
    results3 = db.list_bills(conn, sid, search="housing corporate")
    assert results3 == []


def test_outcome_score_trigger_syncs_ai_impact(conn: sqlite3.Connection):
    sid = db.get_or_create_session(conn, state="VA", name="2026", year=2026)
    bid = make_bill(conn, sid, "HB1")
    conn.commit()

    db.seed_outcome_areas(
        conn,
        sid,
        [
            {"slug": "housing", "name": "Housing", "description": ""},
            {"slug": "mobility", "name": "Mobility", "description": ""},
        ],
    )
    housing = db.get_or_create_outcome_area(conn, sid, "housing")
    mobility = db.get_or_create_outcome_area(conn, sid, "mobility")

    db.set_outcome_score(conn, bid, housing, ai_impact=3)
    db.set_outcome_score(conn, bid, mobility, ai_impact=5)
    conn.commit()

    # tracking.ai_impact should be MAX across areas = 5
    row = conn.execute("SELECT ai_impact FROM tracking WHERE bill_id = ?", (bid,)).fetchone()
    assert row["ai_impact"] == 5

    # Override the higher area down -> overall reflects the next highest
    db.set_outcome_score_override(conn, bid, mobility, 2)
    conn.commit()
    row = conn.execute(
        "SELECT ai_impact FROM tracking WHERE bill_id = ?", (bid,)
    ).fetchone()
    assert row["ai_impact"] == 3  # housing (3) now highest effective


def test_companions_within_session(conn: sqlite3.Connection):
    sid = db.get_or_create_session(conn, state="VA", name="2026", year=2026)
    make_bill(conn, sid, "HB123", title="Relating to housing density reform")
    make_bill(conn, sid, "SB456", title="Relating to housing density reform")
    conn.commit()

    from legiscanner.companions import update_companion_links_db

    count = update_companion_links_db(conn, sid, similarity_threshold=0.90)
    assert count == 1
    # HB123's companion is SB456
    bill = db.get_bill(conn, sid, "HB123")
    assert "SB456" in bill["companion_bills"]


def test_lineage_cross_session(conn: sqlite3.Connection):
    s2025 = db.get_or_create_session(
        conn, state="VA", name="2025", year=2025, make_active=False
    )
    s2026 = db.get_or_create_session(conn, state="VA", name="2026", year=2026)
    title = "Relating to accessory dwelling units"
    b25 = make_bill(conn, s2025, "HB100", title=title, sponsors=["Alice"])
    make_bill(conn, s2026, "HB200", title=title, sponsors=["Alice"])
    # A distinct bill that should NOT link (shares only the boilerplate prefix)
    make_bill(conn, s2026, "HB201", title="Relating to corporate tax credits")
    conn.commit()

    from legiscanner.lineages import update_lineage_links_db

    linked = update_lineage_links_db(conn, similarity_threshold=0.85)
    assert linked >= 2  # at least HB100 + HB200 linked

    lin = db.get_bill_lineage(conn, b25)
    assert lin is not None
    member_numbers = {m["bill_number"] for m in lin["members"]}
    assert {"HB100", "HB200"} <= member_numbers
    assert "HB201" not in member_numbers  # distinct bill not in this lineage

    # Bill belongs to exactly one lineage
    count = conn.execute(
        "SELECT COUNT(*) FROM bill_lineage_members WHERE bill_id = ?", (b25,)
    ).fetchone()[0]
    assert count == 1


def test_lineage_preserved_manual(conn: sqlite3.Connection):
    s2025 = db.get_or_create_session(
        conn, state="VA", name="2025", year=2025, make_active=False
    )
    s2026 = db.get_or_create_session(conn, state="VA", name="2026", year=2026)
    b25 = make_bill(conn, s2025, "HB100", title="Relating to ADU reform")
    b26 = make_bill(
        conn, s2026, "HB200", title="Totally different subject matter entirely"
    )
    conn.commit()

    # Manually link them (no title similarity)
    lid = db.add_lineage_member(conn, b25, link_reason="manual")
    db.add_lineage_member(conn, b26, lineage_id=lid, link_reason="manual")
    conn.commit()

    from legiscanner.lineages import update_lineage_links_db

    # Re-run auto linking; manual membership must survive (b26 has no title match)
    update_lineage_links_db(conn, similarity_threshold=0.85)
    lin = db.get_bill_lineage(conn, b25)
    member_numbers = {m["bill_number"] for m in lin["members"]}
    assert "HB200" in member_numbers


def test_tracking_history_recorded(conn: sqlite3.Connection):
    sid = db.get_or_create_session(conn, state="VA", name="2026", year=2026)
    bid = make_bill(conn, sid, "HB1", tracking={"notes": "initial note"})
    conn.commit()  # noqa: E501

    # upsert recorded a snapshot; now change notes -> trigger records another
    db.set_tracking(conn, bid, notes="updated note")
    conn.commit()

    history = db.get_tracking_history(conn, bid)
    notes = {h["notes"] for h in history}
    assert "initial note" in notes
    assert "updated note" in notes


def test_analysis_run_provenance(conn: sqlite3.Connection):
    sid = db.get_or_create_session(conn, state="VA", name="2026", year=2026)
    bid = make_bill(conn, sid, "HB1")
    conn.commit()

    db.record_analysis_run(
        conn, bid, sid, model="qwen3.6-35b-a3b", model_version="35b-a3b",
        tags=["housing"], reason="Relevant to housing.",
    )
    conn.commit()

    runs = db.get_analysis_runs(conn, bid)
    assert len(runs) == 1
    assert runs[0]["model"] == "qwen3.6-35b-a3b"
    assert runs[0]["reason"] == "Relevant to housing."
