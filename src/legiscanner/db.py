"""SQLite connection and query/mutation layer for Legiscanner.

All bill data lives in one SQLite database (see schema.sql). This module is the
single place that knows how to turn DB rows into the BillOutput-shaped dicts the
web templates and CLI expect, and the only place that builds dynamic filter SQL
for the web app's list/search/sort behavior.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from .models import (
    BillOutput,
    HistoryOutput,
    OutcomeScoreOutput,
    SponsorOutput,
    TextVersionOutput,
    TrackingMetadata,
)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with the pragmas this app relies on.

    WAL mode matters here: the web app and a background pipeline-trigger thread
    can both hold the DB open concurrently, and WAL allows concurrent readers
    during a writer instead of raising "database is locked".

    check_same_thread=False: FastAPI's sync (`def`) dependencies and route handlers
    each run via Starlette's threadpool, which does not guarantee a single request's
    dependency and endpoint execute on the same OS thread. Each call to this function
    still returns a fresh, request-scoped connection (never shared across requests),
    so this only relaxes sqlite3's same-thread check for a connection's own internal
    handoff within one request — it does not introduce cross-request concurrency.
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _current_schema_version(conn: sqlite3.Connection) -> int:
    """Highest recorded migration version, or 0 for a fresh/unmigrated DB."""
    row = conn.execute(
        "SELECT 1 AS x FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if not row:
        return 0
    r = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
    return int(r[0])


def _load_migration(version: int) -> Path | None:
    """Return the migration script path for `version` (NNNN_name.py), if present."""
    if not MIGRATIONS_DIR.exists():
        return None
    for p in sorted(MIGRATIONS_DIR.glob("*.py")):
        if p.name[0].isdigit() and int(p.name.split("_", 1)[0]) == version:
            return p
    return None


def _apply_migrations(conn: sqlite3.Connection) -> int:
    """Run ordered migration scripts whose version is higher than recorded.

    Each migration module must expose apply(conn). Migrations run in ascending
    version order inside their own transaction and are recorded in
    schema_version as they succeed. Returns the number of migrations applied.
    """
    current = _current_schema_version(conn)
    applied = 0
    version = current + 1
    while True:
        path = _load_migration(version)
        if path is None:
            break
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            f"legiscanner_migration_{version}", path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.apply(conn)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        conn.commit()
        applied += 1
        version += 1
    return applied


def init_db(conn: sqlite3.Connection) -> None:
    """Apply schema.sql, then the one-time backfill, then versioned migrations.

    Order matters:
      1. schema.sql (CREATE ... IF NOT EXISTS) creates every table/index/trigger
         — including bill_texts with its content_hash column and unique index.
      2. migrate_bill_texts is a one-time ALTER+backfill for pre-existing DBs
         that predate the content_hash column. On a fresh DB the column already
         exists (from step 1) so it's a no-op; on an old DB it adds the column
         and fills it so the unique index is consistent. It must run AFTER the
         tables exist (running it first crashed on a fresh DB — bill_texts was
         missing).
      3. _apply_migrations runs ordered scripts in migrations/ for any schema
         changes needing data backfills or one-off work beyond plain DDL.
    """
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    migrate_bill_texts(conn)
    _apply_migrations(conn)


def migrate_bill_texts(conn: sqlite3.Connection) -> None:
    """Add content_hash to bill_texts on a pre-existing DB and backfill it.

    New installs already have the column from schema.sql, so this is a no-op
    there. On an existing DB the column is added and every row's hash computed;
    byte-identical (bill_id, content) duplicates left over from the old
    delete-then-reinsert era are collapsed to the lowest id so the unique index
    can be created. Idempotent — safe to run on every startup.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bill_texts)")}
    if "content_hash" in cols:
        return  # already migrated (or fresh install from schema.sql)
    conn.execute("ALTER TABLE bill_texts ADD COLUMN content_hash TEXT")
    seen: dict[tuple[int, str], int] = {}
    to_delete: list[int] = []
    to_hash: dict[int, str] = {}
    for r in conn.execute("SELECT id, bill_id, content FROM bill_texts ORDER BY id").fetchall():
        content = r["content"] or ""
        key = (r["bill_id"], content)
        h = hashlib.sha1(content.encode("utf-8")).hexdigest()
        if key in seen:
            to_delete.append(r["id"])
        else:
            seen[key] = r["id"]
            to_hash[r["id"]] = h
    for rid in to_delete:
        conn.execute("DELETE FROM bill_texts WHERE id = ?", (rid,))
    for rid, h in to_hash.items():
        conn.execute("UPDATE bill_texts SET content_hash = ? WHERE id = ?", (h, rid))
    conn.commit()


# --- Sessions -----------------------------------------------------------


def get_or_create_session(
    conn: sqlite3.Connection,
    *,
    state: str,
    name: str,
    year: int,
    legiscan_session_id: int | None = None,
    make_active: bool = True,
) -> int:
    """Return the id of the (state, name) session, creating it if needed."""
    row = conn.execute(
        "SELECT id FROM sessions WHERE state = ? AND name = ?", (state, name)
    ).fetchone()
    if row:
        session_id = row["id"]
    else:
        cur = conn.execute(
            "INSERT INTO sessions (state, name, year, legiscan_session_id, active) "
            "VALUES (?, ?, ?, ?, ?)",
            (state, name, year, legiscan_session_id, int(make_active)),
        )
        session_id = cur.lastrowid
    if make_active:
        conn.execute("UPDATE sessions SET active = 0 WHERE id != ?", (session_id,))
        conn.execute("UPDATE sessions SET active = 1 WHERE id = ?", (session_id,))
    conn.commit()
    return session_id


def get_active_session(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sessions WHERE active = 1 LIMIT 1").fetchone()


def get_session(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()


def list_sessions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All sessions, most recent year first, active flag included.

    Feeds the web UI's session switcher and the `legiscanner sessions`
    (local) listing.
    """
    return conn.execute("SELECT * FROM sessions ORDER BY year DESC, id DESC").fetchall()


def set_active_session(conn: sqlite3.Connection, session_id: int) -> None:
    """Make one session active, deactivating the rest.

    The active session is what the pipeline/CLI and the web app default to;
    this is the single switch that turns multi-session data into a usable
    "view this session" action.
    """
    row = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        raise ValueError(f"No such session: {session_id}")
    conn.execute("UPDATE sessions SET active = 0")
    conn.execute("UPDATE sessions SET active = 1 WHERE id = ?", (session_id,))
    conn.commit()


# --- Topics ---------------------------------------------------------------


def get_or_create_topic(conn: sqlite3.Connection, session_id: int, name: str) -> int:
    name = name.strip().lower().replace(" ", "-")
    row = conn.execute(
        "SELECT id FROM topics WHERE session_id = ? AND name = ?", (session_id, name)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO topics (session_id, name) VALUES (?, ?)", (session_id, name)
    )
    return cur.lastrowid


def list_topics(conn: sqlite3.Connection, session_id: int, *, ai_taggable_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM topics WHERE session_id = ?"
    params: list[Any] = [session_id]
    if ai_taggable_only:
        sql += " AND is_ai_taggable = 1"
    sql += " ORDER BY name"
    return conn.execute(sql, params).fetchall()


# --- Outcome areas (fixed classification, e.g. a county strategic plan) -----


def seed_outcome_areas(conn: sqlite3.Connection, session_id: int, areas: list[dict]) -> None:
    """Seed (or update) the fixed outcome-area taxonomy for a session.

    `areas` is a list of {"slug", "name", "description"} dicts, in display order.
    Idempotent by (session_id, slug) — safe to re-run if descriptions change.
    """
    for i, area in enumerate(areas):
        conn.execute(
            """
            INSERT INTO outcome_areas (session_id, slug, name, description, sort_order)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_id, slug) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                sort_order = excluded.sort_order
            """,
            (session_id, area["slug"], area["name"], area.get("description"), i),
        )


def list_outcome_areas(conn: sqlite3.Connection, session_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM outcome_areas WHERE session_id = ? ORDER BY sort_order", (session_id,)
    ).fetchall()


def get_or_create_outcome_area(conn: sqlite3.Connection, session_id: int, slug: str) -> int | None:
    """Look up an outcome area by slug. Returns None if it isn't seeded for this session
    (outcome areas are a fixed taxonomy, not freely created like topics — a slug the AI
    invents that isn't in the seeded set is silently skipped, not auto-created)."""
    row = conn.execute(
        "SELECT id FROM outcome_areas WHERE session_id = ? AND slug = ?", (session_id, slug)
    ).fetchone()
    return row["id"] if row else None


def set_outcome_score(
    conn: sqlite3.Connection,
    bill_id: int,
    outcome_area_id: int,
    *,
    ai_impact: int,
    ai_reason: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO bill_outcome_scores (bill_id, outcome_area_id, ai_impact, ai_reason)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(bill_id, outcome_area_id) DO UPDATE SET
            ai_impact = excluded.ai_impact,
            ai_reason = excluded.ai_reason,
            updated_at = datetime('now')
        """,
        (bill_id, outcome_area_id, ai_impact, ai_reason),
    )


def set_outcome_score_override(conn: sqlite3.Connection, bill_id: int, outcome_area_id: int, impact: int | None) -> None:
    """Set (or clear, with impact=None) a user override for one bill's outcome-area score.
    Requires the (bill_id, outcome_area_id) row to already exist (i.e. the AI scored it)."""
    conn.execute(
        "UPDATE bill_outcome_scores SET user_impact_override = ?, updated_at = datetime('now') "
        "WHERE bill_id = ? AND outcome_area_id = ?",
        (impact, bill_id, outcome_area_id),
    )


def replace_outcome_scores(conn: sqlite3.Connection, bill_id: int, session_id: int, scores: list[dict]) -> None:
    """Used by autotag: wipe and re-set this bill's AI-scored outcome areas.

    `scores` is a list of {"slug", "impact", "reason"} dicts. Existing user
    overrides for areas still present in the new score set are preserved;
    overrides for areas no longer scored are dropped along with the row.
    """
    existing_overrides = {
        r["outcome_area_id"]: r["user_impact_override"]
        for r in conn.execute(
            "SELECT outcome_area_id, user_impact_override FROM bill_outcome_scores WHERE bill_id = ?",
            (bill_id,),
        ).fetchall()
        if r["user_impact_override"] is not None
    }
    conn.execute("DELETE FROM bill_outcome_scores WHERE bill_id = ?", (bill_id,))
    for score in scores:
        area_id = get_or_create_outcome_area(conn, session_id, score["slug"])
        if area_id is None:
            continue  # AI invented a slug outside the seeded taxonomy — skip it
        set_outcome_score(conn, bill_id, area_id, ai_impact=score["impact"], ai_reason=score.get("reason", ""))
        if area_id in existing_overrides:
            set_outcome_score_override(conn, bill_id, area_id, existing_overrides[area_id])


def get_bill_outcome_scores(conn: sqlite3.Connection, bill_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT oa.slug AS outcome_area_slug, oa.name AS outcome_area_name,
               bos.ai_impact, bos.ai_reason, bos.user_impact_override
        FROM bill_outcome_scores bos
        JOIN outcome_areas oa ON oa.id = bos.outcome_area_id
        WHERE bos.bill_id = ?
        ORDER BY oa.sort_order
        """,
        (bill_id,),
    ).fetchall()


# --- Bills: write path ------------------------------------------------------


def upsert_bill(conn: sqlite3.Connection, session_id: int, bill: dict) -> int:
    """Insert or update a bill and its child rows from a BillOutput-shaped dict.

    Texts are upserted by content hash (idempotent, stable ids — see the
    bill_texts index in schema.sql); sponsors and history are delete-then-reinsert
    since they have no natural unique key of their own.
    """
    cur = conn.execute(
        """
        INSERT INTO bills (
            session_id, bill_number, legiscan_bill_id, change_hash, title,
            description, status, introduced_date, last_action_date, last_action,
            legiscan_url, state_url, retrieved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id, bill_number) DO UPDATE SET
            legiscan_bill_id = excluded.legiscan_bill_id,
            change_hash = excluded.change_hash,
            title = excluded.title,
            description = excluded.description,
            status = excluded.status,
            introduced_date = excluded.introduced_date,
            last_action_date = excluded.last_action_date,
            last_action = excluded.last_action,
            legiscan_url = excluded.legiscan_url,
            state_url = excluded.state_url,
            retrieved_at = excluded.retrieved_at
        """,
        (
            session_id,
            bill["bill_number"],
            bill.get("bill_id"),
            bill.get("change_hash"),
            bill.get("title"),
            bill.get("description"),
            bill.get("status"),
            bill.get("introduced_date"),
            bill.get("last_action_date"),
            bill.get("last_action"),
            bill.get("legiscan_url"),
            bill.get("state_url"),
            bill.get("retrieved_at"),
        ),
    )
    # cur.lastrowid is unreliable after ON CONFLICT DO UPDATE (it can reflect the
    # rowid SQLite would have assigned the attempted insert, not the existing row's
    # actual id) — always resolve by natural key instead of trusting it.
    bill_id = conn.execute(
        "SELECT id FROM bills WHERE session_id = ? AND bill_number = ?",
        (session_id, bill["bill_number"]),
    ).fetchone()["id"]

    # Upsert texts by content hash: a re-scrape that returns the same text updates
    # the existing row (stable id) instead of re-inserting; a stale text the bill no
    # longer publishes is dropped; genuinely distinct texts under the same version
    # label (SC1/S1, ER/ER2) are all kept. We record this scrape's hashes in a temp
    # table so the final cleanup DELETE can't accidentally remove rows we just wrote.
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _bt_keep (bill_id INTEGER, content_hash TEXT)")
    conn.execute("DELETE FROM _bt_keep WHERE bill_id = ?", (bill_id,))
    for t in bill.get("texts", []):
        content = t.get("content") or ""
        chash = hashlib.sha1(content.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT INTO bill_texts (bill_id, version, mime, content, date, state_url, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(bill_id, content_hash) DO UPDATE SET "
            "version = excluded.version, mime = excluded.mime, content = excluded.content, "
            "date = excluded.date, state_url = excluded.state_url",
            (bill_id, t.get("version"), t.get("format"), content, t.get("date"), t.get("state_url"), chash),
        )
        conn.execute("INSERT INTO _bt_keep (bill_id, content_hash) VALUES (?, ?)", (bill_id, chash))
    conn.execute(
        "DELETE FROM bill_texts WHERE bill_id = ? AND content_hash NOT IN "
        "(SELECT content_hash FROM _bt_keep WHERE bill_id = ?)",
        (bill_id, bill_id),
    )

    conn.execute("DELETE FROM sponsors WHERE bill_id = ?", (bill_id,))
    for i, s in enumerate(bill.get("sponsors", [])):
        conn.execute(
            "INSERT INTO sponsors (bill_id, name, party, role, district, sponsor_order) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (bill_id, s.get("name"), s.get("party"), s.get("role"), s.get("district"), i),
        )

    conn.execute("DELETE FROM history WHERE bill_id = ?", (bill_id,))
    for i, h in enumerate(bill.get("history", [])):
        conn.execute(
            "INSERT INTO history (bill_id, date, action, chamber, seq) VALUES (?, ?, ?, ?, ?)",
            (bill_id, h.get("date"), h.get("action"), h.get("chamber"), i),
        )

    tm = bill.get("tracking_metadata", {})
    conn.execute(
        """
        INSERT INTO tracking (bill_id, watching, notes, ai_reason, ai_impact, user_impact_override)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(bill_id) DO UPDATE SET
            ai_reason = excluded.ai_reason,
            ai_impact = excluded.ai_impact,
            updated_at = datetime('now')
        """,
        (
            bill_id,
            int(tm.get("watching", False)),
            tm.get("notes", ""),
            tm.get("ai_reason", ""),
            tm.get("ai_impact"),
            tm.get("user_impact_override"),
        ),
    )

    for tag in tm.get("ai_tags", []):
        add_topic_link(conn, bill_id, session_id, tag, "ai")
    for tag in tm.get("user_tags", []):
        add_topic_link(conn, bill_id, session_id, tag, "user")

    # The tracking row was upserted above; the AFTER UPDATE trigger only fires
    # on the UPDATE branch, so record a snapshot explicitly to keep the audit
    # trail complete (INSERT branch, or values that didn't change).
    record_tracking_snapshot(conn, bill_id)

    return bill_id


def bare_upsert_bill(conn: sqlite3.Connection, session_id: int, bill_number: str) -> int:
    """Insert a bare bill row (bill_number only) if it doesn't already exist.

    Used by discovery (search/getMasterList) before pipeline `run` fills in full detail.
    """
    row = conn.execute(
        "SELECT id FROM bills WHERE session_id = ? AND bill_number = ?",
        (session_id, bill_number),
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO bills (session_id, bill_number) VALUES (?, ?)",
        (session_id, bill_number),
    )
    bill_id = cur.lastrowid
    conn.execute("INSERT INTO tracking (bill_id) VALUES (?)", (bill_id,))
    record_tracking_snapshot(conn, bill_id)
    return bill_id


def set_tracking(conn: sqlite3.Connection, bill_id: int, **fields: Any) -> None:
    """Update one or more tracking columns for a bill.

    Accepts any of: watching, notes, ai_reason. NOT ai_impact or
    user_impact_override — those are derived as MAX() across this bill's
    bill_outcome_scores rows by a DB trigger; write scores via
    set_outcome_score()/set_outcome_score_override()/replace_outcome_scores()
    instead, and the overall tracking.ai_impact/user_impact_override update
    automatically as a side effect.
    """
    allowed = {"watching", "notes", "ai_reason"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unknown or no-longer-directly-settable tracking field(s): {unknown}")
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values())
    conn.execute(
        f"UPDATE tracking SET {set_clause}, updated_at = datetime('now') WHERE bill_id = ?",
        (*values, bill_id),
    )


def add_topic_link(conn: sqlite3.Connection, bill_id: int, session_id: int, tag_name: str, source: str) -> None:
    topic_id = get_or_create_topic(conn, session_id, tag_name)
    conn.execute(
        "INSERT OR IGNORE INTO bill_topics (bill_id, topic_id, source) VALUES (?, ?, ?)",
        (bill_id, topic_id, source),
    )


def remove_topic_link(conn: sqlite3.Connection, bill_id: int, tag_name: str, source: str) -> None:
    tag_name = tag_name.strip().lower().replace(" ", "-")
    conn.execute(
        """
        DELETE FROM bill_topics
        WHERE bill_id = ? AND source = ?
          AND topic_id = (SELECT id FROM topics WHERE name = ? AND session_id = (
              SELECT session_id FROM bills WHERE id = ?
          ))
        """,
        (bill_id, source, tag_name, bill_id),
    )


def replace_ai_topic_links(conn: sqlite3.Connection, bill_id: int, session_id: int, tags: list[str]) -> None:
    """Used by autotag: wipe and re-set the 'ai' tag set for a bill."""
    conn.execute("DELETE FROM bill_topics WHERE bill_id = ? AND source = 'ai'", (bill_id,))
    for tag in tags:
        add_topic_link(conn, bill_id, session_id, tag, "ai")


# --- Bills: read path --------------------------------------------------------


def _row_to_bill_output(conn: sqlite3.Connection, bill_row: sqlite3.Row) -> dict:
    bill_id = bill_row["id"]

    texts = [
        TextVersionOutput(
            version=r["version"] or "",
            date=r["date"] or "",
            format=r["mime"] or "",
            content=r["content"] or "",
            state_url=r["state_url"],
        )
        for r in conn.execute(
            "SELECT * FROM bill_texts WHERE bill_id = ? "
            "ORDER BY (date IS NULL), date, id",
            (bill_id,),
        ).fetchall()
    ]

    sponsors = [
        SponsorOutput(name=r["name"], party=r["party"], role=r["role"], district=r["district"])
        for r in conn.execute(
            "SELECT * FROM sponsors WHERE bill_id = ? ORDER BY sponsor_order", (bill_id,)
        ).fetchall()
    ]

    history = [
        HistoryOutput(date=r["date"], action=r["action"], chamber=r["chamber"])
        for r in conn.execute(
            "SELECT * FROM history WHERE bill_id = ? ORDER BY seq", (bill_id,)
        ).fetchall()
    ]

    ai_tags = [
        r["name"]
        for r in conn.execute(
            "SELECT t.name FROM bill_topics bt JOIN topics t ON t.id = bt.topic_id "
            "WHERE bt.bill_id = ? AND bt.source = 'ai' ORDER BY t.name",
            (bill_id,),
        ).fetchall()
    ]
    user_tags = [
        r["name"]
        for r in conn.execute(
            "SELECT t.name FROM bill_topics bt JOIN topics t ON t.id = bt.topic_id "
            "WHERE bt.bill_id = ? AND bt.source = 'user' ORDER BY t.name",
            (bill_id,),
        ).fetchall()
    ]

    companions = [
        r["bill_number"]
        for r in conn.execute(
            """
            SELECT b.bill_number FROM companion_pairs_expanded cpe
            JOIN bills b ON b.id = cpe.other_bill_id
            WHERE cpe.bill_id = ?
            ORDER BY b.bill_number
            """,
            (bill_id,),
        ).fetchall()
    ]

    outcome_scores = [
        OutcomeScoreOutput(
            outcome_area_slug=r["outcome_area_slug"],
            outcome_area_name=r["outcome_area_name"],
            ai_impact=r["ai_impact"],
            ai_reason=r["ai_reason"] or "",
            user_impact_override=r["user_impact_override"],
        )
        for r in get_bill_outcome_scores(conn, bill_id)
    ]

    tracking_row = conn.execute("SELECT * FROM tracking WHERE bill_id = ?", (bill_id,)).fetchone()
    tracking = TrackingMetadata(
        ai_tags=ai_tags,
        ai_reason=(tracking_row["ai_reason"] if tracking_row else "") or "",
        ai_impact=(tracking_row["ai_impact"] if tracking_row else None),
        watching=bool(tracking_row["watching"]) if tracking_row else False,
        user_tags=user_tags,
        notes=(tracking_row["notes"] if tracking_row else "") or "",
        user_impact_override=(tracking_row["user_impact_override"] if tracking_row else None),
    )

    session_row = conn.execute("SELECT name FROM sessions WHERE id = ?", (bill_row["session_id"],)).fetchone()

    lineage_row = conn.execute(
        "SELECT lineage_id FROM bill_lineage_members WHERE bill_id = ?", (bill_id,)
    ).fetchone()
    lineage_members: list[dict] = []
    lineage_id: int | None = None
    if lineage_row:
        lineage_id = lineage_row["lineage_id"]
        members = conn.execute(
            """
            SELECT b.id AS bill_id, b.bill_number, b.title, b.status, b.last_action_date,
                   s.id AS session_id, s.name AS session_name, s.year,
                   blm.link_reason, blm.similarity
            FROM bill_lineage_members blm
            JOIN bills b ON b.id = blm.bill_id
            JOIN sessions s ON s.id = b.session_id
            WHERE blm.lineage_id = ?
            ORDER BY s.year DESC, b.bill_number
            """,
            (lineage_id,),
        ).fetchall()
        lineage_members = [dict(m) for m in members]

    output = BillOutput(
        bill_number=bill_row["bill_number"],
        bill_id=bill_row["legiscan_bill_id"] or 0,
        change_hash=bill_row["change_hash"],
        title=bill_row["title"],
        description=bill_row["description"],
        state=conn.execute("SELECT state FROM sessions WHERE id = ?", (bill_row["session_id"],)).fetchone()["state"],
        session=session_row["name"] if session_row else "",
        session_id=bill_row["session_id"],
        status=bill_row["status"],
        introduced_date=bill_row["introduced_date"],
        last_action_date=bill_row["last_action_date"],
        last_action=bill_row["last_action"],
        sponsors=sponsors,
        subjects=[],
        tracking_metadata=tracking,
        outcome_scores=outcome_scores,
        texts=texts,
        history=history,
        companion_bills=companions,
        lineage_id=lineage_id,
        lineage_members=lineage_members,
        legiscan_url=bill_row["legiscan_url"],
        state_url=bill_row["state_url"],
        retrieved_at=bill_row["retrieved_at"] or "",
    )
    return output.model_dump()


def get_bill(conn: sqlite3.Connection, session_id: int, bill_number: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM bills WHERE session_id = ? AND bill_number = ?",
        (session_id, bill_number.upper()),
    ).fetchone()
    if not row:
        return None
    return _row_to_bill_output(conn, row)


def get_bill_texts(conn: sqlite3.Connection, session_id: int, bill_number: str) -> dict | None:
    """Bill identity + ordered text versions (no content) + history.

    Feeds the version-compare page: the selectors need version labels/dates
    only, and the client then pulls full content per version via the diff
    endpoint. Returns None if the bill is not in this session.
    """
    row = conn.execute(
        "SELECT id, bill_number, title, status, introduced_date FROM bills "
        "WHERE session_id = ? AND bill_number = ?",
        (session_id, bill_number.upper()),
    ).fetchone()
    if not row:
        return None

    texts = [
        {
            "id": r["id"],
            "version": r["version"] or "",
            "date": r["date"] or "",
            "state_url": r["state_url"] or "",
            "chars": len(r["content"] or ""),
        }
        for r in conn.execute(
            "SELECT id, version, date, state_url, content FROM bill_texts "
            "WHERE bill_id = ? "
            "ORDER BY (date IS NULL), date, id",
            (row["id"],),
        ).fetchall()
    ]

    history = [
        {"date": r["date"], "action": r["action"], "chamber": r["chamber"] or ""}
        for r in conn.execute(
            "SELECT date, action, chamber FROM history WHERE bill_id = ? ORDER BY seq",
            (row["id"],),
        ).fetchall()
    ]

    return {
        "bill_id": row["id"],
        "bill_number": row["bill_number"],
        "title": row["title"] or "",
        "status": row["status"] or "",
        "introduced_date": row["introduced_date"] or "",
        "texts": texts,
        "history": history,
    }


def get_bill_change_hash(conn: sqlite3.Connection, session_id: int, bill_number: str) -> str | None:
    row = conn.execute(
        "SELECT change_hash FROM bills WHERE session_id = ? AND bill_number = ?",
        (session_id, bill_number.upper()),
    ).fetchone()
    return row["change_hash"] if row else None


def list_bills(
    conn: sqlite3.Connection,
    session_id: int,
    *,
    search: str = "",
    impact_levels: set[int] | None = None,
    hide_low_relevance: bool = False,
    watching_only: bool = False,
    tags: list[str] | None = None,
    bill_numbers: set[str] | None = None,
    sort: str = "bill_number",
) -> list[dict]:
    """Filter/sort bills, returning BillOutput-shaped dicts.

    Mirrors the filter semantics of the old routes.py filter_bills_by_criteria():
    effective impact = COALESCE(user_impact_override, ai_impact); hide_low_relevance
    only applies when no specific impact levels are selected; tag filter is ANY-match.
    """
    sql = """
        SELECT b.*, COALESCE(tr.user_impact_override, tr.ai_impact) AS effective_impact,
               tr.watching AS watching
        FROM bills b
        LEFT JOIN tracking tr ON tr.bill_id = b.id
        WHERE b.session_id = ?
    """
    params: list[Any] = [session_id]

    if bill_numbers:
        placeholders = ",".join("?" for _ in bill_numbers)
        sql += f" AND UPPER(b.bill_number) IN ({placeholders})"
        params.extend(bn.upper() for bn in bill_numbers)

    if watching_only:
        sql += " AND tr.watching = 1"

    if impact_levels:
        placeholders = ",".join("?" for _ in impact_levels)
        sql += f" AND COALESCE(tr.user_impact_override, tr.ai_impact) IN ({placeholders})"
        params.extend(impact_levels)
    elif hide_low_relevance:
        sql += " AND (COALESCE(tr.user_impact_override, tr.ai_impact) IS NULL OR COALESCE(tr.user_impact_override, tr.ai_impact) != 1)"

    if tags:
        placeholders = ",".join("?" for _ in tags)
        sql += f"""
            AND b.id IN (
                SELECT bt.bill_id FROM bill_topics bt JOIN topics t ON t.id = bt.topic_id
                WHERE t.name IN ({placeholders})
            )
        """
        params.extend(tags)

    if search:
        sql += """
            AND b.id IN (
                SELECT rowid FROM bills_fts WHERE bills_fts MATCH ?
            )
        """
        params.append(_fts_query(search))

    sort_map = {
        "impact": "COALESCE(tr.user_impact_override, tr.ai_impact) DESC",
        "last_action_date": "b.last_action_date DESC",
        "title": "b.title ASC",
        "watching": "tr.watching DESC, COALESCE(tr.user_impact_override, tr.ai_impact) DESC",
        "bill_number": "b.bill_number ASC",
    }
    sql += f" ORDER BY {sort_map.get(sort, sort_map['bill_number'])}"

    rows = conn.execute(sql, params).fetchall()
    return [_row_to_bill_output(conn, row) for row in rows]


def _fts_query(search: str) -> str:
    """Build an FTS5 MATCH query that behaves like a substring/AND-of-terms search."""
    # Double quotes would terminate the quoted FTS term and make MATCH a syntax error.
    terms = [t for t in search.replace('"', " ").split() if t]
    if not terms:
        return '""'
    return " AND ".join(f'"{t}"*' for t in terms)


def get_all_tags(conn: sqlite3.Connection, session_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT t.name FROM topics t
        JOIN bill_topics bt ON bt.topic_id = t.id
        JOIN bills b ON b.id = bt.bill_id
        WHERE b.session_id = ?
        ORDER BY t.name
        """,
        (session_id,),
    ).fetchall()
    return [r["name"] for r in rows]


# --- Companions ---------------------------------------------------------------


def replace_companions(conn: sqlite3.Connection, session_id: int, pairs: list[tuple[int, int, float]]) -> None:
    """Recompute companion links for a session: delete existing, insert canonical pairs."""
    conn.execute(
        """
        DELETE FROM companions WHERE bill_id IN (SELECT id FROM bills WHERE session_id = ?)
           OR companion_bill_id IN (SELECT id FROM bills WHERE session_id = ?)
        """,
        (session_id, session_id),
    )
    for a, b, score in pairs:
        lo, hi = (a, b) if a < b else (b, a)
        conn.execute(
            "INSERT OR IGNORE INTO companions (bill_id, companion_bill_id, similarity) VALUES (?, ?, ?)",
            (lo, hi, score),
        )


# --- Bill lineage (cross-session) ------------------------------------------


def get_or_create_lineage(conn: sqlite3.Connection, name: str | None = None) -> int:
    """Create a new, empty bill lineage and return its id."""
    cur = conn.execute("INSERT INTO bill_lineages (name) VALUES (?)", (name,))
    return cur.lastrowid


def list_lineages(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All lineages with member/session counts, most members first."""
    return conn.execute(
        """
        SELECT bl.id, bl.name, bl.created_at, bl.updated_at,
               COUNT(blm.bill_id) AS member_count,
               COUNT(DISTINCT blm.session_id) AS session_count
        FROM bill_lineages bl
        LEFT JOIN bill_lineage_members blm ON blm.lineage_id = bl.id
        GROUP BY bl.id
        ORDER BY member_count DESC, bl.id
        """
    ).fetchall()


def get_lineage(conn: sqlite3.Connection, lineage_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM bill_lineages WHERE id = ?", (lineage_id,)).fetchone()


def add_lineage_member(
    conn: sqlite3.Connection,
    bill_id: int,
    *,
    lineage_id: int | None = None,
    link_reason: str = "manual",
    similarity: float | None = None,
) -> int:
    """Attach a bill to a lineage (creating the lineage if one isn't given).

    A bill belongs to exactly one lineage (enforced by UNIQUE(bill_id)); if the
    bill is already a member of another lineage its membership is moved here so
    manual curation can re-point it. Returns the lineage id.
    """
    bill = conn.execute(
        "SELECT id, session_id FROM bills WHERE id = ?", (bill_id,)
    ).fetchone()
    if not bill:
        raise ValueError(f"No such bill: {bill_id}")

    if lineage_id is None:
        lineage_id = get_or_create_lineage(conn)

    existing = conn.execute(
        "SELECT lineage_id FROM bill_lineage_members WHERE bill_id = ?", (bill_id,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE bill_lineage_members SET lineage_id = ?, session_id = ?, "
            "link_reason = ?, similarity = ? WHERE bill_id = ?",
            (lineage_id, bill["session_id"], link_reason, similarity, bill_id),
        )
    else:
        conn.execute(
            "INSERT INTO bill_lineage_members (lineage_id, bill_id, session_id, link_reason, similarity) "
            "VALUES (?, ?, ?, ?, ?)",
            (lineage_id, bill_id, bill["session_id"], link_reason, similarity),
        )
    conn.execute(
        "UPDATE bill_lineages SET updated_at = datetime('now') WHERE id = ?", (lineage_id,)
    )
    return lineage_id


def remove_lineage_member(conn: sqlite3.Connection, bill_id: int) -> None:
    """Detach a bill from its lineage (and drop the lineage if it empties)."""
    row = conn.execute(
        "SELECT lineage_id FROM bill_lineage_members WHERE bill_id = ?", (bill_id,)
    ).fetchone()
    if not row:
        return
    conn.execute("DELETE FROM bill_lineage_members WHERE bill_id = ?", (bill_id,))
    remaining = conn.execute(
        "SELECT COUNT(*) FROM bill_lineage_members WHERE lineage_id = ?", (row["lineage_id"],)
    ).fetchone()[0]
    if remaining == 0:
        conn.execute("DELETE FROM bill_lineages WHERE id = ?", (row["lineage_id"],))


def get_bill_lineage(conn: sqlite3.Connection, bill_id: int) -> dict | None:
    """Return a bill's lineage: the lineage row plus its member bills.

    Members are grouped by session (year-desc) so a "lineage view" can show
    how a conceptual bill evolved session over session. Returns None if the
    bill is not part of any lineage.
    """
    row = conn.execute(
        """
        SELECT blm.lineage_id FROM bill_lineage_members blm
        WHERE blm.bill_id = ?
        """,
        (bill_id,),
    ).fetchone()
    if not row:
        return None
    lineage_id = row["lineage_id"]
    lineage = get_lineage(conn, lineage_id)
    members = conn.execute(
        """
        SELECT blm.bill_id, blm.link_reason, blm.similarity,
               b.bill_number, b.title, b.status, b.last_action_date,
               s.id AS session_id, s.name AS session_name, s.year
        FROM bill_lineage_members blm
        JOIN bills b ON b.id = blm.bill_id
        JOIN sessions s ON s.id = b.session_id
        WHERE blm.lineage_id = ?
        ORDER BY s.year DESC, b.bill_number
        """,
        (lineage_id,),
    ).fetchall()
    return {"lineage": lineage, "members": members}


def get_bill_lineage_by_number(
    conn: sqlite3.Connection, session_id: int, bill_number: str
) -> dict | None:
    """Convenience wrapper: resolve a bill by (session, number) then get its lineage."""
    row = conn.execute(
        "SELECT id FROM bills WHERE session_id = ? AND bill_number = ?",
        (session_id, bill_number.upper()),
    ).fetchone()
    if not row:
        return None
    return get_bill_lineage(conn, row["id"])


# --- Tracking history (audit trail) ---------------------------------------


def record_tracking_snapshot(conn: sqlite3.Connection, bill_id: int) -> None:
    """Append the bill's current tracking state to tracking_history.

    Called explicitly where the trigger does not fire (e.g. the first
    upsert_bill insert) so the audit trail is complete even for bills whose
    tracking row was created, not updated.
    """
    conn.execute(
        """
        INSERT INTO tracking_history
            (bill_id, watching, notes, ai_reason, ai_impact, user_impact_override)
        SELECT bill_id, watching, notes, ai_reason, ai_impact, user_impact_override
        FROM tracking WHERE bill_id = ?
        """,
        (bill_id,),
    )


def get_tracking_history(
    conn: sqlite3.Connection, bill_id: int, *, limit: int = 50
) -> list[sqlite3.Row]:
    """Recent tracking snapshots for a bill, newest first."""
    return conn.execute(
        "SELECT * FROM tracking_history WHERE bill_id = ? ORDER BY changed_at DESC, id DESC LIMIT ?",
        (bill_id, limit),
    ).fetchall()


# --- AI analysis provenance -----------------------------------------------


def record_analysis_run(
    conn: sqlite3.Connection,
    bill_id: int,
    session_id: int,
    *,
    model: str,
    model_version: str | None = None,
    tags: list[str] | None = None,
    reason: str = "",
) -> int:
    """Record one AI analysis of a bill (provenance for reproducibility)."""
    import json as _json

    cur = conn.execute(
        "INSERT INTO analysis_runs (bill_id, session_id, model, model_version, tags_json, reason) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (bill_id, session_id, model, model_version, _json.dumps(tags or []), reason),
    )
    return cur.lastrowid


def get_analysis_runs(
    conn: sqlite3.Connection, bill_id: int, *, limit: int = 10
) -> list[sqlite3.Row]:
    """Recent analysis runs for a bill, newest first."""
    return conn.execute(
        "SELECT * FROM analysis_runs WHERE bill_id = ? ORDER BY generated_at DESC, id DESC LIMIT ?",
        (bill_id, limit),
    ).fetchall()
