#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from archive_pipeline.io_utils import load_json


REMOVED_KEYS = {
    "legacy_completeness_status",
    "legacy_snapshot",
    "legacy_incident_fields",
    "legacy_page_fields",
    "legacy_page_sections",
}


def remove_payload(project_root: Path, apply: bool = False) -> dict[str, object]:
    if apply:
        raise RuntimeError(
            "destructive_legacy_payload_removal_disabled: historical/local provenance must be preserved"
        )
    incident_root = project_root.resolve() / "data" / "incidents"
    key_counts: Counter[str] = Counter()
    estimated_bytes: Counter[str] = Counter()
    changed_files = 0
    total_files = 0
    for path in sorted(incident_root.glob("*.json")):
        record = load_json(path, {})
        if not record:
            continue
        total_files += 1
        found = REMOVED_KEYS.intersection(record)
        if not found:
            continue
        changed_files += 1
        for key in found:
            key_counts[key] += 1
            estimated_bytes[key] += len(json.dumps(record[key], ensure_ascii=False))
            record.pop(key, None)
        # Audit only.  Historical payloads are recoverable evidence and are not
        # rewritten by this command.
    return {
        "mode": "audit_only",
        "total_files": total_files,
        "changed_files": changed_files,
        "removed_key_counts": dict(sorted(key_counts.items())),
        "estimated_payload_bytes": dict(sorted(estimated_bytes.items())),
        "stable_sequence_field_preserved": "legacy_sequence",
        "provenance_labels_preserved": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit embedded legacy snapshot payloads without modifying them")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(remove_payload(Path(args.project_root), args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
