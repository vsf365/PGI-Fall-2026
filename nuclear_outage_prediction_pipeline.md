# Nuclear Availability and Power-Market Information Pipeline

## 1. Objective

The [PJM pilot workflow](README.md) is the first implementation of this blueprint. It starts with documented unplanned trips and tests their association with real-time prices and the real-time minus day-ahead spread. The perfect-information benchmark determines whether to proceed to outage forecasting and environmental leading indicators. The broader phases below describe later work.

Build a reproducible, point-in-time research system that tests whether public environmental and grid data can predict nuclear-reactor deratings or outages **before the information is fully incorporated into wholesale electricity prices or nuclear-exposed equities**.

The project has two distinct goals:

1. **Operational forecasting:** predict reactor output, derating probability, outage onset, outage duration, and recovery.
2. **Information-timing research:** test whether the forecast contains incremental information after controlling for already-public plant-status, weather, load, prices, and market conditions.

The second goal is the investment-relevant one. A high-accuracy outage model is not automatically tradable if the same information is already visible in NRC status, ISO schedules, prices, or company disclosures.

This document is a research and engineering blueprint, not a recommendation to trade.

---

## 2. Recommended first scope

Start narrow enough to create a clean benchmark:

- Geography: **PJM**, with a second market such as **MISO** for out-of-sample testing.
- Assets: commercial U.S. reactors with reasonably complete historical operating-status data.
- Frequency: daily for the first model; hourly for the market-impact layer.
- History: obtain the longest consistent history available, then use a strict training/validation/test split by date.
- Initial event: next-day **material derating**, for example output below 90% of nameplate capacity, plus separate thresholds at 75%, 50%, and 20%.
- Initial market target: next-day day-ahead versus real-time price spread, adjusted for load, weather, fuel, renewable output, transmission conditions, and contemporaneous public status.

Do not begin by modeling every reactor, every ISO, and every market simultaneously. A smaller panel makes timestamp errors, plant identity problems, and false economic conclusions easier to diagnose.

---

## 3. System architecture

```text
Public raw sources
  ├─ NRC reactor status and power
  ├─ EIA plant, reactor, capacity, ownership, generation
  ├─ NOAA observations and forecasts
  ├─ USGS streamflow, water temperature, gage metadata
  ├─ ISO/RTO load, generation, outages, schedules, LMPs
  └─ SEC company filings and market data
          │
          ▼
Raw immutable landing zone
  - original files/responses
  - retrieval timestamp
  - source URL/query
  - checksum and schema version
          │
          ▼
Normalized source tables
  - reactor master
  - station/site master
  - weather observations/forecasts
  - hydrology observations
  - reactor status/output
  - market observations
  - company/asset ownership
          │
          ▼
Point-in-time feature store
  - one row per reactor × forecast time
  - only data available by the forecast cutoff
  - feature provenance and missingness flags
          │
          ├─ Reactor event models
          ├─ Output forecasts
          ├─ Regional MW shock aggregation
          ├─ Price-response tests
          └─ Equity/event-study tests
```

### Suggested repository layout

```text
data/
  raw/{nrc,eia,noaa,usgs,iso,sec,market}/
  interim/
  curated/
  manifests/
src/
  ingest/
  normalize/
  features/
  labels/
  models/
  market_mapping/
  backtests/
tests/
configs/
notebooks/
reports/
```

Keep raw data immutable. If a source revises history, save the revised retrieval as a new raw object and record the change rather than overwriting the prior file.

---

## 4. Source inventory

### 4.1 Nuclear Regulatory Commission

Primary source: [NRC Power Reactor Status Reports](https://www.nrc.gov/reading-rm/doc-collections/event-status/reactor-status/index).

Use NRC data for the highest-frequency observable reactor-status history available from the agency:

- reactor name and unit identifier;
- operating, shutdown, startup, reduced-power, and other status fields;
- reported power level or percentage of maximum dependable capacity;
- status date/time and reporting date;
- historical daily files and data dictionary;
- outage start, return-to-service, and status transitions where available.

Important checks:

- Identify the exact publication timestamp, not just the operating date.
- Preserve the original text/file and parser version.
- Distinguish “zero output,” “unknown,” “offline,” “testing,” and “not reported.”
- Treat NRC status as a **public-information control variable**. If the model uses it, test whether environmental data adds incremental value beyond it.
- Build a lagged version that only uses the last status report known at the forecast cutoff.

### 4.2 Energy Information Administration

Primary sources: [EIA API documentation](https://www.eia.gov/opendata/documentation.php), [nuclear generation and capacity](https://www.eia.gov/nuclear/generation/), [EIA electric-power data guide](https://www.eia.gov/electricity/data/guide/pdf/guide.pdf).

Use EIA for slower-moving reference and validation data:

- plant and reactor names, locations, latitude/longitude;
- nameplate capacity and operating status;
- generator/unit identifiers;
- owner, operator, and parent-company fields where available;
- cooling technology and cooling-water information;
- monthly/annual generation and capacity factors;
- state, balancing-area, and plant-level metadata.

EIA is generally not the sole source for intraday availability. Use it to construct the plant master, validate capacity, create long-run baselines, and map the physical asset to owners and market regions.

### 4.3 NOAA

Primary sources: [NOAA Climate Data Online API](https://www.ncei.noaa.gov/cdo-web/webservices/v2), [NOAA National Weather Service API](https://www.weather.gov/documentation/services-web-api), [NOAA climate and weather data portal](https://www.ncei.noaa.gov/).

Candidate variables:

- air temperature: current, daily maximum/minimum, wet-bulb or dew point;
- precipitation and snowfall;
- humidity, pressure, wind, cloud cover, solar radiation where available;
- heat-wave and cold-snap indicators;
- forecast temperature and precipitation by forecast issuance time;
- forecast error and forecast revision features;
- accumulated precipitation over 1, 3, 7, 14, and 30 days.

Use observations and forecasts separately. A historical forecast issued at time `t` is valid for a future interval; a later reanalysis or observed value must never be substituted into a live-trading backtest.

### 4.4 U.S. Geological Survey

Primary sources: [USGS Water Services](https://waterservices.usgs.gov/), [NWIS Web Services](https://nwis.waterservices.usgs.gov/), [USGS site inventory](https://waterdata.usgs.gov/nwis/inventory/).

Candidate variables:

- river/receiving-water temperature;
- instantaneous streamflow and discharge;
- gage height and stage;
- 1-day, 3-day, 7-day, and 30-day flow anomalies;
- low-flow indicators and percentile ranks;
- trend and volatility in river temperature;
- missingness, quality flags, and provisional/approved status.

Water measurements are site- and parameter-specific. Store parameter code, statistic code, unit, agency, site latitude/longitude, sample/observation time, retrieval time, and quality flag. Do not silently mix instantaneous values, daily means, and daily maxima.

### 4.5 ISO/RTO market data

Use the market operator’s own data as the authoritative source whenever possible. Relevant examples:

- [PJM Data Miner](https://www.pjm.com/markets-and-operations/etools/data-miner-2) and [PJM API guide](https://www.pjm.com/-/media/DotCom/etools/data-miner-2/data-miner-2-api-guide.pdf);
- [PJM energy-market data](https://www.pjm.com/markets-and-operations/energy);
- [CAISO OASIS historical downloader](https://oasis-bulk.caiso.com/);
- [CAISO OASIS API specification](https://www.caiso.com/documents/oasisapispecification.pdf);
- ISO-NE ISO Express, NYISO market data, MISO market reports, and SPP market data for later expansion.

Collect, where available:

- day-ahead LMPs and real-time LMPs;
- hourly and sub-hourly load and load forecasts;
- generation by fuel type and interchange;
- reserve margins, scarcity indicators, and emergency alerts;
- nuclear unit or plant schedules/outages;
- transmission constraints, congestion, and shadow prices;
- renewable generation and forecasts;
- fuel prices or other marginal-cost controls;
- market publication timestamps and revisions.

Market feeds often have retention limits, delayed endpoints, or different time conventions. Capture the operator’s time zone, UTC conversion, interval-ending convention, DST treatment, and whether the value is preliminary, final, or revised.

### 4.6 SEC, ownership, and equity data

Primary sources: [SEC EDGAR APIs](https://www.sec.gov/edgar/sec-api-documentation), [SEC company facts](https://www.sec.gov/edgar/sec-api-documentation), and issuer filings.

Use these to map:

- reactor → plant → owner → operating subsidiary → public parent;
- ownership percentage and effective date;
- operator versus economic owner;
- merchant versus regulated exposure;
- hedging, power-purchase agreements, and fuel-cost pass-through;
- plant-specific disclosures and outage commentary;
- segment revenue and generation exposure.

For listed securities, source point-in-time prices, shares, corporate actions, dividends, and delistings from a consistent market-data provider. Free sources can be used for prototyping, but record licensing and survivorship limitations. The equity layer should be treated as a separate research track because ownership, hedging, and disclosure timing can dominate the mechanical plant shock.

### 4.7 Optional supporting sources

These are useful after the core pipeline works:

- EPA ECHO or emissions data for thermal plant operating controls;
- NOAA/NWS river forecasts and drought products;
- U.S. Drought Monitor for persistent hydrologic stress;
- FERC filings and market-monitor reports for event interpretation;
- company 8-Ks, earnings releases, and investor presentations;
- transmission topology and nodal mapping from ISO files;
- electricity futures, natural-gas prices, coal prices, and regional weather contracts.

Optional sources should be added only with a clear hypothesis. More feeds do not automatically improve a model.

---

## 5. Canonical data model

Use stable internal IDs even when source identifiers change.

### 5.1 `reactor_master`

Recommended fields:

```text
reactor_id
source_nrc_id
source_eia_id
plant_name
unit_name
reactor_type
latitude
longitude
commercial_operation_date
retirement_date
nameplate_mw
max_dependable_mw
cooling_system
cooling_water_body
state
balancing_area
iso
owner_id
operator_id
valid_from
valid_to
source_retrieved_at
```

### 5.2 `station_master`

```text
station_id
source_station_id
source_system
station_type
latitude
longitude
elevation
parameters_available
water_body
valid_from
valid_to
```

### 5.3 `reactor_status_observation`

```text
reactor_id
observed_at
published_at
available_at
status_code
reported_power_pct
reported_power_mw
status_text
source_file
is_revision
quality_flag
```

`available_at` is the timestamp to use in backtests. If the source does not expose an exact timestamp, use a conservative publication-time assumption and document it.

### 5.4 `weather_observation` and `weather_forecast`

Store both the valid time and the issue time:

```text
station_id
issued_at
valid_at
retrieved_at
variable
value
unit
forecast_horizon_hours
source_model
quality_flag
```

### 5.5 `water_observation`

```text
site_id
observed_at
published_at
parameter_code
statistic_code
value
unit
approval_status
quality_flag
```

### 5.6 `market_observation`

```text
iso
market
node_or_zone
interval_start_utc
interval_end_utc
published_at
value_type
value
unit
status
source_query
```

### 5.7 `ownership_history`

```text
owner_id
issuer_id
reactor_id
ownership_pct
role
valid_from
valid_to
source_document
document_filed_at
```

---

## 6. Entity resolution and geographic mapping

This is one of the highest-risk parts of the project.

### 6.0 Make the nuclear-to-equity mapping the foundation

Before modeling signals, build a versioned security-exposure master. Every downstream feature, event study, and stock-price test should consume this master rather than maintaining its own ticker list.

Keep these relationships separate:

1. physical reactor and plant/site;
2. licensed operator and operating subsidiary;
3. direct owner and co-owner;
4. economic beneficiary or risk bearer, including regulated utilities, merchant generators, joint ventures, PPAs, and hedges;
5. tradable security representing the exposure.

Do not collapse these roles into a single `owner_ticker` field. A company may operate a reactor without owning it, own a minority share without operating it, or have an economic exposure that is partly hedged or passed through to customers.

The mapping layer should be independently testable, reviewable, and queryable as of any historical date.

### 6.1 Reactor identity

Create a manually reviewed crosswalk across NRC, EIA, ISO, SEC, and any plant-status source. Names vary by unit, plant, owner, and historical corporate structure. Do not join on free-text names alone.

#### Recommended `asset_security_exposure` table

```text
exposure_id, reactor_id, plant_id, entity_id, entity_name, entity_role,
parent_entity_id, issuer_id, issuer_name, security_id, ticker, exchange,
share_class, security_type, ownership_pct, economic_exposure_pct,
capacity_share_mw, market_exposure_type, regulated_or_merchant,
hedge_or_ppa_notes, valid_from, valid_to, announced_at, effective_at,
source_document_id, source_url, source_page_or_section, mapping_method,
mapping_confidence, review_status, last_reviewed_at
```

Suggested `entity_role` values: `licensed_operator`, `direct_owner`, `co_owner`, `leaseholder`, `power_purchaser`, `regulated_utility_parent`, `merchant_generator_parent`, `public_parent`, `private_parent`, and `government_entity`.

Suggested `market_exposure_type` values: `direct_generation`, `capacity_payment`, `regulated_rate_base`, `power_purchase_agreement`, `fuel_or_service_revenue`, `decommissioning_liability`, `indirect_parent_exposure`, and `none_or_unclear`.

#### Mapping hierarchy

Maintain normalized histories and derive the final exposure view:

```text
reactor_master
  ├─ operating_license_history
  ├─ ownership_history
  ├─ power_contract_history
  ├─ corporate_entity_history
  └─ security_master / ticker_history
```

The final view should support a point-in-time query such as:

```text
get_exposure(reactor_id, as_of_timestamp)
```

This prevents current ownership from being incorrectly applied to historical stock-price events.

#### Evidence hierarchy

Use the strongest available evidence in this order: NRC licensee and plant records; EIA ownership/operator records; company 10-K, 10-Q, 8-K, annual report, investor presentation, and subsidiary disclosures; SEC exhibits and material contracts; ISO/RTO participant records; state utility commission or FERC filings; then industry databases or news as corroboration. Name matching and search-engine inference are leads only, not final proof.

Every mapping row should cite a document and relevant page, table, exhibit, or section, while preserving both the publication date and effective date.

#### Ticker and security rules

- Store permanent issuer/security identifiers in addition to tickers because tickers change.
- Keep exchange and share class; do not merge common stock, ADRs, preferreds, or units.
- Record ticker changes, mergers, spin-offs, delistings, and corporate actions.
- Distinguish public parent, operating subsidiary, and direct asset owner.
- Keep a `no_direct_ticker` outcome when no public security has defensible exposure.
- Record whether exposure is direct, consolidated, equity-method, regulated, or merely narrative.
- Use point-in-time security identifiers for returns so delisted securities are not silently removed.

#### Confidence and QA

Use explicit states: `confirmed_direct`, `confirmed_indirect`, `probable_needs_review`, `conflicting_sources`, `no_public_security`, `historical_only`, and `retired_or_inactive`.

Automated checks should flag ownership above 100%, capacity shares above reactor capacity, overlapping contradictory intervals, a ticker mapped before the issuer existed, post-divestiture exposures, operator/owner confusion, duplicate entities, and exposures that disappear only because a ticker changed.

The first project milestone should produce a complete reactor and plant inventory, corporate-entity graph, ownership/operator timeline, ticker/security timeline, confidence-ranked exception queue, source-backed exposure table, and regression tests for future updates. Only after this mapping base is stable should stock-price relationships be interpreted.

Validation rules:

- one physical reactor must have one internal ID;
- units at the same site must remain separate;
- capacity must be versioned when uprates or nameplate conventions change;
- retired and permanently shut-down units must not be treated as missing observations;
- all aliases and source IDs should be retained.

### 6.2 Weather stations

Map each reactor to a ranked station set rather than a single arbitrary station:

1. nearest station within a documented radius;
2. stations with adequate coverage and relevant variables;
3. coastal, inland, elevation, and local microclimate filters;
4. inverse-distance or model-based interpolation;
5. station-availability and missingness features.

Use distance, bearing, elevation difference, and water-body relationship as explicit features. Compare nearest-station, multi-station average, and reanalysis/grid products in a robustness test.

### 6.3 USGS water sites

Prefer sites on the same cooling-water body and hydrologically relevant upstream locations. A site that is geographically close but on a different tributary may be less useful than a farther site on the same river reach.

Store the mapping rationale, distance, upstream/downstream relationship, drainage area, and whether the site is affected by dams or tidal conditions.

### 6.4 ISO nodes and zones

Map plants to:

- generator/node if available;
- pricing node or electrically proximate node;
- load zone, reserve zone, and balancing area;
- congestion interface and relevant neighboring zones.

Because a plant’s physical location is not enough to determine price impact, use both a physical mapping and an empirical mapping based on historical price/generation relationships.

---

## 7. Time alignment and point-in-time controls

The central rule is:

> A feature may be used for a forecast issued at time `t` only if it was publicly available by `t` under the assumed data-access channel.

Track four times whenever possible:

- `observed_at`: when the physical event occurred;
- `valid_at`: when a forecast applies;
- `published_at`: when the source published it;
- `retrieved_at`: when the pipeline downloaded it.

Use `available_at = published_at` or a conservative later timestamp. Never use `retrieved_at` as a substitute for publication time when the data was available earlier.

### Common leakage traps

- joining revised NRC data to a historical forecast without using the original vintage;
- using end-of-day output to predict an event that began earlier;
- using NOAA observations when the live strategy would only have had a forecast;
- using final ISO prices or revised generation in a signal that supposedly trades before settlement;
- calculating a climatology using the full sample, including the future;
- using an ownership mapping that was not known at the historical date;
- filling historical missing values with later observations;
- treating a market-published outage as an independent environmental prediction.

### Backtest clocks

Define explicit decision clocks, for example:

- **D-1 06:00 local:** forecast next operating day;
- **D-1 10:00 local:** after a specified weather forecast vintage;
- **D-1 market close:** test day-ahead price implications;
- **intraday:** test real-time update value;
- **post-public-status:** measure incremental value after NRC/ISO status is available.

Run the whole pipeline separately for each clock. A result that exists only after a status report is published is a different result from one that exists before it.

---

## 8. Target and label design

Use multiple labels because “outage” is not one event.

### 8.1 Output regression

Predict:

- next-hour output MW;
- next-day average output MW;
- next-day minimum output MW;
- output as a percentage of nameplate or maximum dependable capacity.

### 8.2 Classification

Define material derating thresholds:

```text
derating_10pct = output <= 90% of capacity
derating_25pct = output <= 75%
derating_50pct = output <= 50%
near_outage    = output <= 20%
```

Create labels at 1, 3, 6, 12, 24, 48, and 72-hour horizons where data supports it.

### 8.3 Event onset and recovery

Separate:

- onset probability;
- continuation probability conditional on an active event;
- recovery probability;
- expected duration;
- expected lost MWh.

This avoids treating a long planned outage and a sudden one-hour derating as equivalent.

### 8.4 Surprise and information labels

For market research, define realized shock relative to what was already expected:

```text
realized_shock_mw = expected_available_mw_before_event - realized_available_mw
environmental_surprise = model_prediction - public_status_baseline
```

The environmental model should be compared against a baseline containing only public plant-status and market variables. The incremental residual is more relevant than raw model accuracy.

---

## 9. Feature engineering

### 9.1 Reactor-history features

- current reported power and status;
- output change over 1, 3, 6, 12, 24, and 72 hours;
- rolling mean, minimum, maximum, and volatility;
- time since startup, shutdown, or last derating;
- recent event count and event duration;
- planned-outage indicators if publicly available;
- unit age, reactor type, cooling system, and historical capacity factor;
- seasonal operating profile and month/day-of-week effects.

### 9.2 Weather features

- current and forecast temperature;
- temperature relative to seasonal normal;
- heat index, dew point, wet-bulb temperature;
- cumulative heat or cold degree days;
- precipitation accumulation and intensity;
- snowmelt proxy where relevant;
- forecast ensemble spread and forecast revision;
- extreme-weather indicators and persistence.

### 9.3 Hydrology features

- river temperature level and anomaly;
- flow level and percentile rank;
- low-flow indicator;
- recent flow trend and volatility;
- river temperature × flow interaction;
- lagged and rolling water variables;
- water-data quality and missingness;
- upstream precipitation and snowmelt proxies.

### 9.4 Grid and market controls

- load level and load forecast error;
- load anomaly relative to season/hour;
- renewable generation and forecast error;
- gas/coal generation and interchange;
- reserve margin and scarcity flags;
- neighboring-region prices;
- day-ahead/real-time spread history;
- congestion and binding constraints;
- natural-gas prices and fuel-spread proxies;
- holidays, DST, weekends, and major weather events.

### 9.5 Missingness and data-quality features

Missingness is often informative. Include:

- station missing flag;
- age of last observation;
- provisional versus approved status;
- source revision flag;
- stale-status flag;
- disagreement across nearby stations;
- feed outage flag.

Do not impute everything to a smooth value. Preserve the distinction between a measured normal value and no usable observation.

---

## 10. Modeling plan

Build models in layers so every improvement has an interpretable benchmark.

### Model 0: naive operational baselines

- persistence: next output equals current output;
- seasonal mean by reactor and hour/month;
- last known status;
- historical transition probabilities;
- planned-outage calendar if available.

### Model 1: public-status baseline

Use only NRC/EIA status history, lagged output, calendar, and market controls. This measures how much can be predicted from information already directly about the unit.

### Model 2: environmental increment

Add NOAA and USGS variables. Compare both total accuracy and incremental information relative to Model 1.

### Model 3: flexible nonlinear model

Candidate models:

- regularized logistic regression and elastic net;
- gradient-boosted trees;
- calibrated random forest or extra trees;
- survival model for event duration;
- hierarchical/panel model with reactor random effects;
- temporal convolution or recurrent model only after tabular baselines are strong.

Use calibration, not only ranking. A 20% outage probability should correspond to roughly 20% realized frequency in comparable bins.

### Panel structure

Pool information across reactors but preserve reactor-specific behavior through:

- reactor fixed effects;
- hierarchical intercepts/slopes;
- unit-level history features;
- grouped validation by reactor and time;
- leave-one-reactor-out tests.

### Explainability

Store feature contributions and partial dependence for every production-quality forecast. Watch for models that appear to use river temperature but are actually using a leaked status variable or a seasonal proxy.

---

## 11. Validation and statistical tests

### 11.1 Time-series validation

Use rolling or expanding windows:

```text
train: earliest period → T1
validate: T1 → T2
train: earliest period → T2
validate: T2 → T3
final test: untouched most-recent period
```

Never randomly shuffle observations across time.

### 11.2 Generalization tests

- leave-one-reactor-out;
- leave-one-plant-out;
- train on one ISO, test on another where feasible;
- warm-season versus cold-season;
- high-load versus normal-load days;
- high-flow versus low-flow regimes;
- pre- and post-regulatory or market-design changes.

### 11.3 Forecast metrics

For regression:

- MAE and RMSE in MW;
- normalized error as a percentage of capacity;
- lost-MWh error;
- error by horizon and operating regime.

For classification:

- log loss;
- Brier score;
- ROC-AUC and PR-AUC;
- calibration slope/intercept;
- recall at a fixed false-positive rate;
- precision among high-confidence events.

PR-AUC and calibrated precision matter more than ROC-AUC when large outages are rare.

### 11.4 Incremental-information tests

Compare nested models:

```text
market outcome ~ public_status_baseline
market outcome ~ public_status_baseline + environmental_forecast
```

Use out-of-sample forecast errors and nested-model tests, but avoid interpreting in-sample coefficient significance as tradability. For price outcomes, test whether environmental predictions explain future changes after controlling for:

- current prices and spreads;
- load and load forecast;
- weather forecast;
- published plant status;
- neighboring prices;
- fuel prices;
- day/hour/season effects;
- transmission and scarcity conditions.

Cluster standard errors by date and reactor/market region as appropriate. Use block bootstrap or permutation methods that preserve time dependence.

---

## 12. Convert reactor forecasts into regional MW shocks

For reactor `i` at time `t`:

```text
expected_available_mw[i,t] = capacity_mw[i] × predicted_availability_fraction[i,t]
expected_lost_mw[i,t] = baseline_available_mw[i,t] - expected_available_mw[i,t]
```

Aggregate to a market region with weights:

```text
regional_shock[r,t] = Σ_i mapping_weight[i,r,t] × expected_lost_mw[i,t]
```

Start with physical-zone weights, then improve using:

- nodal or zonal mapping;
- historical correlation between reactor output and regional generation;
- congestion regime;
- import/export conditions;
- reserve-zone membership;
- marginality or estimated displacement stack.

Use both a gross shock and an effective shock:

```text
effective_shock = gross_lost_mw × scarcity_multiplier × transmission_pass_through
```

Do not assume one lost MW creates one MW of price-relevant shortage. The effect depends on demand, reserves, imports, congestion, and which generator is marginal.

Convert MW to expected lost MWh over the market interval. Maintain uncertainty intervals by simulating the reactor-level probability distribution rather than aggregating only point forecasts.

---

## 13. Market-impact research

### 13.1 Day-ahead versus real-time

Useful outcomes include:

- real-time minus day-ahead LMP;
- absolute DA/RT spread;
- next-hour and next-day price change;
- congestion component and energy component of LMP;
- price response in nearby versus control zones;
- tail-event probability, such as price above a threshold;
- reserve/scarcity response.

Test both levels and changes. Electricity prices are highly autocorrelated and heavy-tailed; use robust transformations, winsorization only with care, and quantile or tail models where appropriate.

### 13.2 Event studies

For each unexpected derating:

1. identify the first public detection time;
2. define the event window in market intervals;
3. estimate expected price behavior using a pre-event model;
4. calculate abnormal price and spread movement;
5. compare to matched non-event periods;
6. separate planned from unplanned events;
7. stratify by load, reserve, congestion, and environmental severity.

### 13.3 Avoiding look-ahead in market simulation

At every simulated decision time, use only:

- data published by the cutoff;
- the correct market’s information set;
- realistic execution delay;
- bid/ask, fees, slippage, and limits where relevant;
- no revised prices or final statuses.

For power markets, simulate the actual decision and settlement convention. For example, a day-ahead signal is not allowed to use real-time information that arrived after day-ahead bidding closed.

---

## 14. Equity-impact research

### 14.1 Exposure mapping

For each listed parent, calculate:

- direct ownership share of reactor capacity;
- operating versus economic ownership;
- regional market exposure;
- merchant versus regulated generation;
- expected generation or EBITDA sensitivity to price;
- hedging and power-purchase-agreement offsets;
- nuclear share of company valuation or segment earnings.

### 14.2 Outcomes

- abnormal return around first public outage information;
- abnormal volume and volatility;
- relative return versus utilities/generators benchmark;
- performance relative to regional power futures or generator ETF;
- option-implied volatility response where reliable data exists.

### 14.3 Event timing

Use the earliest timestamp at which the information was public. Separate:

- environmental signal time;
- NRC/status-report time;
- ISO operational/publication time;
- company filing or press-release time;
- market-open and market-close boundaries.

The equity result must survive controls for market, sector, fuel, rates, weather, and company-specific news. A plant outage may be economically important but not move a stock if exposure is hedged or already disclosed.

---

## 15. Backtest design and economic decision rule

Define the signal before looking at results.

Example:

```text
signal_t = expected regional lost MW from environmental model
          - expected regional lost MW from public-status baseline
```

Potential tests:

- rank regions by positive surprise;
- trade DA/RT spread only when probability and expected MW exceed thresholds;
- compare high-confidence outage days with matched controls;
- use signal magnitude as a continuous exposure rather than a binary trade;
- test long/short relative generator baskets rather than outright equities.

Report:

- gross and net performance;
- hit rate and payoff asymmetry;
- turnover and capacity;
- drawdown and tail loss;
- performance by season, ISO, and regime;
- sensitivity to threshold, latency, and costs;
- performance after removing the largest events;
- performance after correcting for multiple hypothesis testing.

For a fair comparison, publish both a forecast report and a strategy report. Do not optimize the forecast threshold and trading threshold on the same test sample.

---

## 16. Data-quality and operational controls

Create automated checks for:

- duplicate rows and duplicate source records;
- impossible timestamps and DST anomalies;
- negative or impossible MW values;
- output above capacity beyond a documented tolerance;
- units changing without a conversion record;
- abrupt station or gage identity changes;
- missing publication timestamps;
- stale feeds;
- schema changes;
- revised historical values;
- unexpected coverage gaps;
- reactor status contradicting known retirement dates.

Every feature row should be traceable to source records. Keep a manifest with:

```text
run_id
source_name
source_url_or_query
retrieved_at
source_file_hash
parser_version
row_count
min_observed_at
max_observed_at
revision_count
```

Create a daily data-health report before generating forecasts.

---

## 17. Practical implementation sequence

### Phase 0 — research specification

- choose the first ISO and forecast clock;
- define reactor universe and event thresholds;
- define the baseline information set;
- write the data dictionary and timestamp policy;
- document what “publicly available” means for each source.

### Phase 1 — master data and raw ingestion

- build the NRC/EIA reactor crosswalk;
- ingest historical NRC status files;
- ingest EIA plant and capacity metadata;
- create raw-data manifests;
- write schema and quality tests.

### Phase 2 — environmental joins

- map NOAA stations;
- map USGS sites and water parameters;
- ingest observations and historical forecasts where available;
- create point-in-time feature tables;
- manually review a sample of reactor-to-station mappings.

### Phase 3 — baseline forecasting

- create persistence and seasonal baselines;
- train the public-status baseline;
- evaluate by reactor and season;
- inspect errors around known events.

### Phase 4 — environmental increment

- add weather and hydrology variables;
- compare out-of-sample performance and calibration;
- run ablations: weather only, water only, interactions, status plus environment;
- test whether improvements remain after excluding high-leakage periods.

### Phase 5 — ISO market layer

- ingest load, generation, LMP, and transmission controls;
- map reactors to zones/nodes;
- aggregate forecasts to regional shocks;
- test DA/RT and local-versus-control-zone outcomes.

### Phase 6 — equity layer

- build point-in-time ownership history;
- map public parents and securities;
- add returns, volume, and corporate-event controls;
- run event studies and relative-value tests.

### Phase 7 — production-style replay

- replay the system one day at a time using only available vintages;
- log every forecast and input snapshot;
- monitor data delays and missing feeds;
- rerun the full pipeline from raw data in a clean environment.

---

## 18. Minimum viable research outputs

The first useful milestone is not a live trading system. It is a reproducible report containing:

1. a reactor master and reviewed source crosswalk;
2. a point-in-time daily reactor-output panel;
3. baseline forecasts and environmental forecasts;
4. calibration and error tables by reactor and regime;
5. an ablation table showing incremental value of weather and water data;
6. regional expected-MW-shock series;
7. a DA/RT price-response analysis with controls;
8. a leakage audit;
9. a list of events where the environmental model moved before public status;
10. a clear conclusion about whether the signal is predictive, incremental, and economically usable.

---

## 19. Key questions to answer during analysis

### Data and measurement

- Are the reactor status observations genuinely available before the forecast cutoff?
- Are water temperatures measured at a location and depth relevant to the cooling system?
- How often are environmental observations provisional or revised?
- Are plant output and capacity definitions consistent through time?
- How much missingness is operationally realistic?

### Causality and mechanism

- Does the relationship make physical sense for this reactor and cooling technology?
- Is water temperature more informative conditional on river flow?
- Does heat matter only at high load or low reserve margins?
- Does the effect differ for once-through versus closed-cycle cooling?
- Are observed relationships driven by planned maintenance or seasonality?

### Timing and market information

- Did the environmental signal arrive before NRC, ISO, or company status information?
- Is the signal still present after current output and public status are included?
- Is the signal larger in less transparent markets or during stressed conditions?
- Does the market respond to the forecast, the realized output, or the public announcement?

### Economics

- Is expected price impact large enough after costs and slippage?
- Is the signal concentrated in a few extreme events?
- Can the strategy trade the relevant node, zone, spread, or security?
- Is liquidity adequate at the time the signal appears?
- Do ownership, hedging, and regulatory structures dilute the equity exposure?

---

## 20. Principal risks and failure modes

- **Leakage:** revised status, final prices, reanalysis weather, or future ownership data sneak into training.
- **Small sample:** truly large unplanned reactor events are rare; apparent significance can be event-specific.
- **Confounded seasonality:** temperature and outages both follow seasonality, while load and prices create a common cause.
- **Operational heterogeneity:** reactor design, cooling system, maintenance practice, and local hydrology differ materially.
- **Data revisions:** public archives can change, making a retrospective backtest look cleaner than a live process.
- **Market nonstationarity:** transmission, market rules, plant retirements, and weather patterns change.
- **Price endogeneity:** power prices affect dispatch and operations, so price controls can be both confounders and mediators.
- **Ownership mismatch:** the operator, legal owner, and publicly traded parent may all be different entities.
- **Execution mismatch:** a statistically significant price relationship may not be executable at the relevant market deadline.
- **Multiple testing:** many reactors, horizons, features, markets, and thresholds create false discoveries.

Maintain a research log of every model, feature set, cutoff, and hypothesis tested. Reserve a final untouched period for confirmation.

---

## 21. Signal catalogue for later stock-price research

This section is a test plan, not an analysis or conclusion. Each candidate signal should be evaluated only after the asset-security mapping is complete and point-in-time exposure is established.

### 21.1 Unplanned availability shock

Predicted or realized reactor output decline relative to expected available output, multiplied by ownership or economic exposure. Potential channel: lower generation, replacement-power cost, lower merchant margins, or adverse operating disclosure. Control for planned outages, current status, power prices, fuel prices, load, hedging, regulated-rate treatment, and company-specific news.

### 21.2 Environmental-surprise signal

Environmental model prediction minus the prediction from public reactor status and market variables alone. Potential channel: environmental data may reveal deterioration before a formal outage/status disclosure. The environmental timestamp must precede the public-status or company-disclosure event.

### 21.3 Lost-MWh exposure signal

Expected lost MWh over a forecast horizon × ownership/economic exposure × regional power-price sensitivity. This may be more economically meaningful than outage probability. Control for capacity factor, hedge book, PPA structure, regulated recovery, and replacement-power source.

### 21.4 Heat and cooling-water stress

Interactions among air temperature, wet-bulb temperature, river temperature, streamflow, cooling technology, and reserve margin. Test separately for once-through versus closed-cycle cooling and regulated versus merchant ownership.

### 21.5 Low-flow persistence

Multi-day or multi-week flow percentile and trend rather than a single low-flow observation. Potential channel: persistent hydrologic constraints may affect expected availability, maintenance timing, or forward power expectations. Control for drought, precipitation, reservoir operations, transmission, and seasonal maintenance.

### 21.6 Forecast-revision signal

Change in NOAA forecast or hydrology outlook between vintages, especially when the revision moves toward a known operating constraint. Use only vintages available at the simulated decision time.

### 21.7 Duration and recovery signal

Probability of continued derating, expected outage duration, or probability of delayed return to service. Duration may matter more than onset for earnings exposure. Control for outage type, maintenance status, historical recovery, inspection, and company disclosure.

### 21.8 Regional scarcity amplification

Expected lost MW × load anomaly × reserve tightness × transmission/congestion regime. The same reactor loss can have different effects depending on system tightness. Control for imports, neighboring prices, renewable output, gas availability, and binding constraints.

### 21.9 Nuclear-versus-thermal substitution

Pair expected nuclear loss with likely replacement generation by gas, coal, imports, or renewables. Potential relative winners may include competing generators or fuel suppliers. Maintain their ownership and fuel exposure with the same point-in-time discipline.

### 21.10 Relative-owner signal

Compare predicted outage exposure across owners, normalized by market capitalization, generation, EBITDA, or nuclear capacity. Potentially more useful for relative performance than outright market direction. Control for beta, utility factors, regional power exposure, leverage, and regulated/merchant mix.

### 21.11 Cross-sectional surprise ranking

Rank issuers by unexpected exposed lost MW, expected lost MWh, or environmental-surprise score. Test as an event-driven or market-neutral ranking, with controls for liquidity, short availability, size, sector, valuation, and concurrent news.

### 21.12 Disclosure-timing signal

Compare environmental signal time, NRC status time, ISO operational signal time, company filing time, and market-reaction time. This distinguishes prediction from faster interpretation or reaction to an official announcement. Produce a timestamped event ladder for every candidate event.

### 21.13 Planned-outage and maintenance-cycle signal

Planned outage timing, duration, seasonal placement, and deviation from historical maintenance patterns. Analyze separately from unexpected derating prediction; a planned outage can affect forward expectations without being a surprise.

### 21.14 Parent-level amplification

Combine reactor exposure with accounting treatment, parent-company size, segment concentration, balance-sheet sensitivity, PPAs, and hedges. A modest plant shock may matter more for a concentrated parent than for a diversified utility.

### 21.15 Signal research card

Maintain one card per candidate signal:

```text
signal_id
economic_mechanism
required_source_tables
forecast_cutoff
first_public_timestamp
asset_exposure_formula
market_outcome
stock_outcome
baseline_model
confounders
minimum_sample
validation_design
rejection_rule
status
```

Use statuses such as `specified`, `data_ready`, `timing_validated`, `backtest_pending`, `tested`, `rejected`, and `needs_mapping_review`.

### 21.16 Order of future testing

1. Verify reactor-to-security exposure manually for a small pilot universe.
2. Test whether environmental variables predict reactor output beyond public status.
3. Test whether the prediction arrives before official plant-status information.
4. Translate predicted output into owner-level lost MWh.
5. Test stock returns only around timestamped, non-confounded events.
6. Test cross-sectional relative performance.
7. Test robustness across issuers, seasons, markets, and historical periods.
8. Only then consider an executable strategy specification.

Do not rank signal families by apparent stock returns before their exposure mapping and information timing have passed QA.

## 22. Suggested first analyses

1. Plot every reactor’s reported output and status history.
2. Measure how often a material derating follows extreme heat, high river temperature, low flow, or their interaction.
3. Compare persistence, status-only, weather-only, and status-plus-environment forecasts.
4. Examine whether environmental variables improve prediction **before** the next NRC status update.
5. Identify the top 20 environmental-surprise events and manually inspect their timelines.
6. Aggregate the predicted lost MW to the relevant PJM zone and compare with DA/RT spreads.
7. Repeat the entire analysis with a one-day publication delay and with revised data excluded.
8. Only then expand to another ISO or to equities.

---

## 23. Source links and documentation index

- [NRC Power Reactor Status Reports](https://www.nrc.gov/reading-rm/doc-collections/event-status/reactor-status/index)
- [EIA Open Data API documentation](https://www.eia.gov/opendata/documentation.php)
- [EIA nuclear generation and capacity](https://www.eia.gov/nuclear/generation/)
- [EIA electric-power data guide](https://www.eia.gov/electricity/data/guide/pdf/guide.pdf)
- [NOAA Climate Data Online API](https://www.ncei.noaa.gov/cdo-web/webservices/v2)
- [NOAA National Weather Service API](https://www.weather.gov/documentation/services-web-api)
- [USGS Water Services](https://waterservices.usgs.gov/)
- [USGS NWIS Web Services](https://nwis.waterservices.usgs.gov/)
- [PJM Data Miner](https://www.pjm.com/markets-and-operations/etools/data-miner-2)
- [PJM Data Miner API guide](https://www.pjm.com/-/media/DotCom/etools/data-miner-2/data-miner-2-api-guide.pdf)
- [PJM energy-market data](https://www.pjm.com/markets-and-operations/energy)
- [CAISO OASIS historical downloader](https://oasis-bulk.caiso.com/)
- [CAISO OASIS API specification](https://www.caiso.com/documents/oasisapispecification.pdf)
- [SEC EDGAR API documentation](https://www.sec.gov/edgar/sec-api-documentation)
- [SEC company facts](https://www.sec.gov/edgar/sec-api-documentation)

Check each source’s terms, rate limits, retention period, revision policy, and redistribution restrictions before building automated collection or distributing derived datasets.

