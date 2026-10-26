# Deployment Guide - Linux Server Setup

This guide covers setting up Legiscanner on your Linux server with Cloudflare access.

## Prerequisites

- Python 3.11+
- Git
- Optional: an OpenAI-compatible LLM server (Ollama, llama.cpp, ...) reachable from this machine, for AI tagging and chat
- Cloudflared tunnel configured

## Initial Setup on Linux Server

### 1. Clone the Repository

```bash
# SSH into your Linux server
ssh user@your-server

# Clone the repository
git clone https://github.com/baiken315/legiscanner.git
cd legiscanner
```

### 2. Create Virtual Environment

```bash
# Create venv
python3 -m venv venv

# Activate venv
source venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -e .
```

### 4. Configure Environment

```bash
# Copy the example .env file
cp .env.example .env

# Edit .env and add your LegiScan API key
nano .env
```

Add your API key:
```
LEGISCAN_API_KEY=your-key-here
```

### 5. Transfer Existing Data (Optional)

All bill data lives in a single SQLite database file (`legiscanner.db`).
The DB is the **system of record** and is **not committed to git** (it's
git-ignored) — sync it by copy, not by version control:

```bash
# From your MacBook
scp legiscanner.db user@your-server:/path/to/Legiscanner/
scp config/bills.json user@your-server:/path/to/Legiscanner/config/
```

Don't sync the DB from two machines at once — SQLite's file-level locking
doesn't handle concurrent writers well, so treat one machine as the source
of truth per sync cycle (copy before you edit, copy back after).

Before moving or bulk-editing the live DB, take a backup first:

```bash
legiscanner backup --db legiscanner.db -o backups
```

## Running the Web Server

### Option 1: Development Mode

```bash
# Activate venv
source venv/bin/activate

# Start the server
legiscanner serve --port 8000   # binds 127.0.0.1 by default
```

### Option 2: Production with systemd

Create a systemd service file:

```bash
sudo nano /etc/systemd/system/legiscanner.service
```

Add this configuration:
```ini
[Unit]
Description=Legiscanner Web Interface
After=network.target

[Service]
Type=simple
User=your-username
WorkingDirectory=/path/to/Legiscanner
Environment="PATH=/path/to/Legiscanner/venv/bin"
ExecStart=/path/to/Legiscanner/venv/bin/legiscanner serve --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

Enable and start the service:
```bash
sudo systemctl enable legiscanner
sudo systemctl start legiscanner
sudo systemctl status legiscanner
```

View logs:
```bash
sudo journalctl -u legiscanner -f
```

## Cloudflare Tunnel Configuration

> **Warning:** Legiscanner has no built-in authentication. Anyone who can reach
> the web UI can edit notes and tags, trigger pipeline runs (spending your
> LegiScan API quota), and use your LLM through the chat feature. If you expose
> it through a tunnel, put an identity layer in front of it (e.g. Cloudflare
> Access) and restrict it to yourself.

If you haven't already configured a Cloudflare tunnel for this service:

```bash
# In your cloudflared config (usually ~/.cloudflared/config.yml or /etc/cloudflared/config.yml)
# Add an ingress rule:

ingress:
  - hostname: legiscanner.your-domain.com
    service: http://localhost:8000
  # ... other rules ...
  - service: http_status:404
```

Restart cloudflared:
```bash
sudo systemctl restart cloudflared
```

## Ongoing Development Workflow

### On MacBook (Development)

```bash
# Make changes to code
# ... edit files ...

# Commit and push
git add .
git commit -m "Description of changes"
git push
```

### On Linux Server (Production)

```bash
# Pull latest changes
cd /path/to/Legiscanner
source venv/bin/activate
git pull

# Restart the service (if using systemd)
sudo systemctl restart legiscanner

# Or restart manually if running in development mode
# Ctrl+C to stop, then run again:
legiscanner serve --port 8000
```

## Updating Bills

You can run bill updates from any machine that can reach your LLM server:

```bash
# Update bills from LegiScan
legiscanner run --db legiscanner.db --update

# Re-analyze bills (only new ones)
legiscanner autotag --db legiscanner.db --full --new-only

# After adding bills from a new session, connect cross-session lineages
legiscanner link-lineages --db legiscanner.db

# Back up the updated database (it's not tracked in git)
legiscanner backup --db legiscanner.db -o backups
```

> **Restart the web server after pulling new code.** The server loads routes
> and templates into memory at startup, so a `git pull` alone won't pick up
> new pages (e.g. `/lineage`) until the process is restarted:
> `sudo systemctl restart legiscanner` (or kill + relaunch if running with
> `nohup`).

## Configuration Notes

### LLM URL
The LLM endpoint and model are stored per session in the database (`sessions.ollama_url`, `sessions.model`), not in `.env`. Point them at whatever OpenAI-compatible server you run (e.g. `http://localhost:11434/` for Ollama).

### Data Directory
- `legiscanner.db` - SQLite database with all bill/session/tracking data.
  **Git-ignored** (the system of record) — sync by copy, not version control.
- `legiscanner.db-shm` / `legiscanner.db-wal` - SQLite WAL sidecar files
  (also git-ignored); safe to delete when no process has the DB open.
- `backups/` - Output of `legiscanner backup`; git-ignored.
- `config/bills.json` - Optional hand-curated tracked bills list, used by
  `legiscanner import-config` (git-ignored by default).
- `output/texts/` - On-demand plaintext export (`legiscanner export-text`),
  not written automatically (git-ignored).

### Keeping the Database Safe

The DB is the system of record and is **not** in git, so manage it directly:

```bash
# Take a consistent backup before risky operations (uses sqlite3 .backup, safe
# to run while the server is up)
legiscanner backup --db legiscanner.db -o backups
legiscanner backup --db legiscanner.db --keep 14   # prune to the last 14

# Reclaim disk space after deletions (VACUUM INTO a smaller file)
legiscanner compact --db legiscanner.db
```

To sync data to another machine, copy the file (never run writers on two
copies at once):

```bash
scp legiscanner.db user@other-host:/path/to/Legiscanner/
```

## Troubleshooting

### Port already in use
```bash
# Find what's using port 8000
sudo lsof -i :8000

# Kill the process or use a different port
legiscanner serve --port 8001
```

### Permission errors
```bash
# Make sure your user owns the directory
sudo chown -R your-username:your-username /path/to/Legiscanner
```

### LLM connection issues
```bash
# Test connectivity to your OpenAI-compatible LLM server
curl http://your-llm-host:11434/v1/models
```

## Security Notes

- **No authentication.** The web UI is meant for single-user/local use. `serve` binds to
  `127.0.0.1` by default; only bind `0.0.0.0` or expose it via a tunnel if access is
  restricted some other way (see the Cloudflare warning above). Built-in auth is on the roadmap.
- The `.env` file containing your LegiScan API key is git-ignored; each machine needs its own.
- The database (`*.db`) is git-ignored. Don't commit it: it holds your notes and analysis.
- The LegiScan API sends the key as a URL query parameter, so the client keeps `httpx`
  request logging off. Don't enable DEBUG logging for `httpx` in production.
