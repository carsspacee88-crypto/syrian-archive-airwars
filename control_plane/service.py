from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import statistics
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from .config import get_settings
from .models import CollectionItem, CollectionJob, JobEvent

ACTIVE_STATUSES = {"queued", "running", "pause_requested", "paused", "cancel_requested"}
TERMINAL_STATUSES = {"completed", "completed_with_gaps", "completed_with_errors", "failed", "cancelled"}
RESOLVED_SOURCE_STATUSES = {
    "successful",
    "successful_partial",
    "cached",
    "embedded_text_preserved",
}
POLICY_COMPLETE_SOURCE_STATUSES = {"media_metadata_preserved"}
DEFERRED_SOURCE_STATUSES = {"recovery_deferred"}
OPERATIONAL_FAILURE_STATUSES = {"failed", "internal_error"}


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
        message = "أُعيدت عناصر الاسترداد والأخطاء التشغيلية إلى الطابور"
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
    settings = get_settings()

    def aware(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def finite_number(value: Any) -> float | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number >= 0 else None

    def percentile(values: list[float], fraction: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = min(
            len(ordered) - 1,
            max(0, math.ceil(len(ordered) * fraction) - 1),
        )
        return round(ordered[index], 2)

    def median(values: list[float]) -> float | None:
        return round(statistics.median(values), 2) if values else None

    def percentage(numerator: int | float, denominator: int | float) -> float:
        if denominator <= 0:
            return 0.0
        return round(min(100.0, max(0.0, (numerator / denominator) * 100)), 1)

    def detail_values(rows: list[CollectionItem], key: str) -> list[float]:
        values: list[float] = []
        for item in rows:
            detail = item.detail or {}
            # Absence is different from a measured zero. Never manufacture a
            # zero merely because an older collector did not emit the metric.
            if key not in detail:
                continue
            value = finite_number(detail.get(key))
            if value is not None:
                values.append(value)
        return values

    events = session.scalars(
        select(JobEvent)
        .where(JobEvent.job_id == job.id)
        .order_by(JobEvent.id.desc())
        .limit(settings.dashboard_event_limit)
    ).all()
    items = session.scalars(
        select(CollectionItem)
        .where(CollectionItem.job_id == job.id)
        .order_by(CollectionItem.updated_at.desc())
        .limit(settings.dashboard_item_limit)
    ).all()
    total = job.incident_total + job.source_total
    completed = job.incident_completed + job.source_completed
    source_stage = bool(
        job.collect_sources
        and (
            job.current_stage in {"sources", "report", "complete"}
            or job.source_completed
        )
    )
    stage_kind = "source" if source_stage else "incident"
    stage_total = job.source_total if stage_kind == "source" else job.incident_total
    stage_completed = job.source_completed if stage_kind == "source" else job.incident_completed
    first_stage_item = session.scalar(
        select(func.min(CollectionItem.updated_at)).where(
            CollectionItem.job_id == job.id,
            CollectionItem.kind == stage_kind,
        )
    )
    now = now_utc()
    clock_start = aware(first_stage_item or job.started_at or job.created_at)
    active_seconds = max(0.001, (now - clock_start).total_seconds())

    def recent_count(seconds: int) -> int:
        return int(
            session.scalar(
                select(func.count(CollectionItem.id)).where(
                    CollectionItem.job_id == job.id,
                    CollectionItem.kind == stage_kind,
                    CollectionItem.updated_at >= now - timedelta(seconds=seconds),
                )
            )
            or 0
        )

    def window_rate(seconds: int, count: int) -> float:
        denominator = max(0.001, min(float(seconds), active_seconds))
        return (count / denominator) * 60

    count_30 = recent_count(30)
    count_60 = recent_count(60)
    count_300 = recent_count(300)
    rate_30 = window_rate(30, count_30)
    rate_60 = window_rate(60, count_60)
    rate_300 = window_rate(300, count_300)
    actively_processing = job.status in {"running", "pause_requested", "cancel_requested"}
    rate_current = rate_30 if actively_processing else 0.0
    rate_average = rate_300
    forecast_rate = rate_60 if count_60 >= 3 else rate_300 if count_300 else rate_30
    lifetime_rate = (stage_completed / active_seconds) * 60 if stage_completed else 0.0
    remaining = max(0, stage_total - stage_completed)
    eta_seconds = (
        (remaining / forecast_rate) * 60
        if actively_processing and forecast_rate > 0 and remaining
        else 0.0
    )
    elapsed_seconds = max(
        0.0,
        (now - aware(job.started_at or job.created_at)).total_seconds(),
    )
    progress_stale_seconds = max(0.0, (now - aware(job.updated_at)).total_seconds())
    progress_stalled = bool(
        job.status in {"running", "pause_requested", "cancel_requested"}
        and progress_stale_seconds >= settings.progress_stale_after
    )

    metric_items = session.scalars(
        select(CollectionItem)
        .where(
            CollectionItem.job_id == job.id,
            CollectionItem.kind == stage_kind,
        )
        .order_by(CollectionItem.updated_at.desc())
        .limit(settings.dashboard_metric_sample_size)
    ).all()
    durations = [
        value
        for item in metric_items
        if (value := finite_number(item.duration_seconds)) is not None
    ]
    network_values = detail_values(metric_items, "network_seconds")
    queue_values = detail_values(metric_items, "queue_seconds")
    pacing_values = detail_values(metric_items, "pacing_seconds")
    persist_values = detail_values(metric_items, "persist_seconds")
    unattributed_values: list[float] = []
    for item in metric_items:
        duration = finite_number(item.duration_seconds)
        if duration is None:
            continue
        detail = item.detail or {}
        component_values = [
            finite_number(detail.get(key))
            for key in ("network_seconds", "queue_seconds", "pacing_seconds", "persist_seconds")
            if key in detail
        ]
        known_components = [value for value in component_values if value is not None]
        if known_components:
            unattributed_values.append(max(0.0, duration - sum(known_components)))

    source_status_rows = session.execute(
        select(CollectionItem.status, func.count(CollectionItem.id))
        .where(CollectionItem.job_id == job.id, CollectionItem.kind == "source")
        .group_by(CollectionItem.status)
    ).all()
    source_status_counts = {str(status): int(count) for status, count in source_status_rows}
    source_resolved = sum(
        count for status, count in source_status_counts.items() if status in RESOLVED_SOURCE_STATUSES
    )
    source_policy_complete = sum(
        count
        for status, count in source_status_counts.items()
        if status in POLICY_COMPLETE_SOURCE_STATUSES
    )
    source_deferred = sum(
        count for status, count in source_status_counts.items() if status in DEFERRED_SOURCE_STATUSES
    )
    operational_errors = sum(
        count for status, count in source_status_counts.items() if status in OPERATIONAL_FAILURE_STATUSES
    )
    failure_breakdown = {
        status: count
        for status, count in source_status_counts.items()
        if status in DEFERRED_SOURCE_STATUSES | OPERATIONAL_FAILURE_STATUSES
    }
    host_breakdown: dict[str, dict[str, int]] = {}
    host_timing: dict[str, dict[str, list[float]]] = {}
    for item in metric_items:
        if item.kind != "source":
            continue
        host = str((item.detail or {}).get("host") or "").removeprefix("www.")
        if host:
            values = host_breakdown.setdefault(
                host,
                {
                    "total": 0,
                    "successful": 0,
                    "policy_complete": 0,
                    "deferred": 0,
                    "failed": 0,
                },
            )
            values["total"] += 1
            if item.status in RESOLVED_SOURCE_STATUSES:
                values["successful"] += 1
            elif item.status in POLICY_COMPLETE_SOURCE_STATUSES:
                values["policy_complete"] += 1
            elif item.status in DEFERRED_SOURCE_STATUSES:
                values["deferred"] += 1
            else:
                values["failed"] += 1
            timing = host_timing.setdefault(
                host,
                {"duration": [], "network": [], "queue": [], "pacing": []},
            )
            duration = finite_number(item.duration_seconds)
            if duration is not None:
                timing["duration"].append(duration)
            for field, target in (
                ("network_seconds", "network"),
                ("queue_seconds", "queue"),
                ("pacing_seconds", "pacing"),
            ):
                detail = item.detail or {}
                if field not in detail:
                    continue
                value = finite_number(detail.get(field))
                if value is not None:
                    timing[target].append(value)

    host_rows: list[dict[str, Any]] = []
    for host, values in host_breakdown.items():
        timing = host_timing.get(host, {})
        host_rows.append(
            {
                "host": host,
                **values,
                "duration_p90_seconds": percentile(timing.get("duration", []), 0.9),
                "network_p50_seconds": median(timing.get("network", [])),
                "queue_p50_seconds": median(timing.get("queue", [])),
                "pacing_p50_seconds": median(timing.get("pacing", [])),
            }
        )
    host_rows.sort(key=lambda row: row["total"], reverse=True)

    bottleneck: dict[str, Any] | None = None
    if host_rows:
        candidate = max(
            host_rows,
            key=lambda row: (
                float(row["queue_p50_seconds"] or 0)
                + float(row["pacing_p50_seconds"] or 0),
                float(row["duration_p90_seconds"] or 0),
                int(row["total"]),
            ),
        )
        waiting = float(candidate["queue_p50_seconds"] or 0) + float(
            candidate["pacing_p50_seconds"] or 0
        )
        if waiting > 0:
            reason = (
                "queue"
                if float(candidate["queue_p50_seconds"] or 0)
                >= float(candidate["pacing_p50_seconds"] or 0)
                else "pacing"
            )
            bottleneck = {
                "host": candidate["host"],
                "reason": reason,
                "seconds": round(waiting, 2),
                "sample_size": candidate["total"],
            }

    text_applicable_completed = max(0, job.source_completed - source_policy_complete)
    resolved_or_policy = source_resolved + source_policy_complete
    return {
        "id": job.id,
        "range": f"{job.first_sequence:04d}–{job.last_sequence:04d}",
        "status": job.status,
        "stage": job.current_stage,
        "percent": round((completed / total) * 100, 1) if total else 0,
        "stage_percent": round((stage_completed / stage_total) * 100, 1) if stage_total else 0,
        "incidents": {
            "completed": job.incident_completed,
            "failed": job.incident_failed,
            "total": job.incident_total,
        },
        "sources": {
            "completed": job.source_completed,
            "failed": job.source_failed,
            "resolved": source_resolved,
            "policy_complete": source_policy_complete,
            "deferred": source_deferred,
            "operational_errors": operational_errors,
            "total": job.source_total,
        },
        "last_error": job.last_error,
        "engine_version": str((job.configuration or {}).get("engine_version") or "1.0.0"),
        "configuration": job.configuration or {},
        "performance": {
            "elapsed_seconds": round(elapsed_seconds, 1),
            "active_stage_seconds": round(active_seconds, 1),
            "rate_per_minute": round(rate_current, 2),
            "rate_current": round(rate_current, 2),
            "rate_average": round(rate_average, 2),
            "rate_30s": round(rate_30, 2),
            "rate_60s": round(rate_60, 2),
            "rate_300s": round(rate_300, 2),
            "rate_lifetime": round(lifetime_rate, 2),
            "eta_seconds": round(eta_seconds, 1) if eta_seconds else 0,
            "success_rate": percentage(source_resolved, text_applicable_completed),
            "content_coverage_rate": percentage(resolved_or_policy, job.source_total),
            "decision_rate": percentage(resolved_or_policy, job.source_completed),
            "operational_reliability": percentage(
                job.source_completed - operational_errors,
                job.source_completed,
            ),
            "backlog": remaining,
            "progress_stale_seconds": round(progress_stale_seconds, 1),
            "progress_stalled": progress_stalled,
            "metric_sample_size": len(metric_items),
            "recent_p50_seconds": median(durations),
            "recent_p90_seconds": percentile(durations, 0.9),
            "recent_network_p50_seconds": median(network_values),
            "recent_network_p90_seconds": percentile(network_values, 0.9),
            "recent_queue_p50_seconds": median(queue_values),
            "recent_queue_p90_seconds": percentile(queue_values, 0.9),
            "recent_pacing_p50_seconds": median(pacing_values),
            "recent_pacing_p90_seconds": percentile(pacing_values, 0.9),
            "recent_persist_p50_seconds": median(persist_values),
            "recent_persist_p90_seconds": percentile(persist_values, 0.9),
            "recent_unattributed_p50_seconds": median(unattributed_values),
        },
        "bottleneck": bottleneck,
        "failure_breakdown": failure_breakdown,
        "host_breakdown": host_rows[:8],
        "items": [
            {
                "kind": item.kind,
                "identity": item.identity,
                "status": item.status,
                "attempts": item.attempts,
                "duration_seconds": round(float(item.duration_seconds or 0), 2),
                "error": item.error,
                "detail": item.detail or {},
                "updated_at": item.updated_at.isoformat(),
            }
            for item in items
        ],
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "server_time": now_utc().isoformat(),
        "events": [
            {
                "id": event.id,
                "level": event.level,
                "message": event.message,
                "detail": event.detail or {},
                "created_at": event.created_at.isoformat(),
            }
            for event in events
        ],
    }
