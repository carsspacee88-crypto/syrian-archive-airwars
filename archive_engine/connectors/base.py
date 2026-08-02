from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from archive_engine.models import ArchiveProject, RecordType, SiteRecord


@dataclass(frozen=True, slots=True)
class DiscoveredTarget:
    identity: str
    url: str
    record_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedRecord:
    record: SiteRecord
    raw_links: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class Connector(Protocol):
    key: str
    display_name: str

    def record_types(self) -> Sequence[RecordType]: ...

    def analyze(self, project: ArchiveProject, sample_bodies: dict[str, bytes]) -> dict[str, Any]: ...

    def discover(self, project: ArchiveProject) -> Sequence[DiscoveredTarget]: ...

    def parse(self, target: DiscoveredTarget, body: bytes, content_type: str) -> ParsedRecord: ...
