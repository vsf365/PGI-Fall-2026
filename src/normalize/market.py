"""
Market Data Normalization Module.

Reads raw PJM JSON files and produces standardized, curated Parquet tables:
1. normalize_lmps: Canonical market_observation table (DA/RT LMPs, congestion, loss, energy)
2. normalize_load: Metered zonal and RTO hourly load
3. normalize_gen_mix: Generation mix by fuel type, extracting nuclear generation specifically
"""

from __future__ import annotations

import glob
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Tuple, Union

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from configs.config import CURATED_DIR, RAW_ISO

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("market_normalize")

try:
    import pandas as pd
    import numpy as np
except ImportError:
    pd = None  # type: ignore
    np = None  # type: ignore


def _load_raw_json_files(raw_dir: Path, pattern: str) -> List[Dict[str, Any]]:
    """Load and aggregate all records from matching JSON files in raw_dir."""
    search_path = raw_dir / pattern
    files = sorted(glob.glob(str(search_path)))
    all_records: List[Dict[str, Any]] = []

    for file_path in files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = json.load(f)
                if isinstance(content, dict) and "records" in content:
                    all_records.extend(
                        {**row, "source_feed": content.get("feed") or row.get("source_feed")}
                        for row in content["records"]
                    )
                elif isinstance(content, list):
                    all_records.extend(content)
                elif isinstance(content, dict) and "items" in content:
                    all_records.extend(content["items"])
        except Exception as e:
            logger.error(f"Error reading {file_path}: {e}")

    logger.info(f"Loaded {len(all_records)} raw records matching '{pattern}'")
    return all_records


def normalize_lmps(
    raw_dir: Optional[Union[str, Path]] = None,
    raw_records: Optional[List[Dict[str, Any]]] = None,
    output_path: Optional[Union[str, Path]] = None,
    save: bool = True,
) -> Any:
    """
    Normalize Day-Ahead and Real-Time hourly LMPs into canonical market_observation schema.

    Canonical Schema:
    - iso: 'PJM'
    - market: 'DA' or 'RT'
    - node_or_zone: pricing node name or ID
    - pnode_id: integer node ID
    - interval_start_utc: UTC timestamp
    - interval_end_utc: UTC timestamp (start + 1h)
    - value_type: 'total_lmp' | 'congestion_price' | 'marginal_loss_price' | 'system_energy_price'
    - value: price in $/MWh
    - unit: '$/MWh'
    - status: 'actual'

    Also produces wide-format table (pjm_lmps.parquet).

    Args:
        raw_dir: Directory containing raw JSON files (defaults to RAW_ISO)
        raw_records: Optional in-memory records to normalize directly
        output_path: Destination path for canonical market_observations parquet
        save: Whether to write parquet to disk

    Returns:
        pd.DataFrame containing canonical market observations.
    """
    if pd is None:
        raise ImportError("pandas is required for normalize_lmps")

    source_dir = Path(raw_dir) if raw_dir else RAW_ISO
    records = list(raw_records) if raw_records is not None else []

    if raw_records is None and source_dir.exists():
        # Load both DA and RT files
        records.extend(_load_raw_json_files(source_dir, "pjm_da_hrl_lmps_*.json"))
        records.extend(_load_raw_json_files(source_dir, "pjm_rt_hrl_lmps_*.json"))

    if not records:
        logger.warning("No LMP records found to normalize.")
        empty_df = pd.DataFrame(
            columns=[
                "iso",
                "market",
                "node_or_zone",
                "pnode_id",
                "interval_start_utc",
                "interval_end_utc",
                "value_type",
                "value",
                "unit",
                "status",
            ]
        )
        return empty_df

    df = pd.DataFrame(records)

    # Standardize column names (lowercase, stripped)
    df.columns = [c.lower().strip() for c in df.columns]

    # Parse timestamps
    if "datetime_beginning_utc" in df.columns:
        df["interval_start_utc"] = pd.to_datetime(df["datetime_beginning_utc"], utc=True, format="mixed")
    elif "datetime_beginning_ept" in df.columns:
        # EPT is US/Eastern
        df["interval_start_utc"] = (
            pd.to_datetime(df["datetime_beginning_ept"])
            .dt.tz_localize("America/New_York", ambiguous="NaT", nonexistent="NaT")
            .dt.tz_convert("UTC")
        )
    else:
        raise ValueError("Neither datetime_beginning_utc nor datetime_beginning_ept found in LMP records")

    # Drop records with invalid timestamps
    df = df.dropna(subset=["interval_start_utc"])
    df["interval_end_utc"] = df["interval_start_utc"] + pd.Timedelta(hours=1)

    # Feed identity takes precedence. Infer market only for legacy untagged rows.
    market = df.get("market", pd.Series(index=df.index, dtype="object"))
    if "source_feed" in df:
        feed_market = df["source_feed"].map({
            "da_hrl_lmps": "DA", "rt_hrl_lmps": "RT",
            "da_hrl_lmps_archived": "DA", "rt_hrl_lmps_archived": "RT",
        })
        market = feed_market.combine_first(market)
    da = df.filter(regex="_da$").notna().any(axis=1)
    rt = df.filter(regex="_rt$").notna().any(axis=1)
    if (market.isna() & (da == rt)).any():
        raise ValueError("Cannot identify DA or RT for an untagged price row")
    df["market"] = market.mask(market.isna() & da, "DA").mask(market.isna() & rt, "RT")
    if not df["market"].isin(["DA", "RT"]).all():
        raise ValueError("Unexpected PJM market label")

    # Identify node/zone
    if "pnode_name" in df.columns:
        df["node_or_zone"] = df["pnode_name"].astype(str)
    elif "pnode_id" in df.columns:
        df["node_or_zone"] = df["pnode_id"].astype(str)
    else:
        df["node_or_zone"] = "UNKNOWN"

    if "pnode_id" in df.columns:
        df["pnode_id"] = pd.to_numeric(df["pnode_id"], errors="coerce")
    else:
        df["pnode_id"] = None

    # Map price components
    price_cols_map = {
        "total_lmp": ["total_lmp_da", "total_lmp_rt", "total_lmp", "lmp"],
        "congestion_price": ["congestion_price_da", "congestion_price_rt", "congestion_price"],
        "marginal_loss_price": ["marginal_loss_price_da", "marginal_loss_price_rt", "marginal_loss_price"],
        "system_energy_price": ["system_energy_price_da", "system_energy_price_rt", "system_energy_price"],
    }

    obs_records = []
    base_cols = ["iso", "market", "node_or_zone", "pnode_id", "interval_start_utc", "interval_end_utc"]
    base_cols += [col for col in ("source_feed", "source_id", "version_nbr", "row_is_current") if col in df]
    df["iso"] = "PJM"

    for val_type, possible_cols in price_cols_map.items():
        for market_name in ("DA", "RT"):
            market_rows = df[df["market"] == market_name]
            suffix = "_" + market_name.lower()
            candidates = [c for c in possible_cols if c in df and
                          (c.endswith(suffix) or not c.endswith(("_da", "_rt")))]
            if not candidates or market_rows.empty:
                continue
            sub_df = market_rows[base_cols].copy()
            sub_df["value_type"] = val_type
            values = pd.to_numeric(market_rows[candidates[0]], errors="coerce")
            for col in candidates[1:]:
                values = values.combine_first(pd.to_numeric(market_rows[col], errors="coerce"))
            sub_df["value"] = values
            sub_df["unit"] = "$/MWh"
            sub_df["status"] = "actual"
            obs_records.append(sub_df)

    if obs_records:
        canonical_df = pd.concat(obs_records, ignore_index=True)
        canonical_df = canonical_df.dropna(subset=["value"])
        canonical_df = canonical_df.sort_values(["interval_start_utc", "market", "node_or_zone", "value_type"])
    else:
        canonical_df = pd.DataFrame(
            columns=[
                "iso",
                "market",
                "node_or_zone",
                "pnode_id",
                "interval_start_utc",
                "interval_end_utc",
                "value_type",
                "value",
                "unit",
                "status",
            ]
        )

    if save and not canonical_df.empty:
        CURATED_DIR.mkdir(parents=True, exist_ok=True)
        target = Path(output_path) if output_path else CURATED_DIR / "pjm_market_observations.parquet"
        canonical_df.to_parquet(target, index=False)
        logger.info(f"Saved {len(canonical_df)} canonical LMP observations to {target}")

        # Also save wide table for analytical convenience
        try:
            wide_df = canonical_df.pivot_table(
                index=["iso", "market", "node_or_zone", "pnode_id", "interval_start_utc", "interval_end_utc"],
                columns="value_type",
                values="value",
                aggfunc="mean",
            ).reset_index()
            wide_target = CURATED_DIR / "pjm_lmps.parquet"
            wide_df.to_parquet(wide_target, index=False)
            logger.info(f"Saved {len(wide_df)} wide LMP rows to {wide_target}")
        except Exception as e:
            logger.warning(f"Could not build wide LMP table: {e}")

    return canonical_df


def normalize_load(
    raw_dir: Optional[Union[str, Path]] = None,
    raw_records: Optional[List[Dict[str, Any]]] = None,
    output_path: Optional[Union[str, Path]] = None,
    save: bool = True,
) -> Any:
    """
    Normalize Metered Hourly Load (hrl_load_metered).

    Schema:
    - iso: 'PJM'
    - load_area: zone name or 'RTO'
    - interval_start_utc: UTC timestamp
    - interval_end_utc: UTC timestamp (start + 1h)
    - load_mw: float
    - unit: 'MW'
    - status: 'actual'

    Args:
        raw_dir: Directory containing raw JSON files (defaults to RAW_ISO)
        raw_records: Optional in-memory records
        output_path: Destination parquet path
        save: Whether to write parquet

    Returns:
        pd.DataFrame of normalized load observations.
    """
    if pd is None:
        raise ImportError("pandas is required for normalize_load")

    source_dir = Path(raw_dir) if raw_dir else RAW_ISO
    records = raw_records or []

    if not records and source_dir.exists():
        records.extend(_load_raw_json_files(source_dir, "pjm_hrl_load_metered_*.json"))

    if not records:
        logger.warning("No load records found to normalize.")
        return pd.DataFrame(
            columns=["iso", "load_area", "interval_start_utc", "interval_end_utc", "load_mw", "unit", "status"]
        )

    df = pd.DataFrame(records)
    df.columns = [c.lower().strip() for c in df.columns]

    # Timestamp conversion
    if "datetime_beginning_utc" in df.columns:
        df["interval_start_utc"] = pd.to_datetime(df["datetime_beginning_utc"], utc=True)
    elif "datetime_beginning_ept" in df.columns:
        df["interval_start_utc"] = (
            pd.to_datetime(df["datetime_beginning_ept"])
            .dt.tz_localize("US/Eastern", ambiguous="NaT", nonexistent="shift_forward")
            .dt.tz_convert("UTC")
        )
    else:
        raise ValueError("Timestamp column missing in load records")

    df = df.dropna(subset=["interval_start_utc"])
    df["interval_end_utc"] = df["interval_start_utc"] + pd.Timedelta(hours=1)

    df["iso"] = "PJM"
    df["load_area"] = df.get("load_area", "RTO").astype(str).str.upper()

    # Load MW column
    mw_col = next((c for c in ["mw", "load_mw", "metered_mw"] if c in df.columns), None)
    if mw_col:
        df["load_mw"] = pd.to_numeric(df[mw_col], errors="coerce")
    else:
        df["load_mw"] = np.nan

    df["unit"] = "MW"
    df["status"] = "actual"

    curated_cols = ["iso", "load_area", "interval_start_utc", "interval_end_utc", "load_mw", "unit", "status"]
    result_df = df[curated_cols].dropna(subset=["load_mw"]).sort_values(["load_area", "interval_start_utc"])
    result_df = result_df.drop_duplicates(subset=["load_area", "interval_start_utc"])

    if save and not result_df.empty:
        CURATED_DIR.mkdir(parents=True, exist_ok=True)
        target = Path(output_path) if output_path else CURATED_DIR / "pjm_load.parquet"
        result_df.to_parquet(target, index=False)
        logger.info(f"Saved {len(result_df)} load rows to {target}")

    return result_df


def normalize_gen_mix(
    raw_dir: Optional[Union[str, Path]] = None,
    raw_records: Optional[List[Dict[str, Any]]] = None,
    output_path: Optional[Union[str, Path]] = None,
    save: bool = True,
) -> Any:
    """
    Normalize Generation by Fuel Type (gen_by_fuel), specifically extracting nuclear generation.

    Schema:
    - interval_start_utc, interval_end_utc, iso
    - nuclear_mw, gas_mw, coal_mw, wind_mw, solar_mw, hydro_mw, other_mw
    - total_generation_mw: sum of all fuels
    - nuclear_share: nuclear_mw / total_generation_mw
    - renewable_generation_mw: wind + solar + hydro
    - renewable_share: renewable / total
    - fossil_generation_mw: gas + coal
    - fossil_share: fossil / total

    Args:
        raw_dir: Directory containing raw JSON files (defaults to RAW_ISO)
        raw_records: Optional in-memory records
        output_path: Destination parquet path
        save: Whether to write parquet

    Returns:
        pd.DataFrame containing curated generation mix by hour.
    """
    if pd is None:
        raise ImportError("pandas is required for normalize_gen_mix")

    source_dir = Path(raw_dir) if raw_dir else RAW_ISO
    records = raw_records or []

    if not records and source_dir.exists():
        records.extend(_load_raw_json_files(source_dir, "pjm_gen_by_fuel_*.json"))

    if not records:
        logger.warning("No generation mix records found to normalize.")
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df.columns = [c.lower().strip() for c in df.columns]

    # Timestamp conversion
    if "datetime_beginning_utc" in df.columns:
        df["interval_start_utc"] = pd.to_datetime(df["datetime_beginning_utc"], utc=True)
    elif "datetime_beginning_ept" in df.columns:
        df["interval_start_utc"] = (
            pd.to_datetime(df["datetime_beginning_ept"])
            .dt.tz_localize("US/Eastern", ambiguous="NaT", nonexistent="shift_forward")
            .dt.tz_convert("UTC")
        )
    else:
        raise ValueError("Timestamp column missing in generation mix records")

    df = df.dropna(subset=["interval_start_utc"])
    df["interval_end_utc"] = df["interval_start_utc"] + pd.Timedelta(hours=1)
    df["iso"] = "PJM"

    fuel_col = next((c for c in ["fuel_type", "fuel", "type"] if c in df.columns), "fuel_type")
    mw_col = next((c for c in ["mw", "gen_mw", "generation_mw"] if c in df.columns), "mw")

    df["fuel_clean"] = df[fuel_col].astype(str).str.strip().str.title()
    df["mw"] = pd.to_numeric(df[mw_col], errors="coerce").fillna(0.0)

    # Standardize fuel categories
    def standardize_fuel(val: str) -> str:
        v = val.lower()
        if "nuclear" in v:
            return "nuclear_mw"
        elif "gas" in v or "natural gas" in v:
            return "gas_mw"
        elif "coal" in v:
            return "coal_mw"
        elif "wind" in v:
            return "wind_mw"
        elif "solar" in v:
            return "solar_mw"
        elif "hydro" in v:
            return "hydro_mw"
        elif "oil" in v:
            return "oil_mw"
        else:
            return "other_mw"

    df["fuel_category"] = df["fuel_clean"].apply(standardize_fuel)

    # Pivot to wide format: 1 row per interval_start_utc
    pivot_df = df.pivot_table(
        index=["iso", "interval_start_utc", "interval_end_utc"],
        columns="fuel_category",
        values="mw",
        aggfunc="sum",
    ).fillna(0.0).reset_index()

    # Ensure all standard fuel columns exist
    for col in ["nuclear_mw", "gas_mw", "coal_mw", "wind_mw", "solar_mw", "hydro_mw", "oil_mw", "other_mw"]:
        if col not in pivot_df.columns:
            pivot_df[col] = 0.0

    # Summary metrics
    fuel_mw_cols = [c for c in pivot_df.columns if c.endswith("_mw")]
    pivot_df["total_generation_mw"] = pivot_df[fuel_mw_cols].sum(axis=1)

    # Nuclear share
    pivot_df["nuclear_share"] = np.where(
        pivot_df["total_generation_mw"] > 0,
        pivot_df["nuclear_mw"] / pivot_df["total_generation_mw"],
        0.0,
    )

    # Renewable generation & share
    pivot_df["renewable_generation_mw"] = (
        pivot_df["wind_mw"] + pivot_df["solar_mw"] + pivot_df["hydro_mw"]
    )
    pivot_df["renewable_share"] = np.where(
        pivot_df["total_generation_mw"] > 0,
        pivot_df["renewable_generation_mw"] / pivot_df["total_generation_mw"],
        0.0,
    )

    # Fossil generation & share
    pivot_df["fossil_generation_mw"] = pivot_df["gas_mw"] + pivot_df["coal_mw"] + pivot_df["oil_mw"]
    pivot_df["fossil_share"] = np.where(
        pivot_df["total_generation_mw"] > 0,
        pivot_df["fossil_generation_mw"] / pivot_df["total_generation_mw"],
        0.0,
    )

    pivot_df = pivot_df.sort_values("interval_start_utc")

    if save and not pivot_df.empty:
        CURATED_DIR.mkdir(parents=True, exist_ok=True)
        target = Path(output_path) if output_path else CURATED_DIR / "pjm_gen_mix.parquet"
        pivot_df.to_parquet(target, index=False)
        logger.info(f"Saved {len(pivot_df)} generation mix rows to {target}")

    return pivot_df


if __name__ == "__main__":
    print("=" * 60)
    print("Market Data Normalization Module Demonstration")
    print("=" * 60)
    if pd is None:
        print("⚠️  pandas not installed. Install via requirements.txt to run.")
    else:
        # Generate synthetic test records to verify normalization logic
        print("Testing normalize_lmps with synthetic records...")
        now_utc = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        sample_lmps = [
            {
                "datetime_beginning_utc": (now_utc - timedelta(hours=i)).isoformat() + "Z",
                "pnode_id": 51205,
                "pnode_name": "PEACH BOTTOM",
                "total_lmp_da": 35.50 + i,
                "congestion_price_da": 2.10,
                "marginal_loss_price_da": -0.40,
                "system_energy_price_da": 33.80 + i,
            }
            for i in range(5)
        ]
        norm_lmps = normalize_lmps(raw_records=sample_lmps, save=False)
        print(f"Normalized LMP records shape: {norm_lmps.shape}")
        print(norm_lmps.head(4))

        print("\nTesting normalize_load with synthetic records...")
        sample_load = [
            {
                "datetime_beginning_utc": (now_utc - timedelta(hours=i)).isoformat() + "Z",
                "load_area": "PECO",
                "mw": 4850.0 + i * 50,
            }
            for i in range(5)
        ]
        norm_load = normalize_load(raw_records=sample_load, save=False)
        print(f"Normalized Load records shape: {norm_load.shape}")
        print(norm_load.head(2))

        print("\nTesting normalize_gen_mix with synthetic records...")
        sample_gen = []
        for i in range(3):
            t_str = (now_utc - timedelta(hours=i)).isoformat() + "Z"
            sample_gen.extend([
                {"datetime_beginning_utc": t_str, "fuel_type": "Nuclear", "mw": 32000.0},
                {"datetime_beginning_utc": t_str, "fuel_type": "Gas", "mw": 40000.0},
                {"datetime_beginning_utc": t_str, "fuel_type": "Coal", "mw": 15000.0},
                {"datetime_beginning_utc": t_str, "fuel_type": "Wind", "mw": 5000.0},
                {"datetime_beginning_utc": t_str, "fuel_type": "Solar", "mw": 2000.0},
            ])
        norm_mix = normalize_gen_mix(raw_records=sample_gen, save=False)
        print(f"Normalized Gen Mix shape: {norm_mix.shape}")
        print(norm_mix[["interval_start_utc", "nuclear_mw", "nuclear_share", "renewable_share"]])
