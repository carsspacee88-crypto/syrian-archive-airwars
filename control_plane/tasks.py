from __future__ import annotations

import traceback
import time
from typing import Any

from celery import Celery
from sqlalchemy import func, select

from archive_pipeline.speed_pilot import (
    DEFERRED_SOURCE_STATUSES,
    ENGINE_VERSION,
    OPERATIONAL_FAILURE_STATUSES,
    RETRYABLE_SOURCE_STATUSES,
    SUCCESS_SOURCE_STATUSES,
    SpeedPilotRunner,
)
from archive_pipeline.io_utils import load_json

from .config import get_settings
from .db import session_factory
from .models import CollectionItem, CollectionJob
from .service import add_event, now_utc, upsert_item

settings = get_settings()
celery_app = Celery(
    "archive_control_plane", broker=settings.redis_url, backend=settings.redis_url
)
celery_app.conf.update(
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    result_expires=86400,
    timezone="UTC",
)


def _version_tuple(value: object) -> tuple[int, int, int]:
    try:
        parts = [int(part) for part in str(value).split(".")[:3]]
    except ValueError:
        return (0, 0, 0)
    return tuple((parts + [0, 0, 0])[:3])  # type: ignore[return-value]


def enqueue_job(job_id: str) -> str:
    result = run_collection.delay(job_id)
    return str(result.id)


class JobProgressSink:
    """Coalesce per-item collector events into sub-second durable UI updates."""

    def __init__(self, job_id: str, interval: float | None = None, batch_size: int = 32):
        self.job_id = job_id
        self.interval = interval or settings.live_update_interval
        self.batch_size = max(1, batch_size)
        self.pending: dict[tuple[str, str], dict[str, Any]] = {}
        self.source_total: int | None = None
        self.incident_total: int | None = None
        self.last_flush = time.monotonic()
        self.last_control_check = 0.0
        self.stop_requested = False

    def refresh_control(self) -> bool:
        now = time.monotonic()
        if now - self.last_control_check < 0.5:
            return self.stop_requested
        with session_factory()() as session:
            job = session.get(CollectionJob, self.job_id)
            self.stop_requested = bool(
                not job or job.requested_action in {"pause", "cancel"}
            )
        self.last_control_check = now
        return self.stop_requested

    def __call__(self, payload: dict[str, Any]) -> None:
        if payload.get("kind") == "catalog":
            self.source_total = int(payload.get("source_total") or 0)
            self.incident_total = int(payload.get("incident_total") or 0)
            self.flush(force=True)
            return
        kind = str(payload.get("kind") or "")
        identity = str(payload.get("identity") or "")
        if kind not in {"incident", "source"} or not identity:
            return
        self.pending[(kind, identity)] = payload
        if len(self.pending) >= self.batch_size or time.monotonic() - self.last_flush >= self.interval:
            self.flush(force=True)

    def flush(self, force: bool = False) -> None:
        if not self.pending and self.source_total is None and self.incident_total is None:
            return
        if not force and time.monotonic() - self.last_flush < self.interval:
            return
        with session_factory()() as session:
            job = session.get(CollectionJob, self.job_id)
            if not job:
                self.pending.clear()
                return
            self.stop_requested = job.requested_action in {"pause", "cancel"}
            identities = [identity for _kind, identity in self.pending]
            existing_rows = session.execute(
                select(CollectionItem.kind, CollectionItem.identity, CollectionItem.status)
                .where(
                    CollectionItem.job_id == job.id,
                    CollectionItem.identity.in_(identities),
                )
            ).all() if identities else []
            previous_status = {
                (str(kind), str(identity)): str(status)
                for kind, identity, status in existing_rows
            }
            incident_completed = int(job.incident_completed or 0)
            incident_failed = int(job.incident_failed or 0)
            source_completed = int(job.source_completed or 0)
            source_failed = int(job.source_failed or 0)
            for payload in self.pending.values():
                key = (str(payload["kind"]), str(payload["identity"]))
                old_status = previous_status.get(key)
                new_status = str(payload.get("status") or "failed")
                upsert_item(
                    session,
                    job,
                    str(payload["kind"]),
                    str(payload["identity"]),
                    new_status,
                    attempts=int(payload.get("attempts") or 0),
                    duration_seconds=float(payload.get("duration_seconds") or 0),
                    error=payload.get("error"),
                    detail=dict(payload.get("detail") or {}),
                )
                if key[0] == "incident":
                    if old_status is None:
                        incident_completed += 1
                    incident_failed += int(new_status == "failed") - int(old_status == "failed")
                else:
                    if old_status is None:
                        source_completed += 1
                    source_failed += int(new_status in OPERATIONAL_FAILURE_STATUSES) - int(
                        old_status in OPERATIONAL_FAILURE_STATUSES
                    )
            if self.source_total is not None:
                job.source_total = self.source_total
            if self.incident_total is not None:
                job.incident_total = self.incident_total
            job.incident_completed = max(0, incident_completed)
            job.incident_failed = max(0, incident_failed)
            job.source_completed = max(0, source_completed)
            job.source_failed = max(0, source_failed)
            job.updated_at = now_utc()
            session.commit()
        self.pending.clear()
        self.source_total = None
        self.incident_total = None
        self.last_flush = time.monotonic()


def _runner(job: CollectionJob, progress_callback: JobProgressSink | None = None) -> SpeedPilotRunner:
    configuration = job.configuration or {}
    return SpeedPilotRunner(
        settings.project_root,
        settings.legacy_zip,
        first_sequence=job.first_sequence,
        last_sequence=job.last_sequence,
        delay=float(configuration.get("delay", settings.collector_delay)),
        timeout=float(configuration.get("timeout", settings.collector_timeout)),
        retries=int(configuration.get("retries", settings.collector_retries)),
        workers=int(configuration.get("workers", settings.collector_workers)),
        per_host_workers=int(
            configuration.get("per_host_workers", settings.collector_per_host_workers)
        ),
        social_workers=int(
            configuration.get("social_workers", settings.collector_social_workers)
        ),
        archive_workers=int(
            configuration.get("archive_workers", settings.collector_archive_workers)
        ),
        checkpoint_every=int(
            configuration.get("checkpoint_every", settings.collector_checkpoint_every)
        ),
        fast_timeout=float(
            configuration.get("fast_timeout", settings.collector_fast_timeout)
        ),
        incident_mode=str(
            configuration.get("incident_mode", settings.incident_mode)
        ),
        inline_wayback=bool(
            configuration.get("inline_wayback", settings.inline_wayback)
        ),
        progress_callback=progress_callback,
    )


def _reset_failed_progress(runner: SpeedPilotRunner) -> tuple[int, int]:
    incident_failed = {
        int(row.get("sequence") or 0)
        for row in runner.progress.get("incident_timings") or []
        if row.get("error") or row.get("status") == "failed"
    }
    source_failed = {
        source_id
        for source_id, outcome in (runner.progress.get("source_outcomes") or {}).items()
        if outcome.get("status") in RETRYABLE_SOURCE_STATUSES
    }
    runner.progress["incident_completed_sequences"] = [
        value
        for value in runner.progress.get("incident_completed_sequences") or []
        if int(value) not in incident_failed
    ]
    runner.progress["source_completed_ids"] = [
        value
        for value in runner.progress.get("source_completed_ids") or []
        if value not in source_failed
    ]
    for source_id in source_failed:
        runner.progress.get("source_outcomes", {}).pop(source_id, None)
    runner._incident_completed_set = {
        int(value) for value in runner.progress.get("incident_completed_sequences") or []
    }
    runner._source_completed_set = set(runner.progress.get("source_completed_ids") or [])
    runner.save_progress()
    return len(incident_failed), len(source_failed)


def _sync_progress(
    job_id: str, runner: SpeedPilotRunner, result: dict[str, Any] | None = None
) -> None:
    with session_factory()() as session:
        job = session.get(CollectionJob, job_id)
        if not job:
            return
        incident_rows = [
            row
            for row in runner.progress.get("incident_timings") or []
            if job.first_sequence <= int(row.get("sequence") or 0) <= job.last_sequence
        ]
        for row in incident_rows:
            status = str(row.get("status") or "failed")
            upsert_item(
                session,
                job,
                "incident",
                f"{int(row.get('sequence') or 0):04d}",
                status,
                duration_seconds=float(row.get("duration_seconds") or 0),
                error=row.get("error"),
                detail={"internal_id": row.get("internal_id")},
            )
        job.incident_completed = len(
            {int(row.get("sequence") or 0) for row in incident_rows}
        )
        job.incident_failed = sum(
            bool(row.get("error")) or row.get("status") == "failed"
            for row in incident_rows
        )

        timing_by_id = {
            row.get("source_id"): row
            for row in runner.progress.get("source_timings") or []
        }
        outcomes = runner.progress.get("source_outcomes") or {}
        existing_source_rows = session.execute(
            select(CollectionItem.identity, CollectionItem.status).where(
                CollectionItem.job_id == job.id,
                CollectionItem.kind == "source",
            )
        ).all()
        existing_source_status = {
            str(identity): str(status)
            for identity, status in existing_source_rows
        }
        # Live events already contain the rich timings. Reconcile only an item
        # whose file was committed while the dashboard database was briefly
        # unavailable; never rewrite all historical rows after every chunk.
        source_ids_to_reconcile = {
            source_id
            for source_id, outcome in outcomes.items()
            if source_id not in existing_source_status
            or existing_source_status[source_id] != str(outcome.get("status") or "failed")
        }
        for source_id in source_ids_to_reconcile:
            outcome = outcomes[source_id]
            source_record = load_json(
                runner.root / "data" / "sources" / f"{source_id}.json", {}
            ) or {}
            checkpoint_outcome = dict(
                (source_record.get("collection_checkpoint") or {}).get("outcome") or {}
            )
            merged_outcome = {**outcome, **checkpoint_outcome}
            timing = timing_by_id.get(source_id) or {}
            status = str(merged_outcome.get("status") or timing.get("status") or "failed")
            detail = {
                "cache_hit": bool(merged_outcome.get("cache_hit")),
                "archive_deferred": bool(merged_outcome.get("archive_deferred")),
                "text_preserved": bool(merged_outcome.get("text_preserved")),
                "quality_score": merged_outcome.get("quality_score"),
                "source_type": merged_outcome.get("source_type"),
                "host": merged_outcome.get("host") or "",
                "provenance": merged_outcome.get("provenance") or "",
                "resolution_class": merged_outcome.get("resolution_class") or "",
            }
            for metric_key in (
                "network_seconds",
                "queue_seconds",
                "pacing_seconds",
                "retry_seconds",
                "persist_seconds",
            ):
                if merged_outcome.get(metric_key) is not None:
                    detail[metric_key] = merged_outcome[metric_key]
            upsert_item(
                session,
                job,
                "source",
                source_id,
                status,
                attempts=int(timing.get("attempts") or 0),
                duration_seconds=float(
                    timing.get("duration_seconds")
                    or merged_outcome.get("duration_seconds")
                    or 0
                ),
                error=None if status in SUCCESS_SOURCE_STATUSES else status,
                detail=detail,
            )
        job.source_completed = max(int(job.source_completed or 0), len(outcomes))
        job.source_failed = sum(
            str(outcome.get("status") or "") in OPERATIONAL_FAILURE_STATUSES
            for outcome in outcomes.values()
        )
        if result and result.get("total") is not None:
            job.source_total = int(result["total"])
        job.updated_at = now_utc()
        session.commit()


def _control_action(job_id: str) -> str | None:
    with session_factory()() as session:
        job = session.get(CollectionJob, job_id)
        if not job:
            return "cancel"
        action = job.requested_action
        if action == "pause":
            job.status = "paused"
            job.requested_action = None
            add_event(session, job, "توقفت المهمة مؤقتًا عند نقطة حفظ آمنة")
            session.commit()
            return "pause"
        if action == "cancel":
            job.status = "cancelled"
            job.current_stage = "cancelled"
            job.requested_action = None
            job.finished_at = now_utc()
            add_event(session, job, "أُلغيت المهمة مع الاحتفاظ بكل ما حُفظ")
            session.commit()
            return "cancel"
    return None


def _set_stage(job_id: str, stage: str, message: str) -> None:
    with session_factory()() as session:
        job = session.get(CollectionJob, job_id)
        if not job:
            return
        job.current_stage = stage
        add_event(session, job, message)
        session.commit()


@celery_app.task(bind=True, name="archive.run_collection")
def run_collection(self, job_id: str) -> dict[str, Any]:
    runner: SpeedPilotRunner | None = None
    progress_sink = JobProgressSink(job_id)
    try:
        with session_factory()() as session:
            job = session.scalar(
                select(CollectionJob)
                .where(CollectionJob.id == job_id)
                .with_for_update()
            )
            if not job:
                return {"status": "missing"}
            redelivered_after_worker_loss = (
                job.status == "running" and job.task_id == self.request.id
            )
            if job.status != "queued" and not redelivered_after_worker_loss:
                return {"status": job.status}
            retry_failed = job.requested_action == "retry_failed"
            job.requested_action = None
            configuration = dict(job.configuration or {})
            configured_version = configuration.get("engine_version")
            if configured_version and _version_tuple(configured_version) > _version_tuple(ENGINE_VERSION):
                raise RuntimeError(
                    f"unsafe_engine_downgrade:{configured_version}_to_{ENGINE_VERSION}"
                )
            upgrading_legacy_job = configuration.get("engine_version") != ENGINE_VERSION
            if upgrading_legacy_job:
                configuration.update({
                    "workers": settings.collector_workers,
                    "per_host_workers": settings.collector_per_host_workers,
                    "social_workers": settings.collector_social_workers,
                    "archive_workers": settings.collector_archive_workers,
                    "delay": settings.collector_delay,
                    "timeout": settings.collector_timeout,
                    "fast_timeout": settings.collector_fast_timeout,
                    "checkpoint_every": settings.collector_checkpoint_every,
                    "incident_chunk_size": settings.incident_chunk_size,
                    "source_chunk_size": settings.source_chunk_size,
                    "incident_mode": settings.incident_mode,
                    "inline_wayback": settings.inline_wayback,
                    "performance_profile": "balanced",
                })
            configuration["engine_version"] = ENGINE_VERSION
            job.configuration = configuration
            job.status = "running"
            job.current_stage = "preparing"
            job.task_id = self.request.id
            job.started_at = job.started_at or now_utc()
            add_event(
                session,
                job,
                "رُقّيت إعدادات المهمة السابقة إلى محرك V4"
                if upgrading_legacy_job
                else "استأنف عامل بديل المهمة بعد انقطاع العامل السابق"
                if redelivered_after_worker_loss
                else "بدأ عامل الجمع تنفيذ المهمة",
                level="warning" if redelivered_after_worker_loss else "info",
            )
            session.commit()
            runner = _runner(job, progress_sink)

        if retry_failed:
            # Recovery mode is intentionally separate from the fast foreground
            # pass: only unresolved items are requeued and Wayback is enabled.
            runner.inline_wayback = True
            incidents, sources = _reset_failed_progress(runner)
            with session_factory()() as session:
                job = session.get(CollectionJob, job_id)
                if job:
                    add_event(
                        session,
                        job,
                        "أُعيدت عناصر الاسترداد المؤجلة والأخطاء التشغيلية فقط",
                        incidents=incidents,
                        sources=sources,
                    )
                    session.commit()

        _set_stage(job_id, "manifest", "إنشاء بيان النطاق والتحقق من الهويات")
        runner.run("manifest")
        progress_sink.flush(force=True)
        _sync_progress(job_id, runner)
        if _control_action(job_id):
            return {"status": "stopped"}

        with session_factory()() as session:
            job = session.get(CollectionJob, job_id)
            collect_incidents = bool(job and job.collect_incidents)
            collect_sources = bool(job and job.collect_sources)
            write_report = bool(job and job.write_report)
            configuration = (job.configuration if job else {}) or {}
            incident_chunk_size = int(configuration.get("incident_chunk_size", settings.incident_chunk_size))
            source_chunk_size = int(configuration.get("source_chunk_size", settings.source_chunk_size))

        if collect_incidents:
            _set_stage(job_id, "incidents", "بدأ جمع الحوادث على دفعات قابلة للاستئناف")
            action = None
            while True:
                result = runner.run("incidents", incident_chunk_size)
                progress_sink.flush(force=True)
                _sync_progress(job_id, runner, result)
                action = _control_action(job_id)
                if result.get("done") or action:
                    break
            if action or not result.get("done"):
                return {"status": "stopped"}

        if collect_sources:
            _set_stage(
                job_id,
                "sources",
                "بدأ محرك V4: مجدول عادل، استخراج متعدد الصيغ، وطابور استرداد دائم",
            )
            action = None
            while True:
                result = runner.run("sources", source_chunk_size)
                progress_sink.flush(force=True)
                _sync_progress(job_id, runner, result)
                action = _control_action(job_id)
                if result.get("done") or action:
                    break
            if action or not result.get("done"):
                return {"status": "stopped"}

        if write_report:
            _set_stage(job_id, "report", "إنشاء تقرير المهمة النهائي")
            runner.run("report")
            progress_sink.flush(force=True)
            _sync_progress(job_id, runner)

        with session_factory()() as session:
            job = session.get(CollectionJob, job_id)
            if not job:
                return {"status": "missing"}
            job.current_stage = "complete"
            deferred = session.scalar(
                select(func.count(CollectionItem.id)).where(
                    CollectionItem.job_id == job.id,
                    CollectionItem.kind == "source",
                    CollectionItem.status.in_(DEFERRED_SOURCE_STATUSES),
                )
            ) or 0
            job.status = (
                "completed_with_errors"
                if job.incident_failed or job.source_failed
                else "completed_with_gaps"
                if deferred
                else "completed"
            )
            job.finished_at = now_utc()
            add_event(
                session,
                job,
                "انتهت المهمة؛ كل العناصر حُفظت، والمتعذر الخارجي بقي في طابور الاسترداد"
                if deferred
                else "انتهت المهمة",
                level="warning" if job.status != "completed" else "info",
                deferred_sources=int(deferred),
            )
            session.commit()
            return {"status": job.status, "job_id": job.id}
    except Exception as error:
        try:
            progress_sink.flush(force=True)
        except Exception:
            pass
        trace = traceback.format_exc(limit=30)
        with session_factory()() as session:
            job = session.get(CollectionJob, job_id)
            if job:
                job.status = "failed"
                job.current_stage = "infrastructure_error"
                job.last_error = f"{type(error).__name__}: {error}"
                job.finished_at = now_utc()
                add_event(
                    session,
                    job,
                    "تعطل عامل المهمة؛ يمكن استئنافها من آخر نقطة حفظ",
                    level="error",
                    traceback=trace,
                )
                session.commit()
        raise
    finally:
        if runner is not None:
            try:
                runner.close()
            except Exception:
                pass


# Import after ``celery_app`` and legacy task definitions exist so the generic
# engine and release tasks register on the same worker without a second broker.
from . import general_tasks as _general_tasks  # noqa: E402,F401
