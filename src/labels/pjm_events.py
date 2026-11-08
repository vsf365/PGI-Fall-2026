"""Build a review queue and an evidence-backed reactor trip ledger."""

from __future__ import annotations

from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
import re
from zipfile import ZipFile

from bs4 import BeautifulSoup
import pandas as pd

from src.normalize.nrc import normalize_file


REVIEW_FIELDS = (
    "event_id", "classification", "onset_at", "initial_power_pct", "generation_loss_evidence",
    "notice_source_id", "reviewer", "reason", "known_in_advance",
)
CROSSWALK_FIELDS = (
    "docket", "unit_name", "plant_code", "generator_id", "effective_from",
    "effective_to", "evidence",
)


def workbook_rows(book, sheet: str, header_row: int):
    rows = book[sheet].iter_rows(min_row=header_row, values_only=True)
    headers = [str(value).strip() if value is not None else "" for value in next(rows)]
    return [dict(zip(headers, row)) for row in rows if any(value is not None for value in row)]


def candidates(path: Path) -> pd.DataFrame:
    from openpyxl import load_workbook

    with path.open("rb") as handle:
        book = load_workbook(handle, read_only=True, data_only=True)
        rows = workbook_rows(book, "Public Site Data", 1)
        book.close()
    records = []
    for row in rows:
        if row["Scram #"] is None or row["Date"] is None:
            continue
        records.append({
            "event_id": f"{int(row['Year'])}:{int(row['Scram #'])}",
            "event_date": pd.Timestamp(row["Date"]).date(),
            "docket": str(row["Docket"]).zfill(8),
            "reactor": row["Plant Name"],
            "notification_number": str(row["Event_Notification_Number"] or "").removesuffix(".0"),
            "notice_url": row["Link"],
            "retracted": str(row["NRC_retract_flag"]).strip() in {"1", "1.0", "True"},
        })
    frame = pd.DataFrame.from_records(records)
    if frame.empty or frame["event_id"].duplicated().any():
        raise ValueError("Scram workbook contains no candidates or duplicate candidate IDs")
    return frame


def eia_generators(path: Path, year: int) -> pd.DataFrame:
    """Return nuclear generator identifiers and annual net summer capacity."""
    from openpyxl import load_workbook

    with ZipFile(path) as archive:
        plant_file = next(name for name in archive.namelist() if Path(name).name.startswith("2___Plant"))
        generator_file = next(name for name in archive.namelist() if Path(name).name.startswith("3_1_Generator"))
        book = load_workbook(BytesIO(archive.read(plant_file)), read_only=True, data_only=True)
        plants = {str(row["Plant Code"]): row["Balancing Authority Code"]
                  for row in workbook_rows(book, book.sheetnames[0], 2)}
        book.close()
        book = load_workbook(BytesIO(archive.read(generator_file)), read_only=True, data_only=True)
        records = []
        for sheet in ("Operable", "Retired and Canceled"):
            if sheet not in book.sheetnames:
                continue
            for row in workbook_rows(book, sheet, 2):
                if row.get("Energy Source 1") != "NUC":
                    continue
                plant_code = str(row["Plant Code"])
                records.append({
                    "year": year, "plant_code": plant_code, "generator_id": str(row["Generator ID"]),
                    "plant_name": row["Plant Name"], "balancing_authority": plants.get(plant_code),
                    "capacity_mw": pd.to_numeric(row.get("Summer Capacity (MW)"), errors="coerce"),
                })
        book.close()
    return pd.DataFrame.from_records(records)


def reviewed_csv(path: Path, required: tuple[str, ...], unique: str | None = None) -> pd.DataFrame:
    table = pd.read_csv(path, dtype=str, keep_default_na=False).apply(lambda col: col.str.strip())
    missing = set(required) - set(table.columns)
    if missing:
        raise ValueError(f"{path.name} is missing columns: {', '.join(sorted(missing))}")
    if unique and (table[unique].eq("").any() or table[unique].duplicated().any()):
        raise ValueError(f"{path.name} has blank or duplicate {unique} values")
    return table


def utc_timestamp(value: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        raise ValueError(f"An exact timestamp needs a UTC offset: {value!r}")
    return timestamp.tz_convert("UTC")


def notification_numbers(raw: bytes) -> set[str]:
    page = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    return set(re.findall(r"Event\s+Number\s*:\s*(\d+)", page, re.I))


def _status_index(root: Path, sources: list[dict]) -> pd.DataFrame:
    rows = []
    for source in sources:
        for row in normalize_file(root / source["path"]):
            rows.append((row["unit_name"].casefold(), date.fromisoformat(row["report_date"]),
                         row["reported_power_pct"], source["id"]))
    return pd.DataFrame(rows, columns=["unit", "date", "power", "source_id"])


def _classification(candidate: dict, review: dict, mapping: pd.DataFrame, generators: pd.DataFrame,
                    statuses: pd.DataFrame, notices: dict, min_power: int) -> dict:
    row = {**candidate, **{field: review.get(field, "") for field in REVIEW_FIELDS if field != "event_id"}}
    row.update(eligible=False, exclusion="", scope="unknown", capacity_mw=None, estimated_lost_mw=None,
               onset_utc=pd.NaT, status_corroborated=False, status_source_ids="")
    when = candidate["event_date"]
    units = mapping[(mapping.docket == candidate["docket"]) & (mapping.effective_from <= when)
                    & (mapping.effective_to > when)]
    if len(units) > 1:
        raise ValueError(f"Overlapping crosswalk entries for {candidate['docket']} on {when}")
    if len(units) == 1:
        unit = units.iloc[0]
        matched = generators[(generators.year == when.year) & (generators.plant_code == unit.plant_code)
                             & (generators.generator_id == unit.generator_id)]
        if len(matched) == 1:
            generator = matched.iloc[0]
            ba = generator.balancing_authority
            row["scope"] = "pjm" if ba == "PJM" else "outside_pjm" if pd.notna(ba) and ba else "unknown"
            row["eia_source_id"] = generator.source_id
            if pd.notna(generator.capacity_mw) and generator.capacity_mw > 0:
                row["capacity_mw"] = float(generator.capacity_mw)
            nearby = statuses[(statuses.unit == unit.unit_name.casefold())
                              & (statuses.date >= when - timedelta(days=2))
                              & (statuses.date <= when + timedelta(days=2))]
            powers = nearby.groupby("date").power.agg(["min", "max"])
            powers = powers[powers["min"].eq(powers["max"])]
            row["status_corroborated"] = bool(
                (powers.loc[powers.index < when, "max"] >= min_power).any()
                and (powers.loc[powers.index >= when, "max"] < min_power).any()
            )
            row["status_source_ids"] = ";".join(sorted(set(nearby.source_id)))
    if candidate["retracted"]:
        row["exclusion"] = "nrc_retracted"
    elif row["scope"] == "outside_pjm":
        row["exclusion"] = "outside_pjm"
    elif not review or not row["reviewer"] or not row["reason"]:
        row["exclusion"] = "review_required"
    elif row["classification"] not in {"unplanned", "planned", "ambiguous"}:
        raise ValueError(f"Invalid review classification for {candidate['event_id']}")
    elif row["classification"] != "unplanned":
        row["exclusion"] = row["classification"]
    elif row["known_in_advance"] != "no":
        row["exclusion"] = "planned" if row["known_in_advance"] == "yes" else "advance_notice_unknown"
    elif not row["onset_at"] or len(row["onset_at"]) <= 10:
        row["exclusion"] = "onset_date_only"
    else:
        row["onset_utc"] = utc_timestamp(row["onset_at"])
        if abs((pd.Timestamp(row["onset_at"]).date() - when).days) > 1:
            raise ValueError(f"Reviewed onset is inconsistent with candidate date for {candidate['event_id']}")
        power = pd.to_numeric(row["initial_power_pct"], errors="coerce")
        if not pd.notna(power) or not min_power <= power <= 100:
            row["exclusion"] = "initial_power_below_threshold_or_unknown"
        elif not row["generation_loss_evidence"] or candidate["notification_number"] not in notices.get(row["notice_source_id"], set()):
            row["exclusion"] = "generation_loss_evidence_missing"
        elif row["scope"] != "pjm":
            row["exclusion"] = "pjm_mapping_unverified"
        else:
            row["eligible"] = True
            if row["capacity_mw"] is not None:
                row["estimated_lost_mw"] = row["capacity_mw"] * float(power) / 100
    return row


def build_ledger(root: Path, by_kind: dict, config: dict, start: date, end: date) -> pd.DataFrame:
    def single(kind):
        return root / by_kind[kind][0]["path"]

    queue_columns = ("event_id", "event_date", "docket", "reactor", "notification_number", "notice_url", "retracted")
    queue = (candidates(single("nrc_scrams")) if "nrc_scrams" in by_kind
             else pd.DataFrame(columns=queue_columns))
    reviews = (reviewed_csv(single("event_review"), REVIEW_FIELDS, "event_id") if "event_review" in by_kind
               else pd.DataFrame(columns=REVIEW_FIELDS)).set_index("event_id")
    # Planned work may be absent from the scram list. Accept separately reviewed
    # exclusions so those days cannot silently become comparison hours.
    extra = []
    for event_id, review in reviews.loc[~reviews.index.isin(queue.event_id)].iterrows():
        if (not event_id.startswith("local:") or review.classification not in {"planned", "ambiguous"}
                or not all(review.get(field) for field in ("event_date", "docket", "reactor", "notice_url"))):
            raise ValueError(f"Review {event_id} is not in the scram workbook or a documented local exclusion")
        extra.append({
            "event_id": event_id, "event_date": date.fromisoformat(review.event_date),
            "docket": review.docket.zfill(8), "reactor": review.reactor,
            "notification_number": "", "notice_url": review.notice_url, "retracted": False,
        })
    if extra:
        queue = pd.concat([queue, pd.DataFrame(extra)], ignore_index=True)
    # Include adjacent days so boundary events cannot become controls.
    queue = queue[queue.event_date.between(start - timedelta(days=1), end)]
    mapping = (reviewed_csv(single("crosswalk"), CROSSWALK_FIELDS) if "crosswalk" in by_kind
               else pd.DataFrame(columns=CROSSWALK_FIELDS))
    mapping["docket"] = mapping.docket.str.zfill(8)
    for col in ("effective_from", "effective_to"):
        mapping[col] = mapping[col].map(date.fromisoformat)
    if mapping.evidence.eq("").any() or (mapping.effective_from >= mapping.effective_to).any():
        raise ValueError("Crosswalk mappings need evidence and nonempty effective ranges")
    generator_batches = [
        eia_generators(root / source["path"], source["metadata"]["year"]).assign(source_id=source["id"])
        for source in by_kind.get("eia860", [])
    ]
    generators = (pd.concat(generator_batches, ignore_index=True) if generator_batches else
                  pd.DataFrame(columns=["year", "plant_code", "generator_id"]))
    statuses = _status_index(root, by_kind.get("nrc_status", []))
    notices = {}
    for source in by_kind.get("nrc_notice", []):
        raw = (root / source["path"]).read_bytes()
        notices[source["id"]] = notification_numbers(raw)
    return pd.DataFrame.from_records([
        _classification(row, reviews.loc[row["event_id"]].to_dict() if row["event_id"] in reviews.index else {},
                        mapping, generators, statuses, notices, config["study"]["min_initial_power_pct"])
        for row in queue.to_dict("records")
    ], columns=[*queue.columns, *[c for c in REVIEW_FIELDS if c != "event_id"], "eligible", "exclusion",
                "scope", "capacity_mw", "estimated_lost_mw", "onset_utc", "status_corroborated",
                "status_source_ids", "eia_source_id"])
