#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from archive_pipeline.reports import generate_collection_summary, generate_map_coverage


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-zip", required=True)
    parser.add_argument("--output-root", default=".")
    args = parser.parse_args()
    legacy_zip = Path(args.legacy_zip)
    output_root = Path(args.output_root)
    collection = generate_collection_summary(legacy_zip, output_root)
    map_report = generate_map_coverage(legacy_zip, output_root)
    print({"collection": collection["direct_collection"], "map": map_report["counts"]})


if __name__ == "__main__":
    main()
