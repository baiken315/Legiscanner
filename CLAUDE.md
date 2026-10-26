# Legiscanner - Virginia Legislative Bill Scanning Tool

## Project Overview

A pipeline tool for local government staff to scan, retrieve, and organize Virginia General Assembly bills for analysis. The tool discovers and fetches bill data from the LegiScan API based on a configurable topic vocabulary, extracts full bill text and summaries, stores everything in SQLite, and optionally auto-analyzes bills with a local LLM.

**Primary Use Case:** Local government legislative review during Virginia General Assembly sessions, across whatever topic areas the organization currently tracks (not fixed to a single policy domain year over year).

## Architecture

```
┌──────────────┐    ┌──────────────────┐    ┌──────────────────┐
│  Topics /    │───▶│  LegiScan API    │───▶│  SQLite DB       │
│  bare bills  │    │  Pipeline        │    │  (legiscanner.db)│
│  (DB rows)   │    └──────────────────┘    └──────────────────┘
└──────────────┘                                     │
                                                       ▼
                                          ┌─────────────────────┐
                                          │  Web UI (FastAPI +  │
                                          │  HTMX) / CLI / CSV  │
                                          └─────────────────────┘
```

Discovery is two-layered: `legiscanner discover` runs each tracked topic's
`search_keywords` against LegiScan's `search` operation (additive — only adds
bill numbers not already tracked); `legiscanner sync-session` pulls the full
session master list via `getMasterList` as a periodic safety net for anything
keyword search misses. Both only add bare bill-number rows — `legiscanner run`
does the actual `getBill`/`getBillText` fetch and fills in full detail.

There is no more "combined.json" or per-bill JSON files. All state — bill
metadata, text, sponsors, history, tags, tracking, companion links — lives in
one SQLite database file, spanning all sessions (cross-session queryable).

## Tech Stack

- **Language:** Python 3.11+
- **API Client:** Custom LegiScan wrapper (`client.py`)
- **Storage:** SQLite (stdlib `sqlite3`, no ORM — see `db.py`)
- **Web:** FastAPI + HTMX + Jinja2 (`web/`)
- **CLI:** Typer (`cli.py`)
- **LLM tagging:** Any OpenAI-compatible chat endpoint (Ollama, llama.cpp) — configured per session in the DB, not hardcoded
- **State Focus:** Virginia (VA) - state code for LegiScan

## LegiScan API Reference

### Authentication
- API Key required (free registration at https://legiscan.com/legiscan)
- Store key in environment variable: `LEGISCAN_API_KEY`
- Rate limit: 30,000 queries/month on free tier

### Base URL
```
https://api.legiscan.com/?key={API_KEY}&op={OPERATION}&{PARAMS}
```

### Key Endpoints Used

| Operation | Purpose | Key Parameters | Used by |
|-----------|---------|----------------|---------|
| `getSessionList` | List legislative sessions | `state=VA` | `legiscanner sessions` |
| `getMasterList` | All bills in a session | `state=VA` or `id={session_id}` | `legiscanner sync-session` |
| `getBill` | Full bill details | `id={bill_id}` OR `state=VA&bill={bill_number}` | `legiscanner run`, `fetch` |
| `getBillText` | Retrieve bill text content | `id={doc_id}` | `legiscanner run` |
| `search` | Search bills by keyword | `state=VA&query={term}` | `legiscanner discover`, `search` |

**Field-name gotcha:** `getMasterList` returns bill number under the key
`"number"`, while `getBill`/`search`/`search_all` use `"bill_number"`. The
`getMasterList` response dict also includes a non-bill `"session"` metadata
entry mixed in with bill stubs — filter on presence of `"number"` to skip it.
(This tripped up `sync-session` during development — see `cli.py`.)

**Note:** Bill text is base64-encoded in the raw API response;
`client.get_decoded_bill_text()` handles decoding. PDF text extraction is
out of scope — bill text is served as plain text/HTML elsewhere on the open
web, so PDF versions are stored as a placeholder pointing at `state_url`.

## Project Structure

```
legiscanner/
├── CLAUDE.md                 # This file - project context
├── README.md                 # User documentation
├── DEPLOYMENT.md             # Linux server / Cloudflare / systemd setup
├── pyproject.toml            # Project dependencies
├── .env.example               # Environment variable template
├── legiscanner.db             # SQLite database (all bill/session/tracking data) — git-ignored
├── tests/                     # pytest suite (conftest.py + test_db.py)
├── src/
│   └── legiscanner/
│       ├── cli.py             # Typer CLI (run, discover, sync-session, autotag, link-lineages, serve, ...)
│       ├── client.py          # LegiScan API client wrapper (cache TTL + rate-limit budget)
│       ├── db.py              # SQLite connection + query/mutation layer — the data-access boundary
│       ├── schema.sql         # DDL, applied idempotently via db.init_db()
│       ├── migrations/        # Ordered, versioned migrations (run by init_db via schema_version)
│       │   └── 0001_seed_tracking_history.py
│       ├── lineages.py        # Cross-session bill-lineage detection (title similarity + LegiScan IDs)
│       ├── outcome_areas.py   # Fixed outcome-area taxonomy for impact scoring
│       ├── pipeline.py        # Fetch/extract bills from LegiScan, persist via db.py
│       ├── tagger.py          # LLM auto-tagging, prompt built from the session's `topics` table
│       ├── companions.py      # Companion bill (HB/SB pair) detection, pure functions + DB write path
│       ├── models.py          # Pydantic models: LegiScan API shapes + BillOutput DTO
│       ├── scripts/
│       │   └── import_legacy_json.py  # One-time import of a pre-SQLite combined.json
│       └── web/
│           ├── app.py         # FastAPI app factory
│           ├── routes.py      # HTTP routes — all reads/writes go through db.py
│           └── templates/     # Jinja2 templates (bill list, detail, tracking, chat, lineage)
├── config/
│   └── bills.json             # Optional hand-curated bill list (legiscanner import-config)
└── output/
    └── texts/                 # On-demand plaintext export (legiscanner export-text), not auto-written
```

## Database Schema

See `src/legiscanner/schema.sql` for the authoritative DDL. Summary:

- **`sessions`** — one row per (state, legislative session), holds the LLM
  config (`ollama_url`, `model`) for that session. `active = 1` marks which
  session the web app and CLI default to.
- **`topics`** — the tag vocabulary, scoped per session. `description` feeds
  the AI tagging prompt; `search_keywords` (optional) feeds `discover`;
  `is_ai_taggable` excludes a topic from the AI prompt while still allowing
  it as a manual tag. This is what replaced the old hardcoded
  housing/zoning/land-use prompt strings — editing this table changes what
  the AI looks for, no code changes needed.
- **`bills`**, **`bill_texts`**, **`sponsors`**, **`history`** — core bill
  data, straightforward 1:many from `bills`.
- **`bill_topics`** — tag links, composite key `(bill_id, topic_id, source)`
  where `source` is `'ai'` or `'user'`. The same topic can be attached via
  both sources independently (mirrors the old `ai_tags`/`user_tags` split).
- **`tracking`** — 1:1 with `bills`. Watching status, notes, AI impact score,
  user impact override. Split from `bills` to keep the frequently-mutated
  web-app fields separate from pipeline-owned metadata.
- **`companions`** — one row per unordered HB/SB pair
  (`CHECK(bill_id < companion_bill_id)` enforces canonical ordering);
  `companion_pairs_expanded` view gives symmetric reads.
- **`bill_lineages`** / **`bill_lineage_members`** — cross-session bill
  lineages. A lineage groups the same (or same-subject) bill re-introduced
  across sessions; `UNIQUE(bill_id)` means each bill belongs to at most one
  lineage. `link_reason` is `title-similar`, `same-legiscan-bill`, or `manual`
  (manual lineages are preserved across re-runs of `link-lineages`). Detection
  lives in `lineages.py` (title similarity reusing `companions.py` logic,
  shared LegiScan bill ID, sponsor-overlap boost, union-find grouping).
- **`tracking_history`** — audit trail of tracking changes (notes/watching/
  impact). Seeded once for existing bills by migration 0001, then written by
  an `AFTER UPDATE` trigger on `tracking`. This is what lets notes/analysis
  survive across sessions instead of being clobbered on re-discovery.
- **`analysis_runs`** — LLM analysis provenance (model, timestamp, bill,
  source) so `autotag` runs are resumable/auditable.
- **`schema_version`** — tracks applied migrations; `db.init_db` runs
  `schema.sql` then any unapplied scripts in `migrations/` in order.
- **`bills_fts`** — FTS5 full-text index over bill_number/title/description/
  ai_reason, kept in sync via triggers. Replaces an O(n) Python substring
  scan that used to run on every search keystroke.

## Key Implementation Notes

### Data access pattern

`db.py` is the only module that writes raw SQL. Every read function that
returns bill data builds a `BillOutput` (from `models.py`) and returns
`.model_dump()`, so the dict shape is guaranteed to match what
`web/templates/*.html` expect — this is why the SQLite migration required
zero template changes. Don't bypass `db.py` from routes or the pipeline;
add a function there instead.

### SQLite connection notes

- `check_same_thread=False` is required — FastAPI's sync route handlers and
  their `Depends()` dependencies don't run on a guaranteed-consistent OS
  thread (Starlette threadpools them independently), so a request-scoped
  connection needs this relaxed even though it's never shared across
  requests.
- `journal_mode = WAL` — the web app and a background pipeline-trigger
  thread can hold the DB open concurrently; WAL allows concurrent readers
  during a writer.
- After an `INSERT ... ON CONFLICT DO UPDATE`, `cursor.lastrowid` is
  **not reliable** — it can reflect the rowid SQLite would have assigned
  the attempted insert, not the existing row's actual id. Always resolve
  the row by its natural key after an upsert (see `db.upsert_bill`).

### Handling Bill Text
1. Call `getBill` to get bill metadata including `texts` array
2. For each text version, call `getBillText` with the `doc_id`
3. Decode base64 content: `base64.b64decode(response['doc'])`
4. Handle MIME types:
   - `text/html`: Sanitized (scripts/styles/attrs stripped) via `sanitize_html()`
   - `application/pdf`: Out of scope — stored as a placeholder note pointing at `state_url`
   - `text/plain`: Used directly

### Virginia-Specific Notes
- State code: `VA`
- Virginia General Assembly sessions typically run January-March
- Bill prefixes: `HB` (House Bill), `SB` (Senate Bill), `HJ`/`HJR` (House Joint Resolution), `SJ`/`SJR` (Senate Joint Resolution)
- Virginia Legislative Information System (LIS): https://lis.virginia.gov/ — considered as a direct data source instead of LegiScan, but LIS's own API registration page is a client-rendered SPA whose capabilities couldn't be verified; deferred, LegiScan stays the source of truth for now

### Rate Limiting Strategy
- Free tier: 30,000 requests/month (~1,000/day)
- Retries with exponential backoff on transient errors (via `tenacity`)
- File-based response caching in `client.py` (optional `cache_dir`) with a
  configurable TTL (`cache_ttl`, default 1h) — stale cached responses are
  re-fetched rather than served forever
- Proactive client-side budget in `client.py` (`requests_per_minute`, default
  100) throttles request *issuance* so a bulk `run` doesn't burst into the
  LegiScan 429 band, on top of the backoff-on-429
- Use `change_hash` (via `legiscanner run --update`) to skip unchanged bills

### Error Handling
- Handle API errors gracefully (rate limits, timeouts) — see `LegiScanError` hierarchy in `client.py`
- Log failures without stopping entire pipeline run
- Retry transient failures with backoff
- Report which bills failed at end of a `run`

## Dependencies

```toml
[project]
dependencies = [
    "httpx>=0.28.0",
    "pydantic>=2.0",
    "pydantic-settings>=2.0",
    "python-dotenv>=1.0.0",
    "rich>=13.0",
    "typer>=0.12.0",
    "beautifulsoup4>=4.12.3",
    "tenacity>=8.2.0",
    "fastapi>=0.115.0",
    "uvicorn>=0.30.0",
    "jinja2>=3.1.6",
    "python-multipart>=0.0.18",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=4.0",
    "ruff>=0.4.0",
    "mypy>=1.8",
]
```

No SQL library dependency — `db.py` uses stdlib `sqlite3` directly. See
README.md's Quick Start for CLI usage.

## Environment Variables

| Variable | Required | Description |
|----------|----------|--------------|
| `LEGISCAN_API_KEY` | Yes | Your LegiScan API key |
| `LEGISCAN_CACHE_DIR` | No | Directory for caching LegiScan API responses |
| `LOG_LEVEL` | No | Logging level (DEBUG, INFO, WARNING, ERROR) |

LLM endpoint/model are **not** environment variables — they're stored per-session in the `sessions` table (`ollama_url`, `model` columns). See README.md's Configuration section.

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=legiscanner
```

```bash
# Run all tests (no network / no real DB — uses a temp SQLite file)
pytest tests/ -q

# With coverage
pytest tests/ --cov=legiscanner
```

The suite (`tests/test_db.py`, fixtures in `tests/conftest.py`) covers the
`db.py` query/mutation layer: sessions, upsert idempotency, bill-text
deduplication, filtering, FTS search, outcome-score triggers, companion
linking, bill lineages, and tracking history. It runs against a temp database
so it never touches `legiscanner.db` or the network.

## References

- [LegiScan API Documentation](https://legiscan.com/gaits/documentation/legiscan)
- [LegiScan API User Manual (PDF)](https://api.legiscan.com/dl/LegiScan_API_User_Manual.pdf)
- [Virginia LIS](https://lis.virginia.gov/)
- [LegiScan Virginia Page](https://legiscan.com/VA)
