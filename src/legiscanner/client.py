"""LegiScan API client with rate limiting and caching."""

import base64
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .models import LegiScanBill, LegiScanText

logger = logging.getLogger(__name__)

# LegiScan takes the API key as a query parameter, and httpx logs full request
# URLs at INFO. Keep httpx/httpcore quiet so the key never lands in logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

BASE_URL = "https://api.legiscan.com/"


class LegiScanError(Exception):
    """Base exception for LegiScan API errors."""

    pass


class LegiScanRateLimitError(LegiScanError):
    """Raised when rate limit is exceeded."""

    pass


class LegiScanNotFoundError(LegiScanError):
    """Raised when a bill is not found."""

    pass


class LegiScanClient:
    """Client for interacting with the LegiScan API."""

    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: str | Path | None = None,
        timeout: float = 30.0,
        cache_ttl: float | None = 3600.0,
        requests_per_minute: int | None = 100,
    ):
        """
        Initialize the LegiScan client.

        Args:
            api_key: LegiScan API key. If not provided, uses LEGISCAN_API_KEY env var.
            cache_dir: Directory for caching API responses. If not provided, no caching.
            timeout: HTTP request timeout in seconds.
            cache_ttl: Max age (seconds) of a cached response before it is
                considered stale and re-fetched. The free tier is 30k req/mo
                (~1k/day), so a full refresh (~4,500 calls) burns a large chunk
                of the monthly budget; a TTL bounds how long a stale cache can
                serve. Pass None to cache forever.
            requests_per_minute: Proactive client-side throttle. The API's own
                429 handling is reactive (it waits after being told to); this
                paces requests ahead of time so a full run doesn't hammer the
                rate limit. Pass None to disable.
        """
        self.api_key = api_key or os.environ.get("LEGISCAN_API_KEY")
        if not self.api_key:
            raise ValueError(
                "API key required. Set LEGISCAN_API_KEY environment variable "
                "or pass api_key parameter."
            )

        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.client = httpx.Client(timeout=timeout)
        self.cache_ttl = cache_ttl
        self.requests_per_minute = requests_per_minute
        self._request_times: list[float] = []

    def _cache_key(self, operation: str, params: dict[str, Any]) -> str:
        """Generate a cache key for an API request."""
        key_data = f"{operation}:{json.dumps(params, sort_keys=True)}"
        return hashlib.sha256(key_data.encode()).hexdigest()[:16]

    def _get_cached(self, cache_key: str) -> dict[str, Any] | None:
        """Retrieve a cached response if it is fresh (within cache_ttl)."""
        if not self.cache_dir:
            return None

        cache_file = self.cache_dir / f"{cache_key}.json"
        if not cache_file.exists():
            return None
        try:
            payload = json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(payload, dict):
            return None
        # A cache entry is {"ts": <unix>, "data": <response>}. If we have a TTL
        # and the entry is stale, treat it as a miss so it gets refreshed.
        if self.cache_ttl is not None and "ts" in payload:
            age = time.time() - float(payload["ts"])
            if age > self.cache_ttl:
                logger.debug(f"Cache stale for {cache_key} (age {age:.0f}s > ttl {self.cache_ttl:.0f}s)")
                return None
            return payload.get("data")
        # Legacy/undated cache files: honor the on-disk mtime instead.
        if self.cache_ttl is not None:
            age = time.time() - cache_file.stat().st_mtime
            if age > self.cache_ttl:
                return None
        return payload

    def _set_cached(self, cache_key: str, data: dict[str, Any]) -> None:
        """Cache an API response with a timestamp for TTL enforcement."""
        if not self.cache_dir:
            return

        cache_file = self.cache_dir / f"{cache_key}.json"
        try:
            cache_file.write_text(json.dumps({"ts": time.time(), "data": data}))
        except OSError as e:
            logger.warning(f"Failed to cache response: {e}")

    def _throttle(self) -> None:
        """Sleep just enough to keep the request rate under requests_per_minute.

        Maintains a sliding window of request timestamps; if adding "now" would
        exceed the per-minute budget, waits until the oldest timestamp in the
        window ages out. This is proactive pacing, complementing the reactive
        429 retry in _request.
        """
        if not self.requests_per_minute:
            return
        now = time.time()
        # Drop timestamps older than one minute.
        self._request_times = [t for t in self._request_times if now - t < 60.0]
        if len(self._request_times) >= self.requests_per_minute:
            wait = 60.0 - (now - self._request_times[0]) + 0.05
            if wait > 0:
                logger.debug(f"Rate budget hit; sleeping {wait:.1f}s")
                time.sleep(wait)
                now = time.time()
                self._request_times = [t for t in self._request_times if now - t < 60.0]
        self._request_times.append(time.time())

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def _request(
        self, operation: str, params: dict[str, Any] | None = None, use_cache: bool = True
    ) -> dict[str, Any]:
        """
        Make an API request to LegiScan.

        Args:
            operation: API operation name (e.g., 'getBill', 'getBillText')
            params: Additional parameters for the request
            use_cache: Whether to use caching for this request

        Returns:
            API response as a dictionary
        """
        params = params or {}
        cache_key = self._cache_key(operation, params)

        # Check cache first
        if use_cache:
            cached = self._get_cached(cache_key)
            if cached:
                logger.debug(f"Cache hit for {operation}")
                return cached

        # Build request URL
        request_params = {"key": self.api_key, "op": operation, **params}

        self._throttle()
        logger.debug(f"API request: {operation} with params {params}")
        response = self.client.get(BASE_URL, params=request_params)

        # Handle HTTP errors
        if response.status_code == 429:
            raise LegiScanRateLimitError("Rate limit exceeded. Please wait before retrying.")

        # Not raise_for_status(): httpx's HTTPStatusError message embeds the
        # request URL, which includes the API key.
        if response.is_error:
            raise LegiScanError(f"LegiScan HTTP {response.status_code} for {operation}")

        data = response.json()

        # Check for API-level errors
        if data.get("status") == "ERROR":
            error_msg = data.get("alert", {}).get("message", "Unknown error")
            if "not found" in error_msg.lower() or "invalid" in error_msg.lower():
                raise LegiScanNotFoundError(error_msg)
            raise LegiScanError(error_msg)

        # Cache successful response
        if use_cache:
            self._set_cached(cache_key, data)

        return data

    def get_session_list(self, state: str = "VA") -> list[dict[str, Any]]:
        """
        Get list of legislative sessions for a state.

        Args:
            state: State abbreviation (default: VA)

        Returns:
            List of session information dictionaries
        """
        response = self._request("getSessionList", {"state": state})
        sessions = response.get("sessions", [])
        return sessions if isinstance(sessions, list) else list(sessions.values())

    def get_master_list(
        self, state: str = "VA", session_id: int | None = None
    ) -> dict[str, dict[str, Any]]:
        """
        Get master list of all bills in a session.

        Args:
            state: State abbreviation
            session_id: Specific session ID (if not provided, uses current session)

        Returns:
            Dictionary of bill summaries keyed by bill_id
        """
        params: dict[str, Any] = {}
        if session_id:
            params["id"] = session_id
        else:
            params["state"] = state

        response = self._request("getMasterList", params)
        masterlist = response.get("masterlist", {})

        # Handle both list and dict responses
        if isinstance(masterlist, list):
            return {str(b.get("bill_id")): b for b in masterlist if isinstance(b, dict)}
        return masterlist

    def get_bill(
        self,
        bill_id: int | None = None,
        state: str | None = None,
        bill_number: str | None = None,
    ) -> LegiScanBill:
        """
        Get full bill details.

        Args:
            bill_id: LegiScan bill ID
            state: State abbreviation (required if using bill_number)
            bill_number: Bill number (e.g., 'HB1234')

        Returns:
            LegiScanBill object with full bill details
        """
        params: dict[str, Any] = {}
        if bill_id:
            params["id"] = bill_id
        elif state and bill_number:
            params["state"] = state
            params["bill"] = bill_number
        else:
            raise ValueError("Must provide either bill_id or both state and bill_number")

        response = self._request("getBill", params)
        bill_data = response.get("bill", {})

        return LegiScanBill.model_validate(bill_data)

    def get_bill_text(self, doc_id: int) -> LegiScanText:
        """
        Get bill text content.

        Args:
            doc_id: Document ID from bill's texts array

        Returns:
            LegiScanText object with base64-encoded content
        """
        response = self._request("getBillText", {"id": doc_id})
        text_data = response.get("text", {})

        return LegiScanText.model_validate(text_data)

    def get_decoded_bill_text(self, doc_id: int) -> tuple[str, str]:
        """
        Get bill text content decoded from base64.

        Args:
            doc_id: Document ID from bill's texts array

        Returns:
            Tuple of (decoded_content, mime_type)
        """
        text = self.get_bill_text(doc_id)
        decoded = base64.b64decode(text.doc)

        # Try to decode as text
        mime = text.mime or "application/octet-stream"
        if mime.startswith("text/") or mime == "application/xhtml+xml":
            try:
                return decoded.decode("utf-8"), mime
            except UnicodeDecodeError:
                return decoded.decode("latin-1"), mime

        # Return raw bytes as string for binary formats (PDF, etc.)
        return decoded.decode("latin-1", errors="replace"), mime

    def search(
        self,
        state: str = "VA",
        query: str | None = None,
        year: int | None = None,
        session_id: int | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        """
        Search for bills (single page).

        Args:
            state: State abbreviation
            query: Search query string (supports boolean: AND, OR, NOT)
            year: Filter by year (may not work reliably, prefer session_id)
            session_id: Filter to specific legislative session
            page: Page number for pagination

        Returns:
            Search results with summary and results list
        """
        params: dict[str, Any] = {"state": state, "page": page}
        if query:
            params["query"] = query
        if year:
            params["year"] = year
        if session_id:
            params["id"] = session_id

        response = self._request("search", params, use_cache=False)
        return response.get("searchresult", {})

    def search_all(
        self,
        state: str = "VA",
        query: str | None = None,
        year: int | None = None,
        session_id: int | None = None,
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        """
        Search for bills and return ALL results across pages.

        Args:
            state: State abbreviation
            query: Search query string (supports boolean: AND, OR, NOT)
            year: Filter by year (may not work reliably, prefer session_id)
            session_id: Filter to specific legislative session
            max_pages: Maximum pages to fetch (safety limit)

        Returns:
            List of all bill results
        """
        all_results: list[dict[str, Any]] = []
        page = 1

        while page <= max_pages:
            result = self.search(
                state=state, query=query, year=year, session_id=session_id, page=page
            )

            summary = result.get("summary", {})
            total_count = summary.get("count", 0)
            page_count = summary.get("page_total", 1)

            # Extract bills from this page (keys are numeric strings, values are bill dicts)
            for key, value in result.items():
                if key != "summary" and isinstance(value, dict) and "bill_id" in value:
                    all_results.append(value)

            logger.info(f"Search page {page}/{page_count}: found {len(all_results)}/{total_count} bills")

            if page >= page_count:
                break
            page += 1

        return all_results

    def close(self) -> None:
        """Close the HTTP client."""
        self.client.close()

    def __enter__(self) -> "LegiScanClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
