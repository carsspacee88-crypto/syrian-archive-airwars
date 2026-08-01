from __future__ import annotations

import math
import urllib.parse
from collections import defaultdict, deque
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from .io_utils import atomic_write_json, load_json, utc_now


ENGINE_VERSION = "4.0.0"
RECOVERY_SCHEMA_VERSION = "4.0.0"

# These outcomes are complete from the collection job's point of view. A
# deferred source is intentionally not called a failure: it remains in the
# durable recovery queue and can be retried by a later GitHub Action run.
RESOLVED_OUTCOME_STATUSES = {
    "successful",
    "cached",
    "policy_complete",
    "deferred_recovery",
}

POLICY_SOURCE_TYPES = {
    "direct_image_url",
    "direct_video_url",
    "direct_audio_url",
}


def source_host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").casefold().removeprefix("www.")


def source_has_text(record: dict[str, Any]) -> bool:
    return bool(str(record.get("text_original") or "").strip())


def source_is_policy_complete(record: dict[str, Any]) -> bool:
    return str(record.get("source_type") or "") in POLICY_SOURCE_TYPES


def fair_host_order(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Round-robin records by host while keeping stable order within a host."""

    queues: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for record in records:
        host = source_host(str(record.get("original_url") or "")) or "_unknown"
        queues[host].append(record)
    hosts = deque(sorted(queues))
    ordered: list[dict[str, Any]] = []
    while hosts:
        host = hosts.popleft()
        ordered.append(queues[host].popleft())
        if queues[host]:
            hosts.append(host)
    return ordered


def _percentile(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)


def timing_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    materialized = list(rows)
    fields = {
        "total": "duration_seconds",
        "network": "network_seconds",
        "queue": "queue_seconds",
        "throttle": "throttle_seconds",
        "save": "save_seconds",
        "extraction": "extraction_seconds",
    }
    summary: dict[str, Any] = {"sample_size": len(materialized)}
    for label, field in fields.items():
        values = [float(row.get(field) or 0) for row in materialized]
        summary[label] = {
            "p50_seconds": _percentile(values, 0.50),
            "p90_seconds": _percentile(values, 0.90),
        }
    return summary


class RecoveryQueue:
    """A compact, atomic, git-friendly recovery queue for one incident range."""

    def __init__(self, path: Path, scope: dict[str, Any]):
        self.path = path
        loaded = load_json(path, {}) or {}
        self.data: dict[str, Any] = loaded if isinstance(loaded, dict) else {}
        self.data.setdefault("schema_version", RECOVERY_SCHEMA_VERSION)
        self.data.setdefault("engine_version", ENGINE_VERSION)
        self.data.setdefault("scope", deepcopy(scope))
        self.data.setdefault("created_at", utc_now())
        self.data.setdefault("pending", {})
        self.data.setdefault("resolved", {})
        if self.data.get("scope") != scope:
            raise ValueError(f"recovery_scope_mismatch:{path}")

    @property
    def pending(self) -> dict[str, dict[str, Any]]:
        return self.data["pending"]

    def defer(self, record: dict[str, Any], reason: str | None = None) -> None:
        source_id = str(record["source_id"])
        now = utc_now()
        previous = self.pending.get(source_id) or {}
        self.pending[source_id] = {
            "source_id": source_id,
            "original_url": record.get("original_url") or previous.get("original_url") or "",
            "host": source_host(str(record.get("original_url") or previous.get("original_url") or "")),
            "incident_ids": list(record.get("incident_ids") or previous.get("incident_ids") or []),
            "incident_sequences": list(
                record.get("incident_sequences") or previous.get("incident_sequences") or []
            ),
            "first_deferred_at": previous.get("first_deferred_at") or now,
            "last_deferred_at": now,
            "last_attempt_at": previous.get("last_attempt_at"),
            "recovery_attempts": int(previous.get("recovery_attempts") or 0),
            "last_status": record.get("retrieval_status") or "unavailable",
            "reason": reason or record.get("failure_reason") or record.get("retrieval_status") or "unavailable",
        }
        self.data["resolved"].pop(source_id, None)
        self.save()

    def note_attempt(self, source_id: str, status: str | None = None) -> None:
        entry = self.pending.get(source_id)
        if not entry:
            return
        entry["recovery_attempts"] = int(entry.get("recovery_attempts") or 0) + 1
        entry["last_attempt_at"] = utc_now()
        if status:
            entry["last_status"] = status
        self.save()

    def resolve(self, record: dict[str, Any], outcome: str) -> None:
        source_id = str(record["source_id"])
        previous = self.pending.pop(source_id, None)
        if not previous:
            return
        self.data["resolved"][source_id] = {
            "source_id": source_id,
            "outcome": outcome,
            "retrieval_status": record.get("retrieval_status"),
            "text_preserved": source_has_text(record),
            "recovery_attempts": int(previous.get("recovery_attempts") or 0),
            "resolved_at": utc_now(),
        }
        self.save()

    def select(self, limit: int | None = None) -> list[str]:
        records = [
            {"source_id": source_id, "original_url": entry.get("original_url") or ""}
            for source_id, entry in sorted(
                self.pending.items(),
                key=lambda item: (
                    int(item[1].get("recovery_attempts") or 0),
                    str(item[1].get("first_deferred_at") or ""),
                    item[0],
                ),
            )
        ]
        selected = fair_host_order(records)
        if limit is not None:
            selected = selected[: max(0, limit)]
        return [str(record["source_id"]) for record in selected]

    def summary(self) -> dict[str, Any]:
        by_status: dict[str, int] = defaultdict(int)
        by_host: dict[str, int] = defaultdict(int)
        for entry in self.pending.values():
            by_status[str(entry.get("last_status") or "unknown")] += 1
            by_host[str(entry.get("host") or "unknown")] += 1
        return {
            "pending": len(self.pending),
            "resolved": len(self.data.get("resolved") or {}),
            "pending_by_status": dict(sorted(by_status.items())),
            "pending_by_host": dict(sorted(by_host.items(), key=lambda item: (-item[1], item[0]))),
        }

    def save(self) -> None:
        self.data["engine_version"] = ENGINE_VERSION
        self.data["updated_at"] = utc_now()
        atomic_write_json(self.path, self.data)
