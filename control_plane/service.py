from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from .models import CollectionItem, CollectionJob, JobEvent

ACTIVE_STATUSES = {"queued", "running", "pause_requested", "paused", "cancel_requested"}
TERMINAL_STATUSES = {"completed", "completed_with_errors", "failed", "cancelled"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def add_event(
    session: Session,
    job: CollectionJob,
    message: str,
    level: str = "info",
    **detail: Any,
) -> None:
    session.add(JobEvent(job_id=job.id, level=level, message=message, detail=detail))


def create_job(
    session: Session,
    first_sequence: int,
    last_sequence: int,
    *,
    collect_incidents: bool = True,
    collect_sources: bool = True,
    write_report: bool = True,
    configuration: dict[str, Any] | None = None,
) -> CollectionJob:
    if first_sequence < 1 or last_sequence > 8114 or last_sequence < first_sequence:
        raise ValueError("النطاق يجب أن يكون بين 1 و8114 وبترتيب صحيح")
    if not (collect_incidents or collect_sources):
        raise ValueError("اختر جمع الحوادث أو المصادر على الأقل")
    overlap = session.scalar(
        select(CollectionJob).where(
            CollectionJob.status.in_(ACTIVE_STATUSES),
            and_(
                CollectionJob.first_sequence <= last_sequence,
                CollectionJob.last_sequence >= first_sequence,
            ),
        )
    )
    if overlap:
        raise ValueError(f"يتداخل النطاق مع المهمة النشطة {overlap.id}")
    job = CollectionJob(
        first_sequence=first_sequence,
        last_sequence=last_sequence,
        collect_incidents=collect_incidents,
        collect_sources=collect_sources,
        write_report=write_report,
        configuration=configuration or {},
        incident_total=last_sequence - first_sequence + 1,
    )
    session.add(job)
    session.flush()
    add_event(
        session, job, f"أُنشئت المهمة للنطاق {first_sequence:04d}–{last_sequence:04d}"
    )
    session.commit()
    return job


def request_action(session: Session, job: CollectionJob, action: str) -> str:
    if action == "pause" and job.status == "queued":
        job.requested_action = None
        job.status = "paused"
        message = "أُوقفت المهمة مؤقتًا قبل أن يبدأ العامل"
    elif action == "pause" and job.status == "running":
        job.requested_action = "pause"
        job.status = "pause_requested"
        message = "طُلب إيقاف المهمة مؤقتًا"
    elif action == "cancel" and job.status == "paused":
        job.requested_action = None
        job.status = "cancelled"
        job.current_stage = "cancelled"
        job.finished_at = now_utc()
        message = "أُلغيت المهمة المتوقفة مع الاحتفاظ بكل ما حُفظ"
    elif action == "cancel" and job.status == "queued":
        job.requested_action = None
        job.status = "cancelled"
        job.current_stage = "cancelled"
        job.finished_at = now_utc()
        message = "أُلغيت المهمة قبل أن يبدأ الجمع"
    elif action == "cancel" and job.status in {"running", "pause_requested"}:
        job.requested_action = "cancel"
        job.status = "cancel_requested"
        message = "طُلب إلغاء المهمة بعد نقطة الحفظ الآمنة التالية"
    elif action == "resume" and job.status in {"paused", "failed"}:
        job.requested_action = None
        job.status = "queued"
        job.last_error = None
        job.finished_at = None
        message = "أُعيدت المهمة إلى الطابور من نقطة التقدم المحفوظة"
    elif action == "retry_failed" and job.status in TERMINAL_STATUSES:
        job.requested_action = "retry_failed"
        job.status = "queued"
        job.last_error = None
        job.finished_at = None
        message = "أُعيدت العناصر الفاشلة إلى الطابور"
    else:
        raise ValueError("هذا الإجراء غير مسموح في حالة المهمة الحالية")
    add_event(session, job, message)
    session.commit()
    return message


def upsert_item(
    session: Session,
    job: CollectionJob,
    kind: str,
    identity: str,
    status: str,
    *,
    attempts: int = 0,
    duration_seconds: float = 0,
    error: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    values = {
        "job_id": job.id,
        "kind": kind,
        "identity": identity,
        "status": status,
        "attempts": attempts,
        "duration_seconds": duration_seconds,
        "error": error,
        "detail": detail or {},
        "updated_at": now_utc(),
    }
    if session.bind and session.bind.dialect.name == "postgresql":
        statement = pg_insert(CollectionItem).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["job_id", "kind", "identity"],
            set_={
                key: value
                for key, value in values.items()
                if key not in {"job_id", "kind", "identity"}
            },
        )
        session.execute(statement)
        return
    existing = session.scalar(
        select(CollectionItem).where(
            CollectionItem.job_id == job.id,
            CollectionItem.kind == kind,
            CollectionItem.identity == identity,
        )
    )
    if existing:
        for key, value in values.items():
            if key not in {"job_id", "kind", "identity"}:
                setattr(existing, key, value)
    else:
        session.add(CollectionItem(**values))


def job_snapshot(session: Session, job: CollectionJob) -> dict[str, Any]:
    events = session.scalars(
        select(JobEvent)
        .where(JobEvent.job_id == job.id)
        .order_by(JobEvent.id.desc())
        .limit(40)
    ).all()
    total = job.incident_total + job.source_total
    completed = job.incident_completed + job.source_completed
    return {
        "id": job.id,
        "range": f"{job.first_sequence:04d}–{job.last_sequence:04d}",
        "status": job.status,
        "stage": job.current_stage,
        "percent": round((completed / total) * 100, 1) if total else 0,
        "incidents": {
            "completed": job.incident_completed,
            "failed": job.incident_failed,
            "total": job.incident_total,
        },
        "sources": {
            "completed": job.source_completed,
            "failed": job.source_failed,
            "total": job.source_total,
        },
        "last_error": job.last_error,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "events": [
            {
                "level": event.level,
                "message": event.message,
                "created_at": event.created_at.isoformat(),
            }
            for event in events
        ],
    }
