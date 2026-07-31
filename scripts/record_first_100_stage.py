#!/usr/bin/env python3
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive_pipeline.io_utils import atomic_write_json, load_json, utc_now


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True)
    parser.add_argument("--duration-seconds", required=True, type=float)
    parser.add_argument("--result", default="complete")
    parser.add_argument("--project-root", default=".")
    args = parser.parse_args()
    root = Path(args.project_root)
    progress_path = root / "data" / "pilot" / "first-100-progress.json"
    progress = load_json(progress_path, {}) or {}
    progress.setdefault("stage_runs", []).append({
        "stage": args.stage,
        "finished_at": utc_now(),
        "duration_seconds": round(args.duration_seconds, 3),
        "result": args.result,
    })
    progress["updated_at"] = utc_now()
    atomic_write_json(progress_path, progress)
    timing_path = root / "data" / "reports" / "first-100-timing.json"
    timing = load_json(timing_path, {}) or {}
    timing.setdefault("post_collection_stages", []).append({
        "stage": args.stage,
        "duration_seconds": round(args.duration_seconds, 3),
        "result": args.result,
        "finished_at": utc_now(),
    })
    atomic_write_json(timing_path, timing)


if __name__ == "__main__":
    main()
