"""Auto-tagging bills using a local LLM (Ollama/llama.cpp-compatible chat endpoint)."""

import json
import logging
import re
import sqlite3

import httpx

from . import db

logger = logging.getLogger(__name__)

# Fallback-only defaults, used when no session row is available (e.g. standalone CLI use
# against a file with no session context). Normal operation reads ollama_url/model from
# the active session's row instead of these constants.
DEFAULT_OLLAMA_URL = "http://localhost:8080/"
DEFAULT_MODEL = "qwen3.6-35b-a3b"

ANALYSIS_PROMPT_TEMPLATE = """You are a legislative analyst for local government staff. Analyze this bill and provide structured output.

Context: You work for a local government tracking Virginia General Assembly bills that could affect the policy areas your organization tracks. Bills are imported via full-text search, so MOST bills will NOT be relevant - they just happen to mention related words in unrelated contexts.

Bill Title: {title}

Bill Description: {description}

Provide your analysis as a JSON object with these exact fields:

1. "tags": Array of 3-6 lowercase tags. Prefer tags from this list of tracked topics when applicable:
{topic_list}
   You may also use additional descriptive tags not in this list if none of the above fit.

2. "reason": One sentence (max 100 words) summarizing what the bill does and why it matters (or doesn't) to local government.

3. "outcome_scores": Array of objects scoring this bill's impact against the organization's strategic outcome areas below. A bill can score against MULTIPLE areas if it genuinely touches more than one (e.g. a housing bill near transit could score on both Housing and Mobility) — do not force a single area. ONLY include an area if the bill has real, direct relevance to it; most bills will only match 0-2 areas. Each entry: {{"area": "<exact slug from the list below>", "impact": <1-5>, "reason": "<one short sentence specific to this area>"}}.

   Outcome areas (use the exact slug shown):
{outcome_area_list}

   Impact scale for each area:
   - 5: Major direct impact — fundamentally changes local authority or policy in this area
   - 4: Significant impact — meaningful changes to local policy options in this area
   - 3: Moderate impact — incremental changes, studies or task forces that may lead to action
   - 2: Minor impact — tangential relevance, primarily symbolic
   - 1: Minimal but real relevance — rarely needed; if an area isn't genuinely relevant, omit it entirely rather than scoring it 1

   If the bill has no direct relevance to ANY outcome area, return an empty array: "outcome_scores": []

Example output for a bill affecting both housing and transit:
{{"tags": ["housing", "landlord-tenant"], "reason": "Allows localities to enforce habitability standards on behalf of tenants, expanding local government authority over rental housing conditions.", "outcome_scores": [{{"area": "housing-neighborhood-livability", "impact": 5, "reason": "Directly expands local enforcement authority over rental housing conditions."}}, {{"area": "mobility-transportation", "impact": 2, "reason": "Applies to transit-adjacent rental properties specifically."}}]}}

Example output for a bill with no relevance to any tracked outcome area:
{{"tags": ["taxes", "corporate"], "reason": "Modifies corporate income tax credits for manufacturing investments, unrelated to any tracked local government outcome area.", "outcome_scores": []}}

Output ONLY the JSON object, nothing else:"""

TAGGING_PROMPT_TEMPLATE = """You are a legislative bill tagger for local government staff. Given a bill's title and description, output relevant tags.

Prefer tags from this list of tracked topics when applicable:
{topic_list}
You may also use additional descriptive tags not in this list if none of the above fit.

Rules:
1. Output ONLY a JSON array of lowercase tag strings
2. Use 3-6 tags per bill
3. Be specific but not overly granular
4. If the bill is not related to any tracked topic, use "non-relevant" as the first tag

Example output for relevant bill: ["housing", "landlord-tenant", "enforcement"]
Example output for non-relevant bill: ["non-relevant", "transportation"]

Bill Title: {title}

Bill Description: {description}

Output only the JSON array of tags, nothing else:"""


def _format_topic_list(topics: list[sqlite3.Row]) -> str:
    if not topics:
        return "   (no tracked topics configured yet)"
    lines = []
    for t in topics:
        desc = f" - {t['description']}" if t["description"] else ""
        lines.append(f"   - {t['name']}{desc}")
    return "\n".join(lines)


def _format_outcome_area_list(outcome_areas: list[sqlite3.Row]) -> str:
    if not outcome_areas:
        return "   (no outcome areas configured — run `legiscanner seed-outcome-areas`)"
    lines = []
    for a in outcome_areas:
        desc = f" — {a['description']}" if a["description"] else ""
        lines.append(f"   - slug \"{a['slug']}\": {a['name']}{desc}")
    return "\n".join(lines)


def build_analysis_prompt(
    topics: list[sqlite3.Row], outcome_areas: list[sqlite3.Row], title: str, description: str
) -> str:
    """Build the full-analysis prompt from a session's topic vocabulary and outcome areas."""
    return ANALYSIS_PROMPT_TEMPLATE.format(
        topic_list=_format_topic_list(topics),
        outcome_area_list=_format_outcome_area_list(outcome_areas),
        title=title or "Untitled",
        description=description or "No description available.",
    )


def build_tagging_prompt(topics: list[sqlite3.Row], title: str, description: str) -> str:
    """Build the tags-only prompt from a session's active topic vocabulary."""
    return TAGGING_PROMPT_TEMPLATE.format(
        topic_list=_format_topic_list(topics),
        title=title or "Untitled",
        description=description or "No description available.",
    )


class OllamaTagger:
    """Tag bills using a local OpenAI-compatible chat endpoint (Ollama or llama.cpp)."""

    def __init__(
        self,
        ollama_url: str = DEFAULT_OLLAMA_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 60.0,
    ):
        self.ollama_url = ollama_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.client = httpx.Client(timeout=timeout)

    def _extract_tags(self, response_text: str) -> list[str]:
        """Extract tags array from LLM response."""
        response_text = response_text.strip()

        match = re.search(r"\[.*?\]", response_text, re.DOTALL)
        if match:
            try:
                tags = json.loads(match.group())
                if isinstance(tags, list):
                    cleaned = []
                    for tag in tags:
                        if isinstance(tag, str):
                            tag = tag.lower().strip().replace(" ", "-")
                            if tag and len(tag) < 50:
                                cleaned.append(tag)
                    return cleaned[:8]
            except json.JSONDecodeError:
                pass

        if "," in response_text:
            tags = [
                t.strip().lower().replace(" ", "-")
                for t in response_text.replace("[", "").replace("]", "").replace('"', "").split(",")
            ]
            return [t for t in tags if t and len(t) < 50][:8]

        logger.warning(f"Could not parse tags from response: {response_text[:100]}")
        return []

    def tag_bill(self, topics: list[sqlite3.Row], title: str, description: str) -> list[str]:
        """Generate tags for a bill using the configured LLM and topic vocabulary."""
        prompt = build_tagging_prompt(topics, title, description)

        try:
            response = self.client.post(
                f"{self.ollama_url}/v1/chat/completions",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "temperature": 0.3,
                    "max_tokens": 100,
                },
            )
            response.raise_for_status()

            result = response.json()
            response_text = result["choices"][0]["message"]["content"]

            tags = self._extract_tags(response_text)
            logger.debug(f"Generated tags: {tags}")
            return tags

        except httpx.ConnectError:
            logger.error(f"Could not connect to LLM server at {self.ollama_url}")
            return []
        except httpx.HTTPStatusError as e:
            logger.error(f"LLM server API error: {e}")
            return []
        except Exception as e:
            logger.error(f"Tagging error: {e}")
            return []

    def _extract_analysis(self, response_text: str, valid_slugs: set[str]) -> dict:
        """Extract analysis JSON from LLM response.

        valid_slugs restricts outcome_scores to the seeded taxonomy — an area
        slug the model invents or misspells is dropped rather than passed
        through, since outcome areas are a fixed classification (unlike tags,
        which are freely created).
        """
        response_text = response_text.strip()

        match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
                if isinstance(data, dict):
                    result = {"tags": [], "reason": "", "outcome_scores": []}

                    if "tags" in data and isinstance(data["tags"], list):
                        result["tags"] = [
                            str(t).lower().strip().replace(" ", "-")
                            for t in data["tags"]
                            if isinstance(t, str) and len(t) < 50
                        ][:8]

                    if "reason" in data and isinstance(data["reason"], str):
                        reason = data["reason"].strip()
                        if len(reason) > 300:
                            reason = reason[:297] + "..."
                        result["reason"] = reason

                    if "outcome_scores" in data and isinstance(data["outcome_scores"], list):
                        scores = []
                        for entry in data["outcome_scores"]:
                            if not isinstance(entry, dict):
                                continue
                            slug = str(entry.get("area", "")).strip().lower()
                            if slug not in valid_slugs:
                                continue
                            try:
                                impact = int(entry.get("impact"))
                            except (ValueError, TypeError):
                                continue
                            if not (1 <= impact <= 5):
                                continue
                            area_reason = str(entry.get("reason", "")).strip()[:300]
                            scores.append({"slug": slug, "impact": impact, "reason": area_reason})
                        result["outcome_scores"] = scores

                    return result
            except json.JSONDecodeError:
                pass

        logger.warning(f"Could not parse analysis from response: {response_text[:200]}")
        return {"tags": [], "reason": "", "outcome_scores": []}

    def analyze_bill(
        self, topics: list[sqlite3.Row], outcome_areas: list[sqlite3.Row], title: str, description: str
    ) -> dict:
        """Generate full analysis for a bill: tags, reason, and per-outcome-area impact scores."""
        prompt = build_analysis_prompt(topics, outcome_areas, title, description)
        valid_slugs = {a["slug"] for a in outcome_areas}

        try:
            response = self.client.post(
                f"{self.ollama_url}/v1/chat/completions",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "temperature": 0.3,
                    "max_tokens": 500,
                },
            )
            response.raise_for_status()

            result = response.json()
            response_text = result["choices"][0]["message"]["content"]

            analysis = self._extract_analysis(response_text, valid_slugs)
            logger.debug(f"Generated analysis: {analysis}")
            return analysis

        except httpx.ConnectError:
            logger.error(f"Could not connect to LLM server at {self.ollama_url}")
            return {"tags": [], "reason": "", "outcome_scores": []}
        except httpx.HTTPStatusError as e:
            logger.error(f"LLM server API error: {e}")
            return {"tags": [], "reason": "", "outcome_scores": []}
        except Exception as e:
            logger.error(f"Analysis error: {e}")
            return {"tags": [], "reason": "", "outcome_scores": []}

    def check_connection(self) -> bool:
        """Check if the LLM server is reachable."""
        try:
            response = self.client.get(f"{self.ollama_url}/v1/models")
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"LLM server connection check failed: {e}")
            return False

    def close(self):
        """Close HTTP client."""
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _model_version(model: str) -> str | None:
    """Best-effort model version string (the trailing size/quant token, if any).

    Local LLMs (Ollama/llama.cpp) encode the version in the model name
    (e.g. 'qwen3.6-35b-a3b' -> '35b-a3b'). We capture what we can so analysis
    provenance is meaningful without a version API.
    """
    m = re.search(r"(\d+[a-z]*b[a-z0-9-]*)$", model.lower())
    return m.group(1) if m else None


def tagger_for_session(conn: sqlite3.Connection, session_id: int) -> OllamaTagger:
    """Build an OllamaTagger configured from the session's stored ollama_url/model."""
    session = db.get_session(conn, session_id)
    if session:
        return OllamaTagger(ollama_url=session["ollama_url"], model=session["model"])
    return OllamaTagger(ollama_url=DEFAULT_OLLAMA_URL, model=DEFAULT_MODEL)


def auto_tag_bills(
    conn: sqlite3.Connection,
    session_id: int,
    *,
    tagger: OllamaTagger | None = None,
    full_analysis: bool = False,
    new_only: bool = False,
    single_bill: str | None = None,
    filter_impact: int | None = None,
    exclude_tags: list[str] | None = None,
) -> dict:
    """
    Auto-tag bills for a session using full descriptions from the DB.

    Args:
        conn: DB connection
        session_id: Session to operate within
        tagger: An OllamaTagger to use (defaults to one built from the session's config)
        full_analysis: If True, generate tags, reason, and impact score
        new_only: If True, only analyze bills that don't have AI tags yet
        single_bill: If provided, only retag this specific bill number
        filter_impact: If provided, only process bills with this effective impact score
        exclude_tags: If provided, skip bills that have any of these tags (ai or user)

    Returns:
        Dict with processed_count and skipped_count.
    """
    own_tagger = tagger is None
    tagger = tagger or tagger_for_session(conn, session_id)

    try:
        if not tagger.check_connection():
            raise ConnectionError(f"Cannot connect to LLM server at {tagger.ollama_url}")

        topics = db.list_topics(conn, session_id, ai_taggable_only=True)
        outcome_areas = db.list_outcome_areas(conn, session_id)

        sql = """
            SELECT b.id, b.bill_number, b.title, b.description
            FROM bills b
            LEFT JOIN tracking tr ON tr.bill_id = b.id
            WHERE b.session_id = ?
        """
        params: list = [session_id]

        if single_bill:
            sql += " AND UPPER(b.bill_number) = ?"
            params.append(single_bill.upper())
        elif new_only:
            sql += """
                AND b.id NOT IN (
                    SELECT bill_id FROM bill_topics WHERE source = 'ai'
                )
            """
        elif filter_impact is not None:
            sql += " AND COALESCE(tr.user_impact_override, tr.ai_impact) = ?"
            params.append(filter_impact)

        rows = conn.execute(sql, params).fetchall()

        if exclude_tags:
            placeholders = ",".join("?" for _ in exclude_tags)
            excluded_bill_ids = {
                r["bill_id"]
                for r in conn.execute(
                    f"""
                    SELECT bt.bill_id FROM bill_topics bt JOIN topics t ON t.id = bt.topic_id
                    WHERE t.name IN ({placeholders})
                    """,
                    exclude_tags,
                ).fetchall()
            }
            rows = [r for r in rows if r["id"] not in excluded_bill_ids]

        mode = "full analysis" if full_analysis else "tagging"
        logger.info(f"Running {mode} on {len(rows)} bills using {tagger.model}...")

        processed = 0
        for row in rows:
            if full_analysis:
                analysis = tagger.analyze_bill(topics, outcome_areas, row["title"], row["description"])
                db.set_tracking(conn, row["id"], ai_reason=analysis["reason"])
                db.replace_ai_topic_links(conn, row["id"], session_id, analysis["tags"])
                db.replace_outcome_scores(conn, row["id"], session_id, analysis["outcome_scores"])
                # Record provenance so cross-session comparisons are reproducible:
                # which model produced this analysis, and when.
                db.record_analysis_run(
                    conn,
                    row["id"],
                    session_id,
                    model=tagger.model,
                    model_version=_model_version(tagger.model),
                    tags=analysis["tags"],
                    reason=analysis["reason"],
                )
                conn.commit()  # checkpoint: a crash mid-run keeps finished bills
                logger.info(
                    f"  {row['bill_number']}: tags={analysis['tags']}, "
                    f"outcome_scores={[(s['slug'], s['impact']) for s in analysis['outcome_scores']]}"
                )
            else:
                tags = tagger.tag_bill(topics, row["title"], row["description"])
                db.replace_ai_topic_links(conn, row["id"], session_id, tags)
                db.record_analysis_run(
                    conn,
                    row["id"],
                    session_id,
                    model=tagger.model,
                    model_version=_model_version(tagger.model),
                    tags=tags,
                    reason="",
                )
                conn.commit()  # checkpoint
                logger.info(f"  {row['bill_number']}: {tags}")
            processed += 1

        conn.commit()
        return {"processed_count": processed, "skipped_count": 0}
    finally:
        if own_tagger:
            tagger.close()
