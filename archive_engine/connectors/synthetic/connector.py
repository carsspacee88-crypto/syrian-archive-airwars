from __future__ import annotations

import hashlib
from typing import Any, Sequence

from lxml import html

from archive_engine.connectors.base import DiscoveredTarget, ParsedRecord
from archive_engine.models import (
    ArchiveProject,
    FieldProvenance,
    FieldValue,
    RecordType,
    SiteRecord,
    SourceReference,
)
from archive_engine.normalizers.urls import normalize_url
from archive_engine.statuses import CollectionStatus, SourceContentStatus


def _text(document: Any, expression: str) -> str:
    return " ".join(str(value).strip() for value in document.xpath(expression) if str(value).strip())


class SyntheticLibraryConnector:
    """Fixture connector deliberately unrelated to Airwars terminology or markup."""

    key = "synthetic_library"
    display_name = "Synthetic library catalogue"

    def record_types(self) -> Sequence[RecordType]:
        return [RecordType(
            key="catalogue_entry",
            label="Library catalogue entry",
            required_fields=("accession_number", "title", "abstract"),
            field_rules={
                "accession_number": "//article[@data-accession]/@data-accession",
                "title": "//h1[contains(@class,'entry-title')]//text()",
                "abstract": "//section[contains(@class,'abstract')]//text()",
                "published_on": "//time[contains(@class,'published')]/@datetime",
            },
            relationship_rules={
                "references": "//a[contains(@class,'reference-link')]/@href",
            },
        )]

    def analyze(self, project: ArchiveProject, sample_bodies: dict[str, bytes]) -> dict[str, Any]:
        samples = []
        for url, body in sample_bodies.items():
            document = html.fromstring(body.decode("utf-8", errors="replace"))
            samples.append({
                "url": url,
                "record_type": "catalogue_entry" if document.xpath("//article[@data-accession]") else "unknown",
                "fields": [
                    name
                    for name, expression in self.record_types()[0].field_rules.items()
                    if document.xpath(expression)
                ],
                "source_links": len(document.xpath("//a[contains(@class,'reference-link')]/@href")),
            })
        return {
            "connector": self.key,
            "target_url": project.site.target_url,
            "candidate_record_types": [item.key for item in self.record_types()],
            "sample_pages": samples,
            "detected_fields": list(self.record_types()[0].field_rules),
            "source_link_patterns": ["a.reference-link"],
        }

    def discover(self, project: ArchiveProject) -> Sequence[DiscoveredTarget]:
        records = list(project.scope.get("records") or [])
        return [
            DiscoveredTarget(
                identity=str(item.get("id") or hashlib.sha256(str(item["url"]).encode()).hexdigest()[:16]),
                url=str(item["url"]),
                record_type="catalogue_entry",
                metadata={"ordinal": index},
            )
            for index, item in enumerate(records, start=1)
        ]

    def parse(self, target: DiscoveredTarget, body: bytes, content_type: str) -> ParsedRecord:
        document = html.fromstring(body.decode("utf-8", errors="replace"), base_url=target.url)
        root = document.xpath("//article[@data-accession]")
        if not root:
            raise ValueError("malformed_html:catalogue_entry_missing")
        accession = str(root[0].get("data-accession") or "").strip()
        title = _text(document, "//h1[contains(@class,'entry-title')]//text()")
        abstract = _text(document, "//section[contains(@class,'abstract')]//text()")
        published = _text(document, "//time[contains(@class,'published')]/@datetime")
        missing = [name for name, value in (("accession_number", accession), ("title", title), ("abstract", abstract)) if not value]
        if missing:
            raise ValueError("missing_required_field:" + ",".join(missing))
        provenance = [FieldProvenance("synthetic_fixture_public_page", target.url, method="html_selectors")]
        fields = {
            "accession_number": FieldValue("accession_number", accession, accession, provenance),
            "title": FieldValue("title", title, title, provenance),
            "abstract": FieldValue("abstract", abstract, abstract, provenance),
            "published_on": FieldValue("published_on", published, published, provenance, "present" if published else "missing", "optional_field_missing" if not published else None),
        }
        references = []
        raw_links = []
        for index, node in enumerate(document.xpath("//a[contains(@class,'reference-link')][@href]"), start=1):
            raw = str(node.get("href") or "")
            normalized = normalize_url(raw)
            raw_links.append(raw)
            references.append(SourceReference(
                source_reference_id="reference-" + hashlib.sha256(f"{target.identity}\n{index}\n{raw}".encode()).hexdigest()[:24],
                record_id=target.identity,
                raw_url=raw,
                normalized_url=normalized.normalized_value,
                normalization_status=normalized.normalization_status,
                normalization_reason=normalized.normalization_reason,
                label=" ".join(node.itertext()).strip(),
                domain=normalized.domain,
                current_reachability_status=CollectionStatus.DISCOVERED,
                content_preservation_status=SourceContentStatus.URL_PRESERVED,
                provenance=provenance,
                malformed=normalized.normalization_status == "malformed",
            ))
        record = SiteRecord(
            record_id=target.identity,
            record_type=target.record_type,
            canonical_url=target.url,
            fields=fields,
            source_references=references,
            collection_status=CollectionStatus.NORMALIZED,
            record_origin_status="synthetic_fixture_public_page",
            direct_verification_status="DIRECT_FETCH_SUCCESS",
            data_quality_status="complete",
        )
        return ParsedRecord(record, raw_links)
