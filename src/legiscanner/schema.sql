-- Legiscanner SQLite schema.
-- Applied idempotently via db.init_db(). See CLAUDE.md / plan for design rationale.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    id                   INTEGER PRIMARY KEY,
    state                TEXT NOT NULL DEFAULT 'VA',
    name                 TEXT NOT NULL,               -- "2026 Regular Session"
    year                 INTEGER NOT NULL,
    legiscan_session_id  INTEGER,                      -- LegiScan's own session_id (distinct from this table's id)
    ollama_url           TEXT NOT NULL DEFAULT 'http://localhost:8080/',
    model                TEXT NOT NULL DEFAULT 'qwen3.6-35b-a3b',
    active               INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_state_name ON sessions(state, name);

CREATE TABLE IF NOT EXISTS topics (
    id              INTEGER PRIMARY KEY,
    session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    description     TEXT,
    search_keywords TEXT,                              -- optional LegiScan boolean search string for discovery
    is_ai_taggable  INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_topics_session_name ON topics(session_id, name);

CREATE TABLE IF NOT EXISTS bills (
    id                INTEGER PRIMARY KEY,
    session_id        INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    bill_number       TEXT NOT NULL,
    legiscan_bill_id  INTEGER,
    change_hash       TEXT,
    title             TEXT,
    description       TEXT,
    status            TEXT,                            -- status_desc only; numeric code unused downstream
    introduced_date   TEXT,
    last_action_date  TEXT,
    last_action       TEXT,
    legiscan_url      TEXT,
    state_url         TEXT,
    retrieved_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bills_session_billnumber ON bills(session_id, bill_number);
CREATE INDEX IF NOT EXISTS idx_bills_legiscan_bill_id ON bills(legiscan_bill_id);
CREATE INDEX IF NOT EXISTS idx_bills_last_action_date ON bills(session_id, last_action_date);

CREATE TABLE IF NOT EXISTS bill_texts (
    id           INTEGER PRIMARY KEY,
    bill_id      INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    version      TEXT NOT NULL,                        -- LegiScan's `type`, e.g. "Introduced"
    doc_id       INTEGER,
    mime         TEXT,
    content      TEXT,
    date         TEXT,
    state_url    TEXT,
    content_hash TEXT                                  -- sha1(content); stable identity for re-scrape dedup
);
-- A bill can legitimately have MULTIPLE texts for the same version label
-- (e.g. "Comm Sub" for SC1 and S1, "Enrolled" for ER and ER2), so (bill_id, version)
-- is NOT unique. But a given (bill, content) should appear once: on re-scrape the
-- upsert upserts by content hash instead of delete-then-reinsert, which keeps row
-- ids stable (saved /compare?v1=&v2= links survive a re-scrape) and drops only
-- byte-identical stale rows.
CREATE UNIQUE INDEX IF NOT EXISTS idx_bill_texts_bill_content ON bill_texts(bill_id, content_hash);
CREATE INDEX IF NOT EXISTS idx_bill_texts_bill_id ON bill_texts(bill_id);

CREATE TABLE IF NOT EXISTS sponsors (
    id            INTEGER PRIMARY KEY,
    bill_id       INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    party         TEXT,
    role          TEXT,
    district      TEXT,
    sponsor_order INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sponsors_bill_id ON sponsors(bill_id);

CREATE TABLE IF NOT EXISTS history (
    id      INTEGER PRIMARY KEY,
    bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    date    TEXT NOT NULL,
    action  TEXT NOT NULL,
    chamber TEXT,
    seq     INTEGER NOT NULL                            -- explicit ordering, independent of date-string sort
);
CREATE INDEX IF NOT EXISTS idx_history_bill_id ON history(bill_id, seq);

-- One row per (bill, topic, source). Composite PK lets the SAME topic be attached via
-- both 'ai' and 'user' independently (matches ai_tags/user_tags overlap semantics and
-- separate remove-AI-tag/remove-user-tag UI actions).
CREATE TABLE IF NOT EXISTS bill_topics (
    bill_id  INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    source   TEXT NOT NULL CHECK (source IN ('ai', 'user')),
    PRIMARY KEY (bill_id, topic_id, source)
);
CREATE INDEX IF NOT EXISTS idx_bill_topics_topic ON bill_topics(topic_id);
CREATE INDEX IF NOT EXISTS idx_bill_topics_bill ON bill_topics(bill_id);

-- 1:1 with bills. Split out to keep frequently-mutated web-app fields separate from
-- pipeline-owned bill metadata.
--
-- ai_impact / user_impact_override here are the "overall impact" — a derived
-- MAX() across this bill's per-outcome-area scores in bill_outcome_scores,
-- kept in sync by the outcome_scores_sync_* triggers below. Do NOT write these
-- columns directly from application code once outcome scores exist for a bill;
-- write bill_outcome_scores instead and let the triggers update these. They're
-- kept as real (not view/generated) columns so existing bill-card badges,
-- sort, and filter queries keep working unchanged for a quick "how significant
-- is this bill overall" view, without every caller needing to know about
-- outcome areas.
CREATE TABLE IF NOT EXISTS tracking (
    bill_id              INTEGER PRIMARY KEY REFERENCES bills(id) ON DELETE CASCADE,
    watching             INTEGER NOT NULL DEFAULT 0,
    notes                TEXT NOT NULL DEFAULT '',
    ai_reason            TEXT NOT NULL DEFAULT '',
    ai_impact            INTEGER,
    user_impact_override INTEGER,
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tracking_watching ON tracking(watching);
CREATE INDEX IF NOT EXISTS idx_tracking_ai_impact ON tracking(ai_impact);

-- Fixed reference classification (e.g. Fairfax County's 10 strategic plan
-- outcome areas), distinct from the free-form, user-editable `topics` table:
-- topics are ad-hoc searchable tags ("landlord-tenant", "zoning"); outcome
-- areas are a small, stable taxonomy used specifically for impact scoring
-- and strategic-plan alignment reporting. Session-scoped so a different
-- organization's outcome areas don't collide, but not meant to be edited
-- through the same day-to-day UI flow as topics.
CREATE TABLE IF NOT EXISTS outcome_areas (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    slug        TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outcome_areas_session_slug ON outcome_areas(session_id, slug);

-- One row per (bill, outcome_area) that the AI (or a user override) has
-- scored. Absence of a row means "not scored for this area" (not "scored
-- zero") — a bill only gets rows for areas it's actually relevant to,
-- mirroring how bill_topics only links topics that actually apply.
CREATE TABLE IF NOT EXISTS bill_outcome_scores (
    bill_id               INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    outcome_area_id       INTEGER NOT NULL REFERENCES outcome_areas(id) ON DELETE CASCADE,
    ai_impact             INTEGER NOT NULL CHECK (ai_impact BETWEEN 1 AND 5),
    ai_reason             TEXT NOT NULL DEFAULT '',
    user_impact_override  INTEGER CHECK (user_impact_override BETWEEN 1 AND 5),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (bill_id, outcome_area_id)
);
CREATE INDEX IF NOT EXISTS idx_bill_outcome_scores_area ON bill_outcome_scores(outcome_area_id);

-- Keep tracking.ai_impact in sync as MAX(COALESCE(user_impact_override, ai_impact))
-- across this bill's outcome-area scores — i.e. the highest CURRENT relevance
-- across all areas, using a user override where one is set for that specific
-- area. tracking.user_impact_override is deliberately left NULL by these
-- triggers (not derived) since a MAX spanning mixed AI/user sources no longer
-- has a single well-defined "was this AI or human" answer at the bill level;
-- that distinction is only meaningful per-area, and IS shown in the per-area
-- breakdown. Existing bill-level sort/filter/badges keep working via
-- tracking.ai_impact alone without every query needing to know about
-- outcome areas.
--
-- Concretely this means: overriding one low-relevance area to a low score
-- does NOT suppress the overall badge if a different, un-overridden area
-- still scores higher — the overall number always reflects the single
-- highest real-world relevance across everything currently known about
-- the bill.
CREATE TRIGGER IF NOT EXISTS outcome_scores_sync_insert AFTER INSERT ON bill_outcome_scores BEGIN
    UPDATE tracking SET
        ai_impact = (
            SELECT MAX(COALESCE(user_impact_override, ai_impact))
            FROM bill_outcome_scores WHERE bill_id = new.bill_id
        )
    WHERE bill_id = new.bill_id;
END;
CREATE TRIGGER IF NOT EXISTS outcome_scores_sync_update AFTER UPDATE ON bill_outcome_scores BEGIN
    UPDATE tracking SET
        ai_impact = (
            SELECT MAX(COALESCE(user_impact_override, ai_impact))
            FROM bill_outcome_scores WHERE bill_id = new.bill_id
        )
    WHERE bill_id = new.bill_id;
END;
CREATE TRIGGER IF NOT EXISTS outcome_scores_sync_delete AFTER DELETE ON bill_outcome_scores BEGIN
    UPDATE tracking SET
        ai_impact = (
            SELECT MAX(COALESCE(user_impact_override, ai_impact))
            FROM bill_outcome_scores WHERE bill_id = old.bill_id
        )
    WHERE bill_id = old.bill_id;
END;

-- One row per unordered pair, canonical ordering enforced by CHECK. companion_pairs_expanded
-- view gives symmetric reads without duplicating rows.
CREATE TABLE IF NOT EXISTS companions (
    bill_id           INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    companion_bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    similarity        REAL,
    PRIMARY KEY (bill_id, companion_bill_id),
    CHECK (bill_id < companion_bill_id)
);
CREATE INDEX IF NOT EXISTS idx_companions_companion_bill_id ON companions(companion_bill_id);

CREATE VIEW IF NOT EXISTS companion_pairs_expanded AS
    SELECT bill_id AS bill_id, companion_bill_id AS other_bill_id, similarity FROM companions
    UNION ALL
    SELECT companion_bill_id AS bill_id, bill_id AS other_bill_id, similarity FROM companions;

-- Replaces an O(n) Python substring scan over every bill on every search request.
-- NOT contentless (no content='') — bill_number/title/description/ai_reason are all
-- small strings, so duplicating them into the FTS index is cheap and lets triggers use
-- plain UPDATE/DELETE instead of contentless FTS5's fussier 'delete' command syntax.
CREATE VIRTUAL TABLE IF NOT EXISTS bills_fts USING fts5(
    bill_number, title, description, ai_reason,
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS bills_fts_insert AFTER INSERT ON bills BEGIN
    INSERT INTO bills_fts(rowid, bill_number, title, description, ai_reason)
    VALUES (new.id, new.bill_number, new.title, new.description, '');
END;
CREATE TRIGGER IF NOT EXISTS bills_fts_update AFTER UPDATE OF bill_number, title, description ON bills BEGIN
    UPDATE bills_fts SET bill_number = new.bill_number, title = new.title, description = new.description
    WHERE rowid = new.id;
END;
CREATE TRIGGER IF NOT EXISTS bills_fts_delete AFTER DELETE ON bills BEGIN
    DELETE FROM bills_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS tracking_fts_sync_insert AFTER INSERT ON tracking BEGIN
    UPDATE bills_fts SET ai_reason = new.ai_reason WHERE rowid = new.bill_id;
END;
CREATE TRIGGER IF NOT EXISTS tracking_fts_sync_update AFTER UPDATE OF ai_reason ON tracking BEGIN
    UPDATE bills_fts SET ai_reason = new.ai_reason WHERE rowid = new.bill_id;
END;

-- ---------------------------------------------------------------------------
-- Cross-session bill lineage
--
-- Bill identity is (session_id, bill_number) and bill numbers repeat every
-- session (a new HB1234 appears each year), so a "conceptual bill" that recurs
-- across sessions needs an explicit model. A lineage groups the bills that are
-- the same/same-subject legislation re-introduced over time. Members can span
-- several sessions (a 2025 re-introduction + its 2026 successor) and several
-- bills in one session (a House/Senate companion pair).
--
-- This is purely additive: it does not touch bills/companions/tracking. The
-- `companions` table still links House/Senate pairs *within* one session; a
-- lineage can contain both members of such a pair plus their prior-session
-- predecessors.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bill_lineages (
    id             INTEGER PRIMARY KEY,
    name           TEXT,                                  -- optional human label ("2026 ADU bill")
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per (lineage, bill). `UNIQUE (bill_id)` enforces "a bill belongs to
-- exactly one lineage" (0 or 1). link_reason records how the membership was
-- derived so manual curation can tell auto-links from hand-confirmed ones.
CREATE TABLE IF NOT EXISTS bill_lineage_members (
    lineage_id   INTEGER NOT NULL REFERENCES bill_lineages(id) ON DELETE CASCADE,
    bill_id      INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    link_reason  TEXT,                                    -- 'companion' | 'title-similar' | 'same-legiscan-bill' | 'manual'
    similarity   REAL,
    PRIMARY KEY (lineage_id, bill_id),
    UNIQUE (bill_id)
);
CREATE INDEX IF NOT EXISTS idx_blm_session ON bill_lineage_members(session_id);
CREATE INDEX IF NOT EXISTS idx_blm_bill    ON bill_lineage_members(bill_id);

-- ---------------------------------------------------------------------------
-- Tracking history (audit trail)
--
-- `tracking` mutates in place: notes, watch state, and impact scores are
-- overwritten on every change, and across sessions the prior session's values
-- are simply gone. tracking_history preserves a point-in-time snapshot of the
-- mutable tracking fields each time they change, so "what were the notes on
-- this bill last session" is recoverable. Appended by the tracking_history_*
-- triggers below (AFTER UPDATE on tracking); inserts of new tracking rows are
-- not recorded (a brand-new bill has no prior state to preserve).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tracking_history (
    id         INTEGER PRIMARY KEY,
    bill_id    INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    watching   INTEGER,
    notes      TEXT,
    ai_reason  TEXT,
    ai_impact  INTEGER,
    user_impact_override INTEGER,
    changed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tracking_history_bill ON tracking_history(bill_id, changed_at);

CREATE TRIGGER IF NOT EXISTS tracking_history_after_update AFTER UPDATE ON tracking BEGIN
    INSERT INTO tracking_history (bill_id, watching, notes, ai_reason, ai_impact, user_impact_override)
    VALUES (new.bill_id, new.watching, new.notes, new.ai_reason, new.ai_impact, new.user_impact_override);
END;

-- ---------------------------------------------------------------------------
-- AI analysis provenance
--
-- The tagger rewrites ai_reason/ai_tags/outcome scores in place with no record
-- of *when* or *which model* produced them. analysis_runs records one row per
-- (bill, model) analysis so cross-session comparisons are reproducible and
-- auditable. Written by the tagger after a successful analysis.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS analysis_runs (
    id              INTEGER PRIMARY KEY,
    bill_id         INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    model           TEXT,
    model_version   TEXT,
    generated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    tags_json       TEXT,
    reason          TEXT
);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_bill ON analysis_runs(bill_id);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_session ON analysis_runs(session_id);

-- ---------------------------------------------------------------------------
-- Schema versioning
--
-- Replaces the single ad-hoc migrate_bill_texts backfill with an ordered,
-- versioned migration system. db.init_db() runs schema.sql (idempotent) then
-- applies any migration scripts in src/legiscanner/migrations/ whose version is
-- higher than the recorded one, recording each as it goes. See db.py.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
