"""Command-line interface for Legiscanner."""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from . import __version__, db
from .client import LegiScanClient, LegiScanError
from .pipeline import Pipeline, normalize_bill_number

# Load environment variables from .env file
load_dotenv()

app = typer.Typer(
    name="legiscanner",
    help="Virginia Legislative Bill Scanning Tool for Local Government",
    add_completion=False,
)
console = Console()


def setup_logging(verbose: bool = False) -> None:
    """Configure logging with rich output."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_time=False, show_path=False)],
    )


@app.command()
def run(
    config: Path = typer.Option(
        Path("config/bills.json"),
        "--config",
        "-c",
        help="Path to bill configuration JSON file",
    ),
    db_path: Path = typer.Option(
        Path("legiscanner.db"),
        "--db",
        help="Path to the SQLite database file",
    ),
    cache: Path | None = typer.Option(
        None,
        "--cache",
        help="Cache directory for API responses",
    ),
    skip_existing: bool = typer.Option(
        False,
        "--skip-existing",
        "-s",
        help="Skip bills that already have output files",
    ),
    update: bool = typer.Option(
        False,
        "--update",
        "-u",
        help="Smart update: only fetch new bills and bills that have changed (uses change_hash)",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose logging",
    ),
) -> None:
    """
    Run the bill scanning pipeline.

    Fetches all bills from the configuration file, retrieves full text,
    and stores the results in the SQLite database.

    Modes:
      - Default: Fetch all bills fresh
      - --skip-existing: Skip bills that already exist in the DB (fast, but won't catch updates)
      - --update: Smart update - checks change_hash, only fetches new/changed bills
    """
    setup_logging(verbose)

    if not config.exists():
        console.print(f"[red]Error:[/red] Config file not found: {config}")
        console.print("Create a config file or use --config to specify a different path.")
        console.print("See config/bills.example.json for the expected format.")
        raise typer.Exit(1)

    if skip_existing and update:
        console.print("[yellow]Warning:[/yellow] Both --skip-existing and --update specified.")
        console.print("Using --update mode (smarter).")
        skip_existing = False

    console.print(f"[bold blue]Legiscanner v{__version__}[/bold blue]")
    console.print(f"Config: {config}")
    console.print(f"Database: {db_path}")
    if update:
        console.print("Mode: [cyan]Smart Update[/cyan] (checking for changes)")
    elif skip_existing:
        console.print("Mode: [cyan]Skip Existing[/cyan]")
    else:
        console.print("Mode: [cyan]Full Refresh[/cyan]")
    console.print()

    try:
        with Pipeline(cache_dir=cache, db_path=db_path) as pipeline:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                if update:
                    progress.add_task("Checking for bill updates...", total=None)
                else:
                    progress.add_task("Fetching bills from LegiScan...", total=None)
                result = pipeline.run(
                    config_path=config,
                    skip_existing=skip_existing,
                    update_mode=update,
                )

        # Summary
        console.print()
        console.print("[bold green]Pipeline Complete![/bold green]")
        console.print(f"Total bills: {result['bill_count']}")

        if update:
            new_count = len(result.get("new_bills", []))
            updated_count = len(result.get("updated_bills", []))
            unchanged_count = len(result.get("skipped_bills", []))
            console.print(f"  [green]New:[/green] {new_count}")
            console.print(f"  [cyan]Updated:[/cyan] {updated_count}")
            console.print(f"  [dim]Unchanged:[/dim] {unchanged_count}")

            if result.get("new_bills"):
                console.print(f"  New bills: {', '.join(result['new_bills'])}")
            if result.get("updated_bills"):
                console.print(f"  Updated bills: {', '.join(result['updated_bills'])}")

        if result.get("failed_bills"):
            console.print(f"[yellow]Failed bills:[/yellow] {', '.join(result['failed_bills'])}")

        if not update and result.get("skipped_bills"):
            console.print(f"[dim]Skipped (existing):[/dim] {len(result['skipped_bills'])} bills")

        console.print(f"[dim]Companion pairs:[/dim] {result.get('companion_pairs', 0)}")
        console.print()
        console.print(f"[bold]Database:[/bold] {db_path}")

    except ValueError as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        raise typer.Exit(1)
    except LegiScanError as e:
        console.print(f"[red]API error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def fetch(
    bill_number: str = typer.Argument(..., help="Bill number to fetch (e.g., HB1234)"),
    state: str = typer.Option("VA", "--state", "-s", help="State abbreviation"),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Output file path (prints to stdout if not specified)"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Fetch a single bill from LegiScan.

    Quick way to retrieve and view a specific bill's details. Debug/inspection
    command — does not write to the database.
    """
    setup_logging(verbose)

    bill_number = normalize_bill_number(bill_number)
    console.print(f"Fetching {state} {bill_number}...")

    try:
        with LegiScanClient() as client:
            bill = client.get_bill(state=state, bill_number=bill_number)

        console.print()
        console.print(f"[bold]{bill.bill_number}[/bold]: {bill.title}")
        console.print(f"Status: {bill.status_desc}")
        console.print(f"Description: {bill.description}")

        if bill.sponsors:
            sponsors = [s.name or "Unknown" for s in bill.sponsors[:5]]
            console.print(f"Sponsors: {', '.join(sponsors)}")

        if bill.texts:
            console.print(f"Text versions: {len(bill.texts)}")
            for t in bill.texts:
                console.print(f"  - {t.type} ({t.date}): {t.text_size or 'unknown'} bytes")

        if output:
            with open(output, "w") as f:
                json.dump(bill.model_dump(), f, indent=2)
            console.print(f"\nSaved to: {output}")

    except LegiScanError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def sessions(
    state: str = typer.Option("VA", "--state", "-s", help="State abbreviation"),
) -> None:
    """
    List available legislative sessions for a state (from LegiScan).
    """
    setup_logging(False)

    try:
        with LegiScanClient() as client:
            sessions_list = client.get_session_list(state=state)

        table = Table(title=f"Legislative Sessions - {state}")
        table.add_column("Session ID", style="cyan")
        table.add_column("Name", style="green")
        table.add_column("Years")
        table.add_column("Special")

        for session in sessions_list:
            years = f"{session.get('year_start', '?')}-{session.get('year_end', '?')}"
            special = "Yes" if session.get("special") else "No"
            table.add_row(
                str(session.get("session_id", "?")),
                session.get("session_name", "Unknown"),
                years,
                special,
            )

        console.print(table)

    except LegiScanError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    state: str = typer.Option("VA", "--state", "-s", help="State abbreviation"),
    year: int | None = typer.Option(None, "--year", "-y", help="Filter by year"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum results to display"),
) -> None:
    """
    Search for bills by keyword (LegiScan search, display only — does not track).
    """
    setup_logging(False)

    try:
        with LegiScanClient() as client:
            results = client.search(state=state, query=query, year=year)

        summary = results.get("summary", {})
        bills = [v for k, v in results.items() if k != "summary" and isinstance(v, dict)]

        console.print(f"Found {summary.get('count', 0)} results for '{query}'")
        console.print()

        table = Table()
        table.add_column("Bill", style="cyan")
        table.add_column("Title", max_width=60)
        table.add_column("Last Action", max_width=30)

        for bill in bills[:limit]:
            table.add_row(
                bill.get("bill_number", "?"),
                bill.get("title", "No title")[:60],
                bill.get("last_action", "")[:30],
            )

        console.print(table)

        if len(bills) > limit:
            console.print(f"[dim]Showing {limit} of {len(bills)} results[/dim]")

    except LegiScanError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


def _get_or_create_session_id(
    conn, *, state: str, session_name: str, year: int, legiscan_session_id: int | None = None
) -> int:
    return db.get_or_create_session(
        conn, state=state, name=session_name, year=year, legiscan_session_id=legiscan_session_id
    )


@app.command()
def discover(
    db_path: Path = typer.Option(Path("legiscanner.db"), "--db", help="Path to the SQLite database file"),
    state: str = typer.Option("VA", "--state", "-s", help="State abbreviation"),
    legiscan_session_id: int | None = typer.Option(
        None, "--session-id", help="LegiScan session ID to filter results (use 'legiscanner sessions' to list)"
    ),
    session: int | None = typer.Option(
        None, "--session", help="Local DB session id to operate on (defaults to the active session)"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Discover new bills by running each tracked topic's search_keywords against LegiScan.

    For every topic in the active session that has search_keywords set, runs a
    LegiScan boolean search and adds any newly-found bill numbers as bare rows
    (bill_number only — run `legiscanner run` afterward to fetch full detail).
    Additive: does not remove or overwrite bills already being tracked, and does
    NOT auto-tag matches as AI-relevant (a keyword match is not the same as
    AI-confirmed relevance — run `legiscanner autotag` for that).
    """
    setup_logging(verbose)

    conn = db.get_connection(db_path)
    db.init_db(conn)
    session_id = _resolve_session_id(conn, session)
    topics = [t for t in db.list_topics(conn, session_id) if t["search_keywords"]]
    if not topics:
        console.print("[yellow]No topics have search_keywords configured.[/yellow]")
        console.print("Set topics.search_keywords in the database to enable discovery for a topic.")
        raise typer.Exit(0)

    console.print(f"[bold blue]Discovering bills for {len(topics)} topic(s)[/bold blue]")

    seen: set[str] = set()
    new_count = 0
    try:
        with LegiScanClient() as client:
            for topic in topics:
                console.print(f"  Searching topic '{topic['name']}': {topic['search_keywords']}")
                results = client.search_all(
                    state=state, query=topic["search_keywords"], session_id=legiscan_session_id
                )
                for r in results:
                    bn = normalize_bill_number(r.get("bill_number", ""))
                    if not bn or bn in seen:
                        continue
                    seen.add(bn)
                    existing = db.get_bill(conn, session_id, bn)
                    if not existing:
                        db.bare_upsert_bill(conn, session_id, bn)
                        new_count += 1
        conn.commit()
    except LegiScanError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    console.print()
    console.print(f"[bold green]Discovery complete![/bold green]")
    console.print(f"Matched {len(seen)} bills across all topics, [green]{new_count} new[/green] added to tracking.")
    console.print("Run `legiscanner run` to fetch full detail for newly tracked bills.")


@app.command("sync-session")
def sync_session(
    db_path: Path = typer.Option(Path("legiscanner.db"), "--db", help="Path to the SQLite database file"),
    state: str = typer.Option("VA", "--state", "-s", help="State abbreviation"),
    legiscan_session_id: int | None = typer.Option(
        None, "--session-id", help="LegiScan session ID (use 'legiscanner sessions' to list)"
    ),
    session: int | None = typer.Option(
        None, "--session", help="Local DB session id to operate on (defaults to the active session)"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Full-session safety net: pull every bill stub from LegiScan's getMasterList
    and add any not already tracked. Heavier than `discover` (thousands of bills,
    not just topic matches) — intended to run less frequently, as a catch-all for
    anything topic search misses.
    """
    setup_logging(verbose)

    conn = db.get_connection(db_path)
    db.init_db(conn)
    session_id = _resolve_session_id(conn, session)

    console.print("[bold blue]Syncing full session bill list from LegiScan[/bold blue]")

    try:
        with LegiScanClient() as client:
            with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
                progress.add_task("Fetching master list...", total=None)
                master_list = client.get_master_list(state=state, session_id=legiscan_session_id)
    except LegiScanError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    new_count = 0
    for bill_stub in master_list.values():
        # getMasterList uses "number" for the bill number (unlike getBill/search,
        # which use "bill_number"); the dict also carries a non-bill "session" entry
        # with no "number" key, which this skips naturally.
        raw_number = bill_stub.get("number") if isinstance(bill_stub, dict) else None
        if not raw_number:
            continue
        bn = normalize_bill_number(raw_number)
        existing = db.get_bill(conn, session_id, bn)
        if not existing:
            db.bare_upsert_bill(conn, session_id, bn)
            new_count += 1
    conn.commit()

    console.print()
    console.print(f"[bold green]Sync complete![/bold green]")
    console.print(f"Master list had {len(master_list)} bills, [green]{new_count} new[/green] added to tracking.")
    console.print("Run `legiscanner run` to fetch full detail for newly tracked bills.")


@app.command("import-config")
def import_config(
    config: Path = typer.Argument(Path("config/bills.json"), help="Path to bills.json config file"),
    db_path: Path = typer.Option(Path("legiscanner.db"), "--db", help="Path to the SQLite database file"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Add bills from a hand-curated config/bills.json file to the tracked set.

    For the case where you already have a specific list of bill numbers (e.g. from
    an email or spreadsheet) rather than discovering them via topic search.
    """
    setup_logging(verbose)

    if not config.exists():
        console.print(f"[red]Error:[/red] Config file not found: {config}")
        raise typer.Exit(1)

    conn = db.get_connection(db_path)
    db.init_db(conn)

    with open(config) as f:
        data = json.load(f)

    session = db.get_or_create_session(
        conn, state=data.get("state", "VA"), name=data.get("session", "Unknown Session"), year=2026
    )

    new_count = 0
    for bill_input in data.get("bills", []):
        bn = normalize_bill_number(bill_input["bill_number"])
        existing = db.get_bill(conn, session, bn)
        if not existing:
            db.bare_upsert_bill(conn, session, bn)
            new_count += 1
    conn.commit()

    console.print(f"[bold green]Imported {new_count} new bill(s)[/bold green] from {config}")
    console.print("Run `legiscanner run` to fetch full detail.")


@app.command("export-text")
def export_text(
    db_path: Path = typer.Option(Path("legiscanner.db"), "--db", help="Path to the SQLite database file"),
    output: Path = typer.Option(Path("output/texts"), "--output", "-o", help="Output directory for text files"),
    session: int | None = typer.Option(
        None, "--session", help="Local DB session id to export (defaults to the active session)"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Export current DB bill content as plain text files (output/texts/{bill_number}.txt).

    On-demand replacement for the old automatic per-run text export — use this
    when you need plaintext files for an external RAG/embedding pipeline.
    """
    from .models import BillOutput
    from .pipeline import format_bill_as_text

    setup_logging(verbose)

    conn = db.get_connection(db_path)
    db.init_db(conn)
    session_id = _resolve_session_id(conn, session)

    output.mkdir(parents=True, exist_ok=True)

    bills = db.list_bills(conn, session_id, sort="bill_number")
    count = 0
    for bill_dict in bills:
        bill_output = BillOutput.model_validate(bill_dict)
        text_path = output / f"{bill_output.bill_number}.txt"
        with open(text_path, "w") as f:
            f.write(format_bill_as_text(bill_output))
        count += 1

    console.print(f"[bold green]Exported {count} bill text files[/bold green] to {output}")


@app.command()
def autotag(
    db_path: Path = typer.Option(Path("legiscanner.db"), "--db", help="Path to the SQLite database file"),
    full_analysis: bool = typer.Option(
        False, "--full", "-f", help="Full analysis: generate tags, reason, AND impact (not just tags)"
    ),
    new_only: bool = typer.Option(
        False, "--new-only", "-n", help="Only analyze bills that don't have AI tags yet"
    ),
    bill: str | None = typer.Option(
        None, "--bill", "-b", help="Retag a specific bill by number (e.g., HB967). Forces reanalysis."
    ),
    filter_impact: int | None = typer.Option(
        None, "--impact", help="Only process bills with this effective impact score (1-5)"
    ),
    exclude_tags: str | None = typer.Option(
        None, "--exclude-tags", help="Comma-separated list of tags to exclude (checks both ai_tags and user_tags)"
    ),
    ollama_url: str | None = typer.Option(
        None, "--ollama-url", help="LLM server URL override (defaults to the active session's configured URL)"
    ),
    model: str | None = typer.Option(
        None, "--model", "-m", help="Model override (defaults to the session's configured model)"
    ),
    session: int | None = typer.Option(
        None, "--session", help="Local DB session id to operate on (defaults to the active session)"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Auto-tag bills in the database using a local LLM, driven by the session's
    topic vocabulary.

    Modes:
      - Default: Generate tags only (all bills)
      - --full: Generate tags, reason, AND impact score
      - --new-only: Only analyze bills without existing AI tags (faster for daily updates)
      - --bill HB967: Retag a specific bill (forces reanalysis)
      - --impact 2: Only process bills with effective impact score 2
      - --exclude-tags ffx-watch: Skip bills tagged with specific tags
    """
    from .tagger import OllamaTagger, auto_tag_bills

    setup_logging(verbose)

    conn = db.get_connection(db_path)
    db.init_db(conn)
    session_id = _resolve_session_id(conn, session)
    session = db.get_session(conn, session_id)
    if not session:
        console.print("[red]Error:[/red] No active session in the database.")
        raise typer.Exit(1)

    resolved_url = ollama_url or session["ollama_url"]
    resolved_model = model or session["model"]

    exclude_tags_list = None
    if exclude_tags:
        exclude_tags_list = [t.strip() for t in exclude_tags.split(",") if t.strip()]

    mode_str = "Full Analysis" if full_analysis else "Tags Only"
    if bill:
        mode_str += f" (single bill: {bill})"
    elif new_only:
        mode_str += " (new bills only)"
    elif filter_impact is not None or exclude_tags_list:
        filters = []
        if filter_impact is not None:
            filters.append(f"impact={filter_impact}")
        if exclude_tags_list:
            filters.append(f"excluding: {', '.join(exclude_tags_list)}")
        mode_str += f" ({' and '.join(filters)})"

    console.print("[bold blue]Auto-analyzing bills[/bold blue]")
    console.print(f"Database: {db_path}")
    console.print(f"LLM server: {resolved_url}")
    console.print(f"Model: {resolved_model}")
    console.print(f"Mode: [cyan]{mode_str}[/cyan]")
    console.print()

    with OllamaTagger(ollama_url=resolved_url, model=resolved_model) as tagger:
        if not tagger.check_connection():
            console.print(f"[red]Error:[/red] Cannot connect to LLM server at {resolved_url}")
            raise typer.Exit(1)

        try:
            with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
                progress.add_task("Tagging bills...", total=None)
                result = auto_tag_bills(
                    conn,
                    session_id,
                    tagger=tagger,
                    full_analysis=full_analysis,
                    new_only=new_only,
                    single_bill=bill,
                    filter_impact=filter_impact,
                    exclude_tags=exclude_tags_list,
                )
        except ConnectionError as e:
            console.print(f"[red]Connection error:[/red] {e}")
            raise typer.Exit(1)

    console.print()
    console.print("[bold green]Analysis complete![/bold green]")
    console.print(f"Processed: {result['processed_count']} bills")


@app.command()
def serve(
    db_path: Path = typer.Option(Path("legiscanner.db"), "--db", help="Path to the SQLite database file"),
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Host to bind to (the app has no auth; only use 0.0.0.0 on a trusted network)"),
    port: int = typer.Option(8000, "--port", "-p", help="Port to bind to"),
    reload: bool = typer.Option(False, "--reload", "-r", help="Enable auto-reload for development"),
) -> None:
    """
    Start the web interface server.

    Launches a FastAPI server with HTMX-powered bill browser, including
    re-analysis and legislative assistant features.
    """
    try:
        import uvicorn
    except ImportError:
        console.print("[red]Error:[/red] uvicorn not installed.")
        console.print("Install web dependencies: pip install uvicorn fastapi jinja2")
        raise typer.Exit(1)

    if not db_path.exists():
        console.print(f"[red]Error:[/red] Database not found: {db_path}")
        console.print("Run the pipeline first: legiscanner run")
        raise typer.Exit(1)

    console.print("[bold blue]Legiscanner Web Interface[/bold blue]")
    console.print(f"Database: {db_path}")
    console.print(f"Server: http://{host}:{port}")
    console.print()
    console.print("[dim]Press Ctrl+C to stop[/dim]")
    console.print()

    from .web.app import create_app

    app_instance = create_app(db_path=db_path)

    uvicorn.run(app_instance, host=host, port=port, reload=reload, log_level="info")


@app.command("migrate-legacy")
def migrate_legacy(
    combined_json: Path = typer.Argument(Path("output/combined.json"), help="Path to legacy combined.json"),
    db_path: Path = typer.Option(Path("legiscanner.db"), "--db", help="Path to the SQLite database file"),
    state: str = typer.Option("VA", "--state"),
    session_name: str = typer.Option("2026 Regular Session", "--session-name"),
    year: int = typer.Option(2026, "--year"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    One-time import of a legacy combined.json file into the SQLite database.

    Safe to re-run (idempotent, keyed on bill_number within the session).
    """
    from .scripts.import_legacy_json import import_combined_json

    setup_logging(verbose)

    if not combined_json.exists():
        console.print(f"[red]Error:[/red] File not found: {combined_json}")
        raise typer.Exit(1)

    conn = db.get_connection(db_path)
    db.init_db(conn)

    console.print(f"[bold blue]Importing legacy data[/bold blue]")
    console.print(f"Source: {combined_json}")
    console.print(f"Database: {db_path}")
    console.print()

    stats = import_combined_json(conn, combined_json, state=state, session_name=session_name, year=year)

    console.print(f"Bills imported:    {stats['bills']}")
    console.print(f"Texts inserted:    {stats['texts']}")
    console.print(f"Sponsors inserted: {stats['sponsors']}")
    console.print(f"History inserted:  {stats['history']}")
    console.print(f"Topics seeded:     {stats['topics_created']}")
    console.print(f"Watching:          {stats['watching']}")

    if stats["errors"]:
        console.print(f"[red]Errors:[/red] {len(stats['errors'])}")
        for err in stats["errors"][:20]:
            console.print(f"  - {err}")
        raise typer.Exit(1)

    console.print()
    console.print("[bold green]Import complete![/bold green]")


@app.command("link-companions")
def link_companions(
    db_path: Path = typer.Option(Path("legiscanner.db"), "--db", help="Path to the SQLite database file"),
    threshold: float = typer.Option(0.90, "--threshold", "-t", help="Similarity threshold (0.0-1.0) for matching titles"),
    session: int | None = typer.Option(
        None, "--session", help="Local DB session id to operate on (defaults to the active session)"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Detect and link companion bills (HB/SB pairs with matching titles).

    Recomputes and overwrites companion links for the given (default: active) session.
    """
    from .companions import update_companion_links_db

    setup_logging(verbose)

    conn = db.get_connection(db_path)
    db.init_db(conn)
    session_id = _resolve_session_id(conn, session)

    console.print("[bold blue]Detecting companion bills[/bold blue]")
    console.print(f"Similarity threshold: {threshold:.0%}")
    console.print()

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
        progress.add_task("Searching for companion bills...", total=None)
        count = update_companion_links_db(conn, session_id, similarity_threshold=threshold)

    console.print()
    console.print("[bold green]Companion linking complete![/bold green]")
    console.print(f"Found {count} companion pairs")


def _resolve_session_id(conn, session_id: int | None) -> int:
    """Resolve a --session flag to a concrete id, defaulting to the active session."""
    if session_id is not None:
        session = db.get_session(conn, session_id)
        if not session:
            console.print(f"[red]Error:[/red] No session with id {session_id}.")
            raise typer.Exit(1)
        return session_id
    active = db.get_active_session(conn)
    if not active:
        console.print("[red]Error:[/red] No active session in the database.")
        raise typer.Exit(1)
    return active["id"]


@app.command("link-lineages")
def link_lineages(
    db_path: Path = typer.Option(Path("legiscanner.db"), "--db", help="Path to the SQLite database file"),
    threshold: float = typer.Option(
        0.85, "--threshold", "-t", help="Title similarity threshold (0.0-1.0) for cross-session matching"
    ),
    no_sponsor_boost: bool = typer.Option(
        False, "--no-sponsor-boost", help="Disable sponsor-overlap boost for cross-session matches"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Detect and link bill lineages across legislative sessions.

    Groups bills that are the same conceptual legislation re-introduced over time
    (bill numbers repeat every session, so HB1234 in 2025 and HB1234 in 2026 are
    different bills that may belong to the same lineage). Matches by cross-session
    title similarity (optionally boosted by shared sponsors) plus exact
    same-legiscan-bill-id identity. Manual lineages are preserved.
    """
    from .lineages import update_lineage_links_db

    setup_logging(verbose)

    conn = db.get_connection(db_path)
    db.init_db(conn)

    console.print("[bold blue]Detecting bill lineages across sessions[/bold blue]")
    console.print(f"Title similarity threshold: {threshold:.0%}")
    console.print()

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
        progress.add_task("Linking bills across sessions...", total=None)
        count = update_lineage_links_db(
            conn,
            similarity_threshold=threshold,
            use_sponsor_boost=not no_sponsor_boost,
        )

    lineages = db.list_lineages(conn)
    conn.close()
    console.print()
    console.print("[bold green]Lineage linking complete![/bold green]")
    console.print(f"Bills linked: {count} across {len(lineages)} lineage(s)")
    if lineages:
        console.print()
        table = Table(title="Lineages")
        table.add_column("ID", style="cyan")
        table.add_column("Name")
        table.add_column("Bills", justify="right")
        table.add_column("Sessions", justify="right")
        for ln in lineages[:25]:
            table.add_row(
                str(ln["id"]),
                ln["name"] or "(unnamed)",
                str(ln["member_count"]),
                str(ln["session_count"]),
            )
        console.print(table)
        if len(lineages) > 25:
            console.print(f"[dim]... and {len(lineages) - 25} more[/dim]")


@app.command("sessions-local")
def sessions_local(
    db_path: Path = typer.Option(Path("legiscanner.db"), "--db", help="Path to the SQLite database file"),
    make_active: int | None = typer.Option(
        None, "--make-active", "-a", help="Set this session id as the active session and exit"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    List sessions stored in the local database (and optionally switch the active one).

    The active session is what the pipeline, CLI, and web UI default to. Use
    `--make-active <id>` to switch which session you're operating on. (The plain
    `legiscanner sessions` command lists sessions from the LegiScan API instead.)
    """
    setup_logging(verbose)

    conn = db.get_connection(db_path)
    db.init_db(conn)

    if make_active is not None:
        db.set_active_session(conn, make_active)
        session = db.get_session(conn, make_active)
        conn.close()
        console.print(f"[bold green]Active session set to {session['id']}:[/bold green] {session['name']}")
        return

    sessions = db.list_sessions(conn)
    active = db.get_active_session(conn)
    bill_counts = {
        r["session_id"]: r["n"]
        for r in conn.execute(
            "SELECT session_id, COUNT(*) AS n FROM bills GROUP BY session_id"
        ).fetchall()
    }
    conn.close()

    if not sessions:
        console.print("[yellow]No sessions in the database.[/yellow]")
        return

    table = Table(title="Local Sessions")
    table.add_column("Active", justify="center")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="green")
    table.add_column("Year", justify="right")
    table.add_column("LegiScan ID")
    table.add_column("Bills", justify="right")
    for s in sessions:
        table.add_row(
            "*" if (active and s["id"] == active["id"]) else "",
            str(s["id"]),
            s["name"],
            str(s["year"]),
            str(s["legiscan_session_id"] or ""),
            str(bill_counts.get(s["id"], 0)),
        )
    console.print(table)
    console.print("[dim]Use --make-active <id> to switch the active session.[/dim]")


@app.command("seed-outcome-areas")
def seed_outcome_areas_cmd(
    db_path: Path = typer.Option(Path("legiscanner.db"), "--db", help="Path to the SQLite database file"),
    session: int | None = typer.Option(
        None, "--session", help="Local DB session id to seed (defaults to the active session)"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Seed (or update) the session's outcome-area taxonomy.

    Defaults to Fairfax County's 10 strategic plan community outcome areas
    (src/legiscanner/outcome_areas.py). Idempotent — safe to re-run if
    descriptions change; existing per-bill scores are unaffected.
    """
    from .outcome_areas import FAIRFAX_OUTCOME_AREAS

    setup_logging(verbose)

    conn = db.get_connection(db_path)
    db.init_db(conn)
    session_id = _resolve_session_id(conn, session)

    db.seed_outcome_areas(conn, session_id, FAIRFAX_OUTCOME_AREAS)
    conn.commit()

    console.print(f"[bold green]Seeded {len(FAIRFAX_OUTCOME_AREAS)} outcome areas[/bold green]")
    for area in FAIRFAX_OUTCOME_AREAS:
        console.print(f"  - {area['name']}")
    console.print()
    console.print("Run `legiscanner autotag --full` to score bills against these areas.")


@app.command()
def backup(
    db_path: Path = typer.Option(Path("legiscanner.db"), "--db", help="Path to the SQLite database file"),
    output: Path = typer.Option(Path("backups"), "--output", "-o", help="Directory for backup files"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Create an offline backup of the database (sqlite3 .backup).

    Produces a consistent single-file snapshot (safe even while the web server
    has the WAL DB open) at backups/legiscanner-<timestamp>.db. This is the
    recommended way to keep off-machine copies now that legiscanner.db is
    git-ignored — schedule it (cron) for durable backups.
    """
    setup_logging(verbose)

    if not db_path.exists():
        console.print(f"[red]Error:[/red] Database not found: {db_path}")
        raise typer.Exit(1)

    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = output / f"legiscanner-{stamp}.db"

    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    # Verify the backup is readable.
    check = sqlite3.connect(str(dest))
    try:
        n = check.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
    except sqlite3.Error:
        n = -1
    finally:
        check.close()

    if n < 0:
        console.print(f"[red]Backup created but failed verification:[/red] {dest}")
        raise typer.Exit(1)

    size_mb = dest.stat().st_size / (1024 * 1024)
    console.print(f"[bold green]Backup complete:[/bold green] {dest} ({size_mb:.1f} MB, {n} bills)")


@app.command()
def compact(
    db_path: Path = typer.Option(Path("legiscanner.db"), "--db", help="Path to the SQLite database file"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Compact the database (VACUUM INTO) to reclaim space from deleted/updated rows.

    Bill text is the bulk of the DB and grows each session; a periodic VACUUM
    INTO rewrites the file and reclaims the space freed by deduped/dropped text
    rows. Produces a new compacted file, then replaces the original. Run
    `legiscanner backup` first if you want a safety copy.
    """
    setup_logging(verbose)

    if not db_path.exists():
        console.print(f"[red]Error:[/red] Database not found: {db_path}")
        raise typer.Exit(1)

    before_mb = db_path.stat().st_size / (1024 * 1024)
    dest = db_path.with_name(db_path.name + ".vacuum")
    if dest.exists():
        dest.unlink()

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(f"VACUUM INTO '{dest}'")
    finally:
        conn.close()

    # Replace the original with the compacted file.
    dest.replace(db_path)
    after_mb = db_path.stat().st_size / (1024 * 1024)

    console.print(f"[bold green]Compacted:[/bold green] {before_mb:.1f} MB -> {after_mb:.1f} MB")
    console.print(f"[dim]Saved {before_mb - after_mb:.1f} MB[/dim]")


@app.command()
def version() -> None:
    """Show version information."""
    console.print(f"Legiscanner v{__version__}")


def main() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
