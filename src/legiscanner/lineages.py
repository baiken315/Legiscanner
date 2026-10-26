"""Detect and link bill lineages (cross-session recurrence of a bill).

A "lineage" groups bills that are the same/same-subject legislation re-introduced
across legislative sessions. Bill numbers repeat every session (a new HB1234
appears each year), so (session_id, bill_number) is not a stable identity for
this purpose. We derive lineages by comparing bill *titles* across different
sessions using the same normalize_title / SequenceMatcher logic that powers
within-session companion detection in companions.py, plus an exact
same-legiscan-bill-id shortcut and optional sponsor-overlap boosting.

Lineages are a purely additive model (bill_lineages / bill_lineage_members) —
they do not touch bills, companions, or tracking. A bill belongs to exactly one
lineage; re-running the linker recomputes and overwrites the auto-derived
lineages while preserving manually curated ones.
"""

from __future__ import annotations

import logging
import sqlite3

from . import db
from .companions import calculate_title_similarity

logger = logging.getLogger(__name__)

# Title similarity above which two bills from different sessions are considered
# the same conceptual bill. Slightly lower than the companion threshold (0.90)
# because a re-introduced bill's title often drifts a bit session-over-session,
# but high enough to reject merely similar titles (e.g. two different "Relating
# to ..." bills that share only the boilerplate prefix). Sponsor-overlap boost
# (see find_lineage_links) further reduces false positives for genuine matches.
DEFAULT_SIMILARITY_THRESHOLD = 0.85


def _bills_by_session(conn: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    """All bills grouped by session, with sponsors for overlap scoring."""
    rows = conn.execute(
        "SELECT id, session_id, bill_number, title, legiscan_bill_id FROM bills "
        "WHERE title IS NOT NULL AND title != ''"
    ).fetchall()
    by_session: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        by_session.setdefault(r["session_id"], []).append(r)
    return by_session


def _sponsor_names(conn: sqlite3.Connection, bill_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sponsors WHERE bill_id = ? AND name IS NOT NULL", (bill_id,)
    ).fetchall()
    return {r["name"].strip().lower() for r in rows if r["name"]}


def _sponsor_overlap(a: set[str], b: set[str]) -> float:
    """Jaccard overlap of sponsor name sets (0.0 for empty sets)."""
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union)


def find_lineage_links(
    conn: sqlite3.Connection,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    *,
    use_sponsor_boost: bool = True,
) -> list[tuple[int, int, str, float]]:
    """Return cross-session links as (bill_id_a, bill_id_b, reason, score).

    Two bills in *different* sessions are linked when:
      - they share the same non-null legiscan_bill_id (reason 'same-legiscan-bill'), or
      - their normalized titles are similar enough (reason 'title-similar'), with the
        score boosted a little when they also share sponsors (still reported as
        'title-similar'; the boost is folded into the score).
    """
    by_session = _bills_by_session(conn)
    session_ids = list(by_session.keys())
    links: list[tuple[int, int, str, float]] = []
    seen: set[tuple[int, int]] = set()

    for i in range(len(session_ids)):
        for j in range(i + 1, len(session_ids)):
            sa, sb = session_ids[i], session_ids[j]
            for a in by_session[sa]:
                for b in by_session[sb]:
                    pair = (min(a["id"], b["id"]), max(a["id"], b["id"]))
                    if pair in seen:
                        continue

                    # Exact stable identity: same *positive* LegiScan bill id across
                    # sessions. 0/None mean "not yet fetched from LegiScan" (a
                    # sentinel, not a real identity) and must never link bills.
                    if (
                        a["legiscan_bill_id"]
                        and a["legiscan_bill_id"] == b["legiscan_bill_id"]
                    ):
                        seen.add(pair)
                        links.append((a["id"], b["id"], "same-legiscan-bill", 1.0))
                        continue

                    sim = calculate_title_similarity(a["title"], b["title"])
                    if sim < similarity_threshold:
                        continue

                    score = sim
                    if use_sponsor_boost:
                        overlap = _sponsor_overlap(
                            _sponsor_names(conn, a["id"]), _sponsor_names(conn, b["id"])
                        )
                        # A shared-sponsor signal nudges the score up (capped at 1.0)
                        # to reduce false positives from merely similar titles.
                        if overlap > 0:
                            score = min(1.0, sim + 0.05 * overlap)

                    seen.add(pair)
                    links.append((a["id"], b["id"], "title-similar", round(score, 4)))

    logger.info(f"Found {len(links)} cross-session candidate links")
    return links


def _union_find_link(
    bill_ids: list[int], pairs: list[tuple[int, int, str, float]]
) -> dict[int, int]:
    """Group bill ids into lineage buckets via union-find over the given pairs.

    Returns a mapping bill_id -> bucket_id (bucket ids are arbitrary but stable
    within the call).
    """
    parent: dict[int, int] = {b: b for b in bill_ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for a, b, _reason, _score in pairs:
        if a in parent and b in parent:
            union(a, b)

    roots: dict[int, int] = {}
    next_bucket = 0
    for b in bill_ids:
        r = find(b)
        if r not in roots:
            roots[r] = next_bucket
            next_bucket += 1
    return {b: roots[find(b)] for b in bill_ids}


def update_lineage_links_db(
    conn: sqlite3.Connection,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    *,
    use_sponsor_boost: bool = True,
    preserve_manual: bool = True,
) -> int:
    """Recompute cross-session lineage links and persist them.

    Auto-derived lineages ('title-similar' / 'same-legiscan-bill') are
    recomputed and overwritten each run. Lineages containing at least one
    'manual' member are left untouched (preserve_manual) so human curation is
    not clobbered. Returns the number of bills now in auto-derived lineages.
    """
    links = find_lineage_links(
        conn, similarity_threshold, use_sponsor_boost=use_sponsor_boost
    )

    # Identify bills already in a manually-curated lineage so we don't touch them.
    manual_bill_ids: set[int] = set()
    if preserve_manual:
        manual_bill_ids = {
            r["bill_id"]
            for r in conn.execute(
                "SELECT DISTINCT blm.bill_id FROM bill_lineage_members blm "
                "JOIN bill_lineages bl ON bl.id = blm.lineage_id "
                "WHERE blm.link_reason = 'manual'"
            ).fetchall()
        }

    candidate_ids = {b for link in links for b in (link[0], link[1])} - manual_bill_ids

    # Drop the auto-derived memberships from prior runs for these candidates
    # (manual ones are excluded and stay put).
    if candidate_ids:
        placeholders = ",".join("?" for _ in candidate_ids)
        conn.execute(
            f"DELETE FROM bill_lineage_members WHERE bill_id IN ({placeholders}) "
            f"AND link_reason != 'manual'",
            tuple(candidate_ids),
        )

    bucket_of = _union_find_link(sorted(candidate_ids), links)
    reason_by_bill: dict[int, str] = {}
    score_by_bill: dict[int, float] = {}
    for a, b, reason, score in links:
        for bill_id in (a, b):
            # Prefer the strongest signal a bill has (legiscan-id > title-similar).
            prev = reason_by_bill.get(bill_id)
            if prev is None or (reason == "same-legiscan-bill" and prev != "same-legiscan-bill"):
                reason_by_bill[bill_id] = reason
                score_by_bill[bill_id] = score
            elif score > score_by_bill.get(bill_id, 0.0):
                score_by_bill[bill_id] = score

    # Create one lineage per bucket and attach members.
    buckets: dict[int, list[int]] = {}
    for bill_id, bucket in bucket_of.items():
        if bill_id in candidate_ids:
            buckets.setdefault(bucket, []).append(bill_id)

    lineage_count = 0
    for bucket, members in buckets.items():
        if len(members) < 2:
            continue  # a lineage needs at least two bills to be meaningful
        lineage_id = db.get_or_create_lineage(conn)
        for bill_id in members:
            conn.execute(
                "INSERT OR REPLACE INTO bill_lineage_members "
                "(lineage_id, bill_id, session_id, link_reason, similarity) "
                "SELECT ?, id, session_id, ?, ? FROM bills WHERE id = ?",
                (lineage_id, reason_by_bill.get(bill_id, "title-similar"),
                 score_by_bill.get(bill_id), bill_id),
            )
        lineage_count += 1

    # Drop any now-empty auto lineages left over from a prior run.
    conn.execute(
        """
        DELETE FROM bill_lineages WHERE id NOT IN (
            SELECT DISTINCT lineage_id FROM bill_lineage_members
        ) AND id NOT IN (
            SELECT lineage_id FROM bill_lineage_members WHERE link_reason = 'manual'
        )
        """
    )

    conn.commit()
    logger.info(f"Linked {len(candidate_ids)} bills into {lineage_count} cross-session lineages")
    return len(candidate_ids)
