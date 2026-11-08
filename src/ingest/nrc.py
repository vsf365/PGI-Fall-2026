"""Download NRC reactor status archive files without losing source vintages.

The NRC archive provides a daily status series and annual raw text files. The
archive does not provide a reliable per-record publication timestamp, so this
collector records retrieval time and leaves publication time explicitly
unknown. These snapshots support the perfect-information benchmark; they must
not be treated as point-in-time public information until publication latency
has been established separately.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from configs.config import MANIFEST_DIR, RAW_NRC

ARCHIVE_URL = "https://www.nrc.gov/documents-reports/document-collections/events-reports-associated-with/power-reactor-status-reports/{year}"
PARSER_VERSION = "nrc-raw-1"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _discover_annual_file(year: int, session: requests.Session) -> tuple[str, bytes]:
    """Find and download the annual raw status text file from the NRC index."""
    index_url = ARCHIVE_URL.format(year=year)
    response = session.get(index_url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")

    for link in soup.find_all("a", href=True):
        label = " ".join(link.get_text(" ", strip=True).lower().split())
        href = link["href"]
        if "power status" in label and "raw data" in label and "text" in label:
            file_url = urljoin(index_url, href)
            raw_response = session.get(file_url, timeout=120)
            raw_response.raise_for_status()
            return file_url, raw_response.content
    raise RuntimeError(f"Could not find annual raw Power Status text file at {index_url}")


def fetch_year(year: int, *, session: requests.Session | None = None) -> Path:
    """Save an immutable annual snapshot and append its provenance manifest."""
    http = session or requests.Session()
    url, content = _discover_annual_file(year, http)
    retrieved_at = _utc_now()
    digest = hashlib.sha256(content).hexdigest()

    target_dir = RAW_NRC / "annual"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"nrc_power_status_{year}_{retrieved_at:%Y%m%dT%H%M%S}.{digest[:12]}.txt"
    target.write_bytes(content)

    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "run_id": f"nrc-{year}-{retrieved_at:%Y%m%dT%H%M%S}-{digest[:12]}",
        "source_name": "nrc_power_reactor_status",
        "source_url_or_query": url,
        "retrieved_at": retrieved_at.isoformat(),
        "published_at": None,
        "publication_time_status": "not provided by archive; must be researched",
        "source_file_hash": digest,
        "parser_version": PARSER_VERSION,
        "row_count": None,
        "source_year": year,
        "file_path": str(target),
    }
    with (MANIFEST_DIR / "ingest_manifest.jsonl").open("a", encoding="utf-8") as manifest:
        manifest.write(json.dumps(entry, sort_keys=True) + "\n")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("year", type=int, nargs="+", help="NRC archive year(s), e.g. 2024 2025")
    args = parser.parse_args()
    with requests.Session() as session:
        session.headers.update({"User-Agent": "nuclear-availability-research/0.1 (archival data collection)"})
        for year in args.year:
            print(fetch_year(year, session=session))


if __name__ == "__main__":
    main()
