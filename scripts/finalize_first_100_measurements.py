#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive_pipeline.pilot import finalize_site_measurements


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site-root", required=True)
    parser.add_argument("--compressed-artifact", required=True)
    parser.add_argument("--project-root", default=".")
    args = parser.parse_args()
    result = finalize_site_measurements(Path(args.project_root), Path(args.site_root), Path(args.compressed_artifact))
    print(json.dumps({"current_bytes": result["storage"]["current_bytes"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
