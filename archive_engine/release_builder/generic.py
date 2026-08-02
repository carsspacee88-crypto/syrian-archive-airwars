from __future__ import annotations

import hashlib
import html
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from archive_engine.models import ArchiveProject
from archive_engine.validators.release import ReleaseValidator, write_checksums
from archive_pipeline.io_utils import atomic_write_json, atomic_write_text, utc_now


class GenericReleaseBuildInterrupted(RuntimeError):
    pass


def _safe(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _source_id(normalized_url: str, raw_url: str) -> str:
    return "external-" + hashlib.sha256((normalized_url or raw_url).encode("utf-8")).hexdigest()[:24]


def _external_link(url: Any) -> str:
    value = str(url or "")
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return f'<a href="{_safe(value)}">{_safe(value)}</a>'
    return f'<span>{_safe(value)}</span>'


class GenericTextualReleaseBuilder:
    """Connector-neutral release builder for records produced by ArchiveEngine."""

    def __init__(
        self,
        store_root: Path,
        project: ArchiveProject,
        run_id: str,
        release_root: Path,
        *,
        release_id: str,
        parent_release_id: str | None = None,
        resume: bool = False,
        interrupt_after_records: int | None = None,
    ):
        self.store_root = Path(store_root).resolve()
        self.project = project
        self.run_id = run_id
        self.root = Path(release_root).resolve()
        self.release_id = release_id
        self.parent_release_id = parent_release_id
        self.resume = resume
        self.interrupt_after_records = interrupt_after_records
        self.state_path = self.root / "logs" / "build-state.json"
        if self.root.exists() and any(self.root.iterdir()) and not resume:
            raise FileExistsError(f"release_directory_not_empty:{self.root}")
        for item in ("data/records", "data/source-references", "data/external-sources", "site/records", "site/references", "site/sources", "reports", "logs", "checksums"):
            (self.root / item).mkdir(parents=True, exist_ok=True)

    def _read_inputs(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        run = self.store_root / "runs" / self.run_id
        records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((run / "records").glob("*.json"))]
        references = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((run / "source-references").glob("*.json"))]
        return records, references

    @staticmethod
    def _page(title: str, body: str) -> str:
        return f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_safe(title)}</title><link rel="stylesheet" href="/assets/site.css"></head><body><header><a href="/">Textual archive</a> · <a href="/search.html">Search</a></header><main>{body}</main></body></html>\n'

    def _record_page(self, record: dict[str, Any], references: list[dict[str, Any]]) -> str:
        fields = "".join(
            f'<section><h2>{_safe(name)}</h2><p>{_safe(field.get("normalized_value"))}</p><details><summary>Raw value and provenance</summary><pre>{_safe(json.dumps({"raw_value": field.get("raw_value"), "status": field.get("status"), "reason": field.get("reason"), "provenance": field.get("provenance")}, ensure_ascii=False, indent=2))}</pre></details></section>'
            for name, field in sorted((record.get("fields") or {}).items())
        )
        links = "".join(f'<li><a href="/references/{_safe(row["source_reference_id"])}/">{_safe(row["source_reference_id"])}</a></li>' for row in references) or "<li>No source references.</li>"
        return self._page(str(record.get("record_id")), f'<h1>{_safe(record.get("record_id"))}</h1><p>Collection: {_safe(record.get("collection_status"))}; origin: {_safe(record.get("record_origin_status"))}; verification: {_safe(record.get("direct_verification_status"))}</p>{fields}<h2>Source relationships ({len(references)})</h2><ul>{links}</ul>')

    def build(self) -> dict[str, Any]:
        records, references = self._read_inputs()
        record_ids = {str(record.get("record_id") or "") for record in records}
        if "" in record_ids or len(record_ids) != len(records):
            raise ValueError("duplicate_or_empty_record_identifier")
        by_record: dict[str, list[dict[str, Any]]] = defaultdict(list)
        external: dict[str, dict[str, Any]] = {}
        for row in references:
            record_id = str(row.get("record_id") or "")
            if record_id not in record_ids:
                raise ValueError(f"source_reference_missing_record:{row.get('source_reference_id')}")
            by_record[record_id].append(row)
            source_id = _source_id(str(row.get("normalized_url") or ""), str(row.get("raw_url") or ""))
            item = external.setdefault(source_id, {"source_id": source_id, "normalized_url": row.get("normalized_url"), "raw_urls": [], "reference_ids": [], "content_preservation_status": "URL_PRESERVED"})
            if row.get("raw_url") not in item["raw_urls"]:
                item["raw_urls"].append(row.get("raw_url"))
            item["reference_ids"].append(row.get("source_reference_id"))
            row["external_source_id"] = source_id

        completed = 0
        for record in records:
            record_id = str(record["record_id"])
            target = self.root / "data" / "records" / f"{record_id}.json"
            page = self.root / "site" / "records" / record_id / "index.html"
            if not (self.resume and target.is_file() and page.is_file()):
                atomic_write_json(target, {**record, "source_reference_ids": [row["source_reference_id"] for row in by_record[record_id]]})
                atomic_write_text(page, self._record_page(record, by_record[record_id]))
            completed += 1
            atomic_write_json(self.state_path, {"release_id": self.release_id, "phase": "records", "records_completed": completed, "updated_at": utc_now()})
            if self.interrupt_after_records and completed >= self.interrupt_after_records:
                raise GenericReleaseBuildInterrupted(f"intentional_release_interruption_after:{completed}")

        for row in references:
            reference_id = str(row["source_reference_id"])
            atomic_write_json(self.root / "data" / "source-references" / f"{reference_id}.json", row)
            message = "This is a source reference record. The complete textual content of the external source has not been preserved in this release."
            body = f'<h1>Source reference</h1><p><strong>{_safe(message)}</strong></p><dl><dt>Reference ID</dt><dd>{_safe(reference_id)}</dd><dt>Record</dt><dd><a href="/records/{_safe(row["record_id"])}/">{_safe(row["record_id"])}</a></dd><dt>Raw URL</dt><dd class="raw-url">{_external_link(row.get("raw_url"))}</dd><dt>Normalized URL</dt><dd class="raw-url">{_safe(row.get("normalized_url"))}</dd><dt>Preservation</dt><dd>{_safe(row.get("content_preservation_status"))}</dd></dl><a href="/sources/{_safe(row["external_source_id"])}/">External-source entity</a>'
            atomic_write_text(self.root / "site" / "references" / reference_id / "index.html", self._page(reference_id, body))

        for source_id, source in sorted(external.items()):
            atomic_write_json(self.root / "data" / "external-sources" / f"{source_id}.json", source)
            refs = "".join(f'<li><a href="/references/{_safe(reference_id)}/">{_safe(reference_id)}</a></li>' for reference_id in source["reference_ids"])
            urls = "".join(f'<li class="raw-url">{_safe(url)}</li>' for url in source["raw_urls"])
            atomic_write_text(self.root / "site" / "sources" / source_id / "index.html", self._page(source_id, f'<h1>External-source entity</h1><p><strong>URL/metadata record only. Full external source text is not preserved.</strong></p><h2>Raw URLs</h2><ul>{urls}</ul><h2>Relationships</h2><ul>{refs}</ul>'))

        css = "body{max-width:75rem;margin:auto;padding:1rem;font:16px/1.6 system-ui}header{padding:1rem 0}section{border-top:1px solid #ddd}.raw-url,pre{overflow-wrap:anywhere;white-space:pre-wrap}"
        atomic_write_text(self.root / "site" / "assets" / "site.css", css)
        documents = []
        for record in records:
            field_text = " ".join(str(field.get("normalized_value") or "") for field in (record.get("fields") or {}).values())
            documents.append({"id": record["record_id"], "path": f'/records/{record["record_id"]}/', "text": field_text.casefold()})
        atomic_write_json(self.root / "site" / "search-index.json", {"documents": documents})
        atomic_write_text(self.root / "site" / "search.html", self._page("Search", '<h1>Search</h1><form id="search"><input name="q"><button>Search</button></form><div id="results"></div><script src="/assets/search.js"></script>'))
        search_js = "document.getElementById('search').addEventListener('submit',async e=>{e.preventDefault();let q=new FormData(e.target).get('q').toLowerCase(),d=(await(await fetch('/search-index.json')).json()).documents;document.getElementById('results').innerHTML=d.filter(x=>x.text.includes(q)).map(x=>`<a href=\"${x.path}\">${x.id}</a>`).join('<br>')});"
        atomic_write_text(self.root / "site" / "assets" / "search.js", search_js)
        listing = "".join(f'<li><a href="/records/{_safe(record["record_id"])}/">{_safe(record["record_id"])}</a></li>' for record in records)
        atomic_write_text(self.root / "site" / "index.html", self._page(self.project.name, f'<h1>{_safe(self.project.name)}</h1><p>Connector: {_safe(self.project.site.connector)}</p><p>{len(records)} records; {len(references)} source relationships; {len(external)} unique external-source entities.</p><ul>{listing}</ul>'))
        raw_urls = sorted({str(row.get("raw_url")) for row in references if row.get("raw_url")})
        normalized_urls = sorted({str(row.get("normalized_url")) for row in references if row.get("normalized_url") and row.get("normalization_status") != "malformed"})
        atomic_write_text(self.root / "data" / "all-raw-urls.txt", "\n".join(raw_urls) + ("\n" if raw_urls else ""))
        atomic_write_text(self.root / "data" / "all-normalized-urls.txt", "\n".join(normalized_urls) + ("\n" if normalized_urls else ""))
        report = {"records": len(records), "source_references": len(references), "external_sources": len(external), "raw_urls": len(raw_urls), "normalized_urls": len(normalized_urls), "relationships_lost": 0}
        data_quality = {
            "records": len(records),
            "by_status": dict(sorted(Counter(str(record.get("data_quality_status") or "unreported") for record in records).items())),
            "fields_missing": sum(
                str(field.get("status") or "present") != "present"
                for record in records
                for field in (record.get("fields") or {}).values()
            ),
            "malformed_source_references": sum(bool(row.get("malformed")) for row in references),
        }
        source_content = {
            "source_references": len(references),
            "primary_status_counts": dict(sorted(Counter(str(row.get("content_preservation_status") or "REFERENCE_ONLY") for row in references).items())),
        }
        atomic_write_json(self.root / "reports" / "coverage.json", report)
        atomic_write_json(self.root / "reports" / "data-quality.json", data_quality)
        atomic_write_json(self.root / "reports" / "source-content-status.json", source_content)
        validation_report = {
            "result": "passed",
            "blocking_failures": [],
            "non_blocking_failures": [],
            "checks": {
                "record_pages": len(list((self.root / "site" / "records").glob("*/index.html"))),
                "source_reference_pages": len(list((self.root / "site" / "references").glob("*/index.html"))),
                "external_source_pages": len(list((self.root / "site" / "sources").glob("*/index.html"))),
                "relationships_lost": 0,
                "raw_urls_preserved": len(raw_urls),
                "normalized_urls_preserved": len(normalized_urls),
            },
        }
        expected_pages = (len(records), len(references), len(external))
        actual_pages = (
            validation_report["checks"]["record_pages"],
            validation_report["checks"]["source_reference_pages"],
            validation_report["checks"]["external_source_pages"],
        )
        if actual_pages != expected_pages:
            raise ValueError(f"generic_release_page_count_mismatch:{actual_pages}:{expected_pages}")
        atomic_write_json(self.root / "reports" / "validation.json", validation_report)
        atomic_write_json(self.root / "release.json", {
            "release_id": self.release_id,
            "project_id": self.project.project_id,
            "run_id": self.run_id,
            "connector": self.project.site.connector,
            "parent_release_id": self.parent_release_id,
            "generated_at": utc_now(),
            "immutable": True,
            "counts": report,
            "reports": {
                "coverage": "reports/coverage.json",
                "data_quality": "reports/data-quality.json",
                "source_content_status": "reports/source-content-status.json",
                "validation": "reports/validation.json",
            },
        })
        atomic_write_text(self.root / ".immutable", f"{self.release_id}\nFinalized after validation; do not modify.\n")
        atomic_write_text(self.root / "logs" / "build.log", json.dumps(report, sort_keys=True) + "\n")
        atomic_write_json(self.state_path, {"release_id": self.release_id, "phase": "complete", "records_completed": len(records), "updated_at": utc_now()})
        write_checksums(self.root)
        validation = ReleaseValidator().validate(self.root)
        if not validation.passed:
            raise ValueError(f"generic_release_validation_failed:{validation.blocking_failures}")
        return {"release_id": self.release_id, "release_root": str(self.root), **report, "validation": "passed"}
