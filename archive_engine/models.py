from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .statuses import CollectionStatus, RunStatus, SourceContentStatus


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class FieldProvenance:
    origin: str
    source_url: str | None = None
    captured_at: str | None = None
    method: str | None = None
    content_hash: str | None = None
    note: str | None = None


@dataclass(slots=True)
class FieldValue:
    name: str
    raw_value: Any
    normalized_value: Any
    provenance: list[FieldProvenance] = field(default_factory=list)
    status: str = "present"
    reason: str | None = None


@dataclass(slots=True)
class RecordType:
    key: str
    label: str
    required_fields: tuple[str, ...] = ()
    field_rules: dict[str, Any] = field(default_factory=dict)
    relationship_rules: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Location:
    label: str | None = None
    administrative_area: str | None = None
    country: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    coordinate_status: str = "missing"


@dataclass(slots=True)
class PersonReference:
    name: str
    role: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    provenance: list[FieldProvenance] = field(default_factory=list)


@dataclass(slots=True)
class PreservedTextContent:
    text: str
    status: SourceContentStatus
    origin: str
    content_hash: str | None = None
    captured_at: str | None = None
    validation_status: str = "unvalidated"


@dataclass(slots=True)
class CaptureAttempt:
    url: str
    attempted_at: str
    status_code: int | None
    outcome: CollectionStatus
    elapsed_seconds: float = 0.0
    error: str | None = None
    response_hash: str | None = None
    final_url: str | None = None


@dataclass(slots=True)
class SourceReference:
    source_reference_id: str
    record_id: str
    raw_url: str
    normalized_url: str
    normalization_status: str
    normalization_reason: str | None
    label: str = ""
    title: str = ""
    publisher: str = ""
    citation_text: str = ""
    publication_date: str | None = None
    source_type: str = "unknown"
    domain: str = ""
    current_reachability_status: CollectionStatus = CollectionStatus.DISCOVERED
    content_preservation_status: SourceContentStatus = SourceContentStatus.REFERENCE_ONLY
    provenance: list[FieldProvenance] = field(default_factory=list)
    duplicate_relationship: bool = False
    malformed: bool = False
    manual_review: bool = False
    external_source_id: str | None = None
    archived_urls: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExternalSource:
    source_id: str
    raw_urls: list[str]
    normalized_url: str
    reference_ids: list[str] = field(default_factory=list)
    content: PreservedTextContent | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    attempts: list[CaptureAttempt] = field(default_factory=list)
    provenance: list[FieldProvenance] = field(default_factory=list)


@dataclass(slots=True)
class SiteRecord:
    record_id: str
    record_type: str
    canonical_url: str
    fields: dict[str, FieldValue]
    source_references: list[SourceReference] = field(default_factory=list)
    people: list[PersonReference] = field(default_factory=list)
    location: Location | None = None
    collection_status: CollectionStatus = CollectionStatus.DISCOVERED
    record_origin_status: str = "unknown"
    direct_verification_status: str = "not_attempted"
    data_quality_status: str = "unreviewed"
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ArchiveSite:
    target_url: str
    allowed_domains: list[str]
    connector: str


@dataclass(slots=True)
class ArchiveProject:
    project_id: str
    name: str
    site: ArchiveSite
    scope: dict[str, Any]
    collection_limits: dict[str, Any] = field(default_factory=dict)
    rate_policy: dict[str, Any] = field(default_factory=dict)
    text_only: bool = True
    release_name: str = "textual-release"
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        name: str,
        target_url: str,
        connector: str,
        scope: dict[str, Any],
        *,
        allowed_domains: list[str] | None = None,
        collection_limits: dict[str, Any] | None = None,
        rate_policy: dict[str, Any] | None = None,
        text_only: bool = True,
        release_name: str = "textual-release",
    ) -> "ArchiveProject":
        return cls(
            project_id=str(uuid4()),
            name=name,
            site=ArchiveSite(target_url, allowed_domains or [], connector),
            scope=scope,
            collection_limits=collection_limits or {},
            rate_policy=rate_policy or {},
            text_only=text_only,
            release_name=release_name,
        )


@dataclass(slots=True)
class EngineRun:
    run_id: str
    project_id: str
    mode: str
    status: RunStatus = RunStatus.CREATED
    worklist_hash: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class ReleaseDescriptor:
    release_id: str
    project_id: str
    parent_release_id: str | None
    generated_at: str
    manifest_path: str
    checksums_path: str
    validation_path: str
    immutable: bool = True


@dataclass(slots=True)
class ValidationResult:
    passed: bool
    blocking_failures: list[dict[str, Any]] = field(default_factory=list)
    non_blocking_failures: list[dict[str, Any]] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
