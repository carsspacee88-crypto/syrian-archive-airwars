from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import csrf_token, require_admin, require_csrf, verify_admin
from .config import CONTROL_PLANE_ENGINE_VERSION, Settings, get_settings
from .db import get_session, init_db, session_factory
from .models import (
    ArchiveProjectRecord,
    ArchiveReleaseRecord,
    CollectionItem,
    CollectionJob,
    GeneralEngineRunRecord,
    JobEvent,
)
from .service import add_event, create_job, job_snapshot, request_action
from .tasks import enqueue_job
from .general_tasks import (
    enqueue_general_run,
    enqueue_release_build,
    publish_release,
    request_engine_action,
    rollback_release,
)
from archive_engine.statuses import RunStatus, SourceContentStatus
from archive_engine.validators.release import ReleaseValidator

PACKAGE_ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=PACKAGE_ROOT / "templates")


def _current_release_root(settings: Settings) -> Path | None:
    try:
        return (settings.releases_root / "current").resolve(strict=True)
    except FileNotFoundError:
        return None


def _canonical_report(settings: Settings) -> dict:
    current = _current_release_root(settings)
    candidates = [] if current is None else [current / "reports" / "airwars-ground-truth.json"]
    candidates.append(settings.project_root / "reports" / "airwars-ground-truth.json")
    for path in candidates:
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    return {}


def _metric_results(report: dict) -> dict[str, int]:
    values = {key: int(row.get("result") or 0) for key, row in (report.get("metrics") or {}).items() if isinstance(row, dict) and isinstance(row.get("result"), (int, float))}
    values["sources_full_text_total"] = sum(values.get(key, 0) for key in ("sources_full_text_direct", "sources_full_text_archived", "sources_full_text_local_snapshot"))
    return values


def _context(request: Request, **values):
    return {
        "request": request,
        "csrf_token": csrf_token(request),
        "engine_version": CONTROL_PLANE_ENGINE_VERSION,
        **values,
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        app_settings.require_web_secrets()
        init_db()
        yield

    app = FastAPI(
        title="لوحة إدارة الأرشيف السوري",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = app_settings
    app.add_middleware(
        SessionMiddleware,
        secret_key=app_settings.resolved_session_secret
        or "test-only-session-secret-that-is-long-enough",
        https_only=app_settings.secure_cookies,
        same_site="strict",
        max_age=8 * 60 * 60,
    )
    app.mount(
        "/admin/static",
        StaticFiles(directory=PACKAGE_ROOT / "static"),
        name="admin-static",
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/admin/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return templates.TemplateResponse(
            request, "login.html", _context(request, error=None)
        )

    @app.post("/admin/login")
    def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ):
        if not verify_admin(app_settings, username, password):
            return templates.TemplateResponse(
                request,
                "login.html",
                _context(request, error="اسم المستخدم أو كلمة المرور غير صحيحة"),
                status_code=401,
            )
        request.session.clear()
        request.session["admin"] = True
        csrf_token(request)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/logout")
    def logout(request: Request, csrf: str = Form(...)):
        require_admin(request)
        require_csrf(request, csrf)
        request.session.clear()
        return RedirectResponse("/admin/login", status_code=303)

    @app.get("/admin", response_class=HTMLResponse)
    def dashboard(request: Request, session: Annotated[Session, Depends(get_session)]):
        require_admin(request)
        jobs = session.scalars(
            select(CollectionJob).order_by(CollectionJob.created_at.desc()).limit(100)
        ).all()
        active_statuses = {"queued", "running", "pause_requested", "paused", "cancel_requested"}
        dashboard_summary = {
            "active": sum(job.status in active_statuses for job in jobs),
            "completed": sum(job.status in {"completed", "completed_with_gaps", "completed_with_errors"} for job in jobs),
            "sources": sum(job.source_completed for job in jobs),
            "failed_sources": sum(job.source_failed for job in jobs),
        }
        report = _canonical_report(app_settings)
        canonical = _metric_results(report)
        archive_projects = session.scalars(
            select(ArchiveProjectRecord).order_by(ArchiveProjectRecord.created_at.desc()).limit(8)
        ).all()
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            _context(
                request,
                jobs=jobs,
                summary=dashboard_summary,
                canonical=canonical,
                canonical_generated_at=report.get("generated_at"),
                archive_projects=archive_projects,
            ),
        )

    @app.get("/admin/jobs/new", response_class=HTMLResponse)
    def new_job_page(request: Request):
        require_admin(request)
        return templates.TemplateResponse(
            request,
            "new_job.html",
            _context(
                request,
                error=None,
                defaults={
                    "workers": app_settings.collector_workers,
                    "per_host_workers": app_settings.collector_per_host_workers,
                    "social_workers": app_settings.collector_social_workers,
                    "archive_workers": app_settings.collector_archive_workers,
                    "delay": app_settings.collector_delay,
                    "timeout": app_settings.collector_timeout,
                    "fast_timeout": app_settings.collector_fast_timeout,
                    "source_chunk_size": app_settings.source_chunk_size,
                },
            ),
        )

    @app.post("/admin/jobs")
    def new_job(
        request: Request,
        session: Annotated[Session, Depends(get_session)],
        first_sequence: int = Form(...),
        last_sequence: int = Form(...),
        csrf: str = Form(...),
        collect_incidents: str | None = Form(None),
        collect_sources: str | None = Form(None),
        write_report: str | None = Form(None),
        performance_profile: str = Form("balanced"),
        workers: int = Form(64),
        per_host_workers: int = Form(4),
        social_workers: int = Form(12),
        archive_workers: int = Form(12),
        delay: float = Form(0.05),
        timeout: float = Form(6.0),
        fast_timeout: float = Form(3.0),
        source_chunk_size: int = Form(5000),
        incident_mode: str = Form("network_refresh"),
        inline_wayback: str | None = Form(None),
    ):
        require_admin(request)
        require_csrf(request, csrf)
        try:
            if performance_profile not in {"conservative", "balanced", "turbo", "custom"}:
                raise ValueError("ملف الأداء غير معروف")
            if not 1 <= workers <= 128:
                raise ValueError("عدد العمال الكلي يجب أن يكون بين 1 و128")
            if not 1 <= per_host_workers <= min(16, workers):
                raise ValueError("عمال المضيف يجب أن يكونوا بين 1 و16 وألا يتجاوزوا العمال الكليين")
            if not 1 <= social_workers <= min(24, workers):
                raise ValueError("عمال المنصات يجب أن يكونوا بين 1 و24 وألا يتجاوزوا العمال الكليين")
            if not 1 <= archive_workers <= min(24, workers):
                raise ValueError("عمال الأرشيف يجب أن يكونوا بين 1 و24 وألا يتجاوزوا العمال الكليين")
            if not 0 <= delay <= 5:
                raise ValueError("الفاصل بين طلبات المضيف يجب أن يكون بين 0 و5 ثوانٍ")
            if not 2 <= timeout <= 60:
                raise ValueError("المهلة يجب أن تكون بين ثانيتين و60 ثانية")
            if not 1 <= fast_timeout <= timeout:
                raise ValueError("المهلة السريعة يجب أن تكون بين ثانية والمهلة الكلية")
            if not 50 <= source_chunk_size <= 5000:
                raise ValueError("حجم دفعة المصادر يجب أن يكون بين 50 و5000")
            if incident_mode not in {"snapshot_first", "network_refresh"}:
                raise ValueError("نمط الحوادث غير معروف")
            job = create_job(
                session,
                first_sequence,
                last_sequence,
                collect_incidents=collect_incidents is not None,
                collect_sources=collect_sources is not None,
                write_report=write_report is not None,
                configuration={
                    "engine_version": CONTROL_PLANE_ENGINE_VERSION,
                    "performance_profile": performance_profile,
                    "delay": delay,
                    "timeout": timeout,
                    "retries": app_settings.collector_retries,
                    "workers": workers,
                    "per_host_workers": per_host_workers,
                    "social_workers": social_workers,
                    "archive_workers": archive_workers,
                    "checkpoint_every": app_settings.collector_checkpoint_every,
                    "incident_chunk_size": app_settings.incident_chunk_size,
                    "source_chunk_size": source_chunk_size,
                    "fast_timeout": fast_timeout,
                    "incident_mode": incident_mode,
                    "inline_wayback": inline_wayback is not None,
                },
            )
        except ValueError as error:
            return templates.TemplateResponse(
                request,
                "new_job.html",
                _context(
                    request,
                    error=str(error),
                    first_sequence=first_sequence,
                    last_sequence=last_sequence,
                    defaults={
                        "workers": workers,
                        "per_host_workers": per_host_workers,
                        "social_workers": social_workers,
                        "archive_workers": archive_workers,
                        "delay": delay,
                        "timeout": timeout,
                        "fast_timeout": fast_timeout,
                        "source_chunk_size": source_chunk_size,
                    },
                ),
                status_code=422,
            )
        try:
            job.task_id = enqueue_job(job.id)
            session.commit()
        except Exception as error:  # noqa: BLE001 - broker outages must become durable job state
            job.status = "failed"
            job.current_stage = "queue_error"
            job.last_error = f"{type(error).__name__}: {error}"
            add_event(
                session,
                job,
                "تعذر الوصول إلى طابور المهام؛ لم يبدأ جمع أي عنصر",
                level="error",
            )
            session.commit()
        return RedirectResponse(f"/admin/jobs/{job.id}", status_code=303)

    @app.get("/admin/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(
        job_id: str, request: Request, session: Annotated[Session, Depends(get_session)]
    ):
        require_admin(request)
        job = session.get(CollectionJob, job_id)
        if not job:
            raise HTTPException(404, "المهمة غير موجودة")
        items = session.scalars(
            select(CollectionItem)
            .where(CollectionItem.job_id == job.id)
            .order_by(CollectionItem.updated_at.desc())
            .limit(app_settings.dashboard_item_limit)
        ).all()
        events = session.scalars(
            select(JobEvent)
            .where(JobEvent.job_id == job.id)
            .order_by(JobEvent.id.desc())
            .limit(app_settings.dashboard_event_limit)
        ).all()
        return templates.TemplateResponse(
            request, "job.html", _context(request, job=job, items=items, events=events)
        )

    @app.post("/admin/jobs/{job_id}/action")
    def job_action(
        job_id: str,
        request: Request,
        session: Annotated[Session, Depends(get_session)],
        action: str = Form(...),
        csrf: str = Form(...),
    ):
        require_admin(request)
        require_csrf(request, csrf)
        job = session.get(CollectionJob, job_id)
        if not job:
            raise HTTPException(404, "المهمة غير موجودة")
        try:
            request_action(session, job, action)
            if action in {"resume", "retry_failed"}:
                job.task_id = enqueue_job(job.id)
                session.commit()
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        except Exception as error:  # noqa: BLE001 - broker outages must become durable job state
            job.status = "failed"
            job.last_error = f"{type(error).__name__}: {error}"
            add_event(session, job, "تعذر إعادة المهمة إلى الطابور", level="error")
            session.commit()
        return RedirectResponse(f"/admin/jobs/{job.id}", status_code=303)

    @app.get("/admin/api/jobs/{job_id}")
    def job_api(
        job_id: str, request: Request, session: Annotated[Session, Depends(get_session)]
    ):
        require_admin(request)
        job = session.get(CollectionJob, job_id)
        if not job:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return job_snapshot(session, job)

    @app.get("/admin/api/jobs/{job_id}/stream")
    async def job_stream(job_id: str, request: Request):
        require_admin(request)

        def snapshot() -> dict:
            with session_factory()() as session:
                job = session.get(CollectionJob, job_id)
                return {"error": "not_found"} if not job else job_snapshot(session, job)

        async def events():
            last_signature = ""
            last_heartbeat = 0.0
            terminal = {
                "completed",
                "completed_with_gaps",
                "completed_with_errors",
                "failed",
                "cancelled",
            }
            while not await request.is_disconnected():
                payload = await asyncio.to_thread(snapshot)
                if payload.get("error"):
                    yield "event: error\ndata: {\"error\":\"not_found\"}\n\n"
                    return
                loop_time = asyncio.get_running_loop().time()
                # Metrics such as the rolling 30-second rate and stale-progress
                # age must keep moving even when no item has just completed.
                metric_tick = int(loop_time // 5)
                signature = json.dumps({
                    "status": payload.get("status"),
                    "stage": payload.get("stage"),
                    "incidents": payload.get("incidents"),
                    "sources": payload.get("sources"),
                    "updated_at": payload.get("updated_at"),
                    "event": (payload.get("events") or [{}])[0].get("id"),
                    "item": (payload.get("items") or [{}])[0].get("updated_at"),
                    "metric_tick": metric_tick,
                }, sort_keys=True)
                if signature != last_signature:
                    yield "event: snapshot\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
                    last_signature = signature
                    last_heartbeat = loop_time
                    if payload.get("status") in terminal:
                        return
                elif loop_time - last_heartbeat >= 12:
                    yield ": keep-alive\n\n"
                    last_heartbeat = loop_time
                await asyncio.sleep(app_settings.live_update_interval)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/admin/archive/projects", response_class=HTMLResponse)
    def archive_projects(request: Request, session: Annotated[Session, Depends(get_session)]):
        require_admin(request)
        rows = session.scalars(select(ArchiveProjectRecord).order_by(ArchiveProjectRecord.created_at.desc())).all()
        return templates.TemplateResponse(request, "archive_projects.html", _context(request, projects=rows))

    @app.get("/admin/archive/projects/new", response_class=HTMLResponse)
    def archive_project_new(request: Request):
        require_admin(request)
        return templates.TemplateResponse(
            request,
            "archive_project_new.html",
            _context(request, error=None, collection_statuses=[item.value for item in RunStatus]),
        )

    @app.post("/admin/archive/projects")
    def archive_project_create(
        request: Request,
        session: Annotated[Session, Depends(get_session)],
        csrf: str = Form(...),
        name: str = Form(...),
        target_url: str = Form(...),
        connector: str = Form(...),
        allowed_domains: str = Form(""),
        scope_json: str = Form("{}"),
        collection_limits_json: str = Form("{}"),
        rate_policy_json: str = Form("{}"),
        release_name: str = Form("textual-release"),
        text_only: str | None = Form(None),
    ):
        require_admin(request)
        require_csrf(request, csrf)
        try:
            parsed = urlsplit(target_url.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("أدخل رابط HTTP(S) عامًا صالحًا")
            if connector not in {"airwars", "synthetic_library"}:
                raise ValueError("الموصل غير مدعوم")
            scope = json.loads(scope_json or "{}")
            limits = json.loads(collection_limits_json or "{}")
            rate = json.loads(rate_policy_json or "{}")
            if not all(isinstance(value, dict) for value in (scope, limits, rate)):
                raise ValueError("النطاق والحدود والسياسة يجب أن تكون كائنات JSON")
            domains = [item.strip().casefold().removeprefix("www.") for item in allowed_domains.replace("\n", ",").split(",") if item.strip()]
            if not domains:
                domains = [parsed.hostname.casefold().removeprefix("www.")]
            project = ArchiveProjectRecord(
                name=name.strip(), target_url=target_url.strip(), connector=connector,
                allowed_domains=domains, scope=scope, collection_limits=limits,
                rate_policy=rate, text_only=text_only is not None,
                release_name=release_name.strip() or "textual-release",
            )
            session.add(project)
            session.commit()
        except (ValueError, json.JSONDecodeError) as error:
            return templates.TemplateResponse(
                request,
                "archive_project_new.html",
                _context(request, error=str(error), collection_statuses=[item.value for item in RunStatus]),
                status_code=422,
            )
        return RedirectResponse(f"/admin/archive/projects/{project.id}", status_code=303)

    @app.get("/admin/archive/projects/{project_id}", response_class=HTMLResponse)
    def archive_project_detail(project_id: str, request: Request, session: Annotated[Session, Depends(get_session)]):
        require_admin(request)
        project = session.get(ArchiveProjectRecord, project_id)
        if not project:
            raise HTTPException(404, "المشروع غير موجود")
        runs = session.scalars(select(GeneralEngineRunRecord).where(GeneralEngineRunRecord.project_id == project.id).order_by(GeneralEngineRunRecord.created_at.desc())).all()
        releases = session.scalars(select(ArchiveReleaseRecord).where(ArchiveReleaseRecord.project_id == project.id).order_by(ArchiveReleaseRecord.created_at.desc())).all()
        return templates.TemplateResponse(request, "archive_project.html", _context(request, project=project, runs=runs, releases=releases, run_statuses=[item.value for item in RunStatus], source_statuses=[item.value for item in SourceContentStatus]))

    def start_general_run(session: Session, project: ArchiveProjectRecord, mode: str, pilot_limit: int, max_workers: int) -> GeneralEngineRunRecord:
        if mode not in {"analysis", "pilot", "full", "retry"}:
            raise ValueError("نمط تشغيل غير مدعوم")
        row = GeneralEngineRunRecord(
            project_id=project.id,
            mode=mode,
            status=RunStatus.CREATED.value,
            engine_run_id=str(uuid4()),
            configuration={"pilot_limit": max(1, min(pilot_limit, 1000)), "max_workers": max(1, min(max_workers, 32)), "checkpoint_every": 1},
        )
        session.add(row)
        session.flush()
        try:
            row.task_id = enqueue_general_run(row.id)
            row.status = RunStatus.QUEUED.value if mode != "analysis" else RunStatus.ANALYZING.value
        except Exception as error:  # noqa: BLE001
            row.status = RunStatus.FAILED.value
            row.last_error = f"{type(error).__name__}: {error}"
        session.commit()
        return row

    @app.post("/admin/archive/projects/{project_id}/runs")
    def archive_project_run(
        project_id: str,
        request: Request,
        session: Annotated[Session, Depends(get_session)],
        csrf: str = Form(...),
        mode: str = Form(...),
        pilot_limit: int = Form(10),
        max_workers: int = Form(1),
    ):
        require_admin(request)
        require_csrf(request, csrf)
        project = session.get(ArchiveProjectRecord, project_id)
        if not project:
            raise HTTPException(404, "المشروع غير موجود")
        try:
            row = start_general_run(session, project, mode, pilot_limit, max_workers)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return RedirectResponse(f"/admin/archive/runs/{row.id}", status_code=303)

    @app.get("/admin/archive/runs/{run_id}", response_class=HTMLResponse)
    def archive_run_detail(run_id: str, request: Request, session: Annotated[Session, Depends(get_session)]):
        require_admin(request)
        run = session.get(GeneralEngineRunRecord, run_id)
        if not run:
            raise HTTPException(404, "التشغيل غير موجود")
        project = session.get(ArchiveProjectRecord, run.project_id)
        releases = session.scalars(select(ArchiveReleaseRecord).where(ArchiveReleaseRecord.run_id == run.id).order_by(ArchiveReleaseRecord.created_at.desc())).all()
        return templates.TemplateResponse(request, "archive_run.html", _context(request, run=run, project=project, releases=releases))

    @app.post("/admin/archive/runs/{run_id}/action")
    def archive_run_action(
        run_id: str,
        request: Request,
        session: Annotated[Session, Depends(get_session)],
        csrf: str = Form(...),
        action: str = Form(...),
    ):
        require_admin(request)
        require_csrf(request, csrf)
        run = session.get(GeneralEngineRunRecord, run_id)
        if not run:
            raise HTTPException(404, "التشغيل غير موجود")
        try:
            if action == "retry":
                run.mode = "retry"
                run.status = RunStatus.RETRYING.value
                run.last_error = None
                run.task_id = enqueue_general_run(run.id)
            else:
                request_engine_action(run, action)
                run.requested_action = action
                if action == "pause":
                    run.status = RunStatus.PAUSED.value
                elif action == "cancel":
                    run.status = RunStatus.CANCELLED.value
                elif action == "resume":
                    run.status = RunStatus.QUEUED.value
                    run.task_id = enqueue_general_run(run.id)
            session.commit()
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return RedirectResponse(f"/admin/archive/runs/{run.id}", status_code=303)

    @app.post("/admin/archive/runs/{run_id}/release")
    def archive_run_release(run_id: str, request: Request, session: Annotated[Session, Depends(get_session)], csrf: str = Form(...)):
        require_admin(request)
        require_csrf(request, csrf)
        run = session.get(GeneralEngineRunRecord, run_id)
        if not run:
            raise HTTPException(404, "التشغيل غير موجود")
        if run.status not in {RunStatus.COMPLETED.value, RunStatus.COMPLETED_WITH_GAPS.value, RunStatus.PILOT_REVIEW.value}:
            raise HTTPException(409, "يجب أن ينتهي التشغيل قبل بناء الإصدار")
        try:
            run.task_id = enqueue_release_build(run.id)
            run.status = RunStatus.BUILDING_RELEASE.value
            session.commit()
        except Exception as error:  # noqa: BLE001
            run.status = RunStatus.FAILED.value
            run.last_error = f"{type(error).__name__}: {error}"
            session.commit()
        return RedirectResponse(f"/admin/archive/runs/{run.id}", status_code=303)

    @app.post("/admin/archive/releases/{release_id}/validate")
    def archive_release_validate(release_id: str, request: Request, session: Annotated[Session, Depends(get_session)], csrf: str = Form(...)):
        require_admin(request)
        require_csrf(request, csrf)
        release = session.get(ArchiveReleaseRecord, release_id)
        if not release:
            raise HTTPException(404, "الإصدار غير موجود")
        result = ReleaseValidator().validate(Path(release.release_path))
        release.validation = {"passed": result.passed, "blocking_failures": result.blocking_failures, "non_blocking_failures": result.non_blocking_failures, "checks": result.checks}
        release.status = RunStatus.COMPLETED.value if result.passed else RunStatus.FAILED.value
        session.commit()
        return RedirectResponse(f"/admin/archive/projects/{release.project_id}", status_code=303)

    @app.post("/admin/archive/releases/{release_id}/publish")
    def archive_release_publish(release_id: str, request: Request, session: Annotated[Session, Depends(get_session)], csrf: str = Form(...)):
        require_admin(request)
        require_csrf(request, csrf)
        release = session.get(ArchiveReleaseRecord, release_id)
        if not release:
            raise HTTPException(404, "الإصدار غير موجود")
        if not bool((release.validation or {}).get("passed")):
            raise HTTPException(409, "لا يمكن نشر إصدار لم يجتز التحقق")
        previous, current = publish_release(Path(release.release_path))
        release.parent_release_id = release.parent_release_id or previous
        release.published = True
        release.published_at = datetime.now(timezone.utc)
        release.status = RunStatus.PUBLISHED.value
        release.manifest = {**(release.manifest or {}), "active_release": current}
        session.commit()
        return RedirectResponse(f"/admin/archive/projects/{release.project_id}", status_code=303)

    @app.post("/admin/archive/releases/{release_id}/rollback")
    def archive_release_rollback(release_id: str, request: Request, session: Annotated[Session, Depends(get_session)], csrf: str = Form(...)):
        require_admin(request)
        require_csrf(request, csrf)
        release = session.get(ArchiveReleaseRecord, release_id)
        if not release:
            raise HTTPException(404, "الإصدار غير موجود")
        rollback_release(Path(release.release_path))
        for item in session.scalars(select(ArchiveReleaseRecord).where(ArchiveReleaseRecord.published.is_(True))).all():
            item.published = item.id == release.id
        release.status = RunStatus.PUBLISHED.value
        release.published_at = datetime.now(timezone.utc)
        session.commit()
        return RedirectResponse(f"/admin/archive/projects/{release.project_id}", status_code=303)

    @app.get("/admin/archive/records", response_class=HTMLResponse)
    def archive_records(request: Request, group: str, status: str):
        require_admin(request)
        current = _current_release_root(app_settings)
        records: list[str] = []
        if current:
            index_path = current / "data" / "status-index.json"
            if index_path.is_file():
                index = json.loads(index_path.read_text(encoding="utf-8"))
                records = list(((index.get(group) or {}).get(status) or []))
                if group == "sources" and status == "full":
                    records = sorted(sum((list((index.get("sources") or {}).get(item) or []) for item in ("FULL_TEXT_DIRECT", "FULL_TEXT_ARCHIVED", "FULL_TEXT_LOCAL_SNAPSHOT")), []))
                elif group == "sources" and status == "link_only":
                    records = sorted(list((index.get("sources") or {}).get("URL_PRESERVED") or []) + list((index.get("sources") or {}).get("REFERENCE_ONLY") or []))
                elif group == "sources" and status == "manual_review" and not records:
                    report_index = current / "reports" / "status-index.json"
                    if report_index.is_file():
                        report_rows = json.loads(report_index.read_text(encoding="utf-8"))
                        # Primary manual-review/malformed records are exact; the
                        # full overlapping review set remains available in the
                        # ground-truth export and source JSON review_flags.
                        records = sorted(list((report_rows.get("sources") or {}).get("NEEDS_MANUAL_REVIEW") or []) + list((report_rows.get("sources") or {}).get("MALFORMED") or []))
                elif group == "incidents" and status == "all":
                    records = sorted(path.stem for path in (current / "data" / "incidents").glob("*.json"))
                elif group == "sources" and status == "all":
                    records = sorted(path.stem for path in (current / "data" / "sources").glob("*.json"))
                elif group == "references" or group == "source_urls":
                    reference_path = current / "data" / "source-references.jsonl"
                    if reference_path.is_file():
                        selected: list[str] = []
                        with reference_path.open(encoding="utf-8") as handle:
                            for line in handle:
                                if not line.strip():
                                    continue
                                row = json.loads(line)
                                if group == "source_urls":
                                    value = row.get("raw_url") if status == "raw" else row.get("normalized_url")
                                    if value:
                                        selected.append(str(value))
                                elif status == "all" or (status == "duplicate" and row.get("duplicate_relationship")) or (status == "malformed" and row.get("malformed")) or (status == "manual-review" and row.get("manual_review")):
                                    selected.append(str(row.get("source_reference_id")))
                        records = sorted(set(selected)) if group == "source_urls" else selected
            elif (current / "reports" / "status-index.json").is_file():
                index = json.loads((current / "reports" / "status-index.json").read_text(encoding="utf-8"))
                records = list(((index.get(group) or {}).get(status) or []))
        return templates.TemplateResponse(request, "archive_records.html", _context(request, group=group, status=status, records=records, total=len(records)))

    return app


app = create_app()
