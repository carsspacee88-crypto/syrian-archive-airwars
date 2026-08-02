from __future__ import annotations

import html
import json
import os
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from archive_engine.connectors.airwars.connector import (
    AirwarsConnector,
    classify_source_content,
    source_reachability,
    validate_full_source_text,
)
from archive_engine.statuses import SourceContentStatus
from archive_pipeline.io_utils import atomic_write_json, atomic_write_text


FULL_TEXT_STATUSES = {
    SourceContentStatus.FULL_TEXT_DIRECT.value,
    SourceContentStatus.FULL_TEXT_ARCHIVED.value,
    SourceContentStatus.FULL_TEXT_LOCAL_SNAPSHOT.value,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _display(value: Any, fallback: str = "غير متاح") -> str:
    if value is None or value == "" or value == []:
        return f'<span class="missing">{_safe(fallback)}</span>'
    return _safe(value)


def _arabic_number(value: int) -> str:
    return f"{value:,}"


def _jsonable_reference(reference: Any) -> dict[str, Any]:
    row = asdict(reference)
    row["current_reachability_status"] = reference.current_reachability_status.value
    row["content_preservation_status"] = reference.content_preservation_status.value
    return row


def _fold_search(*values: Any) -> str:
    return " ".join(str(value or "").casefold() for value in values if value not in (None, ""))


def _field(label: str, value: Any, *, ltr: bool = False) -> str:
    css = "field ltr" if ltr else "field"
    return f'<div class="{css}"><dt>{_safe(label)}</dt><dd>{_display(value)}</dd></div>'


def _status_badge(status: str) -> str:
    return f'<span class="status-badge status-{_safe(status)} ltr">{_safe(status)}</span>'


def _external_link(url: Any, label: Any | None = None) -> str:
    value = str(url or "")
    shown = value if label is None else str(label)
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return f'<a href="{_safe(value)}" rel="noopener noreferrer">{_safe(shown)}</a>'
    return f'<span class="raw-url">{_display(shown, "رابط غير متاح")}</span>'


def _page(title: str, body: str, *, active: str = "", extra_head: str = "", scripts: str = "") -> str:
    nav = [
        ("home", "/", "الرئيسية"),
        ("cases", "/cases/", "الحوادث"),
        ("search", "/search.html", "البحث"),
        ("map", "/map.html", "الخريطة"),
        ("reports", "/reports/", "التقارير"),
        ("admin", "/admin", "لوحة الإدارة"),
    ]
    links = "".join(
        f'<a href="{href}"{(" aria-current=\"page\"" if key == active else "")}>{label}</a>'
        for key, href, label in nav
    )
    return f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>{_safe(title)} — المدني</title>
  <link rel="stylesheet" href="/assets/textual-site.css">
  {extra_head}
</head>
<body>
<a class="skip-link" href="#main">تجاوز إلى المحتوى</a>
<header class="site-header"><div class="wrap header-inner">
  <a class="brand" href="/"><span class="brand-mark">م</span><span><b>المدني</b><small>الهيكل النصي السوري</small></span></a>
  <nav class="site-nav" aria-label="التنقل الرئيسي">{links}</nav>
</div></header>
<main id="main">{body}</main>
<footer class="site-footer"><div class="wrap">واجهة مستقلة لبيانات توثيقية محفوظة محليًا. لا تتبع هذه الواجهة Airwars، وتعرض أصل كل قيمة وحالة حفظها بدقة.</div></footer>
{scripts}
</body></html>"""


def _source_statement(status: str) -> str:
    if status in FULL_TEXT_STATUSES:
        return "حُفظ النص الرئيسي الكامل لهذا المصدر محليًا واجتاز فحص الاكتمال المحدد في سياسة الإصدار."
    if status == SourceContentStatus.PARTIAL_TEXT.value:
        return "هذا سجل مصدر يتضمن نصًا جزئيًا فقط؛ لم يثبت أن النص الكامل للمصدر الخارجي محفوظ في هذا الإصدار."
    return "هذا سجل مرجع مصدر. لم يُحفظ المحتوى النصي الكامل للمصدر الخارجي في هذا الإصدار."


class InterruptedReleaseBuild(RuntimeError):
    pass


class AirwarsTextualReleaseBuilder:
    """Build the Airwars structural text release without any live-network dependency."""

    def __init__(
        self,
        project_root: Path,
        legacy_zip: Path,
        release_root: Path,
        *,
        release_id: str,
        parent_release_id: str | None = None,
        resume: bool = False,
        interrupt_after: str | None = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.legacy_zip = Path(legacy_zip).resolve()
        self.release_root = Path(release_root).resolve()
        self.release_id = release_id
        self.parent_release_id = parent_release_id
        self.resume = resume
        self.interrupt_after = interrupt_after
        self.state_path = self.release_root / "logs" / "build-state.json"
        if self.release_root.exists() and any(self.release_root.iterdir()) and not resume:
            raise FileExistsError(f"release_directory_not_empty:{self.release_root}")
        for relative in ("checksums", "reports", "logs", "data/incidents", "data/sources", "site", "exports"):
            (self.release_root / relative).mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.is_file():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {
            "release_id": self.release_id,
            "started_at": utc_now(),
            "completed_phases": [],
            "last_phase": "created",
        }

    def _finish_phase(self, name: str, **counts: Any) -> None:
        completed = list(self.state.get("completed_phases") or [])
        if name not in completed:
            completed.append(name)
        self.state.update({"completed_phases": completed, "last_phase": name, "updated_at": utc_now(), **counts})
        atomic_write_json(self.state_path, self.state)
        if self.interrupt_after == name:
            raise InterruptedReleaseBuild(f"intentional_interruption_after:{name}")

    def _phase_done(self, name: str) -> bool:
        return self.resume and name in set(self.state.get("completed_phases") or [])

    def _copy_assets(self) -> None:
        assets = self.release_root / "site" / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        for name in ("textual-site.css", "textual-search.js", "textual-map.css", "textual-map.js"):
            shutil.copy2(self.project_root / "web" / name, assets / name)
        self._finish_phase("assets", asset_files=4)

    def _write_worklists(self, connector: AirwarsConnector, references: list[Any]) -> None:
        worklists = self.release_root / "data" / "worklists"
        worklists.mkdir(parents=True, exist_ok=True)
        incidents = [
            {"sequence": sequence, "incident_id": record["internal_id"], "canonical_url": record.get("canonical_url")}
            for sequence, record in sorted(connector._records_by_sequence.items())
        ]
        atomic_write_json(worklists / "incidents.json", {"immutable": True, "count": len(incidents), "records": incidents})
        atomic_write_json(worklists / "sources.json", {"immutable": True, "count": len(connector.sources.records), "source_ids": sorted(connector.sources.records)})
        atomic_write_json(worklists / "source-references.json", {"immutable": True, "count": len(references), "source_reference_ids": [item.source_reference_id for item in references]})
        self._finish_phase("worklists", incidents=len(incidents), sources=len(connector.sources.records), source_references=len(references))

    def _reference_page(self, row: dict[str, Any]) -> str:
        status = str(row["content_preservation_status"])
        source_link = (
            f'<a class="button" href="/sources/{_safe(row["external_source_id"])}/">فتح سجل المصدر الخارجي</a>'
            if row.get("external_source_id") else '<span class="missing">لا يوجد كيان مصدر مطابق لهذا المرجع.</span>'
        )
        archived = "".join(
            f'<li class="raw-url">{_external_link(url)}</li>'
            for url in row.get("archived_urls") or []
        ) or '<li class="missing">لا توجد نسخة مؤرشفة معروفة.</li>'
        provenance = "".join(
            f'<li><b class="ltr">{_safe(item.get("origin"))}</b> — {_display(item.get("method"))}</li>'
            for item in row.get("provenance") or []
        )
        body = f"""
<section class="hero"><div class="wrap"><div class="breadcrumbs"><a href="/">الرئيسية</a><span>/</span><a href="/cases/">الحوادث</a><span>/</span><span>مرجع مصدر</span></div>
<p class="eyebrow">سجل علاقة مستقل</p><h1>مرجع مصدر</h1><p class="lead">{_source_statement(status)}</p></div></section>
<div class="wrap content-stack">
<section class="panel notice"><h2>حقيقة الحفظ</h2>{_status_badge(status)}<p>وجود هذه الصفحة يعني أن علاقة الحادثة بالمصدر والرابط الأصلي محفوظان؛ ولا يعني تلقائيًا أن صفحة المصدر الخارجية أو نصها الكامل أُرشفت.</p></section>
<section class="panel"><h2>العلاقة</h2><dl class="field-grid">
{_field("معرّف المرجع", row.get("source_reference_id"), ltr=True)}
{_field("معرّف الحادثة", row.get("record_id"), ltr=True)}
{_field("معرّف كيان المصدر", row.get("external_source_id"), ltr=True)}
{_field("العنوان", row.get("title"))}{_field("الناشر/الحساب", row.get("publisher"))}{_field("النطاق", row.get("domain"), ltr=True)}
{_field("نوع المصدر", row.get("source_type"), ltr=True)}{_field("قابلية الوصول الحالية", row.get("current_reachability_status"), ltr=True)}{_field("علاقة مكررة محفوظة", "نعم" if row.get("duplicate_relationship") else "لا")}
</dl></section>
<section class="panel"><h2>الرابط الأصلي كما ظهر</h2><p class="raw-url">{_external_link(row.get("raw_url"))}</p><h3>الرابط المطبّع</h3><p class="raw-url">{_display(row.get("normalized_url"))}</p><dl class="field-grid">{_field("حالة التطبيع", row.get("normalization_status"), ltr=True)}{_field("سبب التطبيع", row.get("normalization_reason"))}{_field("رابط مشوه", "نعم" if row.get("malformed") else "لا")}</dl></section>
<section class="panel"><h2>نص الاقتباس والبيانات الوصفية</h2><p class="preserved-text">{_display(row.get("citation_text"), "لا يوجد نص اقتباس محفوظ")}</p></section>
<section class="panel"><h2>روابط النسخ المؤرشفة المعروفة</h2><ul class="source-list">{archived}</ul></section>
<section class="panel"><h2>المصدر والأصل</h2><ul>{provenance}</ul><div class="case-heading"><a class="button" href="/cases/by-id/{_safe(row.get("record_id"))}.html">فتح الحادثة المرتبطة</a>{source_link}</div></section>
</div>"""
        return _page(f"مرجع {row.get('source_reference_id')}", body)

    def _source_page(self, source_id: str, record: dict[str, Any], rows: list[dict[str, Any]], status: str, reachability: str) -> str:
        full = status in FULL_TEXT_STATUSES
        partial = status == SourceContentStatus.PARTIAL_TEXT.value
        text = str(record.get("text_original") or "")
        quality = record.get("content_quality") or {}
        full_validation = record.get("full_text_validation") or {}
        origin = str(quality.get("provenance") or record.get("preservation_status") or "غير محدد")
        captured = record.get("retrieved_at") or next((item.get("retrieved_at") for item in reversed(record.get("attempt_history") or []) if isinstance(item, dict) and item.get("retrieved_at")), None)
        raw_urls = []
        for url in [record.get("original_url"), *(record.get("observed_original_urls") or [])]:
            if url and url not in raw_urls:
                raw_urls.append(str(url))
        references = "".join(
            f'<li class="reference-card"><a class="ltr" href="/references/{_safe(row["source_reference_id"])}/">{_safe(row["source_reference_id"])}</a><small>الحادثة <a class="ltr" href="/cases/by-id/{_safe(row["record_id"])}.html">{_safe(row["record_id"])}</a></small></li>'
            for row in rows
        ) or '<li class="missing">لا توجد علاقة مرجعية مطابقة؛ بقي كيان المصدر محفوظًا للمراجعة.</li>'
        url_list = "".join(
            f'<li class="raw-url">{_external_link(url)}</li>'
            for url in raw_urls
        ) or '<li class="missing">لا يوجد رابط صالح.</li>'
        archived = "".join(
            f'<li class="raw-url">{_external_link(url)}</li>'
            for url in record.get("archived_urls") or []
        ) or '<li class="missing">لا توجد نسخة مؤرشفة معروفة.</li>'
        if full:
            content = f'<section class="panel success"><h2>النص الرئيسي الكامل المحفوظ</h2><p>طريقة الحصول: <b class="ltr">{_safe(origin)}</b> · تاريخ الالتقاط: {_display(captured)} · التحقق: <b class="ltr">{_safe(full_validation.get("reason") or quality.get("validator_version") or "validated_by_content_quality")}</b> · البصمة: <code class="raw-url">{_display(record.get("content_hash"))}</code></p><div class="preserved-text">{_safe(text)}</div></section>'
        elif partial:
            content = f'<section class="panel notice"><h2>نص جزئي محفوظ</h2><p>{_source_statement(status)}</p><div class="preserved-text">{_display(text, "النص الجزئي غير متاح للعرض")}</div></section>'
        else:
            content = f'<section class="panel danger"><h2>لا يوجد نص كامل محفوظ</h2><p>{_source_statement(status)}</p></section>'
        metadata = "".join(
            _field(label, record.get(key), ltr=ltr)
            for key, label, ltr in (
                ("source_id", "معرّف كيان المصدر", True),
                ("page_title", "عنوان الصفحة", False),
                ("publisher", "الناشر/الحساب", False),
                ("author", "الكاتب", False),
                ("publication_date", "تاريخ النشر", False),
                ("source_type", "نوع المصدر", True),
                ("normalized_url", "الرابط المطبّع", True),
            )
        )
        body = f"""
<section class="hero"><div class="wrap"><div class="breadcrumbs"><a href="/">الرئيسية</a><span>/</span><span>كيان مصدر خارجي</span></div><p class="eyebrow">سجل مصدر خارجي</p><h1>{_display(record.get("page_title") or record.get("publisher"), "مصدر بلا عنوان")}</h1><p class="lead">{_source_statement(status)}</p></div></section>
<div class="wrap content-stack">
<section class="panel"><div class="case-heading"><div><h2>حالة المحتوى</h2>{_status_badge(status)}</div><div><h2>قابلية الوصول</h2>{_status_badge(reachability)}</div></div></section>
<section class="panel"><h2>بيانات المصدر</h2><dl class="field-grid">{metadata}</dl></section>
{content}
<section class="panel"><h2>الروابط الأصلية المرصودة</h2><ul class="source-list">{url_list}</ul><h3>روابط أرشيفية معروفة</h3><ul class="source-list">{archived}</ul></section>
<section class="panel"><h2>علاقات الحوادث المحفوظة ({len(rows):,})</h2><ul class="reference-list">{references}</ul></section>
</div>"""
        return _page(f"المصدر {source_id}", body)

    def _incident_page(self, incident: dict[str, Any], rows: list[dict[str, Any]], previous_sequence: int | None, next_sequence: int | None) -> str:
        sequence = int(incident["legacy_sequence"])
        victims = "".join(
            f'<li class="reference-card"><strong>{_display(item.get("name_local") or item.get("name_original"), "اسم غير متاح")}</strong><small>{_display(item.get("status_ar") or item.get("status_original"))} · {_display(item.get("age_group_ar") or item.get("age_group_original"))} · {_display(item.get("gender_ar") or item.get("gender_original"))}</small></li>'
            for item in incident.get("victims") or []
        ) or '<li class="missing">لا توجد أسماء ضحايا محفوظة في هذا السجل.</li>'
        reference_cards = "".join(
            f'<li class="reference-card"><div class="case-heading"><div><a href="/references/{_safe(row["source_reference_id"])}/"><strong>{_display(row.get("title") or row.get("label") or row.get("publisher"), "مرجع بلا عنوان")}</strong></a><small>{_display(row.get("domain"))} · {_status_badge(str(row.get("content_preservation_status")))}</small></div><span class="raw-url ltr">{_external_link(row.get("raw_url"), "الرابط الأصلي")}</span></div></li>'
            for row in rows
        ) or '<li class="missing">لا توجد مراجع مصادر مرتبطة بهذه الحادثة.</li>'
        provenance_items: list[str] = []
        for field_name, entries in sorted((incident.get("field_provenance") or {}).items()):
            origins = []
            for item in entries or []:
                origin = item.get("source_type") or item.get("origin") or "unknown"
                if origin not in origins:
                    origins.append(str(origin))
            provenance_items.append(f'<li><b class="ltr">{_safe(field_name)}</b>: <span class="ltr">{_safe(", ".join(origins) or "unknown")}</span></li>')
        provenance = "".join(provenance_items) or '<li class="missing">لا يوجد سجل منشأ حقلي.</li>'
        conflicts = "".join(f'<li><pre class="preserved-text">{_safe(json.dumps(item, ensure_ascii=False, sort_keys=True))}</pre></li>' for item in incident.get("conflicts") or []) or '<li class="missing">لا توجد تعارضات مسجلة.</li>'
        prev_link = f'<a class="button" href="/cases/{previous_sequence:04d}/">الحادثة السابقة</a>' if previous_sequence else ""
        next_link = f'<a class="button" href="/cases/{next_sequence:04d}/">الحادثة التالية</a>' if next_sequence else ""
        fields = "".join([
            _field("المعرّف الداخلي", incident.get("incident_id"), ltr=True),
            _field("رمز Airwars", incident.get("incident_code"), ltr=True),
            _field("معرّف Airwars الأصلي", incident.get("original_airwars_identifier"), ltr=True),
            _field("تاريخ الحادثة", incident.get("incident_date"), ltr=True),
            _field("آخر تحديث للمصدر", incident.get("source_last_modified"), ltr=True),
            _field("الدولة", incident.get("country_ar") or incident.get("country")),
            _field("الموقع", incident.get("location_ar") or incident.get("location")),
            _field("خط العرض", incident.get("latitude"), ltr=True),
            _field("خط الطول", incident.get("longitude"), ltr=True),
            _field("حالة الإحداثيات", incident.get("coordinate_status"), ltr=True),
            _field("الجهة المنسوبة", incident.get("alleged_belligerent_ar") or incident.get("alleged_belligerent")),
            _field("التصنيف/التقييم", incident.get("assessment_ar") or incident.get("assessment")),
            _field("نوع الضربة", incident.get("strike_type_ar") or incident.get("strike_type")),
            _field("وفيات مدنية: حد أدنى", incident.get("civilian_deaths_min")),
            _field("وفيات مدنية: حد أعلى", incident.get("civilian_deaths_max")),
            _field("إصابات مدنية: حد أدنى", incident.get("civilian_injuries_min")),
            _field("إصابات مدنية: حد أعلى", incident.get("civilian_injuries_max")),
            _field("حالة الجودة", incident.get("data_quality_status"), ltr=True),
        ])
        body = f"""
<section class="hero"><div class="wrap"><div class="breadcrumbs"><a href="/">الرئيسية</a><span>/</span><a href="/cases/">الحوادث</a><span>/</span><span>{sequence:04d}</span></div><p class="eyebrow">حادثة رقم {sequence:,}</p><div class="case-heading"><div><h1>{_display(incident.get("incident_code"), incident.get("incident_id"))}</h1><p class="lead">{_display(incident.get("location_ar") or incident.get("location"))} · {_display(incident.get("incident_date"))}</p></div>{_status_badge(str(incident.get("direct_verification_status")))}</div></div></section>
<div class="wrap content-stack">
<section class="panel notice"><h2>أصل السجل والتحقق الحالي</h2><dl class="field-grid">{_field("أصل النص والسجل", incident.get("record_origin_status"), ltr=True)}{_field("التحقق المباشر الحالي", incident.get("direct_verification_status"), ltr=True)}{_field("أصل الوصف النصي", incident.get("textual_description_origin"), ltr=True)}</dl><p>الحجب الحالي لا يمحو السجل التاريخي، كما أن السجل التاريخي لا يُحسب نجاحًا مباشرًا.</p></section>
<section class="panel"><h2>الحقول البنيوية</h2><dl class="field-grid">{fields}</dl><h3>رابط Airwars الأصلي</h3><p class="raw-url"><a href="{_safe(incident.get("original_airwars_url"))}" rel="noopener noreferrer">{_display(incident.get("original_airwars_url"))}</a></p></section>
<section class="panel"><h2>الوصف النصي المحفوظ</h2><div class="preserved-text">{_display(incident.get("textual_description"), "لا يوجد وصف نصي صالح")}</div><h3>ملاحظات أصلية أو إضافية</h3><div class="preserved-text">{_display(incident.get("additional_notes"), "لا توجد ملاحظات إضافية")}</div></section>
<section class="panel"><h2>الأشخاص أو الضحايا المذكورون ({len(incident.get("victims") or []):,})</h2><ul class="victim-list">{victims}</ul></section>
<section class="panel"><h2>مراجع المصادر ({len(rows):,})</h2><p>كل بطاقة أدناه تمثل علاقة مستقلة، حتى عند تكرر الرابط داخل حادثة أو بين حوادث.</p><ul class="reference-list">{reference_cards}</ul></section>
<section class="panel"><h2>منشأ الحقول المهمة</h2><ul>{provenance}</ul><p>السجل الكامل القابل للتدقيق متاح في <a class="ltr" href="/release-data/incidents/{_safe(incident.get("incident_id"))}.json">JSON</a>.</p></section>
<section class="panel"><h2>التعارضات أو الالتباسات</h2><ul class="source-list">{conflicts}</ul></section>
<nav class="case-heading" aria-label="الحادثة السابقة والتالية">{prev_link}<a class="button" href="/cases/">فهرس الحوادث</a>{next_link}</nav>
</div>"""
        return _page(str(incident.get("incident_code") or incident.get("incident_id")), body, active="cases")

    def _build_references(self, references: list[Any]) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
        rows: list[dict[str, Any]] = []
        by_incident: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
        data_path = self.release_root / "data" / "source-references.jsonl"
        write_files = not self._phase_done("references")
        handle = data_path.open("w", encoding="utf-8") if write_files else None
        try:
            for reference in references:
                row = _jsonable_reference(reference)
                rows.append(row)
                by_incident[row["record_id"]].append(row)
                if row.get("external_source_id"):
                    by_source[str(row["external_source_id"])].append(row)
                if handle:
                    handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                    target = self.release_root / "site" / "references" / row["source_reference_id"] / "index.html"
                    atomic_write_text(target, self._reference_page(row))
        finally:
            if handle:
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
        if write_files:
            self._finish_phase("references", reference_records=len(rows), reference_pages=len(rows))
        return rows, by_incident, by_source

    def _build_sources(
        self,
        connector: AirwarsConnector,
        by_source: dict[str, list[dict[str, Any]]],
    ) -> tuple[dict[str, str], dict[str, str], Counter[str], list[dict[str, Any]]]:
        statuses: dict[str, str] = {}
        reachability: dict[str, str] = {}
        counts: Counter[str] = Counter()
        search_rows: list[dict[str, Any]] = []
        write_files = not self._phase_done("sources")
        for source_id, record in sorted(connector.sources.records.items()):
            status = classify_source_content(record).value
            reachable = source_reachability(record).value
            validated_full, validation_reason = validate_full_source_text(record)
            statuses[source_id] = status
            reachability[source_id] = reachable
            counts[status] += 1
            rows = by_source.get(source_id, [])
            if write_files:
                enriched = {
                    **record,
                    "content_preservation_status": status,
                    "current_reachability_status": reachable,
                    "full_text_validation": {
                        "passed": validated_full,
                        "reason": validation_reason,
                        "policy": "source-content-status-policy-v1",
                    },
                    "source_reference_ids": [row["source_reference_id"] for row in rows],
                    "source_reference_count": len(rows),
                }
                atomic_write_json(self.release_root / "data" / "sources" / f"{source_id}.json", enriched)
                atomic_write_text(
                    self.release_root / "site" / "sources" / source_id / "index.html",
                    self._source_page(source_id, enriched, rows, status, reachable),
                )
            search_rows.append({
                "kind": "source",
                "incident_id": "",
                "code": source_id,
                "date": record.get("publication_date") or "",
                "location": record.get("publisher") or "",
                "snippet": str(record.get("page_title") or record.get("publisher") or record.get("original_url") or "")[:300],
                "path": f"/sources/{source_id}/",
                "source_statuses": [status],
                "coordinate_status": "",
                "search_text": _fold_search(
                    source_id, record.get("page_title"), record.get("publisher"), record.get("author"),
                    record.get("original_url"), record.get("normalized_url"), " ".join(record.get("observed_original_urls") or []),
                    record.get("source_type"), status, reachable,
                ),
            })
        if write_files:
            self._finish_phase("sources", source_records=len(statuses), source_pages=len(statuses), source_status_counts=dict(sorted(counts.items())))
        return statuses, reachability, counts, search_rows

    def _build_incidents(
        self,
        connector: AirwarsConnector,
        by_incident: dict[str, list[dict[str, Any]]],
    ) -> tuple[list[dict[str, Any]], Counter[str], Counter[str], list[dict[str, Any]], list[dict[str, Any]]]:
        summaries: list[dict[str, Any]] = []
        origin_counts: Counter[str] = Counter()
        direct_counts: Counter[str] = Counter()
        search_rows: list[dict[str, Any]] = []
        map_points: list[dict[str, Any]] = []
        write_files = not self._phase_done("incidents")
        total = len(connector._records_by_sequence)
        for incident in connector.iter_structured_incidents():
            sequence = int(incident["legacy_sequence"])
            incident_id = str(incident["incident_id"])
            rows = by_incident.get(incident_id, [])
            incident = {
                **incident,
                "source_reference_ids": [row["source_reference_id"] for row in rows],
                "source_reference_count": len(rows),
            }
            origin_counts[str(incident["record_origin_status"])] += 1
            direct_counts[str(incident["direct_verification_status"])] += 1
            if write_files:
                atomic_write_json(self.release_root / "data" / "incidents" / f"{incident_id}.json", incident)
                atomic_write_text(
                    self.release_root / "site" / "cases" / f"{sequence:04d}" / "index.html",
                    self._incident_page(incident, rows, sequence - 1 if sequence > 1 else None, sequence + 1 if sequence < total else None),
                )
                redirect = f'<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/cases/{sequence:04d}/"><link rel="canonical" href="/cases/{sequence:04d}/"><title>انتقال</title><a href="/cases/{sequence:04d}/">فتح الحادثة</a></html>\n'
                atomic_write_text(self.release_root / "site" / "cases" / "by-id" / f"{incident_id}.html", redirect)
            source_terms = " ".join(
                _fold_search(row.get("domain"), row.get("title"), row.get("publisher"), row.get("raw_url"), row.get("content_preservation_status"))
                for row in rows
            )
            victim_terms = " ".join(
                _fold_search(item.get("name_original"), item.get("name_local"))
                for item in incident.get("victims") or []
            )
            search_rows.append({
                "kind": "incident",
                "incident_id": incident_id,
                "code": incident.get("incident_code") or incident_id,
                "date": incident.get("incident_date") or "",
                "location": incident.get("location_ar") or incident.get("location") or "",
                "snippet": str(incident.get("textual_description") or "")[:300],
                "path": f"/cases/{sequence:04d}/",
                "source_statuses": sorted({str(row["content_preservation_status"]) for row in rows}),
                "coordinate_status": incident.get("coordinate_status"),
                "search_text": _fold_search(
                    incident_id, incident.get("incident_code"), incident.get("original_airwars_identifier"),
                    incident.get("incident_date"), incident.get("location"), incident.get("location_ar"),
                    incident.get("textual_description"), victim_terms, source_terms,
                    incident.get("alleged_belligerent"), incident.get("alleged_belligerent_ar"),
                    incident.get("assessment"), incident.get("assessment_ar"), incident.get("strike_type"), incident.get("strike_type_ar"),
                ),
            })
            if incident.get("coordinate_status") == "drawable":
                map_points.append({
                    "incident_id": incident_id,
                    "code": incident.get("incident_code") or incident_id,
                    "date": incident.get("incident_date") or "",
                    "location": incident.get("location_ar") or incident.get("location") or "",
                    "lat": float(incident["latitude"]),
                    "lon": float(incident["longitude"]),
                    "path": f"/cases/{sequence:04d}/",
                })
            summaries.append({
                "sequence": sequence,
                "incident_id": incident_id,
                "code": incident.get("incident_code") or incident_id,
                "date": incident.get("incident_date") or "",
                "location": incident.get("location_ar") or incident.get("location") or "",
                "coordinate_status": incident.get("coordinate_status"),
                "direct_verification_status": incident.get("direct_verification_status"),
                "record_origin_status": incident.get("record_origin_status"),
                "source_reference_count": len(rows),
                "usable_text": bool(incident.get("textual_description")),
            })
        if write_files:
            self._finish_phase("incidents", incident_records=len(summaries), incident_pages=len(summaries), by_id_pages=len(summaries))
        return summaries, origin_counts, direct_counts, search_rows, map_points

    def _case_indexes(self, summaries: list[dict[str, Any]], page_size: int = 100) -> None:
        pages = max(1, (len(summaries) + page_size - 1) // page_size)
        for page_number in range(1, pages + 1):
            subset = summaries[(page_number - 1) * page_size:page_number * page_size]
            cards = "".join(
                f'<article class="case-card"><a href="/cases/{item["sequence"]:04d}/"><strong class="ltr">{_safe(item["code"])}</strong><span>{_display(item["location"])}</span><small>{_display(item["date"])}</small><small>{item["source_reference_count"]:,} مرجع مصدر</small></a></article>'
                for item in subset
            )
            pagination = "".join(
                f'<span>{number}</span>' if number == page_number else f'<a href="{("/cases/" if number == 1 else f"/cases/pages/{number}.html")}">{number}</a>'
                for number in range(1, pages + 1)
                if number in {1, pages, page_number - 2, page_number - 1, page_number, page_number + 1, page_number + 2}
            )
            body = f'<section class="hero"><div class="wrap"><p class="eyebrow">الفهرس الكامل</p><h1>الحوادث</h1><p class="lead">{len(summaries):,} سجلًا، ولكل سجل صفحة واحدة وهوية مستقرة.</p></div></section><div class="wrap content-stack"><section class="case-grid">{cards}</section><nav class="pagination" aria-label="صفحات الفهرس">{pagination}</nav></div>'
            target = self.release_root / "site" / "cases" / ("index.html" if page_number == 1 else f"pages/{page_number}.html")
            atomic_write_text(target, _page("فهرس الحوادث", body, active="cases"))

    def _status_page(self, title: str, description: str, records: Iterable[tuple[str, str]], *, kind: str) -> str:
        rows = list(records)
        cards = "".join(
            f'<li class="reference-card"><a class="ltr" href="{_safe(path)}">{_safe(identifier)}</a></li>'
            for identifier, path in rows
        ) or '<li class="missing">لا توجد سجلات في هذه الفئة.</li>'
        body = f'<section class="hero"><div class="wrap"><p class="eyebrow">سجلات وراء العداد</p><h1>{_safe(title)}</h1><p class="lead">{_safe(description)}</p></div></section><div class="wrap content-stack"><section class="panel"><h2>{len(rows):,} سجلًا</h2><ul class="reference-list status-records" data-record-kind="{_safe(kind)}">{cards}</ul></section></div>'
        return _page(title, body)

    def _write_status_pages(
        self,
        summaries: list[dict[str, Any]],
        references: list[dict[str, Any]],
        source_statuses: dict[str, str],
        manual_review_ids: list[str],
    ) -> None:
        source_groups: dict[str, list[str]] = defaultdict(list)
        for source_id, status in source_statuses.items():
            source_groups[status].append(source_id)
        for status in sorted(item.value for item in SourceContentStatus):
            source_ids = source_groups.get(status, [])
            atomic_write_text(
                self.release_root / "site" / "status" / "sources" / status / "index.html",
                self._status_page(
                    f"المصادر: {status}",
                    "هذه فئة حفظ محتوى أولية متبادلة الاستبعاد؛ لا تُستنتج من مجرد وجود صفحة محلية.",
                    ((source_id, f"/sources/{source_id}/") for source_id in sorted(source_ids)),
                    kind="source",
                ),
            )
        atomic_write_text(
            self.release_root / "site" / "status" / "sources" / "manual_review" / "index.html",
            self._status_page(
                "المصادر: مراجعة بشرية",
                "هذه سمة ثانوية قد تتقاطع مع الحالة الأولية؛ تعرض كل كيان يحمل علم مراجعة أو حالة أولية مشوهة/تحتاج مراجعة.",
                ((source_id, f"/sources/{source_id}/") for source_id in manual_review_ids),
                kind="source",
            ),
        )
        incident_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        coordinate_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in summaries:
            incident_groups[str(item["direct_verification_status"])].append(item)
            coordinate_groups[str(item["coordinate_status"])].append(item)
        for status in sorted(set(incident_groups) | {"DIRECT_FETCH_SUCCESS", "BLOCKED_HTTP_403", "DIRECT_FETCH_OTHER_FAILURE", "DEAD"}):
            items = incident_groups.get(status, [])
            atomic_write_text(
                self.release_root / "site" / "status" / "incidents" / status / "index.html",
                self._status_page(
                    f"التحقق المباشر: {status}",
                    "حالة محاولة التحقق المباشر الحالية منفصلة عن أصل السجل النصي المحفوظ.",
                    ((item["incident_id"], f'/cases/{item["sequence"]:04d}/') for item in items),
                    kind="incident",
                ),
            )
        for status in ("drawable", "missing", "malformed", "outside_accepted_range"):
            items = coordinate_groups.get(status, [])
            atomic_write_text(
                self.release_root / "site" / "status" / "coordinates" / status / "index.html",
                self._status_page(
                    f"الإحداثيات: {status}",
                    "كل سجل مستبعد من الرسم ظاهر هنا مع السبب الصريح؛ لا تُحذف السجلات من الأرشيف.",
                    ((item["incident_id"], f'/cases/{item["sequence"]:04d}/') for item in items),
                    kind="incident",
                ),
            )
        for key, predicate, description in (
            ("duplicate", lambda row: row.get("duplicate_relationship"), "علاقات مرجعية مكررة محفوظة كما وردت."),
            ("malformed", lambda row: row.get("malformed"), "روابط خام لم تنجح عملية تطبيعها؛ بقيت قيمها الأصلية محفوظة."),
            ("manual-review", lambda row: row.get("manual_review"), "علاقات تتطلب مراجعة بشرية ولا تُخفى من العدّ."),
        ):
            chosen = [row for row in references if predicate(row)]
            atomic_write_text(
                self.release_root / "site" / "status" / "references" / key / "index.html",
                self._status_page(
                    f"مراجع المصادر: {key}", description,
                    ((row["source_reference_id"], f'/references/{row["source_reference_id"]}/') for row in chosen),
                    kind="source_reference",
                ),
            )

    def _write_search(self, search_rows: list[dict[str, Any]], source_statuses: Iterable[str]) -> None:
        atomic_write_json(self.release_root / "site" / "data" / "search-index.json", {
            "schema_version": "1.0.0",
            "generated_at": utc_now(),
            "fields": ["incident identifier", "date", "location", "narrative", "victim", "source domain", "source title", "original URL", "Airwars code", "actor", "allegation", "source preservation status"],
            "documents": search_rows,
        })
        options = "".join(f'<option value="{_safe(status)}">{_safe(status)}</option>' for status in sorted(set(source_statuses)))
        body = f"""<section class="hero"><div class="wrap"><p class="eyebrow">بحث محلي بلا اعتماد خارجي</p><h1>البحث في الهيكل النصي</h1><p class="lead">يشمل الحوادث والمصادر والرموز والتواريخ والأماكن والسرد وأسماء الضحايا والنطاقات والعناوين والروابط والجهات وحالة الحفظ.</p></div></section>
<div class="wrap content-stack"><section class="panel"><form class="search-form" data-textual-search data-index-url="/data/search-index.json" data-root="">
<label>كلمات البحث<input name="q" autocomplete="off" placeholder="رمز، موقع، اسم، عنوان أو رابط"></label>
<label>حالة حفظ المصدر<select name="source_status"><option value="">الكل</option>{options}</select></label>
<label>حالة الإحداثيات<select name="coordinates"><option value="">الكل</option><option value="drawable">قابل للرسم</option><option value="missing">مفقود</option><option value="malformed">مشوه</option><option value="outside_accepted_range">خارج النطاق</option></select></label>
<label>التاريخ<input name="date" type="text" inputmode="numeric" placeholder="YYYY أو YYYY-MM-DD"></label><button type="submit">بحث</button></form><p data-search-count>أدخل معيارًا ثم ابدأ البحث.</p><div class="search-results" data-search-results></div></section></div>"""
        atomic_write_text(
            self.release_root / "site" / "search.html",
            _page("البحث", body, active="search", scripts='<script src="/assets/textual-search.js" defer></script>'),
        )

    def _write_map(self, points: list[dict[str, Any]], coordinate_counts: Counter[str]) -> None:
        atomic_write_text(
            self.release_root / "site" / "assets" / "map-points.js",
            "window.AIRWARS_TEXTUAL_MAP_POINTS=" + json.dumps(points, ensure_ascii=False, separators=(",", ":")) + ";\n",
        )
        excluded = sum(count for status, count in coordinate_counts.items() if status != "drawable")
        markup = f"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>خريطة الحوادث — المدني</title><link rel="stylesheet" href="/assets/textual-map.css"></head><body><main class="map-screen">
<canvas id="map-canvas" aria-label="خريطة تفاعلية تضم كل الحوادث ذات الإحداثيات الصالحة"></canvas>
<header class="map-topbar"><a class="map-brand" href="/"><span class="map-brand-mark">م</span><span class="map-brand-copy"><b>المدني</b><small>العودة إلى الأرشيف</small></span></a><form id="map-search" class="map-search"><input id="map-search-input" placeholder="رمز الحادثة أو الموقع"><button>بحث</button></form></header>
<section class="map-summary"><h1>تغطية الخريطة الكاملة</h1><dl class="map-counts"><div><dt>المعروض</dt><dd>{coordinate_counts['drawable']:,}</dd></div><div><dt>المستبعد</dt><dd>{excluded:,}</dd></div><div><dt>إجمالي السجلات</dt><dd>{sum(coordinate_counts.values()):,}</dd></div></dl><p>بلا إحداثيات: {coordinate_counts['missing']:,} · مشوهة: {coordinate_counts['malformed']:,} · خارج النطاق: {coordinate_counts['outside_accepted_range']:,}. <a href="/status/coordinates/missing/">عرض المستبعدين</a></p><p id="map-status-text">جارٍ رسم النقاط…</p></section>
<section id="map-detail" class="map-detail" hidden></section><aside class="map-legend"><i class="map-dot"></i> كل نقطة تفتح سجل الحادثة</aside><noscript><p class="map-noscript">تحتاج الخريطة إلى JavaScript المحلي فقط.</p></noscript></main><script src="/assets/map-points.js"></script><script src="/assets/textual-map.js"></script></body></html>"""
        atomic_write_text(self.release_root / "site" / "map.html", markup)

    def _write_home(
        self,
        summaries: list[dict[str, Any]],
        references: list[dict[str, Any]],
        source_counts: Counter[str],
        direct_counts: Counter[str],
        coordinate_counts: Counter[str],
        manual_review_count: int,
    ) -> None:
        full_count = sum(source_counts[status] for status in FULL_TEXT_STATUSES)
        raw_urls = {row["raw_url"] for row in references if row.get("raw_url")}
        normalized_urls = {row["normalized_url"] for row in references if row.get("normalized_url") and row.get("normalization_status") != "malformed"}
        duplicates = sum(bool(row.get("duplicate_relationship")) for row in references)
        malformed_refs = sum(bool(row.get("malformed")) for row in references)
        blocked = direct_counts["BLOCKED_HTTP_403"]
        success = direct_counts["DIRECT_FETCH_SUCCESS"]
        body = f"""
<section class="hero"><div class="wrap hero-grid"><div><p class="eyebrow">الإصدار البنيوي النصي المستقل</p><h1>المدني</h1><p class="lead">واجهة عربية مستقلة تقرّب سجل الحوادث والمراجع والضحايا والنصوص المحفوظة عن سوريا، مع إبقاء الروابط والقيم الخام والأصل والتحقق الحالي منفصلة بوضوح.</p></div><aside class="hero-card"><span>التغطية البنيوية</span><strong>{len(summaries):,} / {len(summaries):,}</strong><p>كل حادثة لها سجل وصفحة؛ وهذا لا يعني نجاح الاتصال المباشر الحالي ولا حفظ النص الكامل لكل مصدر خارجي.</p></aside></div></section>
<div class="wrap content-stack">
<section class="panel"><h2>تغطية الحوادث</h2><div class="stats-grid"><a class="stat" href="/cases/"><strong>{len(summaries):,}</strong><span>سجلات وصفحات حوادث</span></a><a class="stat" href="/status/incidents/BLOCKED_HTTP_403/"><strong>{blocked:,}</strong><span>تحقق مباشر محجوب HTTP 403</span></a><a class="stat" href="/status/incidents/DIRECT_FETCH_SUCCESS/"><strong>{success:,}</strong><span>نجاح مباشر حالي</span></a><a class="stat" href="/reports/"><strong>{sum(item['usable_text'] for item in summaries):,}</strong><span>سجلات ذات وصف بنيوي صالح</span></a></div><p>السجل التاريخي أو المحلي يثبت وجود الهيكل النصي؛ لا يحوّل رد HTTP 403 إلى نجاح مباشر.</p></section>
<section class="panel"><h2>تغطية مراجع المصادر</h2><div class="stats-grid"><a class="stat" href="/exports/"><strong>{len(references):,}</strong><span>علاقات مراجع مصادر مفهرسة وصفحاتها</span></a><a class="stat" href="/exports/all-raw-urls.txt"><strong>{len(raw_urls):,}</strong><span>روابط خام فريدة</span></a><a class="stat" href="/exports/all-normalized-urls.txt"><strong>{len(normalized_urls):,}</strong><span>روابط مطبعة فريدة</span></a><a class="stat" href="/status/references/duplicate/"><strong>{duplicates:,}</strong><span>علاقات مكررة محفوظة</span></a></div><p>روابط مشوهة محفوظة: <a href="/status/references/malformed/">{malformed_refs:,}</a>. لا تختفي العلاقة عند استخدام الرابط نفسه في أكثر من حادثة.</p></section>
<section class="panel"><h2>حفظ محتوى المصادر الخارجية</h2><div class="stats-grid"><a class="stat" href="/search.html"><strong>{sum(source_counts.values()):,}</strong><span>كيانات مصادر وصفحاتها المحلية</span></a><a class="stat" href="/search.html"><strong>{full_count:,}</strong><span>نص كامل مثبت ومحفوظ</span></a><a class="stat" href="/status/sources/PARTIAL_TEXT/"><strong>{source_counts['PARTIAL_TEXT']:,}</strong><span>نص جزئي</span></a><a class="stat" href="/status/sources/METADATA_ONLY/"><strong>{source_counts['METADATA_ONLY']:,}</strong><span>بيانات وصفية فقط</span></a><a class="stat" href="/status/sources/URL_PRESERVED/"><strong>{source_counts['URL_PRESERVED'] + source_counts['REFERENCE_ONLY']:,}</strong><span>رابط/مرجع فقط</span></a><a class="stat" href="/status/sources/FULL_TEXT_DIRECT/"><strong>{source_counts['FULL_TEXT_DIRECT']:,}</strong><span>نص كامل مباشر</span></a><a class="stat" href="/status/sources/FULL_TEXT_ARCHIVED/"><strong>{source_counts['FULL_TEXT_ARCHIVED']:,}</strong><span>نص كامل من نسخة مؤرشفة</span></a><a class="stat" href="/status/sources/FULL_TEXT_LOCAL_SNAPSHOT/"><strong>{source_counts['FULL_TEXT_LOCAL_SNAPSHOT']:,}</strong><span>نص كامل من لقطة محلية</span></a><a class="stat" href="/status/sources/BLOCKED/"><strong>{source_counts['BLOCKED']:,}</strong><span>مصادر محجوبة</span></a><a class="stat" href="/status/sources/DEAD/"><strong>{source_counts['DEAD']:,}</strong><span>روابط ميتة</span></a><a class="stat" href="/status/sources/MALFORMED/"><strong>{source_counts['MALFORMED']:,}</strong><span>مصادر بروابط مشوهة</span></a><a class="stat" href="/status/sources/manual_review/"><strong>{manual_review_count:,}</strong><span>تتطلب مراجعة بشرية</span></a></div><p class="notice panel">صفحة مرجع المصدر المحلية تحفظ العلاقة والرابط والبيانات المتاحة. لا نسميها أرشيفًا كاملًا إلا إذا كان المتن الكامل موجودًا محليًا واجتاز سياسة التحقق.</p></section>
<section class="panel"><h2>الخريطة</h2><div class="stats-grid"><a class="stat" href="/map.html"><strong>{coordinate_counts['drawable']:,}</strong><span>نقطة معروضة</span></a><a class="stat" href="/status/coordinates/missing/"><strong>{coordinate_counts['missing']:,}</strong><span>بلا إحداثيات</span></a><a class="stat" href="/status/coordinates/malformed/"><strong>{coordinate_counts['malformed']:,}</strong><span>إحداثيات مشوهة</span></a><a class="stat" href="/status/coordinates/outside_accepted_range/"><strong>{coordinate_counts['outside_accepted_range']:,}</strong><span>خارج النطاق المقبول</span></a></div></section>
<section class="panel notice"><h2>لماذا ظهر الرقمان 45,075 و45,081؟</h2><p>العدد 45,075 هو فهرس عمل التشغيل الكامل ذي النطاق <span class="ltr">0001-8114</span>. بقيت ستة كيانات مصادر صالحة جُمعت سابقًا من نسخة عامة مؤرشفة للحادثة <span class="ltr">airwars-32716 / CS1033</span> ضمن النطاق <span class="ltr">3000-5999</span>، ثم أسقطها فهرس العمل اللاحق لأن قائمة مصادر الحادثة المطبعة كانت فارغة. العدد القانوني لملفات كيانات المصادر هو 45,081؛ ليست الست صفحات فهارس أو أخطاء عدّ أو معرّفات مكررة. <a href="/reports/airwars-ground-truth.html">الدليل الكامل</a>.</p></section>
</div>"""
        atomic_write_text(self.release_root / "site" / "index.html", _page("الرئيسية", body, active="home"))

    def _write_report_indexes(self) -> None:
        reports_body = """<section class="hero"><div class="wrap"><p class="eyebrow">أدلة قابلة للتدقيق</p><h1>التقارير</h1><p class="lead">النتائج مشتقة من البيانات القانونية، وليست من عدّ ملفات HTML فقط.</p></div></section><div class="wrap content-stack"><section class="panel"><ul class="reference-list"><li><a href="/reports/airwars-ground-truth.html">تقرير الحقيقة الأرضية</a></li><li><a href="/reports/airwars-ground-truth.json">بيانات التقرير JSON</a></li><li><a href="/reports/validation.json">التحقق النهائي للإصدار</a></li><li><a href="/reports/acceptance-matrix.json">مصفوفة القبول</a></li><li><a href="/exports/">صادرات الروابط والعلاقات</a></li></ul></section></div>"""
        exports_body = """<section class="hero"><div class="wrap"><p class="eyebrow">أدلة الروابط</p><h1>الصادرات الكاملة</h1><p class="lead">كل صف يحتفظ بعلاقة الحادثة وبالرابط الخام والمطبع وحالة الحفظ.</p></div></section><div class="wrap content-stack"><section class="panel"><ul class="reference-list"><li><a href="/exports/all-source-references.csv">all-source-references.csv</a></li><li><a href="/exports/all-source-references.jsonl">all-source-references.jsonl</a></li><li><a href="/exports/all-raw-urls.txt">all-raw-urls.txt</a></li><li><a href="/exports/all-normalized-urls.txt">all-normalized-urls.txt</a></li><li><a href="/release-data/link-evidence.jsonl">all declared and embedded link evidence</a></li></ul></section></div>"""
        atomic_write_text(self.release_root / "site" / "reports" / "index.html", _page("التقارير", reports_body, active="reports"))
        atomic_write_text(self.release_root / "site" / "exports" / "index.html", _page("الصادرات", exports_body))

    @staticmethod
    def _walk_links(value: Any, path: str = "$") -> Iterable[tuple[str, str]]:
        if isinstance(value, dict):
            for key, item in value.items():
                yield from AirwarsTextualReleaseBuilder._walk_links(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                yield from AirwarsTextualReleaseBuilder._walk_links(item, f"{path}[{index}]")
        elif isinstance(value, str):
            for match in re.finditer(r"https?://[^\s<>\"']+", value):
                yield path, match.group(0).rstrip(".,);]}")

    def _write_link_evidence(self, connector: AirwarsConnector, references: list[dict[str, Any]]) -> int:
        target = self.release_root / "data" / "link-evidence.jsonl"
        count = 0
        with target.open("w", encoding="utf-8") as handle:
            for row in references:
                for relation, url in (("raw_url", row.get("raw_url")), ("normalized_url", row.get("normalized_url"))):
                    if url:
                        handle.write(json.dumps({"owner_type": "source_reference", "owner_id": row["source_reference_id"], "field_path": relation, "url": url}, ensure_ascii=False, sort_keys=True) + "\n")
                        count += 1
                for index, url in enumerate(row.get("archived_urls") or []):
                    handle.write(json.dumps({"owner_type": "source_reference", "owner_id": row["source_reference_id"], "field_path": f"archived_urls[{index}]", "url": url}, ensure_ascii=False, sort_keys=True) + "\n")
                    count += 1
            for source_id, record in sorted(connector.sources.records.items()):
                for field_path, url in self._walk_links(record):
                    handle.write(json.dumps({"owner_type": "external_source", "owner_id": source_id, "field_path": field_path, "url": url}, ensure_ascii=False, sort_keys=True) + "\n")
                    count += 1
            for sequence, record in sorted(connector._records_by_sequence.items()):
                for field_path, url in self._walk_links(record):
                    handle.write(json.dumps({"owner_type": "incident", "owner_id": record["internal_id"], "sequence": sequence, "field_path": field_path, "url": url}, ensure_ascii=False, sort_keys=True) + "\n")
                    count += 1
            handle.flush()
            os.fsync(handle.fileno())
        return count

    def build(self) -> dict[str, Any]:
        connector = AirwarsConnector(self.project_root, self.legacy_zip)
        references = connector.source_references()
        if not self._phase_done("assets"):
            self._copy_assets()
        if not self._phase_done("worklists"):
            self._write_worklists(connector, references)

        reference_rows, by_incident, by_source = self._build_references(references)
        source_statuses, source_reachability, source_counts, source_search = self._build_sources(connector, by_source)
        summaries, origin_counts, direct_counts, incident_search, map_points = self._build_incidents(connector, by_incident)
        coordinate_counts = Counter(str(item["coordinate_status"]) for item in summaries)
        manual_review_ids = sorted(
            source_id
            for source_id, record in connector.sources.records.items()
            if record.get("review_flags")
            or source_statuses[source_id] in {
                SourceContentStatus.MALFORMED.value,
                SourceContentStatus.NEEDS_MANUAL_REVIEW.value,
            }
        )

        if not self._phase_done("link_evidence"):
            evidence_count = self._write_link_evidence(connector, reference_rows)
            self._finish_phase("link_evidence", link_evidence_rows=evidence_count)
        else:
            evidence_count = int(self.state.get("link_evidence_rows") or 0)

        if not self._phase_done("site_indexes"):
            self._case_indexes(summaries)
            self._write_status_pages(summaries, reference_rows, source_statuses, manual_review_ids)
            self._write_search(incident_search + source_search, source_statuses.values())
            self._write_map(map_points, coordinate_counts)
            self._write_home(summaries, reference_rows, source_counts, direct_counts, coordinate_counts, len(manual_review_ids))
            self._write_report_indexes()
            status_index = {
                "generated_at": utc_now(),
                "sources": {
                    status: sorted(source_id for source_id, value in source_statuses.items() if value == status)
                    for status in sorted(set(source_statuses.values()))
                },
                "incidents": {
                    status: sorted(item["incident_id"] for item in summaries if item["direct_verification_status"] == status)
                    for status in sorted(set(str(item["direct_verification_status"]) for item in summaries))
                },
                "coordinates": {
                    status: sorted(item["incident_id"] for item in summaries if item["coordinate_status"] == status)
                    for status in sorted(set(str(item["coordinate_status"]) for item in summaries))
                },
            }
            status_index["sources"]["manual_review"] = manual_review_ids
            status_index["incidents"]["historical_or_local_origin"] = sorted(
                item["incident_id"] for item in summaries if "historical" in str(item["record_origin_status"])
            )
            status_index["incidents"]["without_usable_text"] = sorted(
                item["incident_id"] for item in summaries if not item["usable_text"]
            )
            atomic_write_json(self.release_root / "data" / "status-index.json", status_index)
            self._finish_phase(
                "site_indexes",
                search_documents=len(incident_search) + len(source_search),
                map_points=len(map_points),
                coordinate_counts=dict(sorted(coordinate_counts.items())),
            )

        reference_counts = Counter(str(row["content_preservation_status"]) for row in reference_rows)
        unique_raw_urls = {str(row["raw_url"]) for row in reference_rows if row.get("raw_url")}
        unique_normalized_urls = {
            str(row["normalized_url"])
            for row in reference_rows
            if row.get("normalized_url") and row.get("normalization_status") != "malformed"
        }
        result = {
            "schema_version": "1.0.0",
            "release_id": self.release_id,
            "release_semantic_identity": "airwars-syria-v0-structured-text",
            "built_at": utc_now(),
            "parent_release_id": self.parent_release_id,
            "inputs": {
                "project_data": str(self.project_root / "data"),
                "historical_package": str(self.legacy_zip),
                "historical_package_sha256": "9f35a90d92f9fb0334cb61ffd8434aac03fd0ee61622908a11923dac40da35f3",
            },
            "counts": {
                "incident_records": len(summaries),
                "incident_pages": len(summaries),
                "source_reference_records": len(reference_rows),
                "source_reference_pages": len(reference_rows),
                "source_entities": len(source_statuses),
                "source_entity_pages": len(source_statuses),
                "unique_raw_source_urls": len(unique_raw_urls),
                "unique_normalized_source_urls": len(unique_normalized_urls),
                "duplicate_source_relationships": sum(bool(row.get("duplicate_relationship")) for row in reference_rows),
                "malformed_source_relationships": sum(bool(row.get("malformed")) for row in reference_rows),
                "link_evidence_rows": evidence_count,
                "search_documents": len(incident_search) + len(source_search),
                "map_points": len(map_points),
                "sources_requires_manual_review": len(manual_review_ids),
            },
            "source_content_status_counts": dict(sorted(source_counts.items())),
            "source_reference_status_counts": dict(sorted(reference_counts.items())),
            "source_reachability_counts": dict(sorted(Counter(source_reachability.values()).items())),
            "incident_origin_counts": dict(sorted(origin_counts.items())),
            "incident_direct_verification_counts": dict(sorted(direct_counts.items())),
            "coordinate_counts": dict(sorted(coordinate_counts.items())),
            "reconciliation": {
                "source_entities": {"left": len(source_statuses), "right": sum(source_counts.values()), "balanced": len(source_statuses) == sum(source_counts.values())},
                "incident_coordinates": {"left": len(summaries), "right": sum(coordinate_counts.values()), "balanced": len(summaries) == sum(coordinate_counts.values())},
                "incident_direct_verification": {"left": len(summaries), "right": sum(direct_counts.values()), "balanced": len(summaries) == sum(direct_counts.values())},
            },
            "generated_storage": {
                "release_root": str(self.release_root),
                "git_policy": "generated release content is excluded from Git and retained in immutable VPS release storage; release.json and checksums enumerate it",
            },
        }
        atomic_write_json(self.release_root / "data" / "build-manifest.json", result)
        atomic_write_text(self.release_root / "logs" / "build.log", json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        self._finish_phase("build_complete", build_counts=result["counts"])
        return result
