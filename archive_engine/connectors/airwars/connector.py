from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator, Sequence

from lxml import html

from archive_engine.connectors.base import DiscoveredTarget, ParsedRecord
from archive_engine.models import (
    ArchiveProject,
    FieldProvenance,
    FieldValue,
    Location,
    PersonReference,
    RecordType,
    SiteRecord,
    SourceReference,
)
from archive_engine.normalizers.urls import normalize_url
from archive_engine.statuses import CollectionStatus, SourceContentStatus
from archive_pipeline.io_utils import clean_text
from archive_pipeline.legacy import LegacyArchive


AIRWARS_INCIDENT_PATTERN = re.compile(r"^https?://(?:www\.)?airwars\.org/civilian-casualties/[^/]+/?$", re.I)

# These strings identify access shells, deletion notices, and generic error pages.
# They are deliberately conservative: a locally saved shell is evidence of an
# attempt, but it is not the complete main text of the cited external source.
NON_CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("facebook_login_shell", re.compile(r"(?:log in to facebook|login to facebook|войдите на facebook|познакомьтесь с тем, что вам нравится|электронный адрес или номер мобильного телефона|facebook\s+email(?: address)?\s+password)", re.I)),
    ("facebook_unavailable", re.compile(r"(?:this (?:facebook )?post is (?:no longer )?available|эта публикация facebook больше не доступна|публикация недоступна|владелец удалил контент|owner (?:has )?removed the content|content (?:is|might be) unavailable|page (?:is|currently) unavailable|страница сейчас недоступна)", re.I)),
    ("video_unavailable", re.compile(r"(?:video (?:is )?unavailable|видео недоступно|данного видео не существует|problems? (?:playing|reproducing) this video|проблемы с воспроизведением)", re.I)),
    ("restricted_or_login_required", re.compile(r"(?:please log in to (?:see|view)|пожалуйста, выполните вход|age-restricted adult content|you do not have (?:access|permission)|у вас нет доступа)", re.I)),
    ("generic_access_shell", re.compile(r"(?:access denied|enable javascript to run this app|verify you are human|captcha|just a moment\.\.\.|account (?:has been )?suspended|this domain is for sale|page cannot be displayed|contact your service provider)", re.I)),
)


def _sha24(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _recursive_statuses(value: Any) -> Iterator[int]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"status", "status_code", "last_http_status", "circuit_open_status"}:
                try:
                    if item is not None:
                        yield int(item)
                except (TypeError, ValueError):
                    pass
            yield from _recursive_statuses(item)
    elif isinstance(value, list):
        for item in value:
            yield from _recursive_statuses(item)


def _recursive_errors(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"error", "failure_reason", "circuit_open_reason", "reason"} and item:
                yield str(item)
            yield from _recursive_errors(item)
    elif isinstance(value, list):
        for item in value:
            yield from _recursive_errors(item)


def source_reachability(record: dict[str, Any]) -> CollectionStatus:
    retrieval = str(record.get("retrieval_status") or "")
    if retrieval in {"successful", "successful_partial", "cached", "embedded_text_preserved", "media_metadata_preserved"}:
        return CollectionStatus.FETCHED
    statuses = set(_recursive_statuses(record.get("attempt_history") or []))
    if statuses & {401, 403, 429, 451}:
        return CollectionStatus.BLOCKED
    if statuses & {404, 410}:
        return CollectionStatus.DEAD
    errors = " ".join(_recursive_errors(record)).casefold()
    if any(token in errors for token in ("access denied", "blocked", "login_required", "captcha", "http_403", "http_429")):
        return CollectionStatus.BLOCKED
    if any(token in errors for token in ("timeout", "tempor", "connection", "circuit")):
        return CollectionStatus.RETRYABLE_FAILURE
    if retrieval == "recovery_deferred":
        return CollectionStatus.NEEDS_MANUAL_REVIEW
    return CollectionStatus.DISCOVERED


def validate_full_source_text(record: dict[str, Any]) -> tuple[bool, str]:
    """Return whether the saved text is proven complete enough to call full text.

    The legacy content-quality flag is necessary but not sufficient.  Earlier
    collection runs occasionally accepted login, deletion, home-page, or player
    shells.  Structured extractors are accepted; generic HTML extraction is
    accepted only for a content-shaped source type and never when a shell marker
    is present.
    """

    text = clean_text(record.get("text_original") or "")
    quality = record.get("content_quality") or {}
    if not text:
        return False, "empty_text"
    if not quality.get("accepted") or str(quality.get("completeness") or "") != "full":
        return False, "legacy_quality_not_full"
    for reason, pattern in NON_CONTENT_PATTERNS:
        if pattern.search(text):
            return False, reason
    method = str(quality.get("extraction_method") or "")
    source_type = str(record.get("source_type") or "")
    provenance = str(quality.get("provenance") or "")
    if method.startswith(("semantic_dom", "jsonld:", "embedded:json:", "pypdf", "json_text_fallback")):
        return True, "structured_extractor_validated"
    if method == "trafilatura" and source_type in {
        "news_article",
        "blog_post",
        "government_or_military_statement",
        "ngo_report",
    } and len(text) >= 100:
        return True, "main_text_extractor_validated"
    if method == "trafilatura" and source_type == "public_facebook_post" and provenance in {
        "facebook_public_embed",
        "source_live",
    } and len(text) >= 20:
        return True, "public_post_text_validated"
    return False, "completeness_not_independently_established"


def classify_source_content(record: dict[str, Any]) -> SourceContentStatus:
    text = clean_text(record.get("text_original") or "")
    quality = record.get("content_quality") or {}
    accepted = bool(quality.get("accepted"))
    completeness = str(quality.get("completeness") or "")
    preservation = str(record.get("preservation_status") or "")
    if text:
        validated_full, _reason = validate_full_source_text(record)
        if not accepted or completeness != "full" or not validated_full:
            return SourceContentStatus.PARTIAL_TEXT
        provenance = str(quality.get("provenance") or "").casefold()
        # Classify by the provenance of the selected text, not by the presence
        # of an unrelated archive URL on the source record.  Earlier records
        # often carried ``archived_text_preserved`` after an archive attempt
        # even when the chosen text came directly from a public oEmbed/feed.
        if "archive" in provenance or "wayback" in provenance:
            return SourceContentStatus.FULL_TEXT_ARCHIVED
        if provenance in {"existing_preserved_text", "local_snapshot", "historical_local"} or preservation == "existing_text_preserved":
            return SourceContentStatus.FULL_TEXT_LOCAL_SNAPSHOT
        return SourceContentStatus.FULL_TEXT_DIRECT
    normalized = normalize_url(record.get("original_url") or "")
    if normalized.normalization_status == "malformed":
        return SourceContentStatus.MALFORMED
    reachability = source_reachability(record)
    if reachability == CollectionStatus.BLOCKED:
        return SourceContentStatus.BLOCKED
    if reachability == CollectionStatus.DEAD:
        return SourceContentStatus.DEAD
    metadata_values = [
        record.get("page_title"), record.get("publisher"), record.get("author"),
        record.get("publication_date"), *(record.get("captions") or []), *(record.get("descriptions") or []),
    ]
    if any(clean_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)) for value in metadata_values if value):
        return SourceContentStatus.METADATA_ONLY
    if reachability in {CollectionStatus.RETRYABLE_FAILURE, CollectionStatus.NEEDS_MANUAL_REVIEW}:
        return SourceContentStatus.NEEDS_MANUAL_REVIEW
    return SourceContentStatus.URL_PRESERVED if normalized.normalized_value else SourceContentStatus.REFERENCE_ONLY


class AirwarsSourceIndex:
    def __init__(self, source_root: Path):
        self.records: dict[str, dict[str, Any]] = {}
        self.by_raw_url: dict[str, str] = {}
        self.by_normalized_url: dict[str, str] = {}
        for path in sorted(Path(source_root).glob("*.json")):
            with path.open(encoding="utf-8") as handle:
                record = json.load(handle)
            source_id = str(record.get("source_id") or "")
            if not source_id:
                continue
            self.records[source_id] = record
            for value in [record.get("original_url"), *(record.get("observed_original_urls") or [])]:
                if value:
                    self.by_raw_url.setdefault(str(value), source_id)
            normalized = str(record.get("normalized_url") or "")
            if normalized:
                self.by_normalized_url.setdefault(normalized, source_id)

    def match(self, raw_url: str) -> tuple[str | None, dict[str, Any] | None]:
        source_id = self.by_raw_url.get(raw_url)
        if not source_id:
            source_id = self.by_normalized_url.get(normalize_url(raw_url).normalized_value)
        return source_id, self.records.get(source_id) if source_id else None

    def primary_status_counts(self) -> Counter[str]:
        return Counter(classify_source_content(record).value for record in self.records.values())


class AirwarsConnector:
    key = "airwars"
    display_name = "Airwars civilian-casualty records"

    def __init__(self, project_root: Path, legacy_zip: Path):
        self.project_root = Path(project_root).resolve()
        self.legacy_zip = Path(legacy_zip).resolve()
        self._records_by_sequence: dict[int, dict[str, Any]] = {}
        for path in sorted((self.project_root / "data" / "incidents").glob("*.json")):
            with path.open(encoding="utf-8") as handle:
                record = json.load(handle)
            self._records_by_sequence[int(record["legacy_sequence"])] = record
        self.sources = AirwarsSourceIndex(self.project_root / "data" / "sources")

    def record_types(self) -> Sequence[RecordType]:
        return [RecordType(
            key="civilian_casualty_incident",
            label="Airwars civilian-casualty incident",
            required_fields=("incident_id", "airwars_identifier", "incident_date", "textual_description"),
            field_rules={"source": "connector_defined"},
            relationship_rules={"source_references": "preserve_every_occurrence"},
        )]

    def analyze(self, project: ArchiveProject, sample_bodies: dict[str, bytes]) -> dict[str, Any]:
        return {
            "connector": self.key,
            "target_url": project.site.target_url,
            "record_types": [asdict(item) for item in self.record_types()],
            "candidate_records": len(self._records_by_sequence),
            "sample_pages": sorted(sample_bodies),
            "detected_fields": list(self.record_types()[0].required_fields),
            "source_link_patterns": ["incident source list", "historical source row", "archived incident recovery"],
        }

    def discover(self, project: ArchiveProject) -> Sequence[DiscoveredTarget]:
        return [
            DiscoveredTarget(
                identity=record["internal_id"],
                url=record["canonical_url"],
                record_type="civilian_casualty_incident",
                metadata={"sequence": sequence, "airwars_id": record.get("airwars_id")},
            )
            for sequence, record in sorted(self._records_by_sequence.items())
        ]

    def parse(self, target: DiscoveredTarget, body: bytes, content_type: str) -> ParsedRecord:
        decoded = body.decode("utf-8", errors="replace")
        document = html.fromstring(decoded)
        code = clean_text(" ".join(document.xpath("//*[@data-incident-code]/@data-incident-code | //h1/text()")))
        narrative = clean_text(" ".join(document.xpath("//*[contains(@class,'assessment') or @data-field='narrative']//text()")))
        date = clean_text(" ".join(document.xpath("//time/@datetime | //*[@data-field='date']/text()")))
        if not narrative:
            raise ValueError("missing_required_field:textual_description")
        provenance = [FieldProvenance("direct_airwars", target.url, method="public_html")]
        fields = {
            "incident_id": FieldValue("incident_id", target.identity, target.identity, provenance),
            "airwars_identifier": FieldValue("airwars_identifier", code, code, provenance, "present" if code else "missing", "selector_empty" if not code else None),
            "incident_date": FieldValue("incident_date", date, date, provenance, "present" if date else "missing", "selector_empty" if not date else None),
            "textual_description": FieldValue("textual_description", narrative, narrative, provenance),
        }
        record = SiteRecord(
            record_id=target.identity,
            record_type=target.record_type,
            canonical_url=target.url,
            fields=fields,
            collection_status=CollectionStatus.NORMALIZED,
            record_origin_status="direct_airwars",
            direct_verification_status="DIRECT_FETCH_SUCCESS",
            data_quality_status="complete" if code and date else "partial",
        )
        return ParsedRecord(record, [target.url])

    @staticmethod
    def _direct_verification(record: dict[str, Any]) -> str:
        """Classify only current direct Airwars attempts.

        An archived page is record-origin evidence, never a direct fetch
        success.  The archived-copy cases still carry current HTTP 403
        evidence and must therefore remain blocked in this dimension.
        """

        extraction = record.get("page_extraction") or {}
        if record.get("api_extraction"):
            return "DIRECT_FETCH_SUCCESS"
        if extraction.get("source_type") == "airwars_live":
            return "DIRECT_FETCH_SUCCESS"
        retrieval = record.get("retrieval_status") or {}
        direct_attempts = [
            retrieval.get("airwars_endpoint") or {},
            retrieval.get("live_page") or {},
        ]
        if any(
            attempt.get("ok") and 200 <= int(attempt.get("status") or 0) < 300
            for attempt in direct_attempts
        ):
            return "DIRECT_FETCH_SUCCESS"
        statuses = set(_recursive_statuses(direct_attempts))
        errors = " ".join(_recursive_errors(direct_attempts)).casefold()
        if 403 in statuses or "http_403" in errors:
            return "BLOCKED_HTTP_403"
        if statuses & {404, 410}:
            return "DEAD"
        return "DIRECT_FETCH_OTHER_FAILURE"

    @staticmethod
    def _coordinate_status(record: dict[str, Any]) -> str:
        lat, lon = record.get("latitude"), record.get("longitude")
        if lat in (None, "") or lon in (None, ""):
            return "missing"
        try:
            latitude, longitude = float(lat), float(lon)
        except (TypeError, ValueError):
            return "malformed"
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return "malformed"
        # Keep this display/audit range identical to the published map policy.
        # It is deliberately broad and never used to alter coordinates.
        if not (31.0 <= latitude <= 38.0 and 34.0 <= longitude <= 43.0):
            return "outside_accepted_range"
        return "drawable"

    def structured_incident(self, sequence: int, legacy: dict[str, Any]) -> dict[str, Any]:
        current = self._records_by_sequence[sequence]
        direct_status = self._direct_verification(current)
        origin = "historical_local_normalized"
        extraction = current.get("page_extraction") or {}
        retrieval = current.get("retrieval_status") or {}
        archive_attempt = retrieval.get("archive_page") or {}
        has_archived_airwars_copy = (
            extraction.get("source_type") == "airwars_archive"
            or bool(archive_attempt.get("ok"))
        )
        if has_archived_airwars_copy:
            origin = "mixed_historical_and_archived_airwars"
        elif direct_status == "DIRECT_FETCH_SUCCESS":
            origin = "mixed_historical_and_direct_airwars"
        historical_incident = legacy.get("incident") or {}
        text = clean_text(current.get("narrative_original") or "")
        text_origin = "archived_airwars_snapshot" if text else "historical_structured_summary"
        if not text:
            text = clean_text(historical_incident.get("ملخص عربي منظم") or current.get("narrative_ar") or "")
        return {
            **current,
            "incident_id": current["internal_id"],
            "original_airwars_identifier": current.get("airwars_id"),
            "original_airwars_url": current.get("canonical_url"),
            "coordinate_status": self._coordinate_status(current),
            "textual_description": text,
            "textual_description_origin": text_origin,
            "direct_verification_status": direct_status,
            "record_origin_status": origin,
            "data_quality_status": "complete_structural_skeleton" if text else "missing_usable_incident_text",
            "historical_structured_fields": historical_incident,
            "historical_page_fields": legacy.get("page_fields") or [],
            "historical_page_sections": legacy.get("page_sections") or [],
            "historical_source_reference_count": len(legacy.get("sources") or []),
            "historical_workbook_sha256": (legacy.get("case") or {}).get("source_workbook_sha256"),
        }

    def _reference(
        self,
        *,
        incident_id: str,
        sequence: int,
        occurrence: int,
        origin: str,
        row: dict[str, Any],
        duplicate: bool,
    ) -> SourceReference:
        raw_url = str(row.get("رابط المصدر") or row.get("url") or row.get("original_url") or "")
        normalized = normalize_url(raw_url)
        source_id, source = self.sources.match(raw_url)
        preservation = classify_source_content(source or {"original_url": raw_url})
        reachability = source_reachability(source or {"original_url": raw_url})
        identity = f"{incident_id}\n{origin}\n{occurrence}\n{raw_url}"
        return SourceReference(
            source_reference_id=f"reference-{_sha24(identity)}",
            record_id=incident_id,
            raw_url=raw_url,
            normalized_url=normalized.normalized_value,
            normalization_status=normalized.normalization_status,
            normalization_reason=normalized.normalization_reason,
            label=str(row.get("اسم المصدر — عربي") or row.get("name_ar") or row.get("publisher") or ""),
            title=str(row.get("عنوان المصدر") or row.get("title") or (source or {}).get("page_title") or ""),
            publisher=str(row.get("اسم المصدر — الأصل") or row.get("name") or row.get("publisher") or (source or {}).get("publisher") or ""),
            citation_text=str(row.get("نص الاقتباس") or row.get("content") or row.get("description") or ""),
            publication_date=str(row.get("تاريخ المصدر") or row.get("date") or (source or {}).get("publication_date") or "") or None,
            source_type=str((source or {}).get("source_type") or row.get("صيغة السجل") or "unknown"),
            domain=normalized.domain,
            current_reachability_status=reachability,
            content_preservation_status=preservation,
            provenance=[FieldProvenance(origin, row.get("رابط الحادثة") or None, method="source_reference_import")],
            duplicate_relationship=duplicate,
            malformed=normalized.normalization_status == "malformed",
            manual_review=bool((source or {}).get("review_flags")) or normalized.normalization_status == "malformed",
            external_source_id=source_id,
            archived_urls=[str(value) for value in [row.get("رابط الأرشيف"), *((source or {}).get("archived_urls") or [])] if value],
        )

    def source_references(self) -> list[SourceReference]:
        references: list[SourceReference] = []
        seen_pairs: dict[tuple[str, str], int] = defaultdict(int)
        with LegacyArchive(self.legacy_zip) as archive:
            for summary in archive.iter_summaries():
                sequence = int(summary["sequence"])
                incident_id = self._records_by_sequence[sequence]["internal_id"]
                legacy = archive.case_data(sequence)
                for occurrence, row in enumerate(legacy.get("sources") or [], start=1):
                    raw = str(row.get("رابط المصدر") or "")
                    pair = (incident_id, raw)
                    references.append(self._reference(
                        incident_id=incident_id,
                        sequence=sequence,
                        occurrence=occurrence,
                        origin="historical_airwars_source_row",
                        row=row,
                        duplicate=seen_pairs[pair] > 0,
                    ))
                    seen_pairs[pair] += 1

        extra_counter: Counter[str] = Counter()
        for source_id, source in sorted(self.sources.records.items()):
            for provenance in source.get("provenance") or []:
                incident_id = str(provenance.get("incident_id") or "")
                raw = str(provenance.get("observed_original_url") or source.get("original_url") or "")
                if not incident_id or (incident_id, raw) in seen_pairs:
                    continue
                extra_counter[incident_id] += 1
                sequence = int(provenance.get("incident_sequence") or 0)
                row = {
                    "url": raw,
                    "publisher": source.get("publisher") or "",
                    "title": source.get("page_title") or "",
                    "date": source.get("publication_date") or "",
                    "رابط الحادثة": provenance.get("airwars_incident_url") or "",
                }
                references.append(self._reference(
                    incident_id=incident_id,
                    sequence=sequence,
                    occurrence=extra_counter[incident_id],
                    origin="recovered_archived_incident_source",
                    row=row,
                    duplicate=False,
                ))
                seen_pairs[(incident_id, raw)] += 1

        for sequence, incident in sorted(self._records_by_sequence.items()):
            incident_id = incident["internal_id"]
            for row in incident.get("sources") or []:
                raw = str(row.get("url") or row.get("original_url") or "")
                if not raw or (incident_id, raw) in seen_pairs:
                    continue
                extra_counter[incident_id] += 1
                references.append(self._reference(
                    incident_id=incident_id,
                    sequence=sequence,
                    occurrence=extra_counter[incident_id],
                    origin="normalized_incident_source",
                    row=row,
                    duplicate=False,
                ))
                seen_pairs[(incident_id, raw)] += 1
        return references

    def iter_structured_incidents(self) -> Iterator[dict[str, Any]]:
        with LegacyArchive(self.legacy_zip) as archive:
            for summary in archive.iter_summaries():
                sequence = int(summary["sequence"])
                yield self.structured_incident(sequence, archive.case_data(sequence))
