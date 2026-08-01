#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Mapping

SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def _required_text(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if value is None or isinstance(value, bool):
        raise ValueError(f"{name}_is_required")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{name}_is_required")
    return text


def _bounded_integer(
    payload: Mapping[str, Any],
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    text = _required_text(payload, name)
    if not re.fullmatch(r"[0-9]+", text):
        raise ValueError(f"{name}_must_be_an_integer")
    value = int(text, 10)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name}_must_be_between_{minimum}_and_{maximum}")
    return value


def _bounded_float(
    payload: Mapping[str, Any],
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    text = _required_text(payload, name)
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"{name}_must_be_a_number") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name}_must_be_between_{minimum}_and_{maximum}")
    return value


def load_request(event_name: str, request_file: Path, inputs: Mapping[str, Any]) -> dict[str, Any]:
    if event_name == "push":
        payload = json.loads(request_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request_file_must_contain_an_object")
        return payload
    if event_name == "workflow_dispatch":
        return dict(inputs)
    raise ValueError("unsupported_event_name")


def resolve_request(payload: Mapping[str, Any]) -> dict[str, str]:
    operation = _required_text(payload, "operation")
    if operation not in {"collect", "recover"}:
        raise ValueError("operation_must_be_collect_or_recover")

    first_sequence = _bounded_integer(payload, "first_sequence", 1, 8114)
    last_sequence = _bounded_integer(payload, "last_sequence", first_sequence, 8114)
    workers = _bounded_integer(payload, "workers", 1, 128)
    per_host_workers = _bounded_integer(payload, "per_host_workers", 1, workers)
    delay = _bounded_float(payload, "delay", 0, 120)
    source_batch_size = _bounded_integer(payload, "source_batch_size", 1, 5000)
    recovery_limit = _bounded_integer(payload, "recovery_limit", 1, 5000)

    default_id = f"{operation}-{first_sequence:04d}-{last_sequence:04d}"
    request_id = str(payload.get("request_id") or default_id).strip()
    if not SAFE_REQUEST_ID.fullmatch(request_id):
        raise ValueError("request_id_has_invalid_characters")

    return {
        "request_id": request_id,
        "operation": operation,
        "first_sequence": str(first_sequence),
        "last_sequence": str(last_sequence),
        "workers": str(workers),
        "per_host_workers": str(per_host_workers),
        "delay": format(delay, ".12g"),
        "source_batch_size": str(source_batch_size),
        "recovery_limit": str(recovery_limit),
    }


def write_github_outputs(outputs: Mapping[str, str], path: Path) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for name, value in outputs.items():
            handle.write(f"{name}={value}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve a V4 request from push or dispatch inputs")
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--request-file", type=Path, required=True)
    parser.add_argument("--operation", default="")
    parser.add_argument("--first-sequence", default="")
    parser.add_argument("--last-sequence", default="")
    parser.add_argument("--workers", default="")
    parser.add_argument("--per-host-workers", default="")
    parser.add_argument("--delay", default="")
    parser.add_argument("--source-batch-size", default="")
    parser.add_argument("--recovery-limit", default="")
    parser.add_argument("--github-output", type=Path, default=None)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    inputs = {
        "operation": args.operation,
        "first_sequence": args.first_sequence,
        "last_sequence": args.last_sequence,
        "workers": args.workers,
        "per_host_workers": args.per_host_workers,
        "delay": args.delay,
        "source_batch_size": args.source_batch_size,
        "recovery_limit": args.recovery_limit,
    }
    outputs = resolve_request(load_request(args.event_name, args.request_file, inputs))
    output_path = args.github_output
    if output_path is None and os.environ.get("GITHUB_OUTPUT"):
        output_path = Path(os.environ["GITHUB_OUTPUT"])
    if output_path is not None:
        write_github_outputs(outputs, output_path)
    print("V4_REQUEST=" + json.dumps(outputs, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
