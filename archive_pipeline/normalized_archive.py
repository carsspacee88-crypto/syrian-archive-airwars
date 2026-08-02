from __future__ import annotations

from pathlib import Path
from typing import Any

from .io_utils import load_json


class NormalizedArchive:
    """Read collection identities from normalized records without a legacy package."""

    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()
        self._paths: dict[int, Path] = {}
        for path in sorted((self.project_root / "data" / "incidents").glob("*.json")):
            record = load_json(path, {})
            sequence = int(record.get("legacy_sequence") or 0)
            if sequence > 0:
                self._paths[sequence] = path

    def __enter__(self) -> "NormalizedArchive":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None

    def _record(self, sequence: int) -> dict[str, Any]:
        path = self._paths.get(sequence)
        if path is None:
            raise KeyError(f"normalized_sequence_not_found:{sequence:04d}")
        record = load_json(path, {})
        if not record:
            raise ValueError(f"normalized_record_invalid:{sequence:04d}")
        return record

    def summary_by_sequence(self, sequence: int) -> dict[str, Any]:
        record = self._record(sequence)
        return {
            "sequence": sequence,
            "number": f"{sequence:04d}",
            "airwars_id": record.get("airwars_id"),
            "airwars_url": record.get("canonical_url"),
            "code": record.get("incident_code"),
            "date": record.get("incident_date"),
            "location_original": record.get("location"),
            "location_ar": record.get("location_ar"),
            "latitude": record.get("latitude"),
            "longitude": record.get("longitude"),
            "completion": record.get("completeness_status"),
            "path": f"cases/{sequence:04d}/",
        }

    def case_data(self, sequence: int) -> dict[str, Any]:
        record = self._record(sequence)
        return {
            "_normalized_record": record,
            "case": {"airwars_id": record.get("airwars_id")},
            "incident": {},
            "sources": [],
            "victims": [],
            "media": [],
            "additional_links": [],
            "api_links": [],
            "page_fields": [],
            "page_sections": [],
        }
