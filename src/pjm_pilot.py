"""Collect, replay, and evaluate the PJM nuclear trip pilot."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import tomllib

import numpy as np
import pandas as pd
import requests

from src.backtests.pjm_oracle import label_windows, oracle_benchmark, price_response
from src.ingest.manifest import compute_file_hash
from src.ingest.pilot import KINDS, SINGLETONS, fetch, load_manifest, missing_notice_pages, pin, write_json
from src.labels.pjm_events import (
    CROSSWALK_FIELDS, REVIEW_FIELDS, build_ledger, eia_generators, notification_numbers, review_queue,
)
from src.normalize.pjm_panel import CALENDAR_FIELDS, hourly_panel


REPO = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO / "configs" / "pjm_pilot.toml"
PERIODS = ("smoke", "development", "validation", "holdout")
ARTIFACTS = ("panel.csv", "events.csv", "coverage.json", "input_manifest.json")
REQUIRED = {"nrc_status", "nrc_scrams", "eia860", "da", "rt", "load", "weather",
            "crosswalk", "event_review", "calendar"}


def _code_hash() -> str:
    # Include the runtime and parsers so a holdout score cannot silently use a
    # different implementation or numerical library from the frozen pilot.
    digest = hashlib.sha256(f"{sys.version}:{pd.__version__}:{np.__version__}".encode())
    for path in sorted((REPO / "src").rglob("*.py")):
        digest.update(path.relative_to(REPO).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _sources(root: Path) -> dict:
    grouped = defaultdict(list)
    for entry in load_manifest(root):
        grouped[entry["kind"]].append(entry)
    for kind in SINGLETONS:
        if len(grouped.get(kind, [])) > 1:
            raise ValueError(f"Pin a single active {kind} input. Use --replace to select the reviewed version.")
    years = [entry["metadata"].get("year") for entry in grouped.get("eia860", [])]
    if None in years or len(years) != len(set(years)):
        raise ValueError("Pin one EIA-860 ZIP per year and specify --year")
    return dict(grouped)


def _dates(config: dict, period: str) -> tuple[date, date]:
    start, end = (date.fromisoformat(value) for value in config["periods"][period])
    if start >= end:
        raise ValueError(f"Invalid bounds for {period}")
    return start, end


def _coverage(root: Path, panel: pd.DataFrame, events: pd.DataFrame, sources: dict, config: dict,
              start: date, end: date) -> dict:
    columns = ("da_archive_lmp", "da_lmp", "rt_lmp", "spread", "load_mw", "temperature_c", "dewpoint_c")
    missing_hours = {name: int(panel[name].isna().sum()) for name in columns}
    eligible = int(panel.study_eligible.sum())
    missing_sources = sorted(REQUIRED - sources.keys())
    missing_notices = missing_notice_pages(root, sources, config, start, end)
    needed_years = set(range(start.year, (end - timedelta(days=1)).year + 1))
    years = {name: {entry["metadata"].get("year") for entry in sources.get(name, [])}
             for name in ("nrc_status", "eia860")}
    missing_years = {name: sorted(needed_years - values) for name, values in years.items()}
    study = config["study"]
    return {
        "start": start.isoformat(), "end_exclusive": end.isoformat(), "hours": len(panel),
        "missing_hours": missing_hours,
        "conflicting_hours": {name: int(panel[f"{name}_conflict"].sum()) for name in ("da", "rt", "load")},
        "missing_sources": missing_sources, "missing_years": missing_years,
        "missing_notice_pages": missing_notices,
        "eligible_hours": eligible, "reviewed_hours": int(panel.event_review_complete.sum()),
        "candidate_events": len(events), "qualified_events": int(events.eligible.sum()),
        "usable_events": int(events.post_start_utc.notna().sum()),
        "unreviewed_events": int(events.exclusion.eq("review_required").sum()),
        "exclusions": dict(Counter(events.loc[events.analysis_exclusion.ne(""), "analysis_exclusion"])),
        "study_ready": bool(
            not missing_sources and not any(missing_years.values()) and not missing_notices
            and min(1 - missing_hours[name] / len(panel) for name in ("da_lmp", "rt_lmp")) >= study["min_market_coverage"]
            and min(1 - missing_hours[name] / len(panel) for name in ("load_mw", "temperature_c", "dewpoint_c"))
            >= study["min_control_coverage"]
            and eligible / len(panel) >= study["min_control_coverage"]
        ),
    }


def _identity(config_path: Path) -> dict:
    return {"config_sha256": compute_file_hash(config_path), "code_sha256": _code_hash()}


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _verify_build(path: Path, period: str, config_path: Path, *, require_ready: bool = True) -> dict:
    build = _read_json(path / "build.json")
    if build["period"] != period or build["identity"] != _identity(config_path):
        raise ValueError(f"{path} has the wrong period, configuration, or implementation")
    for name in ARTIFACTS:
        if compute_file_hash(path / name) != build["artifacts"][name]:
            raise ValueError(f"Build artifact changed: {path / name}")
    if require_ready and not _read_json(path / "coverage.json")["study_ready"]:
        raise ValueError(f"Review the input and coverage gaps in {path / 'coverage.json'}")
    return build


def build(root: Path, output: Path, config_path: Path, config: dict, period: str,
          frozen: Path | None = None) -> dict:
    if output.exists():
        raise ValueError(f"Build output already exists: {output}. Use a fresh directory for replay.")
    lock_sha = None
    if period == "holdout":
        if frozen is None:
            raise ValueError("Freeze the replayed pilot before building the holdout")
        lock = _read_json(frozen)
        if (lock["identity"] != _identity(config_path)
                or lock["holdout_manifest_sha256"] != compute_file_hash(root / "inputs.json")):
            raise ValueError("Holdout input manifest or frozen implementation changed")
        lock_sha = compute_file_hash(frozen)
    sources = _sources(root)
    start, end = _dates(config, period)
    panel = hourly_panel(root, sources, config, start, end)
    ledger = build_ledger(root, sources, config, start, end)
    panel, ledger = label_windows(panel, ledger, config)
    coverage = _coverage(root, panel, ledger, sources, config, start, end)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent, prefix=".pilot-") as temporary:
        folder = Path(temporary)
        panel.to_csv(folder / "panel.csv")
        ledger.to_csv(folder / "events.csv", index=False)
        manifest = root / "inputs.json"
        if manifest.exists():
            (folder / "input_manifest.json").write_bytes(manifest.read_bytes())
        else:
            write_json(folder / "input_manifest.json", {"schema_version": 1, "sources": []})
        write_json(folder / "coverage.json", coverage)
        write_json(folder / "build.json", {
            "period": period, "identity": _identity(config_path), "freeze_sha256": lock_sha,
            "artifacts": {name: compute_file_hash(folder / name) for name in ARTIFACTS},
        })
        folder.rename(output)
    return coverage


def freeze(smoke: Path, replay: Path, development: Path, validation: Path,
           holdout_root: Path, output: Path, config_path: Path) -> None:
    if output.exists():
        raise ValueError(f"Freeze already exists: {output}")
    if smoke.resolve() == replay.resolve():
        raise ValueError("Supply two independent smoke builds")
    first = _verify_build(smoke, "smoke", config_path)
    second = _verify_build(replay, "smoke", config_path)
    if first["artifacts"] != second["artifacts"]:
        raise ValueError("Pinned smoke replay produced different artifacts")
    for path, period in ((development, "development"), (validation, "validation")):
        _verify_build(path, period, config_path)
    holdout_sources = _sources(holdout_root)
    if not REQUIRED.issubset(holdout_sources):
        raise ValueError("Pin the holdout sources before freezing")
    config = tomllib.loads(config_path.read_text())
    if missing_notice_pages(holdout_root, holdout_sources, config, *_dates(config, "holdout")):
        raise ValueError("Pin the supplemental NRC archive notices before freezing")
    write_json(output, {
        "identity": _identity(config_path), "smoke_artifacts": first["artifacts"],
        "development_sha256": compute_file_hash(development / "build.json"),
        "validation_sha256": compute_file_hash(validation / "build.json"),
        "holdout_manifest_sha256": compute_file_hash(holdout_root / "inputs.json"),
    })


def _read_panel(path: Path) -> pd.DataFrame:
    panel = pd.read_csv(path / "panel.csv", dtype={"event_window": str, "grid_disruption": str})
    panel["event_window"] = panel.event_window.fillna("")
    panel["grid_disruption"] = panel.grid_disruption.fillna("")
    panel["interval_start_utc"] = pd.to_datetime(panel.interval_start_utc, utc=True, format="mixed")
    return panel.set_index("interval_start_utc")


def study(development: Path, evaluation: Path, period: str, output: Path, config_path: Path,
          config: dict, frozen: Path | None = None) -> dict:
    _verify_build(development, "development", config_path)
    build_info = _verify_build(evaluation, period, config_path)
    if period == "holdout":
        if frozen is None or build_info["freeze_sha256"] != compute_file_hash(frozen):
            raise ValueError("The holdout build must use the supplied freeze")
        lock = _read_json(frozen)
        if lock["development_sha256"] != compute_file_hash(development / "build.json"):
            raise ValueError("The development build differs from the frozen input")
    panel = _read_panel(evaluation)
    ledger = pd.read_csv(evaluation / "events.csv", dtype={"event_id": str})
    for column in ("onset_utc", "post_start_utc"):
        ledger[column] = pd.to_datetime(ledger[column], utc=True, format="mixed")
    benchmark = oracle_benchmark(_read_panel(development), panel, config)
    report = {
        "period": period, "unit": "USD/MWh",
        "benchmark": benchmark, "price_response": price_response(panel, ledger, config),
        "leading_indicators_unlocked": period == "holdout" and benchmark["gate"] == "pass",
        "development_build_sha256": compute_file_hash(development / "build.json"),
        "evaluation_build_sha256": compute_file_hash(evaluation / "build.json"),
    }
    if output.exists():
        raise ValueError(f"Study output already exists: {output}")
    write_json(output, report)
    return report


def _review_inputs(root: Path, output: Path, config: dict, start: date, end: date) -> None:
    sources = _sources(root)
    if "nrc_scrams" not in sources or "eia860" not in sources:
        raise ValueError("Fetch or pin the NRC scram workbook and EIA-860 ZIP first")
    queue = review_queue(root, sources, config)
    queue = queue[queue.event_date.between(start - timedelta(days=1), end)]
    output.mkdir(parents=True, exist_ok=False)
    queue.to_csv(output / "candidates.csv", index=False)
    pd.concat([eia_generators(root / s["path"], s["metadata"]["year"]) for s in sources["eia860"]]).to_csv(
        output / "eia_generators.csv", index=False
    )
    seed = queue[["event_id", "docket"]].copy()
    seed["notice_source_id"] = queue.candidate_source_id.where(queue.event_id.str.startswith("EN:"), "")
    pd.DataFrame(seed, columns=[*REVIEW_FIELDS, "docket"]).fillna("").to_csv(
        output / "event_review.csv", index=False
    )
    pd.DataFrame(columns=CROSSWALK_FIELDS).to_csv(output / "crosswalk.csv", index=False)
    days = pd.date_range(start, end, inclusive="left").strftime("%Y-%m-%d")
    pd.DataFrame({"delivery_date": days}, columns=CALENDAR_FIELDS).fillna("").to_csv(
        output / "calendar.csv", index=False
    )


def _fetch_notice(root: Path, config: dict, event_id: str) -> dict:
    sources = _sources(root)
    candidate = review_queue(root, sources, config).set_index("event_id").loc[event_id]
    if event_id.startswith("EN:"):
        return next(s for s in sources["nrc_notice"] if s["id"] == candidate.candidate_source_id)
    event_day = candidate.event_date
    legacy = "https://www.nrc.gov/reading-rm/doc-collections/event-status/event"
    # Some legacy links redirect to a different day's report. Confirm the
    # actual event number before storing the evidence.
    urls = [candidate.notice_url] if pd.notna(candidate.notice_url) else []
    urls += [f"{legacy}/{event_day.year}/{event_day:%Y%m%d}en"]
    for url in urls:
        response = requests.get(url, timeout=60)
        if response.ok and candidate.notification_number in notification_numbers(response.content):
            return pin(root, "nrc_notice", response.content, ".html", response.url,
                       datetime.now(timezone.utc).isoformat(), event_id=event_id)
    raise ValueError(f"Could not locate NRC notice {candidate.notification_number} for {event_id}; pin it manually")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("fetch", "pin", "notice", "queue", "build"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        if name in {"fetch", "queue", "build"}:
            command.add_argument("--period", choices=PERIODS, required=True)
        if name in {"queue", "build"}:
            command.add_argument("--output", type=Path, required=True)
        if name == "fetch":
            command.add_argument("--group", choices=("nrc", "notices", "eia", "weather", "pjm"), required=True)
        if name == "notice":
            command.add_argument("event_id")
        if name == "pin":
            command.add_argument("kind", choices=sorted(KINDS))
            command.add_argument("file", type=Path)
            command.add_argument("--source", required=True)
            command.add_argument("--retrieved-at", required=True)
            command.add_argument("--year", type=int)
            command.add_argument("--replace", action="store_true")
        if name == "build":
            command.add_argument("--freeze", type=Path)
    freeze_cmd = commands.add_parser("freeze")
    for name in ("smoke", "replay", "development", "validation", "holdout-root", "output"):
        freeze_cmd.add_argument(f"--{name}", type=Path, required=True)
    study_cmd = commands.add_parser("study")
    study_cmd.add_argument("--period", choices=("validation", "holdout"), required=True)
    for name in ("development", "evaluation", "output"):
        study_cmd.add_argument(f"--{name}", type=Path, required=True)
    study_cmd.add_argument("--freeze", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    config = tomllib.loads(args.config.read_text())
    try:
        if args.command == "fetch":
            fetch(args.root, config, *_dates(config, args.period), args.group)
        elif args.command == "pin":
            metadata = {"year": args.year} if args.year is not None else {}
            entry = pin(args.root, args.kind, args.file.read_bytes(), args.file.suffix.lower(),
                        args.source, args.retrieved_at, replace=args.replace, **metadata)
            print(entry["id"])
        elif args.command == "notice":
            print(_fetch_notice(args.root, config, args.event_id)["id"])
        elif args.command == "queue":
            _review_inputs(args.root, args.output, config, *_dates(config, args.period))
        elif args.command == "build":
            print(json.dumps(build(args.root, args.output, args.config, config, args.period, args.freeze), indent=2))
        elif args.command == "freeze":
            freeze(args.smoke, args.replay, args.development, args.validation, args.holdout_root, args.output, args.config)
        elif args.command == "study":
            print(json.dumps(study(args.development, args.evaluation, args.period, args.output, args.config, config,
                                   args.freeze)["benchmark"], indent=2))
    except (OSError, ValueError, KeyError, requests.RequestException) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
