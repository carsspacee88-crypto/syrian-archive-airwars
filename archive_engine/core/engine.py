from __future__ import annotations

import hashlib
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from archive_engine.connectors.base import Connector, DiscoveredTarget
from archive_engine.fetchers.http import Fetcher
from archive_engine.models import ArchiveProject, EngineRun, utc_now
from archive_engine.statuses import CollectionStatus, RunStatus

from .store import ProjectStore


@dataclass(frozen=True, slots=True)
class RunPolicy:
    max_workers: int = 1
    checkpoint_every: int = 1
    pilot_limit: int = 10


class EngineControl:
    def __init__(self, store: ProjectStore, run_id: str):
        self.store = store
        self.run_id = run_id

    def request(self, action: str) -> None:
        if action not in {"pause", "resume", "cancel"}:
            raise ValueError(f"unsupported_engine_action:{action}")
        self.store.write_json(f"runs/{self.run_id}/control.json", {"action": action, "requested_at": utc_now()})

    def action(self) -> str | None:
        return (self.store.read_json(f"runs/{self.run_id}/control.json", {}) or {}).get("action")


class ArchiveEngine:
    """Connector-neutral, resumable textual collection orchestrator."""

    def __init__(self, store: ProjectStore, connector: Connector, fetcher: Fetcher, policy: RunPolicy | None = None):
        self.store = store
        self.connector = connector
        self.fetcher = fetcher
        self.policy = policy or RunPolicy()

    @staticmethod
    def _worklist_hash(targets: list[DiscoveredTarget]) -> str:
        payload = json.dumps([asdict(item) for item in targets], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def create_run(self, project: ArchiveProject, mode: str, run_id: str | None = None) -> EngineRun:
        if mode not in {"analysis", "pilot", "full", "retry"}:
            raise ValueError(f"unsupported_run_mode:{mode}")
        run = EngineRun(run_id or str(uuid4()), project.project_id, mode)
        self.store.write_json(f"projects/{project.project_id}/project.json", project)
        self.store.write_json(f"runs/{run.run_id}/run.json", run)
        self.store.append_audit(run.run_id, {"at": utc_now(), "event": "run_created", "mode": mode})
        return run

    def analyze(self, project: ArchiveProject, sample_bodies: dict[str, bytes] | None = None) -> dict[str, Any]:
        result = self.connector.analyze(project, sample_bodies or {})
        self.store.write_json(f"projects/{project.project_id}/analysis.json", result)
        return result

    def _load_targets(self, project: ArchiveProject, run: EngineRun) -> list[DiscoveredTarget]:
        existing = self.store.read_json(f"runs/{run.run_id}/worklist.json")
        if existing:
            targets = [DiscoveredTarget(**item) for item in existing["targets"]]
            digest = self._worklist_hash(targets)
            if digest != existing.get("sha256"):
                raise ValueError("immutable_worklist_checksum_mismatch")
            return targets
        targets = list(self.connector.discover(project))
        if run.mode == "pilot":
            targets = targets[: self.policy.pilot_limit]
        identities = [target.identity for target in targets]
        if len(set(identities)) != len(identities):
            duplicates = sorted(identity for identity, count in Counter(identities).items() if count > 1)
            raise ValueError(f"duplicate_discovered_identifier:{','.join(duplicates[:20])}")
        digest = self._worklist_hash(targets)
        self.store.write_json(f"runs/{run.run_id}/worklist.json", {"sha256": digest, "targets": targets})
        run.worklist_hash = digest
        return targets

    def execute(self, project: ArchiveProject, run: EngineRun) -> EngineRun:
        targets = self._load_targets(project, run)
        checkpoint = self.store.read_json(f"runs/{run.run_id}/checkpoint.json", {"completed": {}, "attempted": 0})
        completed: dict[str, str] = dict(checkpoint.get("completed") or {})
        control = EngineControl(self.store, run.run_id)
        if control.action() == "resume":
            self.store.write_json(f"runs/{run.run_id}/control.json", {"action": None, "resumed_at": utc_now()})
        run.status = RunStatus.PILOT_RUNNING if run.mode == "pilot" else RunStatus.RETRYING if run.mode == "retry" else RunStatus.RUNNING
        run.updated_at = utc_now()
        self.store.write_json(f"runs/{run.run_id}/run.json", run)
        counts: dict[str, int] = dict(Counter(completed.values()))
        remaining = [target for target in targets if target.identity not in completed]

        def process(target: DiscoveredTarget) -> tuple[CollectionStatus, dict[str, Any], dict[str, Any] | None]:
            response = self.fetcher.fetch(target.url)
            raw = self.store.preserve_raw(run.run_id, target.identity, response.body, {
                "requested_url": response.requested_url,
                "final_url": response.final_url,
                "status_code": response.status_code,
                "content_type": response.content_type,
                "outcome": response.outcome,
                "attempts": response.attempts,
            })
            status = response.outcome
            parse_failure: dict[str, Any] | None = None
            if status == CollectionStatus.FETCHED:
                try:
                    parsed = self.connector.parse(target, response.body, response.content_type)
                    record = parsed.record
                    status = record.collection_status
                    self.store.write_json(f"runs/{run.run_id}/records/{record.record_id}.json", record)
                    for reference in record.source_references:
                        self.store.write_json(f"runs/{run.run_id}/source-references/{reference.source_reference_id}.json", reference)
                except Exception as error:  # noqa: BLE001 - exact parser failure remains auditable
                    status = CollectionStatus.MALFORMED
                    parse_failure = {"at": utc_now(), "event": "parse_failed", "identity": target.identity, "error": f"{type(error).__name__}:{error}", "raw": raw}
            return status, raw, parse_failure

        processed = len(completed)
        worker_count = max(1, int(self.policy.max_workers))
        for offset in range(0, len(remaining), worker_count):
            action = control.action()
            if action == "cancel":
                run.status = RunStatus.CANCELLED
                break
            if action == "pause":
                run.status = RunStatus.PAUSED
                self.store.write_json(f"runs/{run.run_id}/run.json", run)
                self.store.append_audit(run.run_id, {"at": utc_now(), "event": "paused", "completed": len(completed)})
                return run
            batch = remaining[offset:offset + worker_count]
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="textual-archive") as executor:
                futures = [(target, executor.submit(process, target)) for target in batch]
                for target, future in futures:
                    status, raw, parse_failure = future.result()
                    if parse_failure:
                        self.store.append_audit(run.run_id, parse_failure)
                    completed[target.identity] = status.value
                    counts[status.value] = counts.get(status.value, 0) + 1
                    processed += 1
                    checkpoint = {"completed": completed, "attempted": processed, "updated_at": utc_now()}
                    if processed % max(1, self.policy.checkpoint_every) == 0:
                        self.store.write_json(f"runs/{run.run_id}/checkpoint.json", checkpoint)
                    self.store.append_audit(run.run_id, {"at": utc_now(), "event": "target_finished", "identity": target.identity, "status": status, "raw": raw})
        self.store.write_json(f"runs/{run.run_id}/checkpoint.json", {"completed": completed, "attempted": len(completed), "updated_at": utc_now()})
        run.counts = {"total": len(targets), "completed": len(completed), **counts}
        if run.status != RunStatus.CANCELLED:
            gaps = sum(value not in {CollectionStatus.NORMALIZED.value, CollectionStatus.PARSED.value} for value in completed.values())
            if run.mode == "pilot":
                run.status = RunStatus.PILOT_REVIEW
            else:
                run.status = RunStatus.COMPLETED_WITH_GAPS if gaps else RunStatus.COMPLETED
        run.updated_at = utc_now()
        self.store.write_json(f"runs/{run.run_id}/run.json", run)
        self.store.append_audit(run.run_id, {"at": utc_now(), "event": "run_finished", "status": run.status, "counts": run.counts})
        return run
