from __future__ import annotations

import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from archive_engine.connectors.airwars import AirwarsConnector
from archive_engine.connectors.synthetic import SyntheticLibraryConnector
from archive_engine.core.engine import ArchiveEngine, EngineControl, RunPolicy
from archive_engine.core.store import ProjectStore
from archive_engine.fetchers.http import HttpFetcher
from archive_engine.models import ArchiveProject, ArchiveSite, EngineRun
from archive_engine.publisher.atomic import AtomicPublisher
from archive_engine.release_builder import GenericTextualReleaseBuilder
from archive_engine.statuses import CollectionStatus, RunStatus
from archive_engine.validators.release import ReleaseValidator

from .config import get_settings
from .db import session_factory
from .models import ArchiveProjectRecord, ArchiveReleaseRecord, GeneralEngineRunRecord
from .tasks import celery_app


settings = get_settings()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _project(row: ArchiveProjectRecord) -> ArchiveProject:
    return ArchiveProject(
        project_id=row.id,
        name=row.name,
        site=ArchiveSite(row.target_url, list(row.allowed_domains or []), row.connector),
        scope=dict(row.scope or {}),
        collection_limits=dict(row.collection_limits or {}),
        rate_policy=dict(row.rate_policy or {}),
        text_only=bool(row.text_only),
        release_name=row.release_name,
        created_at=row.created_at.isoformat() if row.created_at else _now().isoformat(),
    )


def _connector(project: ArchiveProject):
    if project.site.connector == "airwars":
        return AirwarsConnector(settings.project_root, settings.legacy_zip)
    if project.site.connector == "synthetic_library":
        return SyntheticLibraryConnector()
    raise ValueError(f"unsupported_connector:{project.site.connector}")


def _fetcher(project: ArchiveProject) -> HttpFetcher:
    rate = project.rate_policy or {}
    limits = project.collection_limits or {}
    return HttpFetcher(
        timeout=float(limits.get("timeout_seconds") or 15),
        retries=int(limits.get("retries") or 2),
        per_host_delay=float(rate.get("per_host_delay_seconds") or 0.2),
        allowed_domains=set(project.site.allowed_domains),
    )


def enqueue_general_run(run_id: str) -> str:
    return str(run_general_engine.delay(run_id).id)


def enqueue_release_build(run_id: str) -> str:
    return str(build_general_release.delay(run_id).id)


@celery_app.task(bind=True, name="archive.general_engine_run")
def run_general_engine(self, run_record_id: str) -> dict[str, Any]:
    with session_factory()() as session:
        row = session.get(GeneralEngineRunRecord, run_record_id)
        if not row:
            return {"status": "missing"}
        project_row = session.get(ArchiveProjectRecord, row.project_id)
        if not project_row:
            return {"status": "missing_project"}
        project = _project(project_row)
        row.task_id = self.request.id
        row.status = RunStatus.ANALYZING.value if row.mode == "analysis" else RunStatus.QUEUED.value
        row.started_at = row.started_at or _now()
        row.last_error = None
        session.commit()

    store = ProjectStore(settings.engine_store_root)
    connector = _connector(project)
    fetcher = _fetcher(project)
    policy = RunPolicy(
        max_workers=int((row.configuration or {}).get("max_workers") or 1),
        checkpoint_every=int((row.configuration or {}).get("checkpoint_every") or 1),
        pilot_limit=int((row.configuration or {}).get("pilot_limit") or 10),
    )
    engine = ArchiveEngine(store, connector, fetcher, policy)
    try:
        if row.mode == "analysis":
            samples: dict[str, bytes] = {}
            response = fetcher.fetch(project.site.target_url)
            if response.outcome == CollectionStatus.FETCHED:
                samples[response.final_url] = response.body
            result = engine.analyze(project, samples)
            with session_factory()() as session:
                row = session.get(GeneralEngineRunRecord, run_record_id)
                project_row = session.get(ArchiveProjectRecord, project.project_id)
                if row and project_row:
                    row.status = RunStatus.COMPLETED.value
                    row.result = result
                    row.counts = {"sample_pages": len(samples), "candidate_record_types": len(result.get("candidate_record_types") or result.get("record_types") or [])}
                    row.finished_at = _now()
                    project_row.analysis = result
                    project_row.status = RunStatus.READY.value
                    session.commit()
            return {"status": RunStatus.COMPLETED.value, "result": result}

        stored = store.read_json(f"runs/{row.engine_run_id}/run.json")
        if stored:
            stored["status"] = RunStatus(stored["status"])
            run = EngineRun(**stored)
        else:
            run = engine.create_run(project, row.mode, row.engine_run_id)
        result_run = engine.execute(project, run)
        checkpoint = store.read_json(f"runs/{row.engine_run_id}/checkpoint.json", {})
        with session_factory()() as session:
            row = session.get(GeneralEngineRunRecord, run_record_id)
            project_row = session.get(ArchiveProjectRecord, project.project_id)
            if row:
                row.status = result_run.status.value
                row.counts = dict(result_run.counts or {})
                row.checkpoint = checkpoint
                row.result = {"worklist_hash": result_run.worklist_hash, "audit_log": f"runs/{row.engine_run_id}/audit.jsonl"}
                if result_run.status in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_GAPS, RunStatus.CANCELLED, RunStatus.PILOT_REVIEW}:
                    row.finished_at = _now()
            if project_row:
                project_row.status = result_run.status.value
            session.commit()
        return {"status": result_run.status.value, "counts": result_run.counts}
    except Exception as error:
        with session_factory()() as session:
            row = session.get(GeneralEngineRunRecord, run_record_id)
            if row:
                row.status = RunStatus.FAILED.value
                row.last_error = f"{type(error).__name__}: {error}"
                row.result = {"traceback": traceback.format_exc(limit=30)}
                row.finished_at = _now()
                session.commit()
        raise


@celery_app.task(bind=True, name="archive.general_release_build")
def build_general_release(self, run_record_id: str) -> dict[str, Any]:
    with session_factory()() as session:
        run_row = session.get(GeneralEngineRunRecord, run_record_id)
        if not run_row:
            return {"status": "missing"}
        project_row = session.get(ArchiveProjectRecord, run_row.project_id)
        if not project_row:
            return {"status": "missing_project"}
        project = _project(project_row)
        stamp = _now().strftime("%Y%m%dT%H%M%SZ")
        release_id = f"{project.release_name}-{stamp}-{run_row.id[:8]}"
        publisher = AtomicPublisher(settings.releases_root)
        parent = publisher.current_release()
        release_row = ArchiveReleaseRecord(
            id=release_id,
            project_id=project.id if hasattr(project, "id") else project.project_id,
            run_id=run_row.id,
            parent_release_id=parent.name if parent else None,
            release_path=str(settings.releases_root / "releases" / release_id),
            status=RunStatus.BUILDING_RELEASE.value,
        )
        session.add(release_row)
        run_row.status = RunStatus.BUILDING_RELEASE.value
        session.commit()
    try:
        result = GenericTextualReleaseBuilder(
            settings.engine_store_root,
            project,
            run_row.engine_run_id,
            Path(release_row.release_path),
            release_id=release_id,
            parent_release_id=release_row.parent_release_id,
        ).build()
        validation = ReleaseValidator().validate(Path(release_row.release_path))
        with session_factory()() as session:
            release_row = session.get(ArchiveReleaseRecord, release_id)
            run_row = session.get(GeneralEngineRunRecord, run_record_id)
            if release_row:
                release_row.status = RunStatus.RELEASE_VALIDATION.value if not validation.passed else RunStatus.COMPLETED.value
                release_row.manifest = result
                release_row.validation = {"passed": validation.passed, "blocking_failures": validation.blocking_failures, "checks": validation.checks}
            if run_row:
                run_row.status = RunStatus.COMPLETED.value if validation.passed else RunStatus.FAILED.value
            session.commit()
        return {"status": "passed" if validation.passed else "failed", **result}
    except Exception as error:
        with session_factory()() as session:
            release_row = session.get(ArchiveReleaseRecord, release_id)
            run_row = session.get(GeneralEngineRunRecord, run_record_id)
            if release_row:
                release_row.status = RunStatus.FAILED.value
                release_row.validation = {"passed": False, "error": f"{type(error).__name__}: {error}"}
            if run_row:
                run_row.status = RunStatus.FAILED.value
                run_row.last_error = f"{type(error).__name__}: {error}"
            session.commit()
        raise


def request_engine_action(run_row: GeneralEngineRunRecord, action: str) -> None:
    if action not in {"pause", "resume", "cancel"}:
        raise ValueError(f"unsupported_engine_action:{action}")
    store = ProjectStore(settings.engine_store_root)
    EngineControl(store, run_row.engine_run_id).request(action)


def publish_release(release_path: Path) -> tuple[str | None, str]:
    publisher = AtomicPublisher(settings.releases_root)
    previous, current = publisher.publish(release_path, lambda path: ReleaseValidator().validate(path).passed and (path / "site" / "index.html").is_file())
    return previous.name if previous else None, current.name


def rollback_release(release_path: Path) -> str:
    publisher = AtomicPublisher(settings.releases_root)
    return publisher.rollback(release_path, lambda path: ReleaseValidator().validate(path).passed and (path / "site" / "index.html").is_file()).name
