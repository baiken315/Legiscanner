"""Main pipeline for fetching and processing Virginia legislative bills."""

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from bs4.element import PreformattedString

from . import db
from .client import LegiScanClient, LegiScanError, LegiScanNotFoundError
from .models import (
    BillConfig,
    BillInput,
    BillOutput,
    HistoryOutput,
    LegiScanBill,
    SponsorOutput,
    TextVersionOutput,
    TrackingMetadata,
)

logger = logging.getLogger(__name__)


# Bill text is rendered with `| safe` in the web UI, so sanitizing is allowlist-based:
# only these structural/formatting tags survive (with all attributes stripped).
_ALLOWED_TAGS = {
    "a", "b", "blockquote", "br", "caption", "center", "code", "col", "colgroup", "del",
    "div", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "ins", "label", "li",
    "ol", "p", "pre", "s", "small", "span", "strike", "strong", "sub", "sup", "table",
    "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
}
# Disallowed tags whose contents should be dropped too (not just unwrapped).
_DROP_WITH_CONTENT = {
    "script", "style", "head", "title", "meta", "link", "iframe", "frame", "frameset",
    "object", "embed", "applet", "form", "svg", "math", "template", "noscript",
    "textarea", "select", "button", "base",
}


def sanitize_html(html_content: str) -> str:
    """Sanitize HTML for display - allowlisted tags only, no attributes, no comments."""
    soup = BeautifulSoup(html_content, "html.parser")
    # Comments, CDATA, doctypes, processing instructions
    for node in soup.find_all(string=lambda s: isinstance(s, PreformattedString)):
        node.extract()
    for element in soup.find_all(_DROP_WITH_CONTENT):
        element.decompose()
    for tag in soup.find_all(True):
        if tag.name in _ALLOWED_TAGS:
            tag.attrs = {}
        else:
            tag.unwrap()
    return str(soup)


def html_to_plain_text(html_content: str) -> str:
    """Extract plain text from HTML for RAG/text output."""
    soup = BeautifulSoup(html_content, "html.parser")
    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return "\n\n".join(lines)


def normalize_bill_number(bill_number: str) -> str:
    """
    Normalize bill number format for API queries.

    Handles variations like 'HB 1234', 'HB1234', 'hb 1234' -> 'HB1234'
    """
    # Remove spaces and convert to uppercase
    normalized = re.sub(r"\s+", "", bill_number.upper())
    return normalized


def format_bill_as_text(bill: BillOutput) -> str:
    """Format a bill as plain text for RAG consumption (used by `legiscanner export-text`)."""
    lines = [
        f"BILL NUMBER: {bill.bill_number}",
        f"TITLE: {bill.title or 'N/A'}",
        f"STATUS: {bill.status or 'N/A'}",
        f"SESSION: {bill.session}",
        f"STATE: {bill.state}",
        "",
    ]

    if bill.sponsors:
        sponsor_strs = [f"{s.name} ({s.party})" if s.party else s.name for s in bill.sponsors]
        lines.append(f"SPONSORS: {', '.join(sponsor_strs)}")
        lines.append("")

    if bill.introduced_date:
        lines.append(f"INTRODUCED: {bill.introduced_date}")
    if bill.last_action_date and bill.last_action:
        lines.append(f"LAST ACTION ({bill.last_action_date}): {bill.last_action}")
    lines.append("")

    if bill.subjects:
        lines.append(f"SUBJECTS: {', '.join(bill.subjects)}")
        lines.append("")

    if bill.companion_bills:
        lines.append(f"COMPANION BILLS: {', '.join(bill.companion_bills)}")
        lines.append("")

    lines.append("SUMMARY:")
    lines.append(bill.description or "No description available.")
    lines.append("")

    for text in bill.texts:
        lines.append("-" * 60)
        lines.append(f"BILL TEXT - {text.version} ({text.date})")
        lines.append("-" * 60)
        content = text.content or ""
        if content.strip().startswith("<"):
            content = html_to_plain_text(content)
        lines.append(content)
        lines.append("")

    lines.append("-" * 60)
    lines.append("TRACKING NOTES:")

    if bill.tracking_metadata.ai_reason:
        lines.append(f"- AI Analysis: {bill.tracking_metadata.ai_reason}")
    if bill.tracking_metadata.ai_impact:
        lines.append(f"- AI Impact Score: {bill.tracking_metadata.ai_impact}/5")
    if bill.tracking_metadata.ai_tags:
        lines.append(f"- AI Tags: {', '.join(bill.tracking_metadata.ai_tags)}")

    if bill.tracking_metadata.watching:
        lines.append("- Status: WATCHING")
    if bill.tracking_metadata.user_tags:
        lines.append(f"- User Tags: {', '.join(bill.tracking_metadata.user_tags)}")
    if bill.tracking_metadata.notes:
        lines.append(f"- Notes: {bill.tracking_metadata.notes}")
    if bill.tracking_metadata.user_impact_override:
        lines.append(f"- User Impact Override: {bill.tracking_metadata.user_impact_override}/5")

    lines.append("")

    if bill.legiscan_url:
        lines.append(f"LegiScan URL: {bill.legiscan_url}")
    if bill.state_url:
        lines.append(f"State URL: {bill.state_url}")

    return "\n".join(lines)


class Pipeline:
    """Pipeline for fetching and processing legislative bills into SQLite."""

    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: str | Path | None = None,
        db_path: str | Path = "legiscanner.db",
    ):
        """
        Initialize the pipeline.

        Args:
            api_key: LegiScan API key
            cache_dir: Directory for caching API responses
            db_path: Path to the SQLite database file
        """
        self.client = LegiScanClient(api_key=api_key, cache_dir=cache_dir)
        self.conn = db.get_connection(db_path)
        db.init_db(self.conn)

    def load_config(self, config_path: str | Path) -> BillConfig:
        """Load bill configuration from JSON file."""
        import json

        config_path = Path(config_path)
        with open(config_path) as f:
            data = json.load(f)
        return BillConfig.model_validate(data)

    def _extract_sponsors(self, bill: LegiScanBill) -> list[SponsorOutput]:
        """Extract sponsor information from bill."""
        sponsors = []
        for s in bill.sponsors or []:
            name = s.name or f"{s.first_name or ''} {s.last_name or ''}".strip()
            if name:
                sponsors.append(
                    SponsorOutput(
                        name=name,
                        party=s.party,
                        role=s.role,
                        district=s.district,
                    )
                )
        return sponsors

    def _extract_history(self, bill: LegiScanBill) -> list[HistoryOutput]:
        """Extract history from bill."""
        history = []
        for h in bill.history or []:
            history.append(
                HistoryOutput(
                    date=h.date,
                    action=h.action,
                    chamber=h.chamber,
                )
            )
        return history

    def _get_last_action(self, bill: LegiScanBill) -> tuple[str | None, str | None]:
        """Get the most recent action date and description."""
        if not bill.history:
            return None, None

        # History is usually in chronological order, get the last one
        last = bill.history[-1]
        return last.date, last.action

    def _get_introduced_date(self, bill: LegiScanBill) -> str | None:
        """Get the introduced date from history."""
        if not bill.history:
            return None

        # First history entry is usually introduction
        return bill.history[0].date if bill.history else None

    def _fetch_bill_texts(self, bill: LegiScanBill) -> list[TextVersionOutput]:
        """Fetch and decode all text versions of a bill."""
        texts = []

        for text_info in bill.texts or []:
            try:
                content, mime = self.client.get_decoded_bill_text(text_info.doc_id)

                # Clean HTML content (preserve structure for rich text display)
                if mime.startswith("text/html") or mime == "application/xhtml+xml":
                    content = sanitize_html(content)
                elif mime == "application/pdf":
                    # PDF extraction is out of scope — bill text is served as plain text/HTML
                    # elsewhere on the open web, so we just point at the original.
                    content = f"[PDF document - {text_info.text_size or 'unknown'} bytes. See state_url for original.]"

                texts.append(
                    TextVersionOutput(
                        version=text_info.type,
                        date=text_info.date,
                        format=mime,
                        content=content,
                        state_url=text_info.state_link,
                    )
                )
                logger.info(f"  Fetched text version: {text_info.type} ({text_info.date})")

            except LegiScanError as e:
                logger.warning(f"  Failed to fetch text {text_info.doc_id}: {e}")
                # Still record the text version with error note
                texts.append(
                    TextVersionOutput(
                        version=text_info.type,
                        date=text_info.date,
                        format=text_info.mime or "unknown",
                        content=f"[Failed to retrieve: {e}]",
                        state_url=text_info.state_link,
                    )
                )

        return texts

    def process_bill(
        self, bill_input: BillInput, state: str, session: str
    ) -> BillOutput | None:
        """
        Fetch and process a single bill.

        Args:
            bill_input: Bill configuration from input
            state: State abbreviation
            session: Session name

        Returns:
            BillOutput or None if bill not found
        """
        bill_number = normalize_bill_number(bill_input.bill_number)
        logger.info(f"Processing {bill_number}...")

        try:
            # Fetch bill details
            bill = self.client.get_bill(state=state, bill_number=bill_number)

            # Extract data
            sponsors = self._extract_sponsors(bill)
            history = self._extract_history(bill)
            last_action_date, last_action = self._get_last_action(bill)
            introduced_date = self._get_introduced_date(bill)
            subjects = [s.subject_name for s in bill.subjects or []]

            # Fetch all text versions
            texts = self._fetch_bill_texts(bill)

            # Build output
            # Initialize tracking metadata with new schema
            # AI fields start empty (to be filled by autotag command)
            # User can populate user_tags from the config tags field
            output = BillOutput(
                bill_number=bill.bill_number,
                bill_id=bill.bill_id,
                change_hash=bill.change_hash,  # Store for update detection
                title=bill.title,
                description=bill.description,
                state=state,
                session=bill.session.session_name if bill.session else session,
                status=bill.status_desc,
                introduced_date=introduced_date,
                last_action_date=last_action_date,
                last_action=last_action,
                sponsors=sponsors,
                subjects=subjects,
                tracking_metadata=TrackingMetadata(
                    ai_tags=[],  # To be filled by autotag
                    # ai_reason is reserved for AI-generated analysis (filled by
                    # autotag). The config's tracking_reason is the *user's* reason
                    # for tracking the bill, so it goes in notes — not masqueraded
                    # as AI output. (Previously it was stuffed into ai_reason as a
                    # placeholder, conflating user intent with AI analysis.)
                    ai_reason="",
                    ai_impact=None,  # To be filled by autotag
                    watching=False,
                    user_tags=bill_input.tags,  # Migrate config tags to user_tags
                    notes=bill_input.tracking_reason or "",
                ),
                texts=texts,
                history=history,
                legiscan_url=bill.url,
                state_url=bill.state_link,
                retrieved_at=datetime.now().isoformat(),
            )

            return output

        except LegiScanNotFoundError:
            logger.warning(f"Bill not found: {bill_number}")
            return None
        except LegiScanError as e:
            logger.error(f"Error processing {bill_number}: {e}")
            return None

    def _load_existing_bill(self, session_id: int, bill_number: str) -> dict | None:
        """Load an existing bill from the DB, if present."""
        return db.get_bill(self.conn, session_id, bill_number)

    def _check_bill_changed(
        self, bill_number: str, state: str, existing_hash: str | None
    ) -> tuple[bool, str | None]:
        """
        Check if a bill has changed by comparing change_hash.

        Args:
            bill_number: Bill number to check
            state: State abbreviation
            existing_hash: The stored change_hash from previous fetch

        Returns:
            Tuple of (has_changed, new_hash)
        """
        if existing_hash is None:
            return True, None  # No existing hash means we need to fetch

        try:
            # Fetch just the bill metadata (not full text) to check hash
            bill = self.client.get_bill(state=state, bill_number=bill_number)
            new_hash = bill.change_hash

            if new_hash != existing_hash:
                logger.info(f"  {bill_number}: change detected (hash changed)")
                return True, new_hash
            else:
                return False, new_hash

        except LegiScanError as e:
            logger.warning(f"  Error checking {bill_number}: {e}")
            return True, None  # On error, assume changed to be safe

    def run(
        self,
        config_path: str | Path = "config/bills.json",
        session_id: int | None = None,
        skip_existing: bool = False,
        update_mode: bool = False,
    ) -> dict:
        """
        Run the full pipeline.

        Args:
            config_path: Path to bill configuration file
            session_id: DB session id to write into (defaults to the active session,
                creating one from the config's session/state if none exists yet)
            skip_existing: Skip bills that already have output files
            update_mode: Smart update - only fetch new bills and bills with changed hash

        Returns:
            Summary dict with counts of new/updated/skipped/failed bills.
        """
        config = self.load_config(config_path)
        logger.info(f"Loaded {len(config.bills)} bills from config")

        if session_id is None:
            active = db.get_active_session(self.conn)
            if active:
                session_id = active["id"]
            else:
                year = config.metadata.session_year if config.metadata and config.metadata.session_year else datetime.now().year
                session_id = db.get_or_create_session(
                    self.conn, state=config.state, name=config.session, year=year
                )

        results: list[dict] = []
        failed: list[str] = []
        skipped: list[str] = []
        updated: list[str] = []
        new_bills: list[str] = []

        for bill_input in config.bills:
            bill_number = normalize_bill_number(bill_input.bill_number)
            existing = self._load_existing_bill(session_id, bill_number)

            if update_mode:
                # Smart update mode
                if existing:
                    # Check if bill has changed using change_hash
                    has_changed, _ = self._check_bill_changed(
                        bill_number, config.state, existing.get("change_hash")
                    )

                    if not has_changed:
                        logger.info(f"Skipping {bill_number} (unchanged)")
                        results.append(existing)
                        skipped.append(bill_number)
                        continue
                    else:
                        # Bill changed, need to re-fetch
                        logger.info(f"Updating {bill_number} (changed)...")
                        result = self.process_bill(bill_input, config.state, config.session)
                        if result:
                            # Preserve all tracking_metadata from previous run
                            # This includes both AI fields and user fields (watching, notes, etc.)
                            result.tracking_metadata = TrackingMetadata.model_validate(
                                existing["tracking_metadata"]
                            )
                            bill_id = db.upsert_bill(self.conn, session_id, result.model_dump())
                            self.conn.commit()
                            results.append(db.get_bill(self.conn, session_id, bill_number))
                            updated.append(bill_number)
                        else:
                            failed.append(bill_number)
                        continue
                else:
                    # New bill not seen before
                    logger.info(f"Fetching {bill_number} (new)...")
                    result = self.process_bill(bill_input, config.state, config.session)
                    if result:
                        db.upsert_bill(self.conn, session_id, result.model_dump())
                        self.conn.commit()
                        results.append(db.get_bill(self.conn, session_id, bill_number))
                        new_bills.append(bill_number)
                    else:
                        failed.append(bill_number)
                    continue

            elif skip_existing:
                # Simple skip existing mode
                if existing:
                    logger.info(f"Skipping {bill_number} (already exists)")
                    results.append(existing)
                    skipped.append(bill_number)
                    continue

            # Default: fetch everything
            result = self.process_bill(bill_input, config.state, config.session)

            if result:
                db.upsert_bill(self.conn, session_id, result.model_dump())
                self.conn.commit()
                results.append(db.get_bill(self.conn, session_id, bill_number))
            else:
                failed.append(bill_number)

        # Detect and link companion bills (same frequency as before: every run)
        from .companions import update_companion_links_db
        logger.info("Detecting companion bills...")
        companion_pairs = update_companion_links_db(self.conn, session_id, similarity_threshold=0.90)

        summary = {
            "session_id": session_id,
            "organization": config.metadata.organization if config.metadata else "Unknown",
            "session": config.session,
            "state": config.state,
            "generated_at": datetime.now().isoformat(),
            "bill_count": len(results),
            "failed_bills": failed,
            "skipped_bills": skipped,
            "companion_pairs": companion_pairs,
        }
        if update_mode:
            summary["updated_bills"] = updated
            summary["new_bills"] = new_bills

        # Log summary
        logger.info(f"Pipeline complete: {len(results)} bills total")
        if update_mode:
            logger.info(f"  New: {len(new_bills)}, Updated: {len(updated)}, Unchanged: {len(skipped)}, Failed: {len(failed)}")
        else:
            logger.info(f"  Processed: {len(results) - len(skipped)}, Skipped: {len(skipped)}, Failed: {len(failed)}")

        return summary

    def close(self) -> None:
        """Close resources."""
        self.client.close()
        self.conn.close()

    def __enter__(self) -> "Pipeline":
        return self

    def __exit__(self, *args) -> None:
        self.close()
