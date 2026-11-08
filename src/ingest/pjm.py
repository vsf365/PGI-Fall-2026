"""
PJM Data Miner 2 API Client.

Fetches market and operational data from PJM Data Miner 2 API:
- Day-Ahead hourly LMPs (da_hrl_lmps)
- Real-Time hourly LMPs (rt_hrl_lmps)
- Metered hourly load by zone / RTO (hrl_load_metered)
- Generation by fuel type (gen_by_fuel)
- Aggregate generation outages (gen_outages_by_type)

Handles:
- Pagination (startRow, rowCount up to 50,000)
- Rate limiting (6 req/min for non-members -> 11s sleep)
- Exponential backoff and retry on HTTP 429 / 5xx
- Archived vs standard endpoint boundaries
- Raw JSON storage with timestamps and SHA-256 manifests
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Union

import requests

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from configs.config import (
    PJM_API_KEY,
    PJM_HEADERS,
    RATE_LIMITS,
    RAW_ISO,
    MANIFEST_DIR,
)

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pjm_ingest")

PJM_BASE_URL = "https://api.pjm.com/api/v1"
DEFAULT_ROW_COUNT = 50000
RATE_LIMIT_SLEEP = RATE_LIMITS.get("pjm", {}).get("sleep", 11.0)
MAX_RETRIES = 5
BACKOFF_FACTOR = 2.0


def _compute_sha256(content: bytes) -> str:
    """Compute SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(content).hexdigest()


def _format_date_param(d: Union[str, date, datetime]) -> str:
    """Format date to YYYY-MM-DD string."""
    if isinstance(d, datetime):
        return d.strftime("%Y-%m-%d")
    elif isinstance(d, date):
        return d.strftime("%Y-%m-%d")
    elif isinstance(d, str):
        # Validate or standardize
        return d[:10]
    raise ValueError(f"Unsupported date type: {type(d)}")


def _record_manifest(
    file_path: Path,
    feed_name: str,
    params: Dict[str, Any],
    row_count: int,
    file_size_bytes: int,
    checksum: str,
) -> None:
    """Append retrieval record to manifests/pjm_manifest.jsonl."""
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    manifest_file = MANIFEST_DIR / "pjm_manifest.jsonl"
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "file_path": str(file_path),
        "feed_name": feed_name,
        "params": {k: str(v) for k, v in params.items()},
        "row_count": row_count,
        "file_size_bytes": file_size_bytes,
        "sha256": checksum,
    }
    with open(manifest_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    logger.debug(f"Manifest updated for {file_path.name}")


def _save_raw_response(
    feed_name: str,
    data: List[Dict[str, Any]],
    start_str: str,
    end_str: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Path:
    """Save raw JSON response to data/raw/iso/."""
    RAW_ISO.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    filename = f"pjm_{feed_name}_{start_str}_{end_str}_{ts}.json"
    file_path = RAW_ISO / filename

    payload = {
        "feed": feed_name,
        "retrieved_at": datetime.utcnow().isoformat() + "Z",
        "record_count": len(data),
        "metadata": metadata or {},
        "records": data,
    }
    content_bytes = json.dumps(payload, indent=2).encode("utf-8")
    with open(file_path, "wb") as f:
        f.write(content_bytes)

    checksum = _compute_sha256(content_bytes)
    _record_manifest(
        file_path=file_path,
        feed_name=feed_name,
        params={"start": start_str, "end": end_str, **(metadata or {})},
        row_count=len(data),
        file_size_bytes=len(content_bytes),
        checksum=checksum,
    )
    logger.info(f"Saved {len(data)} records to {file_path}")
    return file_path


def _paginated_fetch(
    feed_name: str,
    params: Dict[str, Any],
    api_key: Optional[str] = None,
    row_count: int = DEFAULT_ROW_COUNT,
    rate_sleep: float = RATE_LIMIT_SLEEP,
    try_archived_fallback: bool = True,
    on_page: Callable[[requests.Response], None] | None = None,
) -> List[Dict[str, Any]]:
    """
    Fetch all pages for a given PJM feed with pagination, rate limiting, and retries.

    Args:
        feed_name: PJM feed endpoint (e.g. 'da_hrl_lmps', 'hrl_load_metered')
        params: Query parameters (excluding startRow and rowCount)
        api_key: Optional override for PJM subscription key
        row_count: Page size (up to 50000)
        rate_sleep: Delay in seconds between requests (default 11.0s)
        try_archived_fallback: If standard feed returns 0 records or error for older dates,
                               try {feed_name}_archived.

    Returns:
        List of record dictionaries.
    """
    key = api_key or PJM_API_KEY
    if not key or key.startswith("PASTE_"):
        raise ValueError("Set PJM_API_KEY before requesting PJM Data Miner data")
    headers = dict(PJM_HEADERS)
    if key:
        headers["Ocp-Apim-Subscription-Key"] = key

    all_records: List[Dict[str, Any]] = []
    start_row = 1
    page = 1

    current_feed = feed_name
    endpoint_url = f"{PJM_BASE_URL}/{current_feed}"

    expected_total = None
    while True:
        query_params = dict(params)
        query_params["startRow"] = start_row
        query_params["rowCount"] = row_count

        logger.info(
            f"Fetching {current_feed} (page {page}, startRow={start_row}, rowCount={row_count})..."
        )

        response = None
        requested_feed = current_feed
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # Respect rate limiting before each request
                time.sleep(rate_sleep)
                response = requests.get(
                    endpoint_url,
                    headers=headers,
                    params=query_params,
                    timeout=60,
                )

                if response.status_code == 200:
                    break
                elif response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    sleep_time = (
                        float(retry_after)
                        if retry_after
                        else (rate_sleep * (BACKOFF_FACTOR ** attempt))
                    )
                    logger.warning(
                        f"HTTP 429 Rate limit hit. Backing off for {sleep_time:.1f}s (attempt {attempt}/{MAX_RETRIES})"
                    )
                    time.sleep(sleep_time)
                elif (response.status_code in (404, 400) and try_archived_fallback
                      and page == 1 and not current_feed.endswith("_archived")):
                    logger.warning(
                        f"Feed {current_feed} returned HTTP {response.status_code}. Trying archived endpoint: {feed_name}_archived"
                    )
                    current_feed = f"{feed_name}_archived"
                    endpoint_url = f"{PJM_BASE_URL}/{current_feed}"
                    break
                elif response.status_code >= 500:
                    sleep_time = rate_sleep * (BACKOFF_FACTOR ** attempt)
                    logger.warning(
                        f"HTTP {response.status_code} server error. Retrying in {sleep_time:.1f}s (attempt {attempt}/{MAX_RETRIES})"
                    )
                    time.sleep(sleep_time)
                else:
                    response.raise_for_status()

            except requests.HTTPError:
                raise
            except requests.RequestException as e:
                logger.error(f"Network error on attempt {attempt}: {e}")
                if attempt == MAX_RETRIES:
                    raise
                time.sleep(rate_sleep * (BACKOFF_FACTOR ** attempt))

        if response is None:
            raise RuntimeError(f"No response for {current_feed}")
        if response.status_code != 200:
            if current_feed != requested_feed:
                continue
            response.raise_for_status()

        try:
            payload = response.json()
        except ValueError:
            raise ValueError(f"Invalid JSON response for {current_feed}")

        # Extract items from payload
        items: List[Dict[str, Any]] = []
        total_rows: Optional[int] = None

        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = payload.get("items", payload.get("data"))
            total_rows = payload.get("totalRows", payload.get("total", None))
        if not isinstance(items, list) or not isinstance(payload, (dict, list)):
            raise ValueError(f"Unexpected response schema for {current_feed}")

        if not items:
            # If no items returned on page 1 and not yet archived, try archived feed once
            if page == 1 and try_archived_fallback and not current_feed.endswith("_archived"):
                logger.info(
                    f"No items in {current_feed}, checking if archived feed exists: {feed_name}_archived"
                )
                current_feed = f"{feed_name}_archived"
                endpoint_url = f"{PJM_BASE_URL}/{current_feed}"
                continue
            logger.info(f"No further records returned for {current_feed}. Finished pagination.")
            break

        if on_page:
            on_page(response)
        if total_rows is not None:
            total_rows = int(total_rows)
            if expected_total is not None and expected_total != total_rows:
                raise ValueError(f"Row count changed during pagination for {current_feed}")
            expected_total = total_rows
        all_records.extend(items)
        logger.info(f"Fetched {len(items)} records (total accumulated: {len(all_records)})")

        # Stop conditions
        if expected_total is not None and len(all_records) >= expected_total:
            break
        if expected_total is None and len(items) < row_count:
            break

        start_row += len(items)
        page += 1

    if expected_total is not None and len(all_records) != expected_total:
        raise ValueError(f"Incomplete pagination for {current_feed}: {len(all_records)}/{expected_total}")
    return all_records


def _chunk_date_range(
    start_date: Union[str, date, datetime],
    end_date: Union[str, date, datetime],
    chunk_days: int = 30,
) -> List[tuple[str, str]]:
    """Split a date range into smaller windows of chunk_days."""
    d0 = datetime.strptime(_format_date_param(start_date), "%Y-%m-%d").date()
    d1 = datetime.strptime(_format_date_param(end_date), "%Y-%m-%d").date()

    chunks: List[tuple[str, str]] = []
    curr = d0
    while curr <= d1:
        chunk_end = min(curr + timedelta(days=chunk_days - 1), d1)
        chunks.append((curr.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        curr = chunk_end + timedelta(days=1)
    return chunks


def fetch_da_lmps(
    start_date: Union[str, date, datetime],
    end_date: Union[str, date, datetime],
    pnode_ids: Optional[List[Union[int, str]]] = None,
    save: bool = True,
    chunk_days: int = 7,
) -> List[Dict[str, Any]]:
    """
    Fetch Day-Ahead Hourly LMPs (feed: da_hrl_lmps).

    Fields returned typically include:
    - datetime_beginning_ept, datetime_beginning_utc
    - pnode_id, pnode_name
    - total_lmp_da, congestion_price_da, marginal_loss_price_da, system_energy_price_da

    Args:
        start_date: Start date (inclusive)
        end_date: End date (inclusive)
        pnode_ids: Optional list of pricing node IDs to filter
        save: Whether to save raw JSON to data/raw/iso/
        chunk_days: Window size in days to split queries (default 7 days for LMP volume)

    Returns:
        List of LMP dictionaries.
    """
    feed = "da_hrl_lmps"
    chunks = _chunk_date_range(start_date, end_date, chunk_days=chunk_days)
    all_data: List[Dict[str, Any]] = []

    start_str = _format_date_param(start_date)
    end_str = _format_date_param(end_date)

    for c_start, c_end in chunks:
        # PJM API format: YYYY-MM-DD 00:00toYYYY-MM-DD 23:59
        date_param = f"{c_start} 00:00to{c_end} 23:59"
        params: Dict[str, Any] = {"datetime_beginning_ept": date_param}
        if pnode_ids:
            params["pnode_id"] = ";".join(str(p) for p in pnode_ids)

        records = _paginated_fetch(feed_name=feed, params=params)
        all_data.extend(records)

    if save and all_data:
        metadata = {"pnode_ids": pnode_ids or []}
        _save_raw_response(feed, all_data, start_str, end_str, metadata=metadata)

    return all_data


def fetch_rt_lmps(
    start_date: Union[str, date, datetime],
    end_date: Union[str, date, datetime],
    pnode_ids: Optional[List[Union[int, str]]] = None,
    save: bool = True,
    chunk_days: int = 7,
) -> List[Dict[str, Any]]:
    """
    Fetch Real-Time Hourly LMPs (feed: rt_hrl_lmps).

    Fields returned typically include:
    - datetime_beginning_ept, datetime_beginning_utc
    - pnode_id, pnode_name
    - total_lmp_rt, congestion_price_rt, marginal_loss_price_rt, system_energy_price_rt

    Args:
        start_date: Start date (inclusive)
        end_date: End date (inclusive)
        pnode_ids: Optional list of pricing node IDs to filter
        save: Whether to save raw JSON to data/raw/iso/
        chunk_days: Window size in days to split queries

    Returns:
        List of LMP dictionaries.
    """
    feed = "rt_hrl_lmps"
    chunks = _chunk_date_range(start_date, end_date, chunk_days=chunk_days)
    all_data: List[Dict[str, Any]] = []

    start_str = _format_date_param(start_date)
    end_str = _format_date_param(end_date)

    for c_start, c_end in chunks:
        date_param = f"{c_start} 00:00to{c_end} 23:59"
        params: Dict[str, Any] = {"datetime_beginning_ept": date_param}
        if pnode_ids:
            params["pnode_id"] = ";".join(str(p) for p in pnode_ids)

        records = _paginated_fetch(feed_name=feed, params=params)
        all_data.extend(records)

    if save and all_data:
        metadata = {"pnode_ids": pnode_ids or []}
        _save_raw_response(feed, all_data, start_str, end_str, metadata=metadata)

    return all_data


def fetch_load(
    start_date: Union[str, date, datetime],
    end_date: Union[str, date, datetime],
    load_area: Optional[str] = None,
    save: bool = True,
    chunk_days: int = 30,
) -> List[Dict[str, Any]]:
    """
    Fetch Metered Hourly Load (feed: hrl_load_metered).

    Fields returned typically include:
    - datetime_beginning_ept, datetime_beginning_utc
    - load_area (e.g. 'RTO', 'PECO', 'PPL', 'COMED', 'BGE', 'PSEG', 'DUQ', 'ATSI')
    - mw

    Args:
        start_date: Start date
        end_date: End date
        load_area: Optional zone/area filter (e.g. 'RTO' or specific zone)
        save: Whether to save raw JSON
        chunk_days: Window size in days
    """
    feed = "hrl_load_metered"
    chunks = _chunk_date_range(start_date, end_date, chunk_days=chunk_days)
    all_data: List[Dict[str, Any]] = []

    start_str = _format_date_param(start_date)
    end_str = _format_date_param(end_date)

    for c_start, c_end in chunks:
        date_param = f"{c_start} 00:00to{c_end} 23:59"
        params: Dict[str, Any] = {"datetime_beginning_ept": date_param}
        if load_area:
            params["load_area"] = load_area

        records = _paginated_fetch(feed_name=feed, params=params)
        all_data.extend(records)

    if save and all_data:
        metadata = {"load_area": load_area or "ALL"}
        _save_raw_response(feed, all_data, start_str, end_str, metadata=metadata)

    return all_data


def fetch_gen_by_fuel(
    start_date: Union[str, date, datetime],
    end_date: Union[str, date, datetime],
    save: bool = True,
    chunk_days: int = 30,
) -> List[Dict[str, Any]]:
    """
    Fetch Hourly Generation by Fuel Type (feed: gen_by_fuel).

    Fields returned typically include:
    - datetime_beginning_ept, datetime_beginning_utc
    - fuel_type (e.g. 'Nuclear', 'Coal', 'Gas', 'Wind', 'Solar', 'Hydro', 'Oil')
    - mw

    Args:
        start_date: Start date
        end_date: End date
        save: Whether to save raw JSON
        chunk_days: Window size in days
    """
    feed = "gen_by_fuel"
    chunks = _chunk_date_range(start_date, end_date, chunk_days=chunk_days)
    all_data: List[Dict[str, Any]] = []

    start_str = _format_date_param(start_date)
    end_str = _format_date_param(end_date)

    for c_start, c_end in chunks:
        date_param = f"{c_start} 00:00to{c_end} 23:59"
        params: Dict[str, Any] = {"datetime_beginning_ept": date_param}

        records = _paginated_fetch(feed_name=feed, params=params)
        all_data.extend(records)

    if save and all_data:
        _save_raw_response(feed, all_data, start_str, end_str)

    return all_data


def fetch_gen_outages(
    start_date: Union[str, date, datetime],
    end_date: Union[str, date, datetime],
    save: bool = True,
    chunk_days: int = 30,
) -> List[Dict[str, Any]]:
    """
    Fetch Aggregate Generation Outages by Type (feed: gen_outages_by_type).

    Fields returned typically include:
    - forecast_execution_date_ept
    - planned_mw, maintenance_mw, forced_mw, total_outages_mw

    Args:
        start_date: Start date
        end_date: End date
        save: Whether to save raw JSON
        chunk_days: Window size in days
    """
    feed = "gen_outages_by_type"
    chunks = _chunk_date_range(start_date, end_date, chunk_days=chunk_days)
    all_data: List[Dict[str, Any]] = []

    start_str = _format_date_param(start_date)
    end_str = _format_date_param(end_date)

    for c_start, c_end in chunks:
        date_param = f"{c_start} 00:00to{c_end} 23:59"
        params: Dict[str, Any] = {"datetime_beginning_ept": date_param}

        records = _paginated_fetch(feed_name=feed, params=params)
        all_data.extend(records)

    if save and all_data:
        _save_raw_response(feed, all_data, start_str, end_str)

    return all_data


if __name__ == "__main__":
    print("=" * 60)
    print("Testing PJM Data Miner 2 Client")
    print("=" * 60)
    if not PJM_API_KEY:
        print("⚠️  PJM_API_KEY is not configured in configs/.env.")
        print("   This test will verify structure and dry-run without network calls.")
    else:
        print(f"✅ PJM_API_KEY detected: {PJM_API_KEY[:8]}...")
        # Quick 1-day query test
        test_start = "2023-01-01"
        test_end = "2023-01-01"
        print(f"Fetching sample fuel mix for {test_start}...")
        try:
            records = fetch_gen_by_fuel(test_start, test_end, save=False)
            print(f"Retrieved {len(records)} fuel mix records.")
            if records:
                print(f"Sample record: {records[0]}")
        except Exception as e:
            print(f"Fetch encountered error (may need valid key): {e}")
