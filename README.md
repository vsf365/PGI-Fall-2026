# PJM nuclear derating research

The pilot tests the response of PJM RTO real-time prices and the real-time minus
day-ahead spread to documented unplanned reactor trips. The oracle forecast
measures how much advance knowledge of those trips would have improved price
predictions. A passing holdout is the gate for work on leading indicators.

## Collect and replay the January 2023 pilot

Run from the repository root with Python 3.11 or later. Install the dependencies
in `requirements.txt` and set `PJM_API_KEY` in the ignored `configs/.env` or
your environment. Use a single writer per input directory.

```sh
pilot=data/raw/pilot/smoke
python -m src.pjm_pilot fetch --period smoke --root "$pilot" --group nrc
python -m src.pjm_pilot fetch --period smoke --root "$pilot" --group eia
python -m src.pjm_pilot fetch --period smoke --root "$pilot" --group weather
python -m src.pjm_pilot fetch --period smoke --root "$pilot" --group pjm
python -m src.pjm_pilot queue --period smoke --root "$pilot" --output data/interim/pilot/smoke-review
```

The same workflow works for `development` (2018–2022), `validation` (2023),
and `holdout` (2024–2025). Use a separate input directory for each period.
Dates in [the pilot configuration](configs/pjm_pilot.toml) are local delivery
dates and the upper bound is exclusive. Snapshot bytes and the versioned
`inputs.json` are stored in the input directory. The manifest records SHA-256,
source, and retrieval time. Keep the manifest and its snapshots together.

The pinned NRC scram workbook runs through May 12, 2025. Before generating the
holdout review queue, collect the remaining official event notices:

```sh
python -m src.pjm_pilot fetch --period holdout --root data/raw/pilot/holdout --group notices
```

The supplemental `EN:` candidates need a verified docket and manual
classification. Mark an unrelated notice `not_a_trip` with the pinned notice
as evidence. The coverage check requires the archived reports listed for
the supplement window before the holdout can be scored.

To import an official export or completed review file:

```sh
python -m src.pjm_pilot pin --root "$pilot" da /path/to/da.csv \
  --source https://dataminer2.pjm.com/feed/da_hrl_lmps \
  --retrieved-at YYYY-MM-DDTHH:MM:SSZ
```

Use the actual download timestamp and a source URL without credentials. Market
exports can be CSV or JSON. Include `datetime_beginning_utc` on all three feeds,
`pnode_id` and the total LMP on DA and RT, and `load_area` and `mw` on load.
Include `version_nbr` and `row_is_current` when the feed provides them, and
export the originally posted DA version. Pin EIA and NRC annual files with
`--year`. Use `--replace` to select a revised review, crosswalk, or calendar
while preserving the old snapshot bytes.

## Review the evidence

The queue command writes the scram candidates, EIA nuclear generators, and
three templates. Fill and pin `event_review.csv`, `crosswalk.csv`, and
`calendar.csv` using kinds `event_review`, `crosswalk`, and `calendar`.

- **Crosswalk.** Map the NRC docket and status report name to the EIA plant code
  and generator ID. Include the evidence and effective dates `[from, to)`.
  Include confirmed non-PJM candidates so their event days remain eligible as
  controls. EIA annual net summer capability is an approximate scale for lost
  MW. The benchmark uses the binary event indicator.
- **Events.** Run `python -m src.pjm_pilot notice --root "$pilot" 2023:1` to pin
  a candidate's official notification, then put its printed source ID in the
  review. Enter the timestamp with the source offset, initial power, generation
  loss evidence, reviewer, reason, and whether the loss was known in advance.
  Classify the event as `unplanned`, `planned`, or `ambiguous`. Additional
  known planned or ambiguous events can use a `local:` ID with `event_date`,
  `docket`, `reactor`, and `notice_url` columns. The ledger retains all
  exclusions and flags corroboration from the daily status series.
- **Calendar.** Record each delivery day's earliest evidenced DA posting or a
  documented conservative upper bound, with `da_clock_type` set to `exact` or
  `verified_by`, the matching version, and an evidence reference. Use
  `unversioned` only for an export verified to contain the version available
  at the cutoff. PJM documents the normal posting schedule, but delays and
  reposts require date-specific evidence before using a clock or price
  vintage. Mark `event_review_complete=yes` only after reviewing candidate and
  planned events for that date, and document the review in `event_evidence`.
  Record `grid_disruption=yes/no` where reviewed and leave unknowns blank.

| Input | Observation or delivery time | Publication and revisions |
| --- | --- | --- |
| [NRC status](https://www.nrc.gov/documents-reports/nrc-data) | Daily report date and reported reactor power percent. | Historical publication time stays null. The report date carries no intraday onset. |
| [NRC scrams and notices](https://www.nrc.gov/facilities-safety/operating-reactors/reactor-safety-information-topics/operating-reactor-scram-trending) | Workbook date selects candidates. The notice can establish an exact event time and initial power. | Event time, NRC notification time, and public availability are separate. The pilot uses notices retrospectively. |
| [PJM DA and RT LMP](https://dataminer2.pjm.com/feed/da_hrl_lmps/definition) | Hour beginning in UTC, PJM-RTO node 1. | The calendar supplies evidence for the DA cutoff and vintage. RT uses the latest resolvable price as an outcome. |
| [PJM metered load](https://learn.pjm.com/three-priorities/keeping-the-lights-on/data-miner-faqs/what-are-the-differences-between-the-different-types-of-load-data) | Hourly metered RTO demand. | Lagged publication. Use realized load as a retrospective control. |
| [NOAA Global Hourly](https://www.ncei.noaa.gov/products/land-based-station/integrated-surface-database) | Temperature and dew point averaged over the fixed regional stations, using the last quality-accepted pair per station in each UTC hour. | Archive vintages support retrospective controls. Historical availability is unverified. |
| [EIA-860](https://www.eia.gov/electricity/data/eia860/) | Annual generator inventory and balancing authority. | Snapshot and crosswalk document the annual vintage and effective period. |

## Build, freeze, and score

```sh
python -m src.pjm_pilot build --period smoke --root "$pilot" --output data/curated/pilot/smoke
python -m src.pjm_pilot build --period smoke --root "$pilot" --output data/curated/pilot/smoke-replay
```

Each offline build emits `panel.csv`, `events.csv`, `coverage.json`,
`input_manifest.json`, and a checksummed `build.json`. A build can report
gaps before all inputs arrive. The coverage report distinguishes historical DA
prices from prices with verified availability, reports price conflicts, and
shows event exclusions. The study requires 99% verified DA and RT coverage,
90% coverage of the control series and study-eligible hours, and the required
source files. Run the same build for development and validation before scoring
validation with the command below.

```sh
out=data/curated/pilot
python -m src.pjm_pilot study --period validation --development "$out/development" \
  --evaluation "$out/validation" --output "$out/validation-results.json"
python -m src.pjm_pilot freeze --smoke "$out/smoke" --replay "$out/smoke-replay" \
  --development "$out/development" --validation "$out/validation" \
  --holdout-root data/raw/pilot/holdout --output "$out/freeze.json"
python -m src.pjm_pilot build --period holdout --root data/raw/pilot/holdout \
  --freeze "$out/freeze.json" --output "$out/holdout"
python -m src.pjm_pilot study --period holdout --development "$out/development" \
  --evaluation "$out/holdout" --freeze "$out/freeze.json" --output "$out/holdout-results.json"
```

The retrospective price response compares six complete hours before and after
an isolated trip with a matched control window. It reports pre-event differences
and sensitivities for coincident disruptions, realized load, and the largest
price spike. The forecast comparison fits on the development sample and reports
MAE in dollars per MWh across all eligible hours and separately during trips.
It uses paired cluster bootstrap intervals by operating day or event.
The holdout gate requires at least ten qualifying events, a positive lower
confidence bound for the overall spread MAE improvement, and a positive
improvement after dropping the event with the largest absolute spread.
Sparse or statistically unclear results are inconclusive.

Run `python3 -m pytest` for parser, timing, event, replay, and benchmark checks.
