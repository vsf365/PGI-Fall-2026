# Nuclear availability and power market research

This repository is building a point-in-time data pipeline for testing the link
between reactor deratings and power prices. The first data collector archives
the NRC's annual raw reactor status files.

## NRC status archive

From the repository root, run:

```sh
python -m src.ingest.nrc 2024 2025
```

The collector discovers the annual raw text file from each NRC archive index,
saves the exact response under `data/raw/nrc/annual/`, and appends a SHA-256
provenance record to `data/manifests/ingest_manifest.jsonl`. Re-running creates
a new snapshot rather than replacing an earlier one. No API key is needed.

The NRC annual archive is appropriate for reconstructing observed reactor
output and status. The current collector deliberately records `published_at`
as unknown: the archive index does not establish when each daily observation
became public. Therefore these files can support the perfect-information
derating-to-price analysis, but they cannot yet support a latency-sensitive
claim about what a trader knew at a given time. Establish publication/observation
latency, including a conservative delay sensitivity, before using status as a
point-in-time predictor.

## Initial pipeline sequence

1. Archive and inspect NRC annual files and their data dictionary.
2. Validate reactor identifiers, status/output fields, units, and revisions;
   normalize to `observed_at`, `published_at`, and `available_at` fields.
3. Join NRC status to PJM DA/RT prices, load, and market controls for the
   perfect-information test of unplanned deratings.
4. Add additional sources only after that price-response test is specified.

Raw snapshots and generated data are excluded from git. Keep the manifest and
code under version control; store data artifacts locally or in an approved
shared data store.
