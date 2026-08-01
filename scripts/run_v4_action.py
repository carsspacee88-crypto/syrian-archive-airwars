#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive_pipeline.io_utils import atomic_write_json, utc_now
from archive_pipeline.speed_pilot import ENGINE_VERSION, SpeedPilotRunner


def _bounded(value: int, name: str, minimum: int, maximum: int) -> int:
    if not minimum <= value <= maximum:
        raise ValueError(f"{name}_must_be_between_{minimum}_and_{maximum}")
    return value


def _write_action_summary(
    runner: SpeedPilotRunner,
    operation: str,
    status: str,
    stage_results: list[dict[str, Any]],
) -> tuple[Path, Path]:
    report = runner.write_report()
    key = runner.key
    report_root = runner.root / "data" / "reports"
    json_path = report_root / f"v4-action-{key}.json"
    markdown_path = report_root / f"v4-action-{key}.md"
    last_by_stage = {str(row.get("stage")): row for row in stage_results}
    payload = {
        "engine_version": ENGINE_VERSION,
        "operation": operation,
        "status": status,
        "scope": runner.progress["scope"],
        "recorded_at": utc_now(),
        "stages": last_by_stage,
        "collection_report": runner.report_path.relative_to(runner.root).as_posix(),
        "incidents": report["incidents"],
        "sources": report["sources"],
        "recovery": report["recovery"],
        "performance": report["performance"],
    }
    atomic_write_json(json_path, payload)
    markdown = [
        f"# محرك الجمع V{ENGINE_VERSION.split('.', 1)[0]} — {key}",
        "",
        f"- الإصدار: **{ENGINE_VERSION}**",
        f"- العملية: **{operation}**",
        f"- الحالة: **{status}**",
        f"- الحوادث: **{report['incidents']['count']}**",
        f"- المصادر: **{report['sources']['unique']}**",
        f"- النصوص المحفوظة: **{report['sources']['texts_preserved']}** "
        f"(**{report['sources']['text_coverage_percent']}٪**)",
        f"- المؤجل للاسترداد: **{report['recovery']['pending']}**",
        f"- المكتمل بالسياسة: **{report['sources']['policy_completed']}**",
        "- الترجمة الآلية: **معطلة**",
        "- ملفات الوسائط الثنائية: **0**",
        "",
    ]
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text("\n".join(markdown), encoding="utf-8")
    return json_path, markdown_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run collector V4 as a resumable GitHub Action")
    parser.add_argument("--legacy-zip", required=True)
    parser.add_argument("--output-root", default=".")
    parser.add_argument("--operation", choices=("collect", "recover"), default="collect")
    parser.add_argument("--first-sequence", type=int, required=True)
    parser.add_argument("--last-sequence", type=int, required=True)
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument("--per-host-workers", type=int, default=1)
    parser.add_argument("--delay", type=float, default=0.75)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--incident-batch-size", type=int, default=100)
    parser.add_argument("--source-batch-size", type=int, default=1000)
    parser.add_argument("--max-source-batches", type=int, default=0)
    parser.add_argument("--recovery-limit", type=int, default=500)
    parser.add_argument("--time-budget-minutes", type=int, default=300)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    _bounded(args.first_sequence, "first_sequence", 1, 8114)
    _bounded(args.last_sequence, "last_sequence", args.first_sequence, 8114)
    _bounded(args.workers, "workers", 1, 128)
    _bounded(args.per_host_workers, "per_host_workers", 1, args.workers)
    _bounded(args.incident_batch_size, "incident_batch_size", 1, 500)
    _bounded(args.source_batch_size, "source_batch_size", 1, 5000)
    _bounded(args.recovery_limit, "recovery_limit", 1, 5000)
    _bounded(args.time_budget_minutes, "time_budget_minutes", 1, 330)
    if args.delay < 0 or args.delay > 120:
        raise ValueError("delay_must_be_between_0_and_120")
    if args.timeout < 1 or args.timeout > 120:
        raise ValueError("timeout_must_be_between_1_and_120")
    if args.retries < 1 or args.retries > 5:
        raise ValueError("retries_must_be_between_1_and_5")
    if args.max_source_batches < 0:
        raise ValueError("max_source_batches_cannot_be_negative")

    runner = SpeedPilotRunner(
        Path(args.output_root),
        Path(args.legacy_zip),
        first_sequence=args.first_sequence,
        last_sequence=args.last_sequence,
        delay=args.delay,
        timeout=args.timeout,
        retries=args.retries,
        workers=args.workers,
        per_host_workers=args.per_host_workers,
    )
    deadline = time.monotonic() + args.time_budget_minutes * 60
    stage_results: list[dict[str, Any]] = []
    status = "completed"

    manifest = runner.run("manifest")
    stage_results.append(manifest)
    if args.operation == "collect":
        while True:
            result = runner.run("incidents", args.incident_batch_size)
            stage_results.append(result)
            if result.get("done"):
                break
            if not result.get("processed"):
                raise RuntimeError("incident_stage_made_no_progress")
            if time.monotonic() >= deadline:
                status = "checkpointed_time_budget"
                break

        source_batches = 0
        if status == "completed":
            while True:
                result = runner.run("sources", args.source_batch_size)
                stage_results.append(result)
                source_batches += 1
                if result.get("done"):
                    break
                if not result.get("processed"):
                    raise RuntimeError("source_stage_made_no_progress")
                if args.max_source_batches and source_batches >= args.max_source_batches:
                    status = "checkpointed_batch_limit"
                    break
                if time.monotonic() >= deadline:
                    status = "checkpointed_time_budget"
                    break
    else:
        result = runner.run("recover", args.recovery_limit)
        stage_results.append(result)
        if not result.get("done"):
            status = "completed_with_deferred_recovery"

    if runner.recovery.summary()["pending"] and status == "completed":
        status = "completed_with_deferred_recovery"
    json_path, markdown_path = _write_action_summary(runner, args.operation, status, stage_results)
    result = {
        "engine_version": ENGINE_VERSION,
        "operation": args.operation,
        "status": status,
        "scope": runner.progress["scope"],
        "summary_json": json_path.relative_to(runner.root).as_posix(),
        "summary_markdown": markdown_path.relative_to(runner.root).as_posix(),
        "recovery": runner.recovery.summary(),
    }
    print("V4_ACTION_RESULT=" + json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
