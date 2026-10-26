"""Data models for Legiscanner."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Priority(str, Enum):
    """Bill tracking priority levels."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# Input Models (from config/bills.json)


class BillInput(BaseModel):
    """A bill to track from the input configuration."""

    bill_number: str
    tracking_reason: str
    priority: Priority = Priority.MEDIUM
    tags: list[str] = Field(default_factory=list)


class ConfigMetadata(BaseModel):
    """Metadata about the bill tracking configuration."""

    organization: str
    session_year: int | None = None
    last_updated: str | None = None
    source_file: str | None = None
    total_bills: int | None = None
    notes: str | None = None


class BillConfig(BaseModel):
    """Configuration file structure for bills to track."""

    session: str
    state: str = "VA"
    bills: list[BillInput]
    metadata: ConfigMetadata | None = None


# LegiScan API Response Models


class LegiScanSponsor(BaseModel):
    """Sponsor information from LegiScan."""

    people_id: int
    person_hash: str | None = None
    party_id: int | None = None
    party: str | None = None
    role_id: int | None = None
    role: str | None = None
    name: str | None = None
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    suffix: str | None = None
    nickname: str | None = None
    district: str | None = None
    ftm_eid: int | None = None
    votesmart_id: int | None = None
    opensecrets_id: str | None = None
    ballotpedia: str | None = None
    sponsor_type_id: int | None = None
    sponsor_order: int | None = None
    committee_sponsor: int | None = None
    committee_id: int | None = None


class LegiScanHistory(BaseModel):
    """History entry from LegiScan."""

    date: str
    action: str
    chamber: str | None = None
    chamber_id: int | None = None
    importance: int | None = None


class LegiScanTextVersion(BaseModel):
    """Text version info from LegiScan bill response."""

    doc_id: int
    date: str
    type: str
    type_id: int | None = None
    mime: str | None = None
    mime_id: int | None = None
    url: str | None = None
    state_link: str | None = None
    text_size: int | None = None


class LegiScanVote(BaseModel):
    """Vote summary from LegiScan."""

    roll_call_id: int
    date: str
    desc: str | None = None
    yea: int | None = None
    nay: int | None = None
    nv: int | None = None
    absent: int | None = None
    total: int | None = None
    passed: int | None = None
    chamber: str | None = None
    chamber_id: int | None = None
    url: str | None = None
    state_link: str | None = None


class LegiScanAmendment(BaseModel):
    """Amendment info from LegiScan."""

    amendment_id: int
    adopted: int | None = None
    chamber: str | None = None
    chamber_id: int | None = None
    date: str | None = None
    title: str | None = None
    description: str | None = None
    mime: str | None = None
    mime_id: int | None = None
    url: str | None = None
    state_link: str | None = None


class LegiScanSupplement(BaseModel):
    """Supplement info from LegiScan."""

    supplement_id: int
    date: str | None = None
    type: str | None = None
    type_id: int | None = None
    title: str | None = None
    description: str | None = None
    mime: str | None = None
    mime_id: int | None = None
    url: str | None = None
    state_link: str | None = None


class LegiScanSubject(BaseModel):
    """Subject/topic from LegiScan."""

    subject_id: int
    subject_name: str


class LegiScanSession(BaseModel):
    """Session info from LegiScan."""

    session_id: int
    session_name: str | None = None
    session_title: str | None = None
    year_start: int | None = None
    year_end: int | None = None
    special: int | None = None


class LegiScanBill(BaseModel):
    """Full bill details from LegiScan API."""

    bill_id: int
    change_hash: str | None = None
    session_id: int | None = None
    session: LegiScanSession | None = None
    url: str | None = None
    state_link: str | None = None
    completed: int | None = None
    status: int | None = None
    status_desc: str | None = None
    progress: list[dict[str, Any]] | None = None
    state: str | None = None
    state_id: int | None = None
    bill_number: str
    bill_type: str | None = None
    bill_type_id: int | None = None
    body: str | None = None
    body_id: int | None = None
    current_body: str | None = None
    current_body_id: int | None = None
    title: str | None = None
    description: str | None = None
    pending_committee_id: int | None = None
    committee: dict[str, Any] | list[Any] | None = None  # Can be dict or empty list
    referrals: list[dict[str, Any]] | None = None
    history: list[LegiScanHistory] | None = None
    sponsors: list[LegiScanSponsor] | None = None
    sasts: list[dict[str, Any]] | None = None
    subjects: list[LegiScanSubject] | None = None
    texts: list[LegiScanTextVersion] | None = None
    votes: list[LegiScanVote] | None = None
    amendments: list[LegiScanAmendment] | None = None
    supplements: list[LegiScanSupplement] | None = None
    calendar: list[dict[str, Any]] | None = None


class LegiScanText(BaseModel):
    """Bill text content from LegiScan getBillText API."""

    doc_id: int
    bill_id: int | None = None
    date: str | None = None
    type: str | None = None
    type_id: int | None = None
    mime: str | None = None
    mime_id: int | None = None
    doc: str  # base64 encoded content


# Output Models (for AI/RAG consumption)


class SponsorOutput(BaseModel):
    """Simplified sponsor info for output."""

    name: str
    party: str | None = None
    role: str | None = None
    district: str | None = None


class TextVersionOutput(BaseModel):
    """Bill text version for output."""

    version: str
    date: str
    format: str
    content: str  # Decoded text content
    state_url: str | None = None


class HistoryOutput(BaseModel):
    """Simplified history entry for output."""

    date: str
    action: str
    chamber: str | None = None


class OutcomeScoreOutput(BaseModel):
    """A bill's AI (+ optional user override) impact score for one outcome area.

    Outcome areas are a fixed classification (e.g. a county strategic plan's
    priority areas), distinct from the free-form `topics`/tags system. A bill
    only has a row here for areas the AI judged actually relevant — absence
    means "not scored," not "scored zero."
    """

    outcome_area_slug: str
    outcome_area_name: str
    ai_impact: int  # 1-5
    ai_reason: str = ""
    user_impact_override: int | None = None


class TrackingMetadata(BaseModel):
    """Tracking metadata with AI-generated and user-managed fields.

    New schema separates AI analysis (ai_tags, ai_reason, ai_impact) from
    user management (watching, user_tags, notes, user_impact_override).
    """

    # AI-generated fields (from autotag)
    ai_tags: list[str] = Field(default_factory=list)
    ai_reason: str = ""
    ai_impact: int | None = None  # 1-5 scale from AI analysis

    # User-managed fields
    watching: bool = False
    user_tags: list[str] = Field(default_factory=list)
    notes: str = ""
    user_impact_override: int | None = None  # User can override AI impact rating


class LineageMemberOutput(BaseModel):
    """One member bill of a bill's cross-session lineage.

    A lineage is the "conceptual bill" that recurs across sessions (bill
    numbers repeat every session, so HB1234 in 2026 is a different row from
    HB1234 in 2025 but may be the same subject matter). Members are ordered
    session-over-session so the UI can show evolution.
    """

    bill_id: int
    bill_number: str
    title: str | None = None
    status: str | None = None
    last_action_date: str | None = None
    session_id: int
    session_name: str
    year: int
    link_reason: str | None = None
    similarity: float | None = None


class BillOutput(BaseModel):
    """Complete bill output for AI/RAG consumption."""

    bill_number: str
    bill_id: int
    change_hash: str | None = None  # LegiScan change hash for update detection
    title: str | None = None
    description: str | None = None
    state: str
    session: str
    session_id: int | None = None  # DB id of the bill's session (for lineage view)
    status: str | None = None
    introduced_date: str | None = None
    last_action_date: str | None = None
    last_action: str | None = None
    sponsors: list[SponsorOutput] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)
    tracking_metadata: TrackingMetadata
    outcome_scores: list[OutcomeScoreOutput] = Field(default_factory=list)
    texts: list[TextVersionOutput] = Field(default_factory=list)
    history: list[HistoryOutput] = Field(default_factory=list)
    companion_bills: list[str] = Field(default_factory=list)  # Linked HB/SB pairs with similar titles
    lineage_id: int | None = None  # The cross-session lineage this bill belongs to (if any)
    lineage_members: list[LineageMemberOutput] = Field(default_factory=list)
    legiscan_url: str | None = None
    state_url: str | None = None
    retrieved_at: str = Field(default_factory=lambda: datetime.now().isoformat())
