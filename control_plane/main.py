from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import csrf_token, require_admin, require_csrf, verify_admin
from .config import Settings, get_settings
from .db import get_session, init_db
from .models import CollectionItem, CollectionJob, JobEvent
from .service import add_event, create_job, job_snapshot, request_action
from .tasks import enqueue_job

PACKAGE_ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=PACKAGE_ROOT / "templates")


def _context(request: Request, **values):
    return {"request": request, "csrf_token": csrf_token(request), **values}


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
        return templates.TemplateResponse(
            request, "dashboard.html", _context(request, jobs=jobs)
        )

    @app.get("/admin/jobs/new", response_class=HTMLResponse)
    def new_job_page(request: Request):
        require_admin(request)
        return templates.TemplateResponse(
            request, "new_job.html", _context(request, error=None)
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
    ):
        require_admin(request)
        require_csrf(request, csrf)
        try:
            job = create_job(
                session,
                first_sequence,
                last_sequence,
                collect_incidents=collect_incidents is not None,
                collect_sources=collect_sources is not None,
                write_report=write_report is not None,
                configuration={
                    "delay": app_settings.collector_delay,
                    "timeout": app_settings.collector_timeout,
                    "retries": app_settings.collector_retries,
                    "workers": app_settings.collector_workers,
                    "per_host_workers": app_settings.collector_per_host_workers,
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
            .limit(100)
        ).all()
        events = session.scalars(
            select(JobEvent)
            .where(JobEvent.job_id == job.id)
            .order_by(JobEvent.id.desc())
            .limit(100)
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

    return app


app = create_app()
