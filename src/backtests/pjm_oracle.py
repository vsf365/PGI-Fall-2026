"""Retrospective price response and a held-out PJM perfect-information benchmark."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from src.normalize.pjm_panel import available_at_cutoff


OUTCOMES = ("rt_lmp", "spread")


def label_windows(panel: pd.DataFrame, ledger: pd.DataFrame, config: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    panel, ledger = panel.copy(), ledger.copy()
    study = config["study"]
    pre, post = study["pre_hours"], study["post_hours"]
    blocked_days = set()
    event_days = set()
    for row in ledger.itertuples():
        if row.exclusion in {"outside_pjm", "nrc_retracted", "not_a_trip"}:
            continue
        nearby = {(row.event_date + timedelta(days=offset)).isoformat() for offset in (-1, 0, 1)}
        event_days.update(nearby)
        if not row.eligible:
            blocked_days.update(nearby)
    panel["excluded_event_day"] = panel.delivery_date.isin(blocked_days)
    panel["event_window"] = ""
    panel["oracle_event"] = 0
    panel["study_eligible"] = (panel.event_review_complete & ~panel.excluded_event_day
                               & panel.da_lmp.notna() & panel.rt_lmp.notna())
    ledger["analysis_exclusion"] = ledger["exclusion"]
    ledger["post_start_utc"] = pd.Series(pd.NaT, index=ledger.index, dtype="datetime64[ns, UTC]")

    qualified = ledger.loc[ledger.eligible.eq(True)].sort_values("onset_utc")
    for index, row in qualified.iterrows():
        onset = row.onset_utc
        pre_times = pd.date_range(end=onset.floor("h") - pd.Timedelta(hours=1), periods=pre, freq="h")
        post_times = pd.date_range(start=onset.ceil("h"), periods=post, freq="h")
        times = pre_times.append(post_times)
        seen = panel.index.intersection(times)
        isolated = not ((qualified.index != index) & (
            (qualified.onset_utc - onset).abs() <= pd.Timedelta(hours=pre + post))).any()
        if not isolated:
            reason = "overlapping_trip"
        elif len(seen) != len(times) or panel.loc[seen, "split"].nunique() != 1:
            reason = "incomplete_window_or_split_boundary"
        elif not panel.loc[seen, "study_eligible"].all():
            reason = "unreviewed_or_missing_market_hours"
        elif not all(available_at_cutoff(available, onset) and available < onset
                     for available in panel.loc[seen, "da_available_at"]):
            reason = "day_ahead_availability_not_before_trip"
        else:
            reason = ""
        if reason:
            ledger.at[index, "analysis_exclusion"] = reason
            panel.loc[seen, "study_eligible"] = False
        else:
            ledger.at[index, "post_start_utc"] = post_times[0]
            panel.loc[seen, "event_window"] = row.event_id
            panel.loc[post_times, "oracle_event"] = 1
        # If onset is intra-hour, exclude that partial interval from all comparisons.
        partial = onset.floor("h")
        if partial != onset and partial in panel.index:
            panel.at[partial, "study_eligible"] = False
    panel["control_eligible"] = panel.study_eligible & ~panel.delivery_date.isin(event_days)
    return panel, ledger


def _mean_ci(samples: np.ndarray, draws: int, seed: int, weights=None) -> list[float] | None:
    if len(samples) < 2:
        return None
    weights = np.ones(len(samples)) if weights is None else np.asarray(weights)
    rng = np.random.default_rng(seed)
    # Batch bootstrap draws so an hourly multi-year run stays bounded in memory.
    estimates = []
    for size in range(0, draws, 256):
        indices = rng.integers(len(samples), size=(min(256, draws - size), len(samples)))
        estimates.extend(samples[indices].sum(axis=1) / weights[indices].sum(axis=1))
    return np.quantile(estimates, [.025, .975]).tolist()


def _design(panel: pd.DataFrame, oracle: bool) -> np.ndarray:
    columns = [np.ones(len(panel)), panel.da_lmp.to_numpy(dtype=float)]
    for column, levels in (("local_hour", range(1, 24)), ("day_of_week", range(1, 7)),
                           ("month", range(2, 13))):
        columns.extend(panel[column].eq(level).to_numpy(dtype=float) for level in levels)
    if oracle:
        columns.append(panel.oracle_event.to_numpy(dtype=float))
    return np.column_stack(columns)


def _error_summary(scored: pd.DataFrame, cluster: str, study: dict) -> dict:
    if scored.empty:
        return {"hours": 0, "clusters": 0, "baseline_mae": None, "oracle_mae": None,
                "mae_improvement": None, "improvement_ci95": None}
    groups = scored.groupby(cluster).improvement.agg(["sum", "size"])
    return {
        "hours": len(scored), "clusters": len(groups),
        "baseline_mae": float(scored.baseline_error.mean()), "oracle_mae": float(scored.oracle_error.mean()),
        "mae_improvement": float(scored.improvement.mean()),
        "improvement_ci95": _mean_ci(groups["sum"].to_numpy(), study["bootstrap_draws"], study["seed"],
                                      groups["size"].to_numpy()),
    }


def oracle_benchmark(train: pd.DataFrame, evaluation: pd.DataFrame, config: dict) -> dict:
    """Fit two identical calendar/LMP models, with one additional oracle feature."""
    study = config["study"]
    train = train[train.study_eligible]
    evaluation = evaluation[evaluation.study_eligible]
    training_events = train.loc[train.oracle_event.eq(1), "event_window"].nunique()
    if training_events < 2 or evaluation.empty:
        return {"gate": "inconclusive", "reason": "Insufficient eligible training events or evaluation hours"}

    result = {}
    x_train, z_train = _design(train, False), _design(train, True)
    x_eval, z_eval = _design(evaluation, False), _design(evaluation, True)
    event_rows = evaluation[evaluation.oracle_event.eq(1)]
    largest_event = None
    if not event_rows.empty:
        largest_event = event_rows.spread.abs().groupby(event_rows.event_window).max().idxmax()
    for target in OUTCOMES:
        baseline = np.linalg.lstsq(x_train, train[target], rcond=None)[0]
        oracle = np.linalg.lstsq(z_train, train[target], rcond=None)[0]
        scored = evaluation[["event_window", "delivery_date", "oracle_event"]].copy()
        scored["baseline_error"] = np.abs(evaluation[target] - x_eval @ baseline)
        scored["oracle_error"] = np.abs(evaluation[target] - z_eval @ oracle)
        scored["improvement"] = scored.baseline_error - scored.oracle_error
        result[target] = {
            "all_hours": _error_summary(scored, "delivery_date", study),
            "event_hours": _error_summary(scored[scored.oracle_event.eq(1)], "event_window", study),
            "excluding_largest_event": _error_summary(scored[scored.event_window.ne(largest_event)], "delivery_date", study),
        }
    all_hours = result["spread"]["all_hours"]
    events = result["spread"]["event_hours"]
    trimmed = result["spread"]["excluding_largest_event"]
    if events["clusters"] < study["min_holdout_events"] or all_hours["improvement_ci95"] is None:
        gate = "inconclusive"
    elif all_hours["improvement_ci95"][0] > 0:
        gate = "pass" if trimmed["mae_improvement"] > 0 else "no_pass"
    elif all_hours["improvement_ci95"][1] > 0:
        gate = "inconclusive"
    else:
        gate = "no_pass"
    return {"gate": gate, "training_events": training_events, "largest_event_removed": largest_event, **result}


def _windows(panel: pd.DataFrame, ledger: pd.DataFrame, config: dict, *, realized_load: bool,
             exclude_disruptions: bool) -> dict:
    study = config["study"]
    pre, post = study["pre_hours"], study["post_hours"]
    before = pd.Timedelta(hours=pre)
    after = pd.Timedelta(hours=post - 1)
    controls = panel[panel.control_eligible]
    windows = {}
    used_controls = set()
    for event in ledger[ledger.post_start_utc.notna()].sort_values("post_start_utc").itertuples():
        onset, post_start = event.onset_utc, event.post_start_utc
        post_end = post_start + after
        pre_end = onset.floor("h") - pd.Timedelta(hours=1)
        pre_start = onset.floor("h") - before
        pre_block = panel.loc[pre_start:pre_end]
        post_block = panel.loc[post_start:post_end]
        if len(pre_block) != pre or len(post_block) != post:
            continue
        case = pd.concat([pre_block, post_block])
        columns = ["da_lmp", "temperature_c", "dewpoint_c"]
        if case[columns].isna().any().any() or pre_block.load_mw.isna().any():
            continue
        if realized_load and case.load_mw.isna().any():
            continue
        if exclude_disruptions and not case.grid_disruption.eq("no").all():
            continue
        anchor = post_block.iloc[0]
        season = (anchor.month % 12) // 3
        pool = controls[
            controls.local_hour.eq(anchor.local_hour) & controls.day_of_week.eq(anchor.day_of_week)
            & controls.split.eq(anchor.split) & ((controls.month % 12) // 3).eq(season)
        ]
        candidates = []
        features = list(post_block[columns].mean()) + [pre_block.load_mw.mean()]
        # Fixed scale in physical units gives a repeatable, outcome-free distance.
        scales = [20.0, 5.0, 5.0, 10000.0]
        if realized_load:
            features.append(post_block.load_mw.mean())
            scales.append(10000.0)
        for t in pool.index:
            shift = t - post_start
            left = controls.loc[pre_start + shift:pre_end + shift]
            right = controls.loc[t:t + after]
            if len(left) != pre or len(right) != post:
                continue
            block = pd.concat([left, right])
            if (block.split.ne(anchor.split).any() or block[columns].isna().any().any()
                    or left.load_mw.isna().any() or (realized_load and right.load_mw.isna().any())):
                continue
            if exclude_disruptions and not block.grid_disruption.eq("no").all():
                continue
            values = list(right[columns].mean()) + [left.load_mw.mean()]
            if realized_load:
                values.append(right.load_mw.mean())
            distance = float(np.linalg.norm((np.asarray(values) - features) / scales))
            candidates.append((distance, t, block))
        for _, time, block in sorted(candidates, key=lambda candidate: candidate[:2]):
            dates = set(block.delivery_date)
            if dates.isdisjoint(used_controls):
                used_controls.update(dates)
                windows[event.event_id] = (case, block, time)
                break
    return windows


def _effects(windows: dict, config: dict) -> dict:
    study = config["study"]
    pre, post = study["pre_hours"], study["post_hours"]
    results = {"matched_events": len(windows), "matches": {event: str(pair[2]) for event, pair in windows.items()}}
    for target in OUTCOMES:
        if not windows:
            results[target] = None
            continue
        differences = np.stack([case[target].to_numpy() - control[target].to_numpy()
                                for case, control, _ in windows.values()])
        effects = differences[:, pre:].mean(axis=1) - differences[:, :pre].mean(axis=1)
        results[target] = {
            "post_minus_pre_usd_per_mwh": float(effects.mean()),
            "ci95": _mean_ci(effects, study["bootstrap_draws"], study["seed"]),
            "pre_hour_differences": differences[:, :pre].mean(axis=0).tolist(),
            "post_hour_differences": differences[:, -post:].mean(axis=0).tolist(),
        }
    return results


def price_response(panel: pd.DataFrame, ledger: pd.DataFrame, config: dict) -> dict:
    main = _windows(panel, ledger, config, realized_load=False, exclude_disruptions=False)
    spike = max(main, key=lambda event: main[event][0].spread.abs().max(), default=None)
    return {
        "main": _effects(main, config),
        "excluding_grid_disruptions": _effects(_windows(panel, ledger, config, realized_load=False,
                                                       exclude_disruptions=True), config),
        "including_realized_load": _effects(_windows(panel, ledger, config, realized_load=True,
                                                    exclude_disruptions=False), config),
        "largest_spike_event_removed": spike,
        "excluding_largest_spike": _effects({k: v for k, v in main.items() if k != spike}, config),
    }
