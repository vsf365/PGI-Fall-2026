"""
Manifest tracking for raw and interim data objects.

Maintains an immutable record of data ingestion runs, hashes, row counts,
and timestamps as specified in Project Plan §16.
"""

import sys
import glob
from pathlib import Path
import hashlib
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, asdict

# Ensure configs is on path
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

for p in glob.glob("/opt/miniconda3/lib/python*/site-packages"):
    if p not in sys.path:
        sys.path.append(p)

from configs.config import MANIFEST_DIR


@dataclass
class ManifestEntry:
    run_id: str
    source_name: str
    source_url_or_query: str
    retrieved_at: str
    source_file_hash: str
    parser_version: str
    row_count: int
    min_observed_at: Optional[str] = None
    max_observed_at: Optional[str] = None
    revision_count: int = 0
    extra_metadata: Optional[Dict[str, Any]] = None
    published_at: Optional[str] = None
    publication_time_status: Optional[str] = None
    file_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def compute_file_hash(file_path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


class ManifestManager:
    """Manages manifest logging and retrieval."""

    def __init__(self, manifest_dir: Optional[Path] = None):
        self.manifest_dir = manifest_dir or MANIFEST_DIR
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_file = self.manifest_dir / "ingest_manifest.jsonl"

    def record_entry(self, entry: ManifestEntry) -> None:
        """Append a manifest entry to the JSONL manifest log."""
        with open(self.manifest_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")

    def load_entries(self, source_name: Optional[str] = None) -> List[ManifestEntry]:
        """Load manifest entries, optionally filtered by source_name."""
        if not self.manifest_file.exists():
            return []

        entries = []
        with open(self.manifest_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if source_name is None or data.get("source_name") == source_name:
                    known_fields = set(ManifestEntry.__dataclass_fields__)
                    # Preserve forward-compatible source-specific fields instead of
                    # failing when a collector adds provenance metadata.
                    extras = {k: v for k, v in data.items() if k not in known_fields}
                    if extras:
                        data["extra_metadata"] = {**(data.get("extra_metadata") or {}), **extras}
                    entries.append(ManifestEntry(**{k: v for k, v in data.items() if k in known_fields}))
        return entries


if __name__ == "__main__":
    mgr = ManifestManager()
    sample = ManifestEntry(
        run_id="test-run-001",
        source_name="eia_generator",
        source_url_or_query="https://api.eia.gov/v2/electricity/facility-fuel/data/",
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        source_file_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        parser_version="1.0.0",
        row_count=120,
    )
    mgr.record_entry(sample)
    records = mgr.load_entries()
    print(f"Loaded {len(records)} entries. Latest run_id: {records[-1].run_id}")
