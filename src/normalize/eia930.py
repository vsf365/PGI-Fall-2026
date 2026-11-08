"""Align the EIA-930 balance CSV's reported PJM demand to UTC hour starts."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

BALANCING_AUTHORITY = "Balancing Authority"
UTC_HOUR_END = "UTC Time at End of Hour"
REPORTED_DEMAND = "Demand (MW)"


def normalize_pjm_demand(source: Path) -> pd.DataFrame:
    data = pd.read_csv(source, usecols=[BALANCING_AUTHORITY, UTC_HOUR_END, REPORTED_DEMAND])
    pjm = data.loc[data[BALANCING_AUTHORITY].eq("PJM")].copy()
    if pjm.empty:
        raise ValueError(f"No PJM balancing authority records in {source}")
    hour_end = pd.to_datetime(pjm[UTC_HOUR_END], format="%m/%d/%Y %I:%M:%S %p", utc=True)
    if hour_end.isna().any() or hour_end.duplicated().any() or not hour_end.eq(hour_end.dt.floor("h")).all():
        raise ValueError(f"Invalid or duplicate PJM UTC hour endings in {source}")
    return pd.DataFrame({
        "interval_start_utc": hour_end - pd.Timedelta(hours=1),
        "eia930_pjm_demand_mw": pd.to_numeric(pjm[REPORTED_DEMAND]),
    }).sort_values("interval_start_utc", ignore_index=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    data = normalize_pjm_demand(args.source)
    with args.output.open("x", encoding="utf-8", newline="") as output:
        data.to_csv(output, index=False)


if __name__ == "__main__":
    main()
