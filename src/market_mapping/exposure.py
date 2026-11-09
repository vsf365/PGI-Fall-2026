"""Point-in-time nuclear reactor to public-security exposure mapping."""

from __future__ import annotations

import pandas as pd


EXPOSURE_FIELDS = (
    "exposure_id", "reactor_id", "plant_id", "entity_id", "entity_name", "entity_role",
    "parent_entity_id", "issuer_id", "issuer_name", "security_id", "ticker", "exchange",
    "share_class", "security_type", "ownership_pct", "economic_exposure_pct",
    "capacity_share_mw", "market_exposure_type", "regulated_or_merchant",
    "hedge_or_ppa_notes", "valid_from", "valid_to", "announced_at", "effective_at",
    "source_document_id", "source_url", "source_page_or_section", "mapping_method",
    "mapping_confidence", "review_status", "last_reviewed_at",
)

ENTITY_ROLES = {
    "licensed_operator", "direct_owner", "co_owner", "leaseholder", "power_purchaser",
    "regulated_utility_parent", "merchant_generator_parent", "public_parent",
    "private_parent", "government_entity",
}
EXPOSURE_TYPES = {
    "direct_generation", "capacity_payment", "regulated_rate_base",
    "power_purchase_agreement", "fuel_or_service_revenue", "decommissioning_liability",
    "indirect_parent_exposure", "none_or_unclear",
}
CONFIDENCE = {
    "confirmed_direct", "confirmed_indirect", "probable_needs_review", "conflicting_sources",
    "no_public_security", "historical_only", "retired_or_inactive",
}
REVIEW_STATUSES = {"reviewed", "needs_review", "rejected"}
TIME_FIELDS = ("valid_from", "valid_to", "announced_at", "effective_at", "last_reviewed_at")


def validate_exposures(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a row-level exception queue; an empty frame means no detected issues.

    Validation is intentionally conservative. It checks structure, dates, controlled
    vocabularies, percentages, missing evidence, interval overlap, and per-reactor
    ownership totals. It does not infer a security from an entity name or ticker.
    """
    missing = sorted(set(EXPOSURE_FIELDS) - set(frame.columns))
    if missing:
        raise ValueError(f"Missing exposure columns: {missing}")
    data = frame.loc[:, EXPOSURE_FIELDS].copy().fillna("")
    issues: list[dict] = []

    def add(index, code, detail):
        issues.append({"row": index, "exposure_id": data.at[index, "exposure_id"],
                       "reactor_id": data.at[index, "reactor_id"], "issue": code, "detail": detail})

    for column in TIME_FIELDS:
        data[column] = pd.to_datetime(data[column].replace("", pd.NA), utc=True, errors="coerce")
    for column in ("ownership_pct", "economic_exposure_pct", "capacity_share_mw"):
        data[column] = pd.to_numeric(data[column].replace("", pd.NA), errors="coerce")

    for index in data.index[data.exposure_id.duplicated(keep=False) & data.exposure_id.ne("")]:
        add(index, "duplicate_exposure_id", data.at[index, "exposure_id"])

    for index, row in data.iterrows():
        for field in ("exposure_id", "reactor_id", "entity_id", "entity_name", "entity_role",
                      "market_exposure_type", "mapping_confidence", "review_status"):
            if not str(row[field]).strip():
                add(index, "missing_required", field)
        for field, allowed in (("entity_role", ENTITY_ROLES), ("market_exposure_type", EXPOSURE_TYPES),
                               ("mapping_confidence", CONFIDENCE), ("review_status", REVIEW_STATUSES)):
            if row[field] and row[field] not in allowed:
                add(index, "invalid_value", f"{field}={row[field]}")
        if not row["source_document_id"] or not row["source_url"] or not row["source_page_or_section"]:
            add(index, "missing_evidence", "source document ID, URL, and page/section are required")
        if not pd.isna(row["valid_from"]) and not pd.isna(row["valid_to"]) and row["valid_from"] >= row["valid_to"]:
            add(index, "invalid_interval", "valid_to must be later than valid_from")
        for field in ("ownership_pct", "economic_exposure_pct"):
            value = row[field]
            if not pd.isna(value) and not 0 <= value <= 100:
                add(index, "percentage_out_of_range", f"{field}={value}")
        if not pd.isna(row["capacity_share_mw"]) and row["capacity_share_mw"] < 0:
            add(index, "negative_capacity_share", str(row["capacity_share_mw"]))
        if row["review_status"] == "reviewed" and pd.isna(row["last_reviewed_at"]):
            add(index, "missing_review_time", "reviewed rows need last_reviewed_at")

    # Disjoint co-owner records may sum to 100%; flag only when they share a date.
    owners = data[data.entity_role.isin({"direct_owner", "co_owner"}) & data.ownership_pct.notna()]
    for reactor, group in owners.groupby("reactor_id"):
        boundaries = sorted(set(group.valid_from.dropna()) | set(group.valid_to.dropna()))
        for start in boundaries:
            active = group[(group.valid_from <= start) & (group.valid_to.isna() | (start < group.valid_to))]
            if active.ownership_pct.sum() > 100.0001:
                for index in active.index:
                    add(index, "ownership_over_100", f"{reactor} totals {active.ownership_pct.sum():g}% from {start}")

    # Overlapping contradictory role/security mappings need human resolution.
    keys = ["reactor_id", "entity_id", "entity_role", "security_id"]
    for _, group in data.groupby(keys, dropna=False):
        records = list(group.sort_values("valid_from").iterrows())
        for position, (current_index, current) in enumerate(records):
            for _, prior in records[:position]:
                overlaps = pd.isna(prior.valid_to) or current.valid_from < prior.valid_to
                disagrees = prior.ticker != current.ticker or prior.ownership_pct != current.ownership_pct
                if overlaps and disagrees:
                    add(current_index, "overlapping_conflict", "overlapping rows disagree on ticker or ownership")

    return pd.DataFrame(issues, columns=("row", "exposure_id", "reactor_id", "issue", "detail"))


def get_exposure(frame: pd.DataFrame, reactor_id: str, as_of_timestamp: str | pd.Timestamp) -> pd.DataFrame:
    """Return reviewed exposures effective and publicly announced by the cutoff."""
    missing = sorted(set(EXPOSURE_FIELDS) - set(frame.columns))
    if missing:
        raise ValueError(f"Missing exposure columns: {missing}")
    cutoff = pd.Timestamp(as_of_timestamp)
    if cutoff.tzinfo is None:
        raise ValueError("as_of_timestamp must include a timezone")
    cutoff = cutoff.tz_convert("UTC")
    data = frame.copy()
    for field in TIME_FIELDS:
        data[field] = pd.to_datetime(data[field].replace("", pd.NA), utc=True, errors="coerce")
    data = data[(data.reactor_id == reactor_id) & (data.review_status == "reviewed")
                & (data.valid_from <= cutoff) & (data.valid_to.isna() | (cutoff < data.valid_to))
                & (data.announced_at <= cutoff) & (data.effective_at <= cutoff)]
    return data.sort_values(["entity_role", "entity_name", "ticker"], na_position="last").reset_index(drop=True)
