"""Leakage-safe reactor-level joins for environmental observations.

Inputs are deliberately source-neutral: normalize NOAA or USGS data to the
observation schema below, then join it to a reviewed, effective-dated station
crosswalk. Availability timestamps, rather than observation timestamps, govern
what a forecast could have known.
"""

from __future__ import annotations

import pandas as pd


MAPPING_COLUMNS = {"reactor_id", "station_id", "valid_from", "valid_to"}
OBSERVATION_COLUMNS = {"station_id", "observed_at", "available_at", "variable", "value"}
TARGET_COLUMNS = {"reactor_id", "forecast_at"}


def _utc(series: pd.Series, name: str) -> pd.Series:
    result = pd.to_datetime(series, utc=True, errors="coerce")
    if result.isna().any():
        raise ValueError(f"{name} contains a missing or invalid timestamp")
    return result


def environmental_features(
    targets: pd.DataFrame,
    station_map: pd.DataFrame,
    observations: pd.DataFrame,
) -> pd.DataFrame:
    """Return target rows with latest available mapped-station features.

    ``station_map`` uses half-open effective dates ``[valid_from, valid_to)``;
    a blank ``valid_to`` means the mapping remains active. For each target,
    station, and variable, the newest observation whose ``available_at`` is no
    later than ``forecast_at`` is selected. Station values are then averaged.
    Counts and maximum observation age are included for missingness/staleness
    checks. The returned frame preserves target row order and columns.
    """
    missing = {
        "targets": TARGET_COLUMNS - set(targets.columns),
        "station_map": MAPPING_COLUMNS - set(station_map.columns),
        "observations": OBSERVATION_COLUMNS - set(observations.columns),
    }
    missing = {name: sorted(columns) for name, columns in missing.items() if columns}
    if missing:
        raise ValueError(f"Missing environmental join columns: {missing}")
    if targets.empty:
        return targets.copy()

    t = targets.copy().reset_index(drop=True)
    t["_target_row"] = range(len(t))
    t["forecast_at"] = _utc(t.forecast_at, "forecast_at")
    m = station_map.copy()
    m["valid_from"] = _utc(m.valid_from, "valid_from")
    m["valid_to"] = pd.to_datetime(m.valid_to.replace("", pd.NA), utc=True, errors="coerce")
    o = observations.copy()
    o["observed_at"] = _utc(o.observed_at, "observed_at")
    o["available_at"] = _utc(o.available_at, "available_at")
    o["value"] = pd.to_numeric(o.value, errors="coerce")
    o = o[o.value.notna() & o.station_id.notna() & o.variable.notna()]

    rows: list[dict] = []
    for target in t.itertuples(index=False, name=None):
        # Preserve the original target columns, followed by the synthetic row ID.
        values = dict(zip(t.columns, target))
        row_features = {"_target_row": values["_target_row"]}
        cutoff = values["forecast_at"]
        active = m[(m.reactor_id == values["reactor_id"])
                   & (m.valid_from <= cutoff)
                   & (m.valid_to.isna() | (cutoff < m.valid_to))]
        station_rows = o[o.station_id.isin(active.station_id)
                         & (o.available_at <= cutoff)
                         & (o.observed_at <= cutoff)]
        if not station_rows.empty:
            latest = (station_rows.sort_values(["observed_at", "available_at"])
                      .drop_duplicates(["station_id", "variable"], keep="last"))
            for variable, group in latest.groupby("variable", sort=True):
                prefix = str(variable)
                row_features.update({
                    f"{prefix}_mean": group.value.mean(),
                    f"{prefix}_station_count": int(group.station_id.nunique()),
                    f"{prefix}_max_age_hours": (cutoff - group.observed_at).dt.total_seconds().max() / 3600,
                    f"{prefix}_latest_available_at": group.available_at.max(),
                })
        rows.append(row_features)

    features = pd.DataFrame(rows)
    result = t.merge(features, on="_target_row", how="left", validate="one_to_one")
    return result.drop(columns="_target_row")
