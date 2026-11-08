from pathlib import Path

import pytest

from src.normalize.nrc import normalize_file


def test_normalize_pipe_delimited_status_and_keep_publication_unknown(tmp_path: Path):
    raw = tmp_path / "status.txt"
    raw.write_text(
        "ReportDt|Unit|Power|Comment\n"
        "10/25/2026 12:00:00 AM|Example Unit 1|87|Reduced power\n",
        encoding="utf-8",
    )

    [row] = normalize_file(raw)

    assert row["unit_name"] == "Example Unit 1"
    assert row["report_date"] == "2026-10-25"
    assert row["observed_at"] is None
    assert row["reported_power_pct"] == 87
    assert row["published_at"] is None
    assert row["available_at"] is None


def test_normalize_rejects_missing_required_columns(tmp_path: Path):
    raw = tmp_path / "status.txt"
    raw.write_text("ReportDt|Unit\n10/25/2026|Example Unit 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Missing NRC columns"):
        normalize_file(raw)
