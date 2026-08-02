#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from archive_engine.reports import AirwarsGroundTruthAudit


def main() -> None:
    parser = argparse.ArgumentParser(description="Recompute Airwars ground truth from canonical incident, source, and historical structural inputs")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--legacy-zip", required=True)
    parser.add_argument("--release-root")
    parser.add_argument("--output-root", default=".")
    args = parser.parse_args()
    report = AirwarsGroundTruthAudit(
        Path(args.project_root),
        Path(args.legacy_zip),
        Path(args.release_root) if args.release_root else None,
    ).generate(Path(args.output_root))
    print(json.dumps({
        "report": str(Path(args.output_root).resolve() / "reports" / "airwars-ground-truth.json"),
        "incidents": report["metrics"]["incident_records_total"]["result"],
        "source_references": report["metrics"]["source_reference_records_total"]["result"],
        "source_entities": report["metrics"]["source_record_files_total"]["result"],
        "reconciled": all(item["balanced"] for item in report["reconciliation_equations"].values()),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
