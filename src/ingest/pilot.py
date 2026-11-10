"""Pin pilot source bytes and collect official inputs for offline replay."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import requests

from src.ingest import nrc, pjm
from src.ingest.manifest import ManifestManager, compute_file_hash


KINDS = {
    "nrc_status", "nrc_scrams", "nrc_notice", "nrc_notice_index", "eia860", "da", "rt", "load", "settled",
    "weather", "crosswalk", "event_review", "calendar",
}
SINGLETONS = {"nrc_scrams", "crosswalk", "event_review", "calendar"}


def canonical_json(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_bytes(canonical_json(value))
    temporary.replace(path)


def load_manifest(root: Path, *, verify: bool = True) -> list[dict]:
    path = root / "inputs.json"
    if not path.exists():
        return []
    manifest = json.loads(path.read_text())
    if manifest["schema_version"] != 1:
        raise ValueError("Unsupported input manifest version")
    sources = manifest["sources"]
    if verify:
        for source in sources:
            path = root / source["path"]
            if not path.is_file() or compute_file_hash(path) != source["sha256"]:
                raise ValueError(f"Missing or changed pinned snapshot: {path}")
    return sources


def pin(root: Path, kind: str, data: bytes, suffix: str, url: str, retrieved_at: str,
        *, replace: bool = False, **metadata) -> dict:
    if kind not in KINDS or not data or not re.fullmatch(r"\.[a-z0-9]+", suffix):
        raise ValueError(f"Invalid or empty snapshot for {kind}")
    retrieved = datetime.fromisoformat(retrieved_at)
    if retrieved.utcoffset() is None or not url.strip():
        raise ValueError("Supply a timezone-aware retrieval time and a source URL or evidence reference")
    digest = hashlib.sha256(data).hexdigest()
    source_id = f"{kind}:{digest}"
    sources = load_manifest(root, verify=False)
    if replace:
        if kind not in SINGLETONS:
            raise ValueError("Only single-file inputs can be replaced in a manifest")
        sources = [source for source in sources if source["kind"] != kind or source["id"] == source_id]
    for source in sources:
        if source["id"] == source_id:
            if compute_file_hash(root / source["path"]) != digest:
                raise ValueError(f"Previously pinned bytes changed: {source['path']}")
            if source["metadata"] != metadata:
                raise ValueError(f"Previously pinned metadata differs for {source_id}")
            if replace:
                write_json(root / "inputs.json", {"schema_version": 1, "sources": sources})
            return source
    relative = Path("snapshots") / kind / f"{digest}{suffix}"
    snapshot = root / relative
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    if not snapshot.exists():
        temporary = snapshot.with_name(f".{snapshot.name}.{uuid4().hex}.tmp")
        temporary.write_bytes(data)
        temporary.replace(snapshot)
    if compute_file_hash(snapshot) != digest:
        raise ValueError(f"Snapshot path contains unexpected bytes: {snapshot}")
    entry = {
        "id": source_id, "kind": kind, "path": relative.as_posix(), "sha256": digest,
        "source": url, "retrieved_at": retrieved.astimezone(timezone.utc).isoformat(),
        "published_at": None, "available_at": None, "metadata": metadata,
    }
    sources.append(entry)
    write_json(root / "inputs.json", {"schema_version": 1, "sources": sorted(sources, key=lambda s: s["id"])})
    return entry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _download(session: requests.Session, url: str) -> requests.Response:
    response = session.get(url, timeout=90)
    response.raise_for_status()
    return response


def notice_range(config: dict, start: date, end: date) -> tuple[date, date] | None:
    through = date.fromisoformat(config["sources"]["nrc_scrams_through"])
    if end - timedelta(days=1) <= through:
        return None
    # Notices may appear after the physical event. Allow one week beyond the
    # delivery window for the initial posting and near-boundary candidates.
    return max(start, through), end + timedelta(days=7)


def notice_links(html: bytes, url: str, start: date, end: date) -> dict[date, str]:
    links = {}
    for link in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        target = urljoin(url, link["href"])
        parsed = urlsplit(target)
        match = re.search(r"/(\d{8})en(?:\.html)?$", parsed.path, re.I)
        if parsed.hostname == "www.nrc.gov" and match:
            day = datetime.strptime(match[1], "%Y%m%d").date()
            if start <= day < end:
                links[day] = target
    return links


def missing_notice_pages(root: Path, by_kind: dict, config: dict, start: date, end: date) -> list[str]:
    window = notice_range(config, start, end)
    if window is None:
        return []
    lower, upper = window
    indexes = {entry["metadata"].get("year"): entry for entry in by_kind.get("nrc_notice_index", [])}
    captured = {entry["metadata"].get("report_date") for entry in by_kind.get("nrc_notice", [])}
    missing = []
    for year in range(lower.year, (upper - timedelta(days=1)).year + 1):
        if year not in indexes:
            missing.append(f"index:{year}")
            continue
        entry = indexes[year]
        raw = (root / entry["path"]).read_bytes()
        links = notice_links(raw, entry["source"], lower, upper)
        if not notice_links(raw, entry["source"], date(year, 1, 1), date(year + 1, 1, 1)):
            missing.append(f"unreadable_index:{year}")
        missing.extend(day.isoformat() for day in sorted(links) if day.isoformat() not in captured)
    return missing


def _fetch_notice_archive(root: Path, config: dict, start: date, end: date, session: requests.Session) -> None:
    import time as pacing

    window = notice_range(config, start, end)
    if window is None:
        return
    lower, upper = window
    sources = load_manifest(root)
    existing = {entry["metadata"].get("report_date") for entry in sources if entry["kind"] == "nrc_notice"}
    indexes = {entry["metadata"].get("year"): entry for entry in sources if entry["kind"] == "nrc_notice_index"}
    for year in range(lower.year, (upper - timedelta(days=1)).year + 1):
        if year in indexes:
            entry = indexes[year]
        else:
            url = config["sources"]["nrc_notice_index"].format(year=year)
            response = session.get(url, timeout=60)
            if response.status_code in (403, 404):
                response = _download(session, f"https://www.nrc.gov/reading-rm/doc-collections/event-status/event/{year}/index")
            response.raise_for_status()
            if not notice_links(response.content, response.url, date(year, 1, 1), date(year + 1, 1, 1)):
                raise ValueError(f"No notice links found in NRC archive index for {year}")
            entry = pin(root, "nrc_notice_index", response.content, ".html", response.url, _now(), year=year)
        links = notice_links((root / entry["path"]).read_bytes(), entry["source"], lower, upper)
        for day, url in sorted(links.items()):
            if day.isoformat() in existing:
                continue
            pacing.sleep(.4)
            response = _download(session, url)
            soup = BeautifulSoup(response.content, "html.parser")
            content = soup.get_text(" ", strip=True)
            report = soup.select_one(".nrc-event-report-day")
            empty_report = report is not None and "No events found" in report.get_text(" ", strip=True)
            if not empty_report and not re.search(r"Event\s+Number\s*:", content, re.I):
                raise ValueError(f"Expected an NRC event report at {response.url}")
            pin(root, "nrc_notice", response.content, ".html", response.url, _now(), report_date=day.isoformat())


def market_chunks(start: date, end: date, delivery_timezone: str, days: int, today: date):
    """Query UTC ranges without crossing archive or calendar-year boundaries."""
    local = ZoneInfo(delivery_timezone)
    lower = datetime.combine(start, time.min, local).astimezone(timezone.utc)
    upper = datetime.combine(end, time.min, local).astimezone(timezone.utc)
    cutoff = today - timedelta(days=731)
    dates = [date(year, 1, 1) for year in range(lower.year + 1, upper.year + 1)]
    cutoff_dates = [cutoff + timedelta(days=offset) for offset in (-1, 0, 1, 2)]
    dates += cutoff_dates
    breaks = [datetime.combine(day, time.min, timezone.utc) for day in dates]
    # PJM's moving archive boundary falls at midnight Eastern, inside a UTC day.
    breaks += [datetime.combine(day, time.min, local).astimezone(timezone.utc) for day in cutoff_dates]
    boundaries = [lower, *sorted({d for d in breaks if lower < d < upper}), upper]
    for first, last in zip(boundaries, boundaries[1:]):
        while first < last:
            stop = min(first + timedelta(days=days), last)
            yield first, stop - timedelta(hours=1)
            first = stop


def _fetch_pjm(root: Path, config: dict, start: date, end: date) -> None:
    if not pjm.PJM_API_KEY or pjm.PJM_API_KEY.startswith("PASTE_"):
        raise ValueError("PJM_API_KEY is required for live collection. Alternatively pin Data Miner exports.")
    # Archived prices support the node type filter. The panel selects the node ID.
    study = config["study"]
    today = datetime.now(timezone.utc).date()
    for kind in ("da", "rt", "load"):
        feed = config["sources"][kind]
        chunks = market_chunks(start, end, study["timezone"], 7 if kind != "load" else 28, today)
        for first, last in chunks:
            last_second = last + timedelta(minutes=59, seconds=59)
            query = {
                "datetime_beginning_utc": f"{first:%Y-%m-%d %H:%M:%S} to {last_second:%Y-%m-%d %H:%M:%S}",
                "download": "true",
                "format": "json",
            }
            query.update({"load_area": study["load_area"]} if kind == "load" else {"type": study.get("node_type", "ZONE")})
            pages = []
            rows = pjm._paginated_fetch(
                feed, query, try_archived_fallback=False,
                on_page=lambda page: pages.append((page.content, page.url, _now())),
            )
            if not rows:
                raise ValueError(f"No rows for {feed} from {first} through {last}")
            for body, url, retrieved_at in pages:
                pin(root, kind, body, ".json", url, retrieved_at, feed=feed)


def fetch(root: Path, config: dict, start: date, end: date, group: str) -> None:
    """Fetch one source group. Run one writer per pilot directory."""
    if group == "pjm":
        _fetch_pjm(root, config, start, end)
        return
    years = range(start.year, (end - timedelta(days=1)).year + 1)
    with requests.Session() as session:
        if group == "nrc":
            for year in years:
                directory = root / "nrc-downloads"
                raw = nrc.fetch_year(year, session=session, raw_dir=directory, manifest_dir=directory)
                entry = next(e for e in ManifestManager(directory).load_entries() if e.file_path == str(raw))
                pin(root, "nrc_status", raw.read_bytes(), ".txt", entry.source_url_or_query, entry.retrieved_at, year=year)
            response = _download(session, config["sources"]["nrc_scrams"])
            if not response.content.startswith(b"PK"):
                raise ValueError("Expected NRC scram workbook bytes")
            pin(root, "nrc_scrams", response.content, ".xlsx", response.url, _now())
        elif group == "notices":
            _fetch_notice_archive(root, config, start, end, session)
        elif group == "weather":
            utc_end = datetime.combine(end, time.min, ZoneInfo(config["study"]["timezone"])).astimezone(timezone.utc)
            weather = config["weather"]
            isd_last_year = weather.get("isd_last_year", (utc_end - timedelta(hours=1)).year)
            for year in range(start.year, (utc_end - timedelta(hours=1)).year + 1):
                for station in weather["stations"]:
                    if year <= isd_last_year:
                        response = _download(session, weather["url"].format(year=year, station=station))
                        if not response.content.lstrip().startswith(b'"STATION"'):
                            raise ValueError(f"Expected Global Hourly station CSV at {response.url}")
                        pin(root, "weather", response.content, ".csv", response.url, _now(), station=station, year=year)
                    if year >= isd_last_year and "ghcnh_url" in weather:
                        station_id = weather["ghcnh_stations"][station]
                        response = _download(session, weather["ghcnh_url"].format(year=year, station=station_id))
                        if not response.content.lstrip().startswith(b"STATION|"):
                            raise ValueError(f"Expected GHCNh station PSV at {response.url}")
                        pin(root, "weather", response.content, ".psv", response.url, _now(),
                            station=station, ghcnh_station=station_id, year=year)
        elif group == "eia":
            response = _download(session, config["sources"]["eia860_index"])
            links = BeautifulSoup(response.content, "html.parser").find_all("a", href=True)
            for year in years:
                urls = {urljoin(response.url, link["href"]) for link in links
                        if link["href"].lower().endswith(f"eia860{year}.zip")}
                if len(urls) != 1:
                    raise ValueError(f"Could not uniquely locate the official EIA-860 ZIP for {year}")
                archive = _download(session, urls.pop())
                if not archive.content.startswith(b"PK"):
                    raise ValueError(f"Expected EIA-860 ZIP bytes at {archive.url}")
                pin(root, "eia860", archive.content, ".zip", archive.url, _now(), year=year)
        else:
            raise ValueError(f"Unknown source group: {group}")
