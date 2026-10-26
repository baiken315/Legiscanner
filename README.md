# Legiscanner

Virginia Legislative Bill Scanning Tool for Local Government.

Tracks Virginia General Assembly bills across topics you configure, pulls full
bill data (text, sponsors, history) from the LegiScan API, stores everything
in a single SQLite database, and optionally auto-analyzes bills with a local
LLM. Includes a web interface for browsing, tagging, and taking notes.

## Features

- **Topic-driven discovery**: Configure tracked topics with search keywords; `discover` finds matching bills via LegiScan search, `sync-session` catches anything discovery misses via a full session sync
- **SQLite storage**: One database file, cross-session queryable, no JSON-file rewrites on every edit
- **Full Bill Data**: Fetches complete bill text, sponsors, history, and status
- **Smart Updates**: Only re-fetches bills that have changed (using LegiScan's `change_hash`)
- **AI Analysis**: Auto-generate tags, relevance reasoning, and impact scores using a local LLM (Ollama or llama.cpp), driven by your own topic vocabulary — not hardcoded categories
- **Web Interface**: Interactive bill browser with re-analysis and an AI legislative assistant
- **Companion bill detection**: Automatically links House/Senate companion bills by title similarity
- **Cross-session bill lineages**: Tracks how the same bill evolves across legislative sessions (title-similarity detection), with a lineage view + cross-session diff in the UI
- **Multi-session**: One database spans all legislative sessions; browse any session via the UI switcher or `--session` CLI flag
- **Tracking history**: Notes/watch/analysis changes are journaled (`tracking_history`) so context survives across sessions

## Quick Start

```bash
# 1. Install
pip install -e .

# 2. Set your API key
export LEGISCAN_API_KEY="your-key-here"

# 3a. Bring in an existing bill list (hand-curated), OR discover by topic (3b)
legiscanner import-config config/bills.json --db legiscanner.db

# 3b. Or configure topics with search keywords in the DB, then:
legiscanner discover --db legiscanner.db --session-id 2237

# 4. Fetch full bill data
legiscanner run --db legiscanner.db

# 5. Auto-analyze with your configured LLM (optional)
legiscanner autotag --db legiscanner.db --full

# 6. Start web interface
legiscanner serve --db legiscanner.db
```

## Architecture

Everything lives in one SQLite database (default `legiscanner.db`):

| Table | Purpose |
|---|---|
| `sessions` | Legislative sessions tracked (state, year, LLM config) — one DB spans all sessions |
| `topics` | Your tracked topic vocabulary per session (drives both AI tagging prompts and discovery search) |
| `bills` | Core bill metadata |
| `bill_texts`, `sponsors`, `history` | Bill detail, one row per item |
| `bill_topics` | Tag links, tracked separately for `ai`- vs `user`-sourced tags |
| `tracking` | Watching status, notes, impact scores (AI + user override) |
| `tracking_history` | Audit trail of tracking changes (seeded at first run, then by a trigger) |
| `companions` | Detected House/Senate companion bill pairs |
| `bill_lineages`, `bill_lineage_members` | Cross-session bill lineages (a bill's evolution across sessions) |
| `analysis_runs` | LLM analysis provenance (model, timestamp, per-bill run) |
| `schema_version` | Tracks applied schema migrations |
| `bills_fts` | Full-text search index over bill number/title/description/AI analysis |

## Commands

### Discovery

```bash
legiscanner sessions                          # List LegiScan session IDs for a state
legiscanner discover --session-id 2237        # Search each topic's keywords, add new matches
legiscanner sync-session --session-id 2237    # Full session sync (safety net, heavier)
legiscanner import-config config/bills.json   # Add bills from a hand-curated list
legiscanner search "housing"                  # One-off LegiScan keyword search (display only)
```

`discover` and `sync-session` only add bare bill-number rows — run `legiscanner run` afterward to fetch full detail.

### `legiscanner run`

Fetch full bill data from LegiScan for everything currently tracked in the DB.

```bash
legiscanner run --db legiscanner.db                 # Full refresh
legiscanner run --db legiscanner.db --update         # Smart update (only changed bills)
legiscanner run --db legiscanner.db --skip-existing  # Skip bills already in the DB
```

### `legiscanner autotag`

Auto-analyze bills using your configured LLM (Ollama or llama.cpp, OpenAI-compatible
chat endpoint). Tag vocabulary and impact-scoring context are built from the active
session's `topics` table — edit that table to change what the AI looks for.

```bash
legiscanner autotag --db legiscanner.db                    # Tags only (fast)
legiscanner autotag --db legiscanner.db --full              # Tags + reason + impact score
legiscanner autotag --db legiscanner.db --full --new-only   # Only untagged bills
legiscanner autotag --db legiscanner.db --full --bill HB967  # Retag one bill
legiscanner autotag --db legiscanner.db --model qwen3.6-35b-a3b --ollama-url http://host:8080/
```

### `legiscanner serve`

Start the interactive web interface with AI-powered features.

```bash
legiscanner serve --db legiscanner.db        # Start on http://127.0.0.1:8000
legiscanner serve --db legiscanner.db --port 8080
legiscanner serve --db legiscanner.db --reload   # Development mode
```

Features:
- Bill browser with search, filter (impact, tags, watching), and sort
- **Re-analyze button**: Retag any bill directly from the UI
- **Legislative Assistant**: AI chat for analyzing bills, political scenarios, and local government implications
- **Bulk actions**: Watch/unwatch, tag/untag across a filtered set
- **CSV export**: Configurable columns, respects current filters

### `legiscanner link-companions`

Recompute House/Senate companion bill pairs by title similarity.

```bash
legiscanner link-companions --db legiscanner.db --threshold 0.90
```

### `legiscanner link-lineages`

Recompute cross-session bill lineages from cross-session title similarity
(and shared LegiScan bill IDs). Run this after adding bills from a new session
so the app can connect re-introduced bills across years.

```bash
legiscanner link-lineages --db legiscanner.db
legiscanner link-lineages --db legiscanner.db --threshold 0.90   # stricter matching
```

### `legiscanner sessions-local`

List the legislative sessions in your local database (with bill counts) and
optionally set one as the active default.

```bash
legiscanner sessions-local --db legiscanner.db
legiscanner sessions-local --db legiscanner.db --make-active 3
```

### `legiscanner backup` / `legiscanner compact`

The database is the system of record and is **not** committed to git — keep a
backup before risky operations, and compact to reclaim disk space.

```bash
legiscanner backup --db legiscanner.db -o backups          # consistent copy (sqlite3 .backup)
legiscanner backup --db legiscanner.db --keep 14           # prune to last 14 backups
legiscanner compact --db legiscanner.db                    # VACUUM INTO a smaller file
```

### `--session` flag

The session-scoped commands (`discover`, `sync-session`, `autotag`, `export-text`,
`link-companions`, `seed-outcome-areas`) accept `--session <id>` to target a
specific session instead of the active one. `--session` also works on the web
app via the `?session=` query param or the in-page switcher.

### `legiscanner export-text`

Export current DB bill content as plain text files, for external RAG/embedding
pipelines that want files on disk rather than DB access.

```bash
legiscanner export-text --db legiscanner.db --output output/texts
```

### `legiscanner migrate-legacy`

One-time import of a legacy `combined.json` (from the pre-SQLite version of
Legiscanner) into the database. Idempotent — safe to re-run.

```bash
legiscanner migrate-legacy output/combined.json --db legiscanner.db \
  --state VA --session-name "2026 Regular Session" --year 2026
```

### Other Commands

```bash
legiscanner fetch HB1234    # Fetch and display a single bill (debug, doesn't write to DB)
legiscanner version         # Show version
```

## Configuration

### Environment Variables

```bash
export LEGISCAN_API_KEY="your-api-key"
```

### LLM Setup (for auto-tagging)

The LLM endpoint and model are stored per-session in the database (`sessions.ollama_url`,
`sessions.model`), not in environment variables or code constants — update them directly:

```sql
UPDATE sessions SET ollama_url = 'http://your-host:8080/', model = 'your-model-name'
WHERE active = 1;
```

`--ollama-url`/`--model` flags on `legiscanner autotag` override the session config for
a single run.

### Topic Vocabulary

Tags aren't hardcoded — they come from the `topics` table, scoped per session:

```sql
INSERT INTO topics (session_id, name, description, search_keywords, is_ai_taggable)
VALUES (1, 'housing', 'Housing supply, affordability, landlord-tenant issues',
        'housing OR affordable housing OR dwelling', 1);
```

- `description` shapes the AI tagging prompt
- `search_keywords` (optional) drives `legiscanner discover` for that topic
- `is_ai_taggable = 0` excludes a topic from the AI prompt while still allowing it as a manual user tag

## Workflow Examples

### Fresh Start

```bash
# Configure topics with search keywords (via SQL, or a future admin UI), then:
legiscanner sessions                          # Find your VA session ID
legiscanner discover --session-id 2237
legiscanner run --db legiscanner.db
legiscanner autotag --db legiscanner.db --full
legiscanner serve --db legiscanner.db
```

### Daily Update

```bash
legiscanner run --db legiscanner.db --update
legiscanner autotag --db legiscanner.db --full --new-only
legiscanner link-companions --db legiscanner.db
```

### Adding a New Legislative Session (cross-session history)

When a new session starts, bring in its bills and connect them to last year's:

```bash
legiscanner sessions                              # find the new session's LegiScan ID
legiscanner discover --session-id 2312 --session 2   # add bills for the new local session
legiscanner run --db legiscanner.db
legiscanner autotag --db legiscanner.db --full
legiscanner link-companions --db legiscanner.db
legiscanner link-lineages --db legiscanner.db      # connect re-introduced bills across sessions
```

After `link-lineages`, re-introduced bills show a &#8635; indicator, the **Lineages**
link lists the lineages, and each bill's detail modal gains a Lineage tab with a
cross-session timeline + diff.

### From a Spreadsheet / Hand-Curated List

```json
// config/bills.json
{
  "session": "2026 Regular Session",
  "state": "VA",
  "bills": [
    {"bill_number": "HB1234", "tracking_reason": "Housing supply", "priority": "high", "tags": []},
    {"bill_number": "SB567", "tracking_reason": "Zoning reform", "priority": "medium", "tags": []}
  ]
}
```

```bash
legiscanner import-config config/bills.json --db legiscanner.db
legiscanner run --db legiscanner.db
legiscanner autotag --db legiscanner.db --full
```

## Security

Legiscanner's web UI is designed for **single-user, local use** and has **no
authentication or CSRF protection**. Anyone who can reach it can edit notes and
tags, trigger LegiScan fetches (using your API quota), and use your LLM via the
chat feature. `legiscanner serve` binds to `127.0.0.1` by default; if you bind
to `0.0.0.0` or publish it through a tunnel, put an access-control layer in front
of it (e.g. Cloudflare Access, a VPN, or a reverse proxy with auth).

Keep `.env` (your LegiScan API key) and `legiscanner.db` (your notes and
analysis) out of version control; both are git-ignored.

To report a security issue, please open a GitHub issue without exploit details
and ask for a private contact, or use GitHub's private vulnerability reporting.

## Roadmap

- Built-in authentication and CSRF protection for the web UI (multi-user / hosted use)
- Content-Security-Policy (requires moving inline scripts/handlers out of the templates)
- Admin UI for managing topics and sessions (currently edited via SQL)

## License

MIT — see [LICENSE](LICENSE).
