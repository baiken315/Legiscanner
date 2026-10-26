"""Detect and link companion bills (HB/SB pairs with matching titles)."""

import logging
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


def normalize_title(title: str) -> str:
    """Normalize bill title for comparison.

    Removes common variations and standardizes format.
    """
    if not title:
        return ""

    # Convert to lowercase
    normalized = title.lower().strip()

    # Remove common prefixes that vary
    prefixes_to_remove = [
        "relating to ",
        "concerning ",
        "regarding ",
        "an act to ",
    ]
    for prefix in prefixes_to_remove:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]

    # Remove trailing periods
    normalized = normalized.rstrip(".")

    return normalized


def calculate_title_similarity(title1: str, title2: str) -> float:
    """Calculate similarity score between two bill titles.

    Returns a score between 0.0 (completely different) and 1.0 (identical).
    """
    norm1 = normalize_title(title1)
    norm2 = normalize_title(title2)

    if not norm1 or not norm2:
        return 0.0

    # Use SequenceMatcher for fuzzy matching
    return SequenceMatcher(None, norm1, norm2).ratio()


def are_companion_bills(bill1_number: str, bill2_number: str) -> bool:
    """Check if two bills are likely companions (one HB, one SB).

    Args:
        bill1_number: First bill number (e.g., "HB1234")
        bill2_number: Second bill number (e.g., "SB5678")

    Returns:
        True if one is HB and the other is SB (or HJR/SJR)
    """
    b1 = bill1_number.upper().strip()
    b2 = bill2_number.upper().strip()

    # Extract prefixes (HB, SB, HJR, SJR, etc.)
    def get_prefix(bill_num: str) -> str:
        # Find where digits start
        for i, char in enumerate(bill_num):
            if char.isdigit():
                return bill_num[:i]
        return bill_num

    prefix1 = get_prefix(b1)
    prefix2 = get_prefix(b2)

    # Check if one is House and one is Senate
    house_prefixes = {"HB", "HJR", "HJ", "HR"}
    senate_prefixes = {"SB", "SJR", "SJ", "SR"}

    is_house_senate = (prefix1 in house_prefixes and prefix2 in senate_prefixes) or \
                     (prefix1 in senate_prefixes and prefix2 in house_prefixes)

    return is_house_senate


def find_companion_bills(bills: list[dict], similarity_threshold: float = 0.90) -> dict[str, list[str]]:
    """Find companion bills across the entire dataset.

    Args:
        bills: List of bill dictionaries with 'bill_number' and 'title' keys
        similarity_threshold: Minimum similarity score to consider bills companions (0.0-1.0)

    Returns:
        Dictionary mapping bill_number -> list of companion bill_numbers
    """
    companions: dict[str, list[str]] = {}

    # Build a map for quick lookups
    bill_map = {b["bill_number"]: b for b in bills}

    # Compare all pairs
    for i, bill1 in enumerate(bills):
        bill1_num = bill1["bill_number"]
        bill1_title = bill1.get("title", "")

        if not bill1_title:
            continue

        for bill2 in bills[i+1:]:
            bill2_num = bill2["bill_number"]
            bill2_title = bill2.get("title", "")

            if not bill2_title:
                continue

            # Skip if not HB/SB pair
            if not are_companion_bills(bill1_num, bill2_num):
                continue

            # Calculate similarity
            similarity = calculate_title_similarity(bill1_title, bill2_title)

            if similarity >= similarity_threshold:
                # Add to companions list
                if bill1_num not in companions:
                    companions[bill1_num] = []
                if bill2_num not in companions:
                    companions[bill2_num] = []

                if bill2_num not in companions[bill1_num]:
                    companions[bill1_num].append(bill2_num)
                if bill1_num not in companions[bill2_num]:
                    companions[bill2_num].append(bill1_num)

                logger.debug(
                    f"Found companion: {bill1_num} <-> {bill2_num} "
                    f"(similarity: {similarity:.2%})"
                )

    return companions


def find_companion_pairs(
    bills: list[dict], similarity_threshold: float = 0.90
) -> list[tuple[str, str, float]]:
    """Find companion bill pairs, keeping the similarity score that matched.

    Args:
        bills: List of bill dictionaries with 'bill_number' and 'title' keys
        similarity_threshold: Minimum similarity score to consider bills companions (0.0-1.0)

    Returns:
        List of (bill_number_a, bill_number_b, similarity) tuples, one per pair.
    """
    pairs: list[tuple[str, str, float]] = []

    for i, bill1 in enumerate(bills):
        bill1_num = bill1["bill_number"]
        bill1_title = bill1.get("title", "")
        if not bill1_title:
            continue

        for bill2 in bills[i + 1:]:
            bill2_num = bill2["bill_number"]
            bill2_title = bill2.get("title", "")
            if not bill2_title:
                continue

            if not are_companion_bills(bill1_num, bill2_num):
                continue

            similarity = calculate_title_similarity(bill1_title, bill2_title)
            if similarity >= similarity_threshold:
                pairs.append((bill1_num, bill2_num, similarity))
                logger.debug(f"Found companion: {bill1_num} <-> {bill2_num} (similarity: {similarity:.2%})")

    return pairs


def update_companion_links(combined_data: dict, similarity_threshold: float = 0.90) -> dict:
    """Update a combined.json structure with companion bill links.

    Args:
        combined_data: Dictionary with 'bills' list
        similarity_threshold: Minimum similarity score (0.0-1.0)

    Returns:
        Updated combined_data with companion_bills populated
    """
    bills = combined_data.get("bills", [])

    logger.info(f"Searching for companion bills among {len(bills)} bills...")
    companions = find_companion_bills(bills, similarity_threshold)

    # Update each bill with its companions
    companion_count = 0
    for bill in bills:
        bill_number = bill["bill_number"]
        if bill_number in companions:
            bill["companion_bills"] = sorted(companions[bill_number])
            companion_count += 1
        else:
            bill["companion_bills"] = []

    logger.info(f"Found {len(companions)} bills with companions ({companion_count} total)")

    # Add to metadata
    if "metadata" not in combined_data:
        combined_data["metadata"] = {}

    combined_data["metadata"]["companion_pairs"] = len(companions) // 2 if companions else 0

    return combined_data


def update_companion_links_db(conn, session_id: int, similarity_threshold: float = 0.90) -> int:
    """Recompute companion links for a session and persist them to the companions table.

    Same O(n^2) title-similarity pass as update_companion_links, but writes through
    db.replace_companions() instead of rewriting a JSON file. Returns the pair count.
    """
    from . import db

    rows = conn.execute(
        "SELECT id, bill_number, title FROM bills WHERE session_id = ?", (session_id,)
    ).fetchall()
    bills = [{"bill_number": r["bill_number"], "title": r["title"]} for r in rows]
    id_by_number = {r["bill_number"]: r["id"] for r in rows}

    logger.info(f"Searching for companion bills among {len(bills)} bills...")
    pairs = find_companion_pairs(bills, similarity_threshold)

    id_pairs = [
        (id_by_number[a], id_by_number[b], score)
        for a, b, score in pairs
    ]
    db.replace_companions(conn, session_id, id_pairs)
    conn.commit()

    logger.info(f"Found {len(id_pairs)} companion pairs")
    return len(id_pairs)
