"""Normalize NRC Power Status pipe-delimited snapshots to a stable schema."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from configs.config import CURATED_DIR

PARSER_VERSION = "nrc-status-1"
REQUIRED_COLUMNS = {"reportdt", "unit", "power"}


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower().lstrip("\ufeff"))


def _parse_report_date(value: str) -> datetime:
    value = value.strip()
    for pattern in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized NRC report date: {value!r}")


def normalize_file(path: str | Path) -> list[dict[str, Any]]:
    """Read one immutable NRC raw snapshot and return canonical observations.

    NRC `ReportDt` is an observation/report date, not a proven publication
    timestamp. `available_at` is intentionally null pending publication-lag
    research. Power is retained as reported percentage; it is not converted to
    MW without a point-in-time capacity crosswalk.
    """
    source = Path(path)
    text = source.read_text(encoding="utf-8-sig")
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = "|" if first_line.count("|") >= first_line.count(",") else ","
    reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError(f"No header row in {source}")
    columns = {_key(name): name for name in reader.fieldnames if name}
    missing = REQUIRED_COLUMNS - columns.keys()
    if missing:
        raise ValueError(f"Missing NRC columns {sorted(missing)} in {source}")

    normalized = []
    for line_number, row in enumerate(reader, start=2):
        unit = (row.get(columns["unit"]) or "").strip()
        if not unit:
            continue
        power_raw = (row.get(columns["power"]) or "").strip()
        try:
            power = float(power_raw) if power_raw else None
        except ValueError as exc:
            raise ValueError(f"Invalid Power value on line {line_number}: {power_raw!r}") from exc
        if power is not None and not 0 <= power <= 120:
            raise ValueError(f"Power outside expected 0-120% range on line {line_number}: {power}")

        observed_at = _parse_report_date(row[columns["reportdt"]])
        normalized.append(
            {
                "unit_name": unit,
                "observed_at": observed_at.isoformat(),
                "published_at": None,
                "available_at": None,
                "reported_power_pct": power,
                "source_file": str(source),
                "source_line": line_number,
                "parser_version": PARSER_VERSION,
                "raw_record": {str(k).strip(): (v or "").strip() for k, v in row.items() if k},
            }
        )
    return normalized


def write_jsonl(records: list[dict[str, Any]], output: str | Path) -> Path:
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw_file", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or CURATED_DIR / f"{args.raw_file.stem}.jsonl"
    records = normalize_file(args.raw_file)
    write_jsonl(records, output)
    print(f"Wrote {len(records)} rows to {output}")


if __name__ == "__main__":
    main()
