#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from archive_pipeline.modern_site_builder import build_modern_site


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the static site from normalized records only")
    parser.add_argument("--site-root", required=True)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--resume", action="store_true", help="Keep already-rendered case and source pages")
    args = parser.parse_args()
    result = build_modern_site(Path(args.site_root), Path(args.project_root), resume=args.resume)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
