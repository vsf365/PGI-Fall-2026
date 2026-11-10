from datetime import date, datetime, time, timedelta, timezone
import json
import tomllib
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from requests import Response

from src.backtests.pjm_oracle import label_windows, oracle_benchmark, price_response
from src.ingest import pjm
from src.ingest.pilot import fetch, load_manifest, market_chunks, missing_notice_pages, pin
from src.labels.pjm_events import _classification, notice_candidates
from src.normalize.market import normalize_lmps
from src.normalize.pjm_panel import available_at_cutoff, hourly_panel
from src.pjm_pilot import DEFAULT_CONFIG, build


@pytest.fixture
def config():
    return tomllib.loads(DEFAULT_CONFIG.read_text())


def pin_test(root, kind, data, suffix=".json"):
    return pin(root, kind, data, suffix, "fixture", "2026-10-25T13:00:00Z")


def test_missing_or_future_availability_does_not_pass_cutoff():
    cutoff = pd.Timestamp("2023-01-05T18:00:00Z")
    assert not available_at_cutoff(pd.NaT, cutoff)
    assert not available_at_cutoff(pd.Timestamp("2023-01-05T19:00:00Z"), cutoff)
    assert available_at_cutoff(cutoff, cutoff)


def test_market_queries_split_at_utc_year_and_keep_all_delivery_hours():
    chunks = list(market_chunks(date(2023, 12, 31), date(2024, 1, 2),
                                "America/New_York", 7, date(2026, 11, 8)))
    assert len(chunks) == 2
    assert chunks[0][0].hour == 5 and chunks[-1][-1].hour == 4
    assert chunks[1][0] - chunks[0][-1] == timedelta(hours=1)
    assert sum(int((last - first).total_seconds() / 3600) + 1 for first, last in chunks) == 48


@pytest.mark.parametrize("today", [date(2026, 7, 10), date(2026, 11, 10)])
def test_market_queries_split_at_the_eastern_archive_boundary(today):
    cutoff = today - timedelta(days=731)
    chunks = list(market_chunks(cutoff - timedelta(days=2), cutoff + timedelta(days=3),
                               "America/New_York", 7, today))
    boundary = datetime.combine(cutoff, time.min, ZoneInfo("America/New_York")).astimezone(timezone.utc)
    assert boundary in {first for first, _ in chunks}
    assert not any(first < boundary <= last for first, last in chunks)
    assert sum(int((last - first).total_seconds() / 3600) + 1 for first, last in chunks) == 120
    assert all(second[0] - first[1] == timedelta(hours=1) for first, second in zip(chunks, chunks[1:]))


def test_pjm_download_paginates_using_the_header_total(monkeypatch):
    starts = []

    def get_page(url, *, params, **kwargs):
        start = params["startRow"]
        starts.append(start)
        response = Response()
        response.status_code = 200
        response.headers["X-TotalRows"] = "2"
        response._content = json.dumps([{"pnode_id": start}]).encode()
        return response

    monkeypatch.setattr(pjm.requests, "get", get_page)
    rows = pjm._paginated_fetch("da_hrl_lmps", {"download": "true"},
                                api_key="fixture", row_count=10, rate_sleep=0)
    assert starts == [1, 2]
    assert rows == [{"pnode_id": 1}, {"pnode_id": 2}]


def test_ghcnh_fills_station_hours_with_quality_accepted_celsius(tmp_path, config):
    station = config["weather"]["stations"][0]
    config["study"]["min_weather_stations"] = 1
    legacy = pin_test(tmp_path, "weather", pd.DataFrame([{
        "STATION": station, "DATE": "2025-09-01T04:10:00", "TMP": "+0200,1", "DEW": "+0100,1",
    }]).to_csv(index=False).encode(), ".csv")
    base = {
        "STATION": config["weather"]["ghcnh_stations"][station],
        "temperature": "21.5", "dew_point_temperature": "11.5",
        "temperature_Source_Code": "223", "dew_point_temperature_Source_Code": "223",
        "temperature_Quality_Code": "1", "dew_point_temperature_Quality_Code": "1",
    }
    rows = [
        {**base, "DATE": "2025-09-01T04:50:00"},
        {**base, "DATE": "2025-09-01T05:50:00"},
        {**base, "DATE": "2025-09-01T06:50:00", "temperature_Quality_Code": "s"},
        {**base, "DATE": "2025-09-01T07:50:00", "temperature_Quality_Code": "5"},
        {**base, "DATE": "2025-09-01T08:50:00", "temperature": "-9999"},
    ]
    ghcnh = pin_test(tmp_path, "weather", pd.DataFrame(rows).to_csv(sep="|", index=False).encode(), ".psv")
    panel = hourly_panel(tmp_path, {"weather": [ghcnh, legacy]}, config, date(2025, 9, 1), date(2025, 9, 2))
    assert panel.iloc[0].temperature_c == 20 and panel.iloc[0].dewpoint_c == 10
    assert panel.iloc[0].weather_source_ids == legacy["id"]
    assert panel.iloc[1].temperature_c == 21.5 and panel.iloc[1].dewpoint_c == 11.5
    assert panel.iloc[1].weather_source_ids == ghcnh["id"]
    assert panel.iloc[2:5].temperature_c.isna().all()


def test_weather_fetch_uses_ghcnh_after_isd_retirement(tmp_path, config, monkeypatch):
    urls = []

    def get_weather(session, url, **kwargs):
        urls.append(url)
        response = Response()
        response.status_code = 200
        response.url = url
        station = url.rsplit("/", 1)[1].split("_")[1]
        response._content = (
            f"STATION|DATE|temperature|dew_point_temperature\n{station}|2026-01-01T00:00:00|1.5|-2.5\n"
        ).encode()
        return response

    monkeypatch.setattr("requests.Session.get", get_weather)
    fetch(tmp_path, config, date(2026, 1, 1), date(2026, 1, 2), "weather")
    assert len(urls) == 4 and all("/2026/psv/GHCNh_" in url for url in urls)
    sources = load_manifest(tmp_path)
    assert all(entry["path"].endswith(".psv") for entry in sources)


def test_supplement_detects_missing_archive_pages_and_queues_possible_trip(tmp_path, config):
    base = "https://www.nrc.gov/documents-reports/document-collections/events-reports-associated-with/event-notification-reports"
    index = pin(tmp_path, "nrc_notice_index", (
        f'<a href="{base}/2025/20251231en">31</a>'
    ).encode(), ".html", f"{base}/2025", "2026-10-25T13:00:00Z", year=2025)
    grouped = {"nrc_notice_index": [index]}
    start, end = date(2025, 12, 30), date(2026, 1, 1)
    assert missing_notice_pages(tmp_path, grouped, config, start, end) == ["2025-12-31", "index:2026"]
    page = b"""<div>Power Reactor Event Number: 58123 Facility: Example Region: 1
    Unit: [2] [] [] Event Date: 12/30/2025 Event Text Reactor trip</div>
    <div>Event Number: 58124 Rep Org: Other Event Date: 12/30/2025</div>"""
    notice = pin(tmp_path, "nrc_notice", page, ".html", f"{base}/2025/20251231en",
                 "2026-10-25T13:00:00Z", report_date="2025-12-31")
    grouped["nrc_notice"] = [notice]
    assert missing_notice_pages(tmp_path, grouped, config, start, end) == ["index:2026"]
    [candidate] = notice_candidates(page, notice)
    assert candidate["event_id"] == "EN:58123"
    assert candidate["event_date"] == date(2025, 12, 30)
    assert candidate["candidate_source_id"] == notice["id"] and candidate["docket"] == ""


def test_mixed_prices_use_feed_and_row_specific_values():
    record = {"datetime_beginning_utc": "2023-01-05T12:00:00", "pnode_id": 1}
    prices = normalize_lmps(raw_records=[
        {**record, "source_feed": "da_hrl_lmps", "total_lmp_da": 31},
        {**record, "source_feed": "rt_hrl_lmps", "total_lmp_rt": 47},
    ], save=False)
    assert prices.set_index("market").value.to_dict() == {"DA": 31, "RT": 47}


@pytest.mark.parametrize(("start", "end", "hours"), [
    (date(2023, 3, 12), date(2023, 3, 13), 23),
    (date(2023, 11, 5), date(2023, 11, 6), 25),
])
def test_delivery_grid_keeps_dst_hours_and_missing_prices(tmp_path, config, start, end, hours):
    panel = hourly_panel(tmp_path, {}, config, start, end)
    assert len(panel) == hours
    assert panel.index.is_unique
    assert panel.local_hour.eq(1).sum() == (2 if hours == 25 else 1)
    assert panel.local_hour.eq(2).sum() == (0 if hours == 23 else 1)
    assert panel.spread.isna().all()


def test_panel_pins_posted_vintage_and_flags_conflicting_outcome(tmp_path, config):
    timestamp = "2023-01-05T05:00:00"
    base = {"datetime_beginning_utc": timestamp, "pnode_id": 1}
    da = pin_test(tmp_path, "da", json.dumps([
        {**base, "total_lmp_da": 25, "version_nbr": 1},
        {**base, "total_lmp_da": 30, "version_nbr": 2, "row_is_current": True},
    ]).encode())
    rt = pin_test(tmp_path, "rt", json.dumps([
        {**base, "total_lmp_rt": 31, "version_nbr": 2, "row_is_current": True},
        {**base, "total_lmp_rt": 35, "version_nbr": 2, "row_is_current": True},
        {**base, "datetime_beginning_utc": "2023-01-05T06:00:00", "total_lmp_rt": 12},
    ]).encode())
    audit = pin_test(tmp_path, "calendar", (
        "delivery_date,da_available_at,da_clock_type,da_version,da_evidence,"
        "event_review_complete,event_evidence,grid_disruption\n"
        "2023-01-05,2023-01-04T13:30:00-05:00,verified_by,1,posting log,yes,scram review,no\n"
    ).encode(), ".csv")
    panel = hourly_panel(tmp_path, {"da": [da], "rt": [rt], "calendar": [audit]}, config,
                         date(2023, 1, 5), date(2023, 1, 6))
    first, second = panel.iloc[0], panel.iloc[1]
    assert first.da_lmp == 25 and first.da_archive_lmp == 30 and first.da_conflict
    assert pd.isna(first.rt_lmp) and first.rt_conflict
    assert pd.isna(second.da_lmp) and pd.isna(second.spread) and second.rt_lmp == 12


def test_settlement_prices_cannot_supply_the_benchmarks_posted_vintage(tmp_path, config):
    settled = pin_test(tmp_path, "settled", (
        "datetime_beginning_utc,pnode_id,pnode_name,total_lmp_da,total_lmp_rt\n"
        "1/5/2023 5:00:00 AM,1,PJM,25,31\n"
        "1/5/2023 5:00:00 AM,2,Other,90,100\n"
    ).encode(), ".csv")
    audit = pin_test(tmp_path, "calendar", (
        "delivery_date,da_available_at,da_clock_type,da_version,da_evidence,"
        "event_review_complete,event_evidence,grid_disruption\n"
        "2023-01-05,2023-01-04T13:30:00-05:00,verified_by,unversioned,posting log,yes,scram review,no\n"
    ).encode(), ".csv")
    panel = hourly_panel(tmp_path, {"settled": [settled], "calendar": [audit]}, config,
                         date(2023, 1, 5), date(2023, 1, 6))
    first = panel.iloc[0]
    assert first.settled_spread == 6 and first.settled_source_ids == settled["id"]
    assert panel[["da_lmp", "da_available_at", "rt_lmp", "spread"]].isna().all().all()


@pytest.mark.parametrize(("classification", "onset", "exclusion"), [
    ("planned", "2023-01-05T12:42:00-05:00", "planned"),
    ("ambiguous", "", "ambiguous"),
    ("unplanned", "2023-01-05", "onset_date_only"),
    ("unplanned", "2023-01-05T12:42:00-05:00", ""),
])
def test_ledger_excludes_planned_ambiguous_and_date_only(classification, onset, exclusion):
    when = date(2023, 1, 5)
    candidate = {"event_id": "2023:1", "event_date": when, "docket": "05000000",
                 "retracted": False, "notification_number": "111"}
    review = {"classification": classification, "onset_at": onset, "initial_power_pct": "100",
              "generation_loss_evidence": "Generator tripped", "notice_source_id": "notice",
              "reviewer": "reviewer", "reason": "Reviewed notice", "known_in_advance": "no"}
    mapping = pd.DataFrame([{"docket": "05000000", "unit_name": "unit", "plant_code": "1",
                             "generator_id": "1", "effective_from": date(2023, 1, 1),
                             "effective_to": date(2024, 1, 1)}])
    generators = pd.DataFrame([{"year": 2023, "plant_code": "1", "generator_id": "1",
                                "balancing_authority": "PJM", "capacity_mw": 990, "source_id": "eia"}])
    statuses = pd.DataFrame([("unit", date(2023, 1, 4), 100, "nrc"), ("unit", when, 0, "nrc")],
                            columns=["unit", "date", "power", "source_id"])
    row = _classification(candidate, review, mapping, generators, statuses, {"notice": {"111"}}, 90)
    assert row["eligible"] == (exclusion == "")
    assert row["exclusion"] == exclusion and row["status_corroborated"]
    if not exclusion:
        assert row["onset_utc"] == pd.Timestamp("2023-01-05T17:42:00Z")


def test_only_six_complete_hours_see_the_oracle(tmp_path, config):
    panel = hourly_panel(tmp_path, {}, config, date(2023, 1, 5), date(2023, 1, 6))
    panel["da_lmp"] = 25
    panel["rt_lmp"] = 35
    panel["event_review_complete"] = True
    panel["da_available_at"] = pd.Timestamp("2023-01-04T18:00:00Z")
    ledger = pd.DataFrame([{"event_id": "2023:1", "event_date": date(2023, 1, 5), "eligible": True,
                            "exclusion": "", "onset_utc": pd.Timestamp("2023-01-05T17:42:00Z")}])
    labeled, _ = label_windows(panel, ledger, config)
    assert list(labeled.index[labeled.oracle_event.eq(1)]) == list(
        pd.date_range("2023-01-05T18:00:00Z", periods=6, freq="h")
    )
    assert not labeled.at[pd.Timestamp("2023-01-05T17:00:00Z"), "study_eligible"]
    assert not labeled.control_eligible.any()
    panel["da_available_at"] = pd.NaT
    labeled, excluded = label_windows(panel, ledger, config)
    assert labeled.oracle_event.sum() == 0
    assert excluded.analysis_exclusion.iloc[0] == "day_ahead_availability_not_before_trip"


def test_pinned_replay_and_holdout_guard(tmp_path, config):
    root = tmp_path / "raw"
    source = pin_test(root, "da", b"[]")
    a, b = tmp_path / "first", tmp_path / "second"
    build(root, a, DEFAULT_CONFIG, config, "smoke")
    build(root, b, DEFAULT_CONFIG, config, "smoke")
    for name in ("panel.csv", "events.csv", "coverage.json", "input_manifest.json", "build.json"):
        assert (a / name).read_bytes() == (b / name).read_bytes()
    assert not json.loads((a / "coverage.json").read_text())["study_ready"]
    with pytest.raises(ValueError, match="Freeze"):
        build(root, tmp_path / "held", DEFAULT_CONFIG, config, "holdout")
    (root / source["path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed pinned"):
        load_manifest(root)


def test_heldout_oracle_detects_real_increment_on_synthetic_data(config):
    def sample(start):
        hours = pd.date_range(start, periods=24 * 12, freq="h")
        affected = hours.hour.isin([12, 13, 14, 15, 16, 17]) & (hours.day % 2 == 0)
        date_str = hours.strftime("%Y-%m-%d")
        return pd.DataFrame({
            "study_eligible": True, "oracle_event": affected.astype(int), "delivery_date": date_str,
            "local_hour": hours.hour, "day_of_week": hours.dayofweek, "month": hours.month,
            "event_window": np.where(affected, date_str, ""), "da_lmp": 25.0,
            "spread": np.where(affected, 10.0, 0.0), "rt_lmp": np.where(affected, 35.0, 25.0),
        })

    train, holdout = sample("2022-01-01"), sample("2024-01-01")
    assert oracle_benchmark(train, holdout, config)["gate"] == "inconclusive"
    config["study"]["min_holdout_events"] = 4
    report = oracle_benchmark(train, holdout, config)
    assert report["gate"] == "pass"
    assert report["spread"]["event_hours"]["oracle_mae"] < 1e-10
    assert report["spread"]["excluding_largest_event"]["mae_improvement"] > 0


def test_matched_price_response_uses_clean_control_and_reports_pretrends(tmp_path, config):
    panel = hourly_panel(tmp_path, {}, config, date(2023, 1, 1), date(2023, 1, 29))
    panel["da_lmp"] = panel["rt_lmp"] = 25.0
    panel["spread"] = 0.0
    panel["event_review_complete"] = True
    panel["da_available_at"] = pd.Timestamp("2022-12-31T18:30:00Z")
    panel["grid_disruption"] = "no"
    panel["temperature_c"] = 5.0
    panel["dewpoint_c"] = 2.0
    panel["load_mw"] = 100000.0
    onset = pd.Timestamp("2023-01-12T17:42:00Z")
    affected = pd.date_range(onset.ceil("h"), periods=6, freq="h")
    panel.loc[affected, "spread"] = 15.0
    panel.loc[affected, "rt_lmp"] = 40.0
    panel.loc[affected, "grid_disruption"] = "yes"
    ledger = pd.DataFrame([{"event_id": "2023:1", "event_date": date(2023, 1, 12),
                            "eligible": True, "exclusion": "", "onset_utc": onset}])
    panel, ledger = label_windows(panel, ledger, config)
    result = price_response(panel, ledger, config)
    assert result["main"]["matched_events"] == 1
    assert result["main"]["spread"]["post_minus_pre_usd_per_mwh"] == 15
    assert result["main"]["spread"]["pre_hour_differences"] == [0] * 6
    assert result["excluding_grid_disruptions"]["matched_events"] == 0
