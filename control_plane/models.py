from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class CollectionJob(Base):
    __tablename__ = "collection_jobs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    first_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    last_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", index=True
    )
    requested_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    current_stage: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued"
    )
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    collect_incidents: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    collect_sources: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    write_report: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )

    incident_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    incident_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    incident_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    items: Mapped[list[CollectionItem]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    events: Mapped[list[JobEvent]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    @property
    def sequence_count(self) -> int:
        return self.last_sequence - self.first_sequence + 1


class CollectionItem(Base):
    __tablename__ = "collection_items"
    __table_args__ = (
        Index("uq_job_kind_identity", "job_id", "kind", "identity", unique=True),
        Index("ix_collection_items_job_kind_updated", "job_id", "kind", "updated_at"),
        Index("ix_collection_items_job_kind_status", "job_id", "kind", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("collection_jobs.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    identity: Mapped[str] = mapped_column(String(96), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_seconds: Mapped[float] = mapped_column(nullable=False, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    job: Mapped[CollectionJob] = relationship(back_populates="items")


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("collection_jobs.id", ondelete="CASCADE"), index=True
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    job: Mapped[CollectionJob] = relationship(back_populates="events")


class ArchiveProjectRecord(Base):
    """Persisted configuration for a connector-neutral archive project."""

    __tablename__ = "archive_projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    connector: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    allowed_domains: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    collection_limits: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    rate_policy: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    text_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    release_name: Mapped[str] = mapped_column(String(160), nullable=False, default="textual-release")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="CREATED", index=True)
    analysis: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    engine_runs: Mapped[list[GeneralEngineRunRecord]] = relationship(back_populates="project", cascade="all, delete-orphan")
    releases: Mapped[list[ArchiveReleaseRecord]] = relationship(back_populates="project", cascade="all, delete-orphan")


class GeneralEngineRunRecord(Base):
    __tablename__ = "general_engine_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("archive_projects.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="CREATED", index=True)
    requested_action: Mapped[str | None] = mapped_column(String(24), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    engine_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    counts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[ArchiveProjectRecord] = relationship(back_populates="engine_runs")


class ArchiveReleaseRecord(Base):
    __tablename__ = "archive_releases"

    id: Mapped[str] = mapped_column(String(180), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("archive_projects.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    parent_release_id: Mapped[str | None] = mapped_column(String(180), nullable=True)
    release_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="BUILDING_RELEASE", index=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[ArchiveProjectRecord] = relationship(back_populates="releases")
