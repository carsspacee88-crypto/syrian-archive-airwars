from __future__ import annotations

import traceback
from typing import Any

from celery import Celery
from sqlalchemy import select

from archive_pipeline.speed_pilot import SpeedPilotRunner

from .config import get_settings
from .db import session_factory
from .models import CollectionJob
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


def enqueue_job(job_id: str) -> str:
    result = run_collection.delay(job_id)
    return str(result.id)


def _runner(job: CollectionJob) -> SpeedPilotRunner:
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
        if outcome.get("status") not in {"successful", "cached"}
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
        for source_id, outcome in outcomes.items():
            timing = timing_by_id.get(source_id) or {}
            status = str(outcome.get("status") or timing.get("status") or "failed")
            upsert_item(
                session,
                job,
                "source",
                source_id,
                status,
                attempts=int(timing.get("attempts") or 0),
                duration_seconds=float(timing.get("duration_seconds") or 0),
                error=None if status == "successful" else status,
                detail={
                    "cache_hit": bool(outcome.get("cache_hit")),
                    "archive_deferred": bool(outcome.get("archive_deferred")),
                },
            )
        job.source_completed = len(outcomes)
        job.source_failed = sum(
            str(outcome.get("status") or "") not in {"successful", "cached"}
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
            job.status = "running"
            job.current_stage = "preparing"
            job.task_id = self.request.id
            job.started_at = job.started_at or now_utc()
            add_event(
                session,
                job,
                "استأنف عامل بديل المهمة بعد انقطاع العامل السابق"
                if redelivered_after_worker_loss
                else "بدأ عامل الجمع تنفيذ المهمة",
                level="warning" if redelivered_after_worker_loss else "info",
            )
            session.commit()
            runner = _runner(job)

        if retry_failed:
            incidents, sources = _reset_failed_progress(runner)
            with session_factory()() as session:
                job = session.get(CollectionJob, job_id)
                if job:
                    add_event(
                        session,
                        job,
                        "أُعيد ضبط العناصر الفاشلة فقط",
                        incidents=incidents,
                        sources=sources,
                    )
                    session.commit()

        _set_stage(job_id, "manifest", "إنشاء بيان النطاق والتحقق من الهويات")
        runner.run("manifest")
        _sync_progress(job_id, runner)
        if _control_action(job_id):
            return {"status": "stopped"}

        with session_factory()() as session:
            job = session.get(CollectionJob, job_id)
            collect_incidents = bool(job and job.collect_incidents)
            collect_sources = bool(job and job.collect_sources)
            write_report = bool(job and job.write_report)

        if collect_incidents:
            _set_stage(job_id, "incidents", "بدأ جمع الحوادث على دفعات قابلة للاستئناف")
            action = None
            while True:
                result = runner.run("incidents", settings.incident_chunk_size)
                _sync_progress(job_id, runner, result)
                action = _control_action(job_id)
                if result.get("done") or action:
                    break
            if action or not result.get("done"):
                return {"status": "stopped"}

        if collect_sources:
            _set_stage(
                job_id, "sources", "بدأ جمع المصادر؛ فشل عنصر لا يوقف بقية العناصر"
            )
            action = None
            while True:
                result = runner.run("sources", settings.source_chunk_size)
                _sync_progress(job_id, runner, result)
                action = _control_action(job_id)
                if result.get("done") or action:
                    break
            if action or not result.get("done"):
                return {"status": "stopped"}

        if write_report:
            _set_stage(job_id, "report", "إنشاء تقرير المهمة النهائي")
            runner.run("report")
            _sync_progress(job_id, runner)

        with session_factory()() as session:
            job = session.get(CollectionJob, job_id)
            if not job:
                return {"status": "missing"}
            job.current_stage = "complete"
            job.status = (
                "completed_with_errors"
                if job.incident_failed or job.source_failed
                else "completed"
            )
            job.finished_at = now_utc()
            add_event(
                session,
                job,
                "انتهت المهمة",
                level="warning" if job.status.endswith("errors") else "info",
            )
            session.commit()
            return {"status": job.status, "job_id": job.id}
    except Exception as error:
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
