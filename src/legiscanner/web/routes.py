"""FastAPI routes for Legiscanner web interface."""

import csv
import html
import io
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .. import db
from ..tagger import OllamaTagger, tagger_for_session

# Pipeline state (module-level, fine for single-server use)
_pipeline_state: dict[str, Any] = {
    "running": False,
    "action": None,
    "started_at": None,
    "last_result": None,
    "last_error": None,
}

logger = logging.getLogger(__name__)
router = APIRouter()


class RetagRequest(BaseModel):
    """Request body for retag endpoint."""
    bill_number: str


class ChatRequest(BaseModel):
    """Request body for chat endpoint."""
    bill_number: str
    message: str


_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")


def _clean_tag(raw: str) -> str:
    """Normalize a user-entered tag name; returns "" if it isn't a valid slug.

    Tags end up in HTML attributes and JS on the page, so keep them to a
    conservative slug alphabet rather than relying only on output escaping.
    """
    tag = raw.strip().lower().replace(" ", "-")
    return tag if _TAG_RE.match(tag) else ""


def get_conn(request: Request) -> sqlite3.Connection:
    """FastAPI dependency: a short-lived connection to the app's DB."""
    return db.get_connection(request.app.state.db_path)


def get_session_id(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session: int | None = None,
) -> int:
    """FastAPI dependency: the session to operate on.

    Defaults to the active session. A `?session=<id>` query param (or a
    `session` form field on POST) overrides it, which is what makes multi-session
    data usable end-to-end. Invalid ids fall back to the active session.
    """
    if session is not None:
        row = db.get_session(conn, session)
        if row:
            return session
    active = db.get_active_session(conn)
    if not active:
        raise HTTPException(status_code=500, detail="No active session configured in the database")
    return active["id"]


def get_all_sessions(conn: sqlite3.Connection = Depends(get_conn)) -> list[sqlite3.Row]:
    """FastAPI dependency: all sessions for the UI's session switcher."""
    return db.list_sessions(conn)


@router.post("/session/switch")
async def switch_session(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Set the active session (from the session switcher). Returns JSON.

    This is the persistent "switch which session you're working in" action —
    it updates sessions.active so the pipeline/CLI and every default route
    operate on the chosen session.
    """
    form = await request.form()
    try:
        session_id = int(form.get("session_id", ""))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid session id")
    if not db.get_session(conn, session_id):
        raise HTTPException(status_code=404, detail=f"No session with id {session_id}")
    db.set_active_session(conn, session_id)
    session = db.get_session(conn, session_id)
    return JSONResponse({"ok": True, "session_id": session_id, "session_name": session["name"]})


def _parse_filters(
    search: str = "",
    impact: str = "",
    hide_low_relevance: str = "",
    watching_only: str = "",
    tags: str = "",
    bill_numbers: str = "",
) -> dict:
    """Parse the web app's query/form filter params into db.list_bills() kwargs."""
    selected_tags = [t.strip() for t in tags.split(",") if t.strip()]

    filter_bill_numbers: set[str] = set()
    if bill_numbers:
        for bn in bill_numbers.split(","):
            bn_clean = bn.strip().upper()
            if bn_clean:
                filter_bill_numbers.add(bn_clean)

    selected_impacts: set[int] = set()
    if impact:
        for level in impact.split(","):
            try:
                level_int = int(level.strip())
                if 1 <= level_int <= 5:
                    selected_impacts.add(level_int)
            except ValueError:
                pass

    return {
        "search": search,
        "impact_levels": selected_impacts,
        "hide_low_relevance": hide_low_relevance == "on",
        "watching_only": watching_only == "on",
        "tags": selected_tags,
        "bill_numbers": filter_bill_numbers,
    }


def get_bill_tags(bill: dict[str, Any]) -> list[str]:
    """Get combined tags for a bill (ai_tags + user_tags)."""
    tm = bill.get("tracking_metadata", {})
    return list(set(tm.get("ai_tags", []) + tm.get("user_tags", [])))


def get_bill_impact(bill: dict[str, Any]) -> int:
    """Get effective impact score for a bill (user override takes precedence)."""
    tm = bill.get("tracking_metadata", {})
    if tm.get("user_impact_override") is not None:
        return tm["user_impact_override"]
    if tm.get("ai_impact") is not None:
        return tm["ai_impact"]
    return 3


def is_watching(bill: dict[str, Any]) -> bool:
    return bill.get("tracking_metadata", {}).get("watching", False)


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session_id: int = Depends(get_session_id),
    all_sessions: list[sqlite3.Row] = Depends(get_all_sessions),
):
    """Main bill listing page."""
    templates = request.app.state.templates

    filtered_bills = db.list_bills(conn, session_id, hide_low_relevance=True)
    all_tags = db.get_all_tags(conn, session_id)
    session = db.get_session(conn, session_id)

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "bills": filtered_bills,
            "all_tags": all_tags,
            "bill_count": len(filtered_bills),
            "all_sessions": all_sessions,
            "current_session_id": session_id,
            "current_session_name": session["name"] if session else "",
        }
    )


@router.get("/bills", response_class=HTMLResponse)
async def bills_list(
    request: Request,
    search: str = "",
    impact: str = "",
    hide_low_relevance: str = "",
    watching_only: str = "",
    tags: str = "",
    sort: str = "bill_number",
    bill_numbers: str = "",
    session: int | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
    session_id: int = Depends(get_session_id),
):
    """Get filtered/sorted bill list (HTMX partial).

    An explicit `session` param (from the session switcher's hx-vals) overrides
    the active-session default so a user can browse any session.
    """
    templates = request.app.state.templates
    filters = _parse_filters(search, impact, hide_low_relevance, watching_only, tags, bill_numbers)
    if session is not None and db.get_session(conn, session):
        session_id = session
    filtered_bills = db.list_bills(conn, session_id, sort=sort, **filters)

    return templates.TemplateResponse(
        "partials/bill_list.html",
        {"request": request, "bills": filtered_bills, "bill_count": len(filtered_bills)}
    )


@router.get("/bill/{bill_number}", response_class=HTMLResponse)
async def bill_detail(
    request: Request, bill_number: str,
    conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id),
):
    """Get bill detail modal content (HTMX partial)."""
    templates = request.app.state.templates
    bill = db.get_bill(conn, session_id, bill_number)
    if not bill:
        raise HTTPException(status_code=404, detail=f"Bill {bill_number} not found")

    return templates.TemplateResponse("partials/bill_detail.html", {"request": request, "bill": bill})


@router.post("/bill/{bill_number}/retag", response_class=HTMLResponse)
async def retag_bill(
    request: Request, bill_number: str,
    conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id),
):
    """Retag a bill using the configured LLM. Returns updated tracking info partial."""
    templates = request.app.state.templates
    bill = db.get_bill(conn, session_id, bill_number)
    if not bill:
        raise HTTPException(status_code=404, detail=f"Bill {bill_number} not found")

    bill_row = conn.execute(
        "SELECT id FROM bills WHERE session_id = ? AND bill_number = ?", (session_id, bill_number.upper())
    ).fetchone()

    try:
        with tagger_for_session(conn, session_id) as tagger:
            if not tagger.check_connection():
                return templates.TemplateResponse(
                    "partials/tracking_info.html",
                    {"request": request, "bill": bill, "error": "Cannot connect to LLM server. Make sure it's running."},
                )

            topics = db.list_topics(conn, session_id, ai_taggable_only=True)
            outcome_areas = db.list_outcome_areas(conn, session_id)
            analysis = tagger.analyze_bill(topics, outcome_areas, bill.get("title", ""), bill.get("description", ""))

            db.set_tracking(conn, bill_row["id"], ai_reason=analysis["reason"])
            db.replace_ai_topic_links(conn, bill_row["id"], session_id, analysis["tags"])
            db.replace_outcome_scores(conn, bill_row["id"], session_id, analysis["outcome_scores"])
            conn.commit()

            bill = db.get_bill(conn, session_id, bill_number)
            logger.info(f"Retagged {bill_number}: {analysis}")

            return templates.TemplateResponse(
                "partials/tracking_info.html", {"request": request, "bill": bill, "success": True}
            )

    except Exception as e:
        logger.exception(f"Error retagging {bill_number}")
        return templates.TemplateResponse(
            "partials/tracking_info.html", {"request": request, "bill": bill, "error": str(e)}
        )


@router.post("/bill/{bill_number}/watch", response_class=HTMLResponse)
async def toggle_watch(
    request: Request, bill_number: str,
    conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id),
):
    """Toggle watching status for a bill."""
    templates = request.app.state.templates
    bill = db.get_bill(conn, session_id, bill_number)
    if not bill:
        raise HTTPException(status_code=404, detail=f"Bill {bill_number} not found")

    bill_row = conn.execute(
        "SELECT id FROM bills WHERE session_id = ? AND bill_number = ?", (session_id, bill_number.upper())
    ).fetchone()
    new_watching = not bill["tracking_metadata"]["watching"]
    db.set_tracking(conn, bill_row["id"], watching=int(new_watching))
    conn.commit()

    bill = db.get_bill(conn, session_id, bill_number)
    logger.info(f"Toggled watching for {bill_number}: {new_watching}")

    return templates.TemplateResponse("partials/tracking_info.html", {"request": request, "bill": bill})


@router.post("/bill/{bill_number}/outcome-score/{area_slug}/override", response_class=HTMLResponse)
async def set_outcome_score_override(
    request: Request, bill_number: str, area_slug: str,
    conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id),
):
    """Set or clear a user impact override for one bill's outcome-area score.

    Overall bill impact (tracking.ai_impact/user_impact_override) is derived
    as MAX() across all outcome-area scores via a DB trigger — it updates
    automatically as a side effect of this write, not set directly here.
    """
    templates = request.app.state.templates
    try:
        bill_row = conn.execute(
            "SELECT id FROM bills WHERE session_id = ? AND bill_number = ?", (session_id, bill_number.upper())
        ).fetchone()
        if not bill_row:
            raise HTTPException(status_code=404, detail=f"Bill {bill_number} not found")

        area_id = db.get_or_create_outcome_area(conn, session_id, area_slug)
        if area_id is None:
            raise HTTPException(status_code=404, detail=f"Outcome area '{area_slug}' not found")

        form = await request.form()
        impact_str = form.get("impact", "")

        if impact_str:
            try:
                impact = int(impact_str)
                if 1 <= impact <= 5:
                    db.set_outcome_score_override(conn, bill_row["id"], area_id, impact)
                    logger.info(f"Set outcome-score override for {bill_number}/{area_slug}: {impact}")
            except ValueError:
                logger.error(f"Invalid impact value: {impact_str}")
        else:
            db.set_outcome_score_override(conn, bill_row["id"], area_id, None)
            logger.info(f"Cleared outcome-score override for {bill_number}/{area_slug}")

        conn.commit()
        bill = db.get_bill(conn, session_id, bill_number)

        return templates.TemplateResponse(
            "partials/tracking_info.html", {"request": request, "bill": bill, "success": "Impact updated."}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating outcome score for {bill_number}/{area_slug}: {e}", exc_info=True)
        raise


@router.post("/bill/{bill_number}/tag/add", response_class=HTMLResponse)
async def add_user_tag(
    request: Request, bill_number: str,
    conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id),
):
    """Add a user tag to a bill."""
    templates = request.app.state.templates
    bill_row = conn.execute(
        "SELECT id FROM bills WHERE session_id = ? AND bill_number = ?", (session_id, bill_number.upper())
    ).fetchone()
    if not bill_row:
        raise HTTPException(status_code=404, detail=f"Bill {bill_number} not found")

    form = await request.form()
    tag = _clean_tag(form.get("tag", ""))

    if not tag:
        bill = db.get_bill(conn, session_id, bill_number)
        return templates.TemplateResponse(
            "partials/tracking_info.html",
            {
                "request": request, "bill": bill,
                "error": "Tags must be 1-50 characters: letters, numbers, hyphens, underscores.",
            },
        )

    db.add_topic_link(conn, bill_row["id"], session_id, tag, "user")
    conn.commit()
    logger.info(f"Added user tag '{tag}' to {bill_number}")

    bill = db.get_bill(conn, session_id, bill_number)
    return templates.TemplateResponse("partials/tracking_info.html", {"request": request, "bill": bill})


@router.post("/bill/{bill_number}/tag/remove", response_class=HTMLResponse)
async def remove_user_tag(
    request: Request, bill_number: str,
    conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id),
):
    """Remove a user tag from a bill."""
    templates = request.app.state.templates
    bill_row = conn.execute(
        "SELECT id FROM bills WHERE session_id = ? AND bill_number = ?", (session_id, bill_number.upper())
    ).fetchone()
    if not bill_row:
        raise HTTPException(status_code=404, detail=f"Bill {bill_number} not found")

    form = await request.form()
    tag = form.get("tag", "").strip()

    db.remove_topic_link(conn, bill_row["id"], tag, "user")
    conn.commit()
    logger.info(f"Removed user tag '{tag}' from {bill_number}")

    bill = db.get_bill(conn, session_id, bill_number)
    return templates.TemplateResponse("partials/tracking_info.html", {"request": request, "bill": bill})


@router.post("/bill/{bill_number}/ai-tag/remove", response_class=HTMLResponse)
async def remove_ai_tag(
    request: Request, bill_number: str,
    conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id),
):
    """Remove an AI tag from a bill."""
    templates = request.app.state.templates
    bill_row = conn.execute(
        "SELECT id FROM bills WHERE session_id = ? AND bill_number = ?", (session_id, bill_number.upper())
    ).fetchone()
    if not bill_row:
        raise HTTPException(status_code=404, detail=f"Bill {bill_number} not found")

    form = await request.form()
    tag = form.get("tag", "").strip()

    db.remove_topic_link(conn, bill_row["id"], tag, "ai")
    conn.commit()
    logger.info(f"Removed AI tag '{tag}' from {bill_number}")

    bill = db.get_bill(conn, session_id, bill_number)
    return templates.TemplateResponse("partials/tracking_info.html", {"request": request, "bill": bill})


@router.post("/bill/{bill_number}/notes", response_class=HTMLResponse)
async def update_notes(
    request: Request, bill_number: str,
    conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id),
):
    """Update notes for a bill (full replace, from the notes textarea form)."""
    templates = request.app.state.templates
    bill_row = conn.execute(
        "SELECT id FROM bills WHERE session_id = ? AND bill_number = ?", (session_id, bill_number.upper())
    ).fetchone()
    if not bill_row:
        raise HTTPException(status_code=404, detail=f"Bill {bill_number} not found")

    form = await request.form()
    notes = form.get("notes", "").strip()

    db.set_tracking(conn, bill_row["id"], notes=notes)
    conn.commit()
    logger.info(f"Updated notes for {bill_number}")

    bill = db.get_bill(conn, session_id, bill_number)
    return templates.TemplateResponse(
        "partials/tracking_info.html", {"request": request, "bill": bill, "success": "Notes saved."}
    )


@router.post("/bill/{bill_number}/notes/append", response_class=HTMLResponse)
async def append_note(
    request: Request, bill_number: str,
    conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id),
):
    """Append text (e.g. a Legislative Assistant reply) to a bill's existing notes.

    Used by the "Add to Notes" button on chat messages, distinct from the
    notes textarea's full-replace POST /notes above.
    """
    templates = request.app.state.templates
    bill_row = conn.execute(
        "SELECT id FROM bills WHERE session_id = ? AND bill_number = ?", (session_id, bill_number.upper())
    ).fetchone()
    if not bill_row:
        raise HTTPException(status_code=404, detail=f"Bill {bill_number} not found")

    form = await request.form()
    addition = form.get("text", "").strip()
    if not addition:
        raise HTTPException(status_code=400, detail="No text provided")

    bill = db.get_bill(conn, session_id, bill_number)
    existing_notes = bill["tracking_metadata"]["notes"]

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"[{timestamp}] From Legislative Assistant:\n{addition}"
    new_notes = f"{existing_notes}\n\n{entry}" if existing_notes else entry

    db.set_tracking(conn, bill_row["id"], notes=new_notes)
    conn.commit()
    logger.info(f"Appended chat response to notes for {bill_number}")

    bill = db.get_bill(conn, session_id, bill_number)
    return templates.TemplateResponse(
        "partials/tracking_info.html", {"request": request, "bill": bill, "success": "Added to notes."}
    )


# Legislative Assistant chat system prompt
ASSISTANT_SYSTEM_PROMPT = """You are a legislative analyst assistant for local government staff in Virginia. You help analyze bills and their potential impacts on local authority and the policy areas your organization tracks.

Context about the bill:
Title: {title}
Description: {description}

Current Analysis:
- Impact Score: {impact}/5
- Tags: {tags}
- Analysis: {reason}

Full Bill Text:
{bill_text}

Your role:
- Provide objective, factual analysis of legislative implications
- Consider impacts on local government authority and home rule
- Identify potential unintended consequences
- Consider political dynamics in the Virginia General Assembly
- Be concise but thorough

Respond in a helpful, professional tone appropriate for local government staff."""


@router.post("/bill/{bill_number}/chat", response_class=HTMLResponse)
async def chat_about_bill(
    request: Request, bill_number: str, chat_request: ChatRequest,
    conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id),
):
    """Chat with the legislative assistant about a bill."""
    templates = request.app.state.templates
    bill = db.get_bill(conn, session_id, bill_number)
    if not bill:
        raise HTTPException(status_code=404, detail=f"Bill {bill_number} not found")

    bill_text = ""
    texts = bill.get("texts", [])
    if texts:
        bill_text = texts[0].get("content", "")[:8000]

    tracking = bill.get("tracking_metadata", {})
    session = db.get_session(conn, session_id)

    system_prompt = ASSISTANT_SYSTEM_PROMPT.format(
        title=bill.get("title", ""),
        description=bill.get("description", ""),
        impact=tracking.get("ai_impact") or tracking.get("user_impact_override") or 3,
        tags=", ".join(tracking.get("ai_tags", [])),
        reason=tracking.get("ai_reason", ""),
        bill_text=bill_text,
    )

    try:
        import httpx

        ollama_url = session["ollama_url"].rstrip("/") if session else "http://localhost:8080"
        model = session["model"] if session else "unknown"

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{ollama_url}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": chat_request.message},
                    ],
                    "stream": False,
                },
            )
            response.raise_for_status()
            result = response.json()
            assistant_message = result["choices"][0]["message"]["content"]

            return templates.TemplateResponse(
                "partials/chat_message.html",
                {"request": request, "role": "assistant", "content": assistant_message},
            )

    except httpx.ConnectError:
        return templates.TemplateResponse(
            "partials/chat_message.html",
            {"request": request, "role": "error", "content": "Cannot connect to LLM server. Make sure it's running."},
        )
    except Exception:
        logger.exception(f"Error in chat for {bill_number}")
        return templates.TemplateResponse(
            "partials/chat_message.html",
            {"request": request, "role": "error", "content": "The LLM request failed. Check the server log for details."},
        )


@router.get("/bill/{bill_number}/chat", response_class=HTMLResponse)
async def get_chat_interface(
    request: Request, bill_number: str,
    conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id),
):
    """Get the chat interface for a bill (HTMX partial)."""
    templates = request.app.state.templates
    bill = db.get_bill(conn, session_id, bill_number)
    if not bill:
        raise HTTPException(status_code=404, detail=f"Bill {bill_number} not found")

    suggested_prompts = [
        "What are the key implications of this bill for local government?",
        "How might this affect the policy areas we track?",
        "What political factors should I consider with this bill?",
        "Could this preempt local authority? If so, how?",
        "Who are the likely supporters and opponents of this bill?",
        "What amendments might improve this bill from a local perspective?",
    ]

    return templates.TemplateResponse(
        "partials/chat_interface.html",
        {"request": request, "bill": bill, "suggested_prompts": suggested_prompts},
    )


@router.post("/bills/bulk/watch", response_class=HTMLResponse)
async def bulk_watch(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id),
):
    """Watch all bills matching the current filter."""
    templates = request.app.state.templates
    form = await request.form()
    filters = _parse_filters(
        form.get("search", ""), form.get("impact", ""), form.get("hide_low_relevance", ""),
        form.get("watching_only", ""), form.get("tags", ""), form.get("bill_numbers", ""),
    )
    sort = form.get("sort", "bill_number")

    # One set-based update instead of a per-bill SELECT + UPDATE (N+1).
    bill_numbers = {b["bill_number"] for b in db.list_bills(conn, session_id, sort=sort, **filters)}
    if bill_numbers:
        placeholders = ",".join("?" for _ in bill_numbers)
        cur = conn.execute(
            f"UPDATE tracking SET watching = 1, updated_at = datetime('now') "
            f"WHERE watching = 0 AND bill_id IN ("
            f"SELECT id FROM bills WHERE session_id = ? AND bill_number IN ({placeholders}))",
            [session_id, *bill_numbers],
        )
        count = cur.rowcount
    else:
        count = 0
    conn.commit()
    logger.info(f"Bulk watch: set {count} bills to watching")

    filtered = db.list_bills(conn, session_id, sort=sort, **filters)
    return templates.TemplateResponse(
        "partials/bill_list.html", {"request": request, "bills": filtered, "bill_count": len(filtered)}
    )


@router.post("/bills/bulk/unwatch", response_class=HTMLResponse)
async def bulk_unwatch(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id),
):
    """Unwatch all bills matching the current filter."""
    templates = request.app.state.templates
    form = await request.form()
    filters = _parse_filters(
        form.get("search", ""), form.get("impact", ""), form.get("hide_low_relevance", ""),
        form.get("watching_only", ""), form.get("tags", ""), form.get("bill_numbers", ""),
    )
    sort = form.get("sort", "bill_number")

    bill_numbers = {b["bill_number"] for b in db.list_bills(conn, session_id, sort=sort, **filters)}
    if bill_numbers:
        placeholders = ",".join("?" for _ in bill_numbers)
        cur = conn.execute(
            f"UPDATE tracking SET watching = 0, updated_at = datetime('now') "
            f"WHERE watching = 1 AND bill_id IN ("
            f"SELECT id FROM bills WHERE session_id = ? AND bill_number IN ({placeholders}))",
            [session_id, *bill_numbers],
        )
        count = cur.rowcount
    else:
        count = 0
    conn.commit()
    logger.info(f"Bulk unwatch: set {count} bills to unwatched")

    filtered = db.list_bills(conn, session_id, sort=sort, **filters)
    return templates.TemplateResponse(
        "partials/bill_list.html", {"request": request, "bills": filtered, "bill_count": len(filtered)}
    )


@router.post("/bills/bulk/tag", response_class=HTMLResponse)
async def bulk_tag(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id),
):
    """Add a tag to all bills matching the current filter."""
    templates = request.app.state.templates
    form = await request.form()
    filters = _parse_filters(
        form.get("search", ""), form.get("impact", ""), form.get("hide_low_relevance", ""),
        form.get("watching_only", ""), form.get("tags", ""), form.get("bill_numbers", ""),
    )
    sort = form.get("sort", "bill_number")
    tag = _clean_tag(form.get("bulk_tag", ""))

    filtered = db.list_bills(conn, session_id, sort=sort, **filters)
    if tag:
        topic_id = db.get_or_create_topic(conn, session_id, tag)
        bill_numbers = {b["bill_number"] for b in filtered}
        if bill_numbers:
            placeholders = ",".join("?" for _ in bill_numbers)
            cur = conn.execute(
                f"INSERT OR IGNORE INTO bill_topics (bill_id, topic_id, source) "
                f"SELECT b.id, ?, 'user' FROM bills b "
                f"WHERE b.session_id = ? AND b.bill_number IN ({placeholders}) "
                f"AND NOT EXISTS (SELECT 1 FROM bill_topics bt WHERE bt.bill_id = b.id "
                f"AND bt.topic_id = ? AND bt.source = 'user')",
                [topic_id, session_id, *bill_numbers, topic_id],
            )
            count = cur.rowcount
        else:
            count = 0
        conn.commit()
        logger.info(f"Bulk tag: added '{tag}' to {count} bills")
        filtered = db.list_bills(conn, session_id, sort=sort, **filters)

    return templates.TemplateResponse(
        "partials/bill_list.html", {"request": request, "bills": filtered, "bill_count": len(filtered)}
    )


@router.post("/bills/bulk/delete-tag", response_class=HTMLResponse)
async def bulk_delete_tag(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id),
):
    """Remove a tag from all bills matching the current filter (both user and AI tags)."""
    templates = request.app.state.templates
    form = await request.form()
    filters = _parse_filters(
        form.get("search", ""), form.get("impact", ""), form.get("hide_low_relevance", ""),
        form.get("watching_only", ""), form.get("tags", ""), form.get("bill_numbers", ""),
    )
    sort = form.get("sort", "bill_number")
    tag = form.get("delete_tag", "").strip().lower().replace(" ", "-")

    filtered = db.list_bills(conn, session_id, sort=sort, **filters)
    if tag:
        bill_numbers = {b["bill_number"] for b in filtered}
        user_count = ai_count = 0
        if bill_numbers:
            placeholders = ",".join("?" for _ in bill_numbers)
            topic_id = conn.execute(
                "SELECT id FROM topics WHERE session_id = ? AND name = ?", (session_id, tag)
            ).fetchone()
            if topic_id:
                cur = conn.execute(
                    f"DELETE FROM bill_topics WHERE source = 'user' AND topic_id = ? AND bill_id IN "
                    f"(SELECT id FROM bills WHERE session_id = ? AND bill_number IN ({placeholders}))",
                    [topic_id["id"], session_id, *bill_numbers],
                )
                user_count = cur.rowcount
                cur = conn.execute(
                    f"DELETE FROM bill_topics WHERE source = 'ai' AND topic_id = ? AND bill_id IN "
                    f"(SELECT id FROM bills WHERE session_id = ? AND bill_number IN ({placeholders}))",
                    [topic_id["id"], session_id, *bill_numbers],
                )
                ai_count = cur.rowcount
        conn.commit()
        logger.info(f"Bulk delete tag: removed '{tag}' from {user_count} user tags and {ai_count} AI tags")
        filtered = db.list_bills(conn, session_id, sort=sort, **filters)

    return templates.TemplateResponse(
        "partials/bill_list.html", {"request": request, "bills": filtered, "bill_count": len(filtered)}
    )


# Available columns for CSV export
AVAILABLE_COLUMNS = {
    "bill_number": "Bill Number",
    "title": "Title",
    "status": "Status",
    "ai_impact": "AI Impact (1-5, overall)",
    "user_impact_override": "User Impact Override (overall)",
    "outcome_scores": "Outcome Area Scores",
    "ai_reason": "AI Analysis",
    "ai_tags": "AI Tags",
    "user_tags": "User Tags",
    "watching": "Watching",
    "notes": "Notes",
    "introduced_date": "Introduced",
    "last_action_date": "Last Action Date",
    "last_action": "Last Action",
    "sponsors": "Sponsors",
    "companion_bills": "Companion Bills",
    "subjects": "Subjects",
    "description": "Description",
    "legiscan_url": "LegiScan URL",
    "state_url": "State URL",
}


@router.get("/export/csv")
async def export_csv(
    request: Request,
    search: str = "",
    impact: str = "",
    hide_low_relevance: str = "",
    watching_only: str = "",
    tags: str = "",
    sort: str = "bill_number",
    bill_numbers: str = "",
    columns: str = "bill_number,title,status,ai_impact,ai_reason,ai_tags,watching",
    conn: sqlite3.Connection = Depends(get_conn),
    session_id: int = Depends(get_session_id),
):
    """Export filtered bills as CSV with selected columns."""
    filters = _parse_filters(search, impact, hide_low_relevance, watching_only, tags, bill_numbers)
    filtered = db.list_bills(conn, session_id, sort=sort, **filters)

    selected_columns = [c.strip() for c in columns.split(",") if c.strip() in AVAILABLE_COLUMNS]
    if not selected_columns:
        selected_columns = ["bill_number", "title", "status"]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([AVAILABLE_COLUMNS[col] for col in selected_columns])

    for bill in filtered:
        writer.writerow([get_column_value(bill, col) for col in selected_columns])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=legiscanner_bills_{len(filtered)}.csv"},
    )


def get_column_value(bill: dict[str, Any], column_id: str) -> str:
    """Extract and format a column value from a bill."""
    tm = bill.get("tracking_metadata", {})

    if column_id == "bill_number":
        return bill.get("bill_number", "")
    elif column_id == "title":
        return bill.get("title", "")
    elif column_id == "status":
        return bill.get("status", "")
    elif column_id == "ai_impact":
        impact = tm.get("ai_impact")
        return str(impact) if impact is not None else ""
    elif column_id == "user_impact_override":
        override = tm.get("user_impact_override")
        return str(override) if override is not None else ""
    elif column_id == "ai_reason":
        return tm.get("ai_reason", "")
    elif column_id == "outcome_scores":
        scores = bill.get("outcome_scores", [])
        parts = []
        for s in scores:
            effective = s.get("user_impact_override") if s.get("user_impact_override") is not None else s.get("ai_impact")
            parts.append(f"{s.get('outcome_area_name', '')}: {effective}")
        return "; ".join(parts)
    elif column_id == "ai_tags":
        tags = tm.get("ai_tags", [])
        return ", ".join(tags) if tags else ""
    elif column_id == "user_tags":
        tags = tm.get("user_tags", [])
        return ", ".join(tags) if tags else ""
    elif column_id == "watching":
        return "Yes" if tm.get("watching", False) else "No"
    elif column_id == "notes":
        return tm.get("notes", "")
    elif column_id == "introduced_date":
        return bill.get("introduced_date", "")
    elif column_id == "last_action_date":
        return bill.get("last_action_date", "")
    elif column_id == "last_action":
        return bill.get("last_action", "")
    elif column_id == "sponsors":
        sponsors = bill.get("sponsors", [])
        return "; ".join(s.get("name", "") for s in sponsors if s.get("name"))
    elif column_id == "companion_bills":
        companions = bill.get("companion_bills", [])
        return ", ".join(companions) if companions else ""
    elif column_id == "subjects":
        subjects = bill.get("subjects", [])
        return ", ".join(subjects) if subjects else ""
    elif column_id == "description":
        return bill.get("description", "")
    elif column_id == "legiscan_url":
        return bill.get("legiscan_url", "")
    elif column_id == "state_url":
        return bill.get("state_url", "")
    else:
        return ""




@router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request, conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id)):
    """Help/info page explaining the app."""
    templates = request.app.state.templates
    session = db.get_session(conn, session_id)
    bill_count = conn.execute(
        "SELECT COUNT(*) FROM bills WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    topic_count = conn.execute(
        "SELECT COUNT(*) FROM topics WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    oa_count = conn.execute(
        "SELECT COUNT(*) FROM outcome_areas WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    lineage_count = len(db.list_lineages(conn))
    session_count = len(db.list_sessions(conn))

    return templates.TemplateResponse(
        "help.html",
        {
            "request": request,
            "session_name": session["name"] if session else "Unknown",
            "session_year": session["year"] if session else "",
            "bill_count": bill_count,
            "topic_count": topic_count,
            "outcome_area_count": oa_count,
            "model_name": session["model"] if session else "",
            "lineage_count": lineage_count,
            "session_count": session_count,
        },
    )


# --- Bill version comparison -------------------------------------------------


@router.get("/compare", response_class=HTMLResponse)
async def compare_index(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id),
):
    """Bill picker for the version-comparison tool (full page)."""
    templates = request.app.state.templates
    rows = conn.execute(
        "SELECT b.bill_number, b.title, COUNT(bt.id) AS versions FROM bills b "
        "JOIN bill_texts bt ON bt.bill_id = b.id "
        "WHERE b.session_id = ? GROUP BY b.id HAVING COUNT(bt.id) > 1 "
        "ORDER BY versions DESC, b.bill_number",
        (session_id,),
    ).fetchall()
    comparable = [
        {"bill_number": r["bill_number"], "title": r["title"] or "", "versions": r["versions"]}
        for r in rows
    ]
    return templates.TemplateResponse(
        "compare_index.html", {"request": request, "comparable": comparable}
    )


def _bill_text_content(bill_id: int, text_id: int, conn: sqlite3.Connection) -> str:
    """One bill version's stored content as plain text.

    LegiScan text is sanitized HTML (see pipeline.sanitize_html); the diff
    engine wants plain text, so HTML is flattened with html_to_plain_text.
    """
    from ..pipeline import html_to_plain_text

    row = conn.execute(
        "SELECT content FROM bill_texts WHERE bill_id = ? AND id = ?", (bill_id, text_id)
    ).fetchone()
    if not row or not row["content"]:
        return ""
    content = row["content"]
    if content.strip().startswith("<"):
        return html_to_plain_text(content)
    return content


def _version_label(text: dict) -> str:
    parts = [text.get("version") or "Unknown"]
    if text.get("date"):
        parts.append(text["date"])
    return " - ".join(parts)


@router.get("/bill/{bill_number}/compare", response_class=HTMLResponse)
async def bill_compare(
    request: Request, bill_number: str,
    conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id),
):
    """Version-compare page for a bill (full page, not an HTMX partial)."""
    templates = request.app.state.templates
    info = db.get_bill_texts(conn, session_id, bill_number)
    if not info:
        raise HTTPException(status_code=404, detail=f"Bill {bill_number} not found")

    return templates.TemplateResponse("compare.html", {"request": request, "bill": info})


# --- Cross-session bill lineage ---------------------------------------------


def _latest_text_id(bill_id: int, conn: sqlite3.Connection) -> int | None:
    """The id of a bill's most recent text version (for cross-session diffs)."""
    row = conn.execute(
        "SELECT id FROM bill_texts WHERE bill_id = ? "
        "ORDER BY (date IS NULL), date, id DESC LIMIT 1",
        (bill_id,),
    ).fetchone()
    return row["id"] if row else None


def _lineage_text_options(lineage: dict, conn: sqlite3.Connection) -> list[dict]:
    """Build diff-able text options for every member of a lineage.

    Each entry carries enough to label a selector and to fetch a diff: the
    member bill, its session, and (if it has text) a representative text
    version id. Members without any fetched text are still listed so the
    lineage view is complete.
    """
    options = []
    for m in lineage["members"]:
        options.append(
            {
                "bill_id": m["bill_id"],
                "bill_number": m["bill_number"],
                "title": m["title"] or "",
                "session_name": m["session_name"],
                "year": m["year"],
                "status": m["status"] or "",
                "last_action_date": m["last_action_date"] or "",
                "text_id": _latest_text_id(m["bill_id"], conn),
                "link_reason": m["link_reason"] or "",
                "similarity": m["similarity"],
            }
        )
    return options


@router.get("/lineage", response_class=HTMLResponse)
async def lineage_index(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    session_id: int = Depends(get_session_id),
    all_sessions: list[sqlite3.Row] = Depends(get_all_sessions),
):
    """Index of cross-session bill lineages (full page)."""
    templates = request.app.state.templates
    lineages = db.list_lineages(conn)
    session = db.get_session(conn, session_id)
    return templates.TemplateResponse(
        "lineage_index.html",
        {
            "request": request,
            "lineages": lineages,
            "all_sessions": all_sessions,
            "current_session_id": session_id,
            "current_session_name": session["name"] if session else "",
        },
    )


@router.get("/lineage/diff")
async def lineage_diff(
    a_bill: int, a_text: int, b_bill: int, b_text: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Plain-text lines for two (possibly cross-session) bill text versions.

    Registered BEFORE /lineage/{lineage_id} so the literal "diff" segment is not
    captured as a lineage_id. Feeds the same client-side diff engine the
    in-session compare tool uses, but keyed on bill_id + text_id so it can
    compare bills in different sessions.
    """
    from ..pipeline import html_to_plain_text

    def text_lines(bill_id: int, text_id: int) -> tuple[list[str], str]:
        row = conn.execute(
            "SELECT content, version, date FROM bill_texts WHERE bill_id = ? AND id = ?",
            (bill_id, text_id),
        ).fetchone()
        if not row or not row["content"]:
            return [], f"{row['version'] if row else ''}"
        content = row["content"]
        if content.strip().startswith("<"):
            content = html_to_plain_text(content)
        lines = [ln.rstrip() for ln in content.split("\n")]
        label = row["version"] or "Unknown"
        if row["date"]:
            label = f"{label} - {row['date']}"
        return lines, label

    a_lines, a_label = text_lines(a_bill, a_text)
    b_lines, b_label = text_lines(b_bill, b_text)

    a_bill_row = conn.execute(
        "SELECT bill_number, title FROM bills WHERE id = ?", (a_bill,)
    ).fetchone()
    b_bill_row = conn.execute(
        "SELECT bill_number, title FROM bills WHERE id = ?", (b_bill,)
    ).fetchone()

    return JSONResponse({
        "v1": {
            "id": a_text,
            "label": f"{a_bill_row['bill_number'] if a_bill_row else ''} {a_label}",
            "bill": a_bill_row["bill_number"] if a_bill_row else "",
            "title": a_bill_row["title"] if a_bill_row else "",
            "lines": a_lines,
        },
        "v2": {
            "id": b_text,
            "label": f"{b_bill_row['bill_number'] if b_bill_row else ''} {b_label}",
            "bill": b_bill_row["bill_number"] if b_bill_row else "",
            "title": b_bill_row["title"] if b_bill_row else "",
            "lines": b_lines,
        },
    })


@router.get("/lineage/{lineage_id}", response_class=HTMLResponse)
async def lineage_detail(
    request: Request, lineage_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
    all_sessions: list[sqlite3.Row] = Depends(get_all_sessions),
):
    """A specific lineage's members (reached from the lineage index)."""
    templates = request.app.state.templates
    lineage = db.get_lineage(conn, lineage_id)
    if not lineage:
        raise HTTPException(status_code=404, detail=f"No lineage {lineage_id}")
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
    options = _lineage_text_options({"lineage": lineage, "members": members}, conn)
    active = db.get_active_session(conn)
    return templates.TemplateResponse(
        "lineage.html",
        {
            "request": request,
            "bill": None,
            "lineage": {"lineage": lineage, "members": members},
            "text_options": options,
            "all_sessions": all_sessions,
            "current_session_id": active["id"] if active else None,
            "current_session_name": active["name"] if active else "",
        },
    )


@router.get("/bill/{bill_number}/lineage", response_class=HTMLResponse)
async def bill_lineage(
    request: Request, bill_number: str,
    conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id),
    all_sessions: list[sqlite3.Row] = Depends(get_all_sessions),
):
    """Cross-session lineage view for a bill: how it evolved across sessions."""
    templates = request.app.state.templates
    lineage = db.get_bill_lineage_by_number(conn, session_id, bill_number)
    if not lineage:
        raise HTTPException(status_code=404, detail=f"Bill {bill_number} has no lineage")

    bill = db.get_bill(conn, session_id, bill_number)
    options = _lineage_text_options(lineage, conn)
    session = db.get_session(conn, session_id)

    return templates.TemplateResponse(
        "lineage.html",
        {
            "request": request,
            "bill": bill,
            "lineage": lineage,
            "text_options": options,
            "all_sessions": all_sessions,
            "current_session_id": session_id,
            "current_session_name": session["name"] if session else "",
        },
    )


@router.get("/bill/{bill_number}/compare/diff")
async def bill_compare_diff(
    bill_number: str, v1: int, v2: int,
    conn: sqlite3.Connection = Depends(get_conn), session_id: int = Depends(get_session_id),
):
    """Plain-text lines of the two requested versions, for client-side diffing."""
    info = db.get_bill_texts(conn, session_id, bill_number)
    if not info:
        raise HTTPException(status_code=404, detail=f"Bill {bill_number} not found")

    by_id = {t["id"]: t for t in info["texts"]}
    if v1 not in by_id or v2 not in by_id:
        raise HTTPException(status_code=400, detail="Unknown version id")

    def lines(text_id: int) -> list[str]:
        return [ln.rstrip() for ln in _bill_text_content(info["bill_id"], text_id, conn).split("\n")]

    return JSONResponse({
        "v1": {"id": v1, "label": _version_label(by_id[v1]), "lines": lines(v1)},
        "v2": {"id": v2, "label": _version_label(by_id[v2]), "lines": lines(v2)},
    })


# --- Pipeline Management ---

def _run_pipeline(db_path: Path, mode: str) -> None:
    """Run pipeline synchronously in a background thread."""
    from ..pipeline import Pipeline

    config_path = db_path.parent / "config" / "bills.json"
    try:
        with Pipeline(db_path=db_path) as pipeline:
            if mode == "update":
                result = pipeline.run(config_path=config_path, update_mode=True)
                new = len(result.get("new_bills", []))
                updated = len(result.get("updated_bills", []))
                unchanged = len(result.get("skipped_bills", []))
                _pipeline_state["last_result"] = f"Smart update complete: {new} new, {updated} updated, {unchanged} unchanged"
            else:
                result = pipeline.run(config_path=config_path)
                count = result.get("bill_count", 0)
                _pipeline_state["last_result"] = f"Full refresh complete: {count} bills"
    except Exception as e:
        _pipeline_state["last_error"] = str(e)
        _pipeline_state["last_result"] = None
    finally:
        _pipeline_state["running"] = False
        _pipeline_state["started_at"] = None


def _run_autotag(db_path: Path) -> None:
    """Run autotag on new bills in a background thread."""
    from .. import db as db_module
    from ..tagger import auto_tag_bills

    try:
        conn = db_module.get_connection(db_path)
        session = db_module.get_active_session(conn)
        if not session:
            raise RuntimeError("No active session configured")
        auto_tag_bills(conn, session["id"], full_analysis=True, new_only=True)
        conn.close()
        _pipeline_state["last_result"] = "Auto-tag complete (new bills only)"
    except Exception as e:
        _pipeline_state["last_error"] = str(e)
        _pipeline_state["last_result"] = None
    finally:
        _pipeline_state["running"] = False
        _pipeline_state["started_at"] = None


def _pipeline_status_html(polling: bool = False) -> str:
    """Generate pipeline status HTML fragment."""
    if _pipeline_state["running"]:
        action_label = {
            "update": "Smart update",
            "refresh": "Full refresh",
            "autotag": "Auto-tagging",
        }.get(_pipeline_state["action"], "Running")
        poll_attrs = 'hx-get="/pipeline/status" hx-trigger="every 2s" hx-target="#pipeline-status" hx-swap="innerHTML"' if polling else ""
        return f'<span class="pipeline-running" {poll_attrs}>⟳ {action_label} in progress...</span>'

    if _pipeline_state["last_error"]:
        return f'<span class="pipeline-error">✗ Error: {html.escape(_pipeline_state["last_error"])}</span>'

    if _pipeline_state["last_result"]:
        return f'<span class="pipeline-done">✓ {html.escape(_pipeline_state["last_result"])}</span>'

    return '<span class="pipeline-idle">Ready</span>'


@router.get("/pipeline/status", response_class=HTMLResponse)
async def pipeline_status(request: Request):
    """Get current pipeline status HTML fragment."""
    return HTMLResponse(_pipeline_status_html(polling=_pipeline_state["running"]))


RUNNING_HTML = '<span class="pipeline-running" hx-get="/pipeline/status" hx-trigger="every 2s" hx-target="#pipeline-status" hx-swap="innerHTML">⟳ {label} in progress...</span>'


def _start_pipeline(action: str, label: str) -> None:
    _pipeline_state["running"] = True
    _pipeline_state["action"] = action
    _pipeline_state["started_at"] = datetime.now().isoformat()
    _pipeline_state["last_error"] = None


@router.post("/pipeline/update", response_class=HTMLResponse)
async def pipeline_update(request: Request, background_tasks: BackgroundTasks):
    """Trigger smart update pipeline."""
    if _pipeline_state["running"]:
        return HTMLResponse('<span class="pipeline-running">Already running...</span>')
    _start_pipeline("update", "Smart update")
    background_tasks.add_task(_run_pipeline, request.app.state.db_path, "update")
    return HTMLResponse(RUNNING_HTML.format(label="Smart update"))


@router.post("/pipeline/refresh", response_class=HTMLResponse)
async def pipeline_refresh(request: Request, background_tasks: BackgroundTasks):
    """Trigger full pipeline refresh."""
    if _pipeline_state["running"]:
        return HTMLResponse('<span class="pipeline-running">Already running...</span>')
    _start_pipeline("refresh", "Full refresh")
    background_tasks.add_task(_run_pipeline, request.app.state.db_path, "refresh")
    return HTMLResponse(RUNNING_HTML.format(label="Full refresh"))


@router.post("/pipeline/autotag", response_class=HTMLResponse)
async def pipeline_autotag(request: Request, background_tasks: BackgroundTasks):
    """Trigger autotag on new/untagged bills."""
    if _pipeline_state["running"]:
        return HTMLResponse('<span class="pipeline-running">Already running...</span>')
    _start_pipeline("autotag", "Auto-tagging")
    background_tasks.add_task(_run_autotag, request.app.state.db_path)
    return HTMLResponse(RUNNING_HTML.format(label="Auto-tagging new bills"))
