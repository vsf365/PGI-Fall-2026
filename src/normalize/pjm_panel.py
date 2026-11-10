"""Assemble the PJM pilot panel on the complete hourly UTC delivery grid."""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
import re

import numpy as np
import pandas as pd

from src.labels.pjm_events import reviewed_csv, utc_timestamp
from src.normalize.market import normalize_lmps


CALENDAR_FIELDS = (
    "delivery_date", "da_available_at", "da_clock_type", "da_version", "da_evidence",
    "event_review_complete", "event_evidence", "grid_disruption",
)

EXPORT_ALIASES = {
    "datetime_beginning_utc": "datetime_beginning_utc",
    "pricing_node_id": "pnode_id",
    "pricing_node_name": "pnode_name",
    "total_lmp_day_ahead": "total_lmp_da",
    "total_lmp_real_time": "total_lmp_rt",
    "version_number": "version_nbr",
    "latest_version": "row_is_current",
    "load_mw": "mw",
}


def market_records(root: Path, sources: list[dict], feed: str):
    for source in sources:
        path = root / source["path"]
        if path.suffix == ".csv":
            batch = pd.read_csv(path, dtype=str, keep_default_na=False).to_dict("records")
        else:
            body = json.loads(path.read_text())
            if isinstance(body, dict) and body.get("feed", feed) not in (feed, f"{feed}_archived"):
                raise ValueError(f"Wrong source feed for {path}")
            batch = body if isinstance(body, list) else body.get("items", body.get("records", body.get("data")))
        if not isinstance(batch, list):
            raise ValueError(f"Expected PJM records in {path}")
        for row in batch:
            clean = {}
            for name, value in row.items():
                key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
                clean[EXPORT_ALIASES.get(key, key)] = value if value != "" else None
            yield {**clean, "source_feed": feed, "source_id": source["id"]}


def _versions(column: pd.Series) -> pd.Series:
    values = pd.to_numeric(column, errors="coerce")
    return values.astype("Int64").astype("string").fillna("unversioned")


def _resolve(rows: pd.DataFrame, value: str, version: str = "") -> tuple:
    """Select the stated vintage or latest outcome, without averaging conflicts."""
    if rows.empty:
        return np.nan, False, ""
    conflict = rows[value].nunique() > 1
    if version:
        selected = rows[rows.version == version]
    else:
        selected = rows[rows.current] if rows.current.any() else rows
        versions = pd.to_numeric(selected.version, errors="coerce")
        if not selected.current.any() and versions.notna().all():
            selected = selected[versions == versions.max()]
    if selected.empty or selected[value].nunique() != 1:
        return np.nan, conflict, ""
    return float(selected[value].iloc[0]), conflict, ";".join(sorted(set(selected.source_id)))


def _by_hour(rows: pd.DataFrame, value: str) -> dict:
    if rows.empty:
        return {}
    rows = rows.copy()
    rows["version"] = _versions(rows.get("version_nbr", pd.Series(index=rows.index, dtype="object")))
    rows["current"] = rows.get("row_is_current", pd.Series(False, index=rows.index)).astype(str).str.lower().isin(
        {"true", "1", "1.0", "yes"}
    )
    rows[value] = pd.to_numeric(rows[value], errors="coerce")
    rows = rows[np.isfinite(rows[value])]
    return dict(tuple(rows.groupby("interval_start_utc")))


def _ghcnh_weather(path: Path, stations: dict[str, str]) -> pd.DataFrame:
    fields = {"temperature": "temperature_c", "dew_point_temperature": "dewpoint_c"}
    columns = ["STATION", "DATE"]
    for field in fields:
        columns.extend([field, f"{field}_Quality_Code", f"{field}_Source_Code"])
    data = pd.read_csv(path, sep="|", usecols=columns, dtype=str)
    data["STATION"] = data.STATION.map({ghcnh: isd for isd, ghcnh in stations.items()})
    if data.STATION.isna().any():
        raise ValueError(f"Unknown GHCNh station in {path}")
    # GHCNh preserves source-specific numeric QC codes and uses letters for failures.
    isd_sources = {"313", "314", "315", "322", "335", "343", "344", "346"}
    other_sources = {"220", "221", "222", "223", "347", "348"}
    for field, column in fields.items():
        source = data[f"{field}_Source_Code"]
        quality = data[f"{field}_Quality_Code"]
        accepted = (source.isin(isd_sources) & quality.isin({"0", "1", "4", "5"})) | (
            source.isin(other_sources) & quality.eq("1")
        )
        values = pd.to_numeric(data[field], errors="coerce")
        data[column] = values.where(accepted & values.between(-90, 65))
    return data


def _weather(root: Path, sources: list[dict], config: dict) -> pd.DataFrame:
    batches = []
    for source in sources:
        path = root / source["path"]
        is_ghcnh = path.suffix == ".psv"
        if is_ghcnh:
            data = _ghcnh_weather(path, config["weather"]["ghcnh_stations"])
        else:
            data = pd.read_csv(path, usecols=["STATION", "DATE", "TMP", "DEW"], dtype=str)
            for field, column in (("TMP", "temperature_c"), ("DEW", "dewpoint_c")):
                bits = data[field].str.extract(r"^([+-]\d{4}),([0145])$")
                values = pd.to_numeric(bits[0], errors="coerce").div(10)
                data[column] = values.where(values.between(-90, 65))
        data = data[data.STATION.isin(config["weather"]["stations"])].copy()
        data["time"] = pd.to_datetime(data.DATE, utc=True, format="mixed")
        data["interval_start_utc"] = data.time.dt.floor("h")
        data["source_priority"] = int(is_ghcnh)
        data["source_id"] = source["id"]
        batches.append(data[["STATION", "time", "interval_start_utc", "source_priority", "source_id",
                             "temperature_c", "dewpoint_c"]].dropna(subset=["temperature_c", "dewpoint_c"]))
    cols = ["temperature_c", "dewpoint_c", "weather_stations", "weather_source_ids"]
    if not batches:
        return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="UTC"))
    combined = pd.concat(batches, ignore_index=True)
    station_hour = ["interval_start_utc", "STATION"]
    priority = combined.groupby(station_hour).source_priority.transform("min")
    combined = combined[combined.source_priority.eq(priority)]
    latest = combined.groupby(station_hour).time.transform("max")
    readings = combined[combined.time.eq(latest)].groupby(station_hour).agg(
        temperature_c=("temperature_c", "first"), dewpoint_c=("dewpoint_c", "first"),
        temp_values=("temperature_c", "nunique"), dew_values=("dewpoint_c", "nunique"),
        source_id=("source_id", lambda ids: ";".join(sorted(set(ids)))),
    )
    readings = readings[readings.temp_values.eq(1) & readings.dew_values.eq(1)]
    weather = readings.groupby(level=0).agg(
        temperature_c=("temperature_c", "mean"), dewpoint_c=("dewpoint_c", "mean"),
        weather_stations=("temperature_c", "count"),
        weather_source_ids=("source_id", lambda ids: ";".join(sorted(set(";".join(ids).split(";"))))),
    )
    weather.loc[weather.weather_stations < config["study"]["min_weather_stations"],
                ["temperature_c", "dewpoint_c"]] = np.nan
    return weather


def available_at_cutoff(available_at, cutoff) -> bool:
    return bool(pd.notna(available_at) and pd.notna(cutoff) and available_at <= cutoff)


def _calendar(root: Path, sources: list[dict], timezone: str) -> dict:
    if not sources:
        return {}
    frame = reviewed_csv(root / sources[0]["path"], CALENDAR_FIELDS, "delivery_date")
    result = {}
    for row in frame.to_dict("records"):
        delivery = date.fromisoformat(row["delivery_date"])
        clock = row["da_available_at"]
        if row["event_review_complete"] not in {"yes", "no", ""} or row["grid_disruption"] not in {"yes", "no", ""}:
            raise ValueError(f"Invalid audit calendar flags on {delivery}")
        row["event_review_complete"] = row["event_review_complete"] == "yes" and bool(row["event_evidence"])
        if clock:
            timestamp = utc_timestamp(clock)
            if (timestamp.tz_convert(timezone).date() != delivery - timedelta(days=1)
                    or row["da_clock_type"] not in {"exact", "verified_by"}
                    or not row["da_version"] or not row["da_evidence"]):
                raise ValueError(f"Incomplete or inconsistent DA posting evidence on {delivery}")
            row["da_available_at"] = timestamp
        else:
            row["da_available_at"] = pd.NaT
        result[delivery] = row
    return result


def _calendar_split(delivery: date, periods: dict) -> str:
    for name in ("development", "validation", "holdout"):
        start, end = (date.fromisoformat(value) for value in periods[name])
        if start <= delivery < end:
            return name
    return "outside"


def _settled_prices(root: Path, sources: list[dict], node_id: int) -> dict:
    rows = pd.DataFrame.from_records([
        row for row in market_records(root, sources, "rt_da_monthly_lmps")
        if str(row.get("pnode_id")) == str(node_id)
    ])
    if rows.empty:
        return {}
    rows["interval_start_utc"] = pd.to_datetime(rows.datetime_beginning_utc, utc=True, format="mixed")
    return {
        kind: _by_hour(rows, f"total_lmp_{kind}")
        for kind in ("da", "rt")
    }


def hourly_panel(root: Path, by_kind: dict, config: dict, start: date, end: date) -> pd.DataFrame:
    study = config["study"]
    tz = study["timezone"]
    grid = pd.date_range(pd.Timestamp(start, tz=tz).tz_convert("UTC"),
                         pd.Timestamp(end, tz=tz).tz_convert("UTC"), freq="h", inclusive="left")
    prices = []
    for market in ("da", "rt"):
        feed = config["sources"][market]
        prices.extend(row for row in market_records(root, by_kind.get(market, []), feed)
                      if str(row.get("pnode_id")) == str(study["node_id"]))
    # Use the documented UTC field. An EPT-only export cannot distinguish the
    # repeated fall-back hour and is not accepted by the research panel.
    if any(not row.get("datetime_beginning_utc") for row in prices):
        raise ValueError("PJM LMP snapshots must include Datetime Beginning UTC")
    if any(row.get("pnode_name") and row["pnode_name"].strip().upper() != study["node_name"].upper()
           for row in prices):
        raise ValueError("The requested PJM node ID did not match the configured node name")
    values = normalize_lmps(raw_records=prices, save=False)
    values = values[values.value_type.eq("total_lmp")]
    da = _by_hour(values[values.market.eq("DA")], "value")
    rt = _by_hour(values[values.market.eq("RT")], "value")
    load = pd.DataFrame.from_records([
        row for row in market_records(root, by_kind.get("load", []), config["sources"]["load"])
        if str(row.get("load_area", "")).upper() == study["load_area"]
    ])
    if not load.empty:
        load["interval_start_utc"] = pd.to_datetime(load.datetime_beginning_utc, utc=True, format="mixed")
    load_by_hour = _by_hour(load, "mw")
    settled = _settled_prices(root, by_kind.get("settled", []), study["node_id"])
    calendar = _calendar(root, by_kind.get("calendar", []), tz)
    empty = pd.DataFrame()
    records = []
    for timestamp in grid:
        local = timestamp.tz_convert(tz)
        delivery = local.date()
        audit = calendar.get(delivery, {})
        verified_at = audit.get("da_available_at", pd.NaT)
        day_prices = da.get(timestamp, empty)
        archive_price, da_conflict, archive_id = _resolve(day_prices, "value")
        verified_price, _, verified_id = _resolve(day_prices, "value", audit["da_version"]) if pd.notna(verified_at) else (np.nan, False, "")
        rt_price, rt_conflict, rt_id = _resolve(rt.get(timestamp, empty), "value")
        load_mw, load_conflict, load_id = _resolve(load_by_hour.get(timestamp, empty), "mw")
        settled_da, settled_da_conflict, settled_da_id = _resolve(
            settled.get("da", {}).get(timestamp, empty), "total_lmp_da"
        )
        settled_rt, settled_rt_conflict, settled_rt_id = _resolve(
            settled.get("rt", {}).get(timestamp, empty), "total_lmp_rt"
        )
        records.append({
            "interval_start_utc": timestamp, "delivery_date": delivery.isoformat(),
            "split": _calendar_split(delivery, config["periods"]),
            "local_hour": local.hour, "day_of_week": local.dayofweek, "month": local.month,
            "pnode_id": study["node_id"], "pnode_name": study["node_name"], "da_archive_lmp": archive_price,
            "da_lmp": verified_price, "da_available_at": verified_at if np.isfinite(verified_price) else pd.NaT,
            "da_clock_type": audit.get("da_clock_type", ""), "rt_lmp": rt_price,
            "spread": rt_price - verified_price, "archive_spread": rt_price - archive_price,
            "settled_da_lmp": settled_da, "settled_rt_lmp": settled_rt,
            "settled_spread": settled_rt - settled_da,
            "settled_conflict": settled_da_conflict or settled_rt_conflict,
            "settled_source_ids": ";".join(sorted({settled_da_id, settled_rt_id} - {""})),
            "load_mw": load_mw, "da_conflict": da_conflict, "rt_conflict": rt_conflict,
            "load_conflict": load_conflict, "da_source_ids": verified_id or archive_id,
            "rt_source_ids": rt_id, "load_source_ids": load_id,
            "event_review_complete": audit.get("event_review_complete", False),
            "grid_disruption": audit.get("grid_disruption", ""),
        })
    panel = pd.DataFrame.from_records(records).set_index("interval_start_utc")
    return panel.join(_weather(root, by_kind.get("weather", []), config))
