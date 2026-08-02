from __future__ import annotations

import json
import math
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from .io_utils import atomic_write_json, atomic_write_text, load_json, utc_now
from .site_builder import STATUS_AR, _e, _fmt, _patch_map


PAGE_SIZE = 100


def _navigation(prefix: str, current: str) -> str:
    links = [
        ("index", f"{prefix}index.html", "الحوادث"),
        ("map", f"{prefix}map.html", "الخريطة"),
        ("methodology", f"{prefix}methodology.html", "المنهجية"),
        ("report", f"{prefix}data/reports/collection-summary.json", "تقرير الجمع"),
    ]
    return '<nav class="site-nav" aria-label="التنقل الرئيسي">' + "".join(
        f'<a href="{href}"{' aria-current="page"' if key == current else ""}>{label}</a>'
        for key, href, label in links
    ) + "</nav>"


def _header(prefix: str, current: str) -> str:
    return f'''<a class="skip-link" href="#main-content">انتقل إلى المحتوى</a>
<header class="site-header"><div class="wrap header-inner">
  <a class="brand" href="{prefix}index.html"><span class="brand-mark" aria-hidden="true">س</span><span class="brand-copy"><strong>الأرشيف السوري</strong><small>واجهة البيانات الموحّدة</small></span></a>
  {_navigation(prefix, current)}
</div></header>'''


def _footer(prefix: str) -> str:
    return f'''<footer class="site-footer"><div class="wrap"><p>واجهة عربية مستقلة تعرض السجلات الموحّدة وبيانات منشئها. ليست تابعة لـAirwars. <a href="{prefix}methodology.html">المنهجية وحالة الجمع</a>.</p></div></footer>
<script src="{prefix}assets/js/site.js" defer></script>'''


def _document(title: str, prefix: str, current: str, body: str, description: str = "الأرشيف السوري للحوادث الموثقة") -> str:
    return f'''<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="description" content="{_e(description)}"><meta name="referrer" content="strict-origin-when-cross-origin"><title>{_e(title)}</title><link rel="stylesheet" href="{prefix}assets/css/style.css"></head><body>
{_header(prefix, current)}<main id="main-content">{body}</main>{_footer(prefix)}</body></html>'''


def _load_records(project_root: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted((project_root / "data" / "incidents").glob("*.json")):
        record = load_json(path, {})
        if record:
            records.append(record)
    records.sort(key=lambda item: int(item.get("legacy_sequence") or 0))
    return records


def _summary_record(record: dict[str, Any]) -> dict[str, Any]:
    sequence = int(record.get("legacy_sequence") or 0)
    return {
        "sequence": sequence,
        "number": f"{sequence:04d}",
        "internal_id": record.get("internal_id"),
        "airwars_id": record.get("airwars_id"),
        "code": record.get("incident_code") or "",
        "date": record.get("incident_date") or "",
        "location_ar": record.get("location_ar") or "",
        "location_original": record.get("location") or "",
        "region_ar": record.get("region_ar") or "",
        "district_ar": record.get("district_ar") or "",
        "governorate_ar": record.get("governorate_ar") or "",
        "military_ar": record.get("alleged_belligerent_ar") or "",
        "military_original": record.get("alleged_belligerent") or "",
        "strike_type_ar": record.get("strike_type_ar") or "",
        "strike_type_original": record.get("strike_type") or "",
        "latitude": record.get("latitude"),
        "longitude": record.get("longitude"),
        "completion": record.get("completeness_status") or "partial",
        "path": f"cases/{sequence:04d}/",
    }


def _search_panel(prefix: str) -> str:
    return f'''<section class="panel archive-search" aria-labelledby="search-title"><h2 id="search-title">ابحث في كل السجلات</h2><form class="search-form" data-archive-search data-summary-url="{prefix}data/cases-summary.json" data-case-root="{prefix}">
      <label>كلمة أو رمز<input name="q" type="search" placeholder="مثال: Aleppo أو RS0798"></label>
      <label>حالة السجل<select name="status"><option value="">الكل</option><option value="complete">مكتمل</option><option value="partial">جزئي</option><option value="blocked">محجوب</option><option value="pending_review">مراجعة</option></select></label>
      <label>الإحداثيات<select name="coordinates"><option value="">الكل</option><option value="yes">لها إحداثيات</option><option value="no">بلا إحداثيات</option></select></label>
      <button type="submit">بحث</button></form><div class="search-results" data-search-results hidden></div></section>'''


def _case_card(record: dict[str, Any], prefix: str) -> str:
    sequence = int(record.get("legacy_sequence") or 0)
    status = record.get("completeness_status") or "partial"
    location = record.get("location_ar") or record.get("location") or "الموقع غير متاح"
    return f'''<article class="case-card"><a href="{prefix}cases/{sequence:04d}/"><span class="case-number">{sequence:04d}</span><h3 class="ltr">{_e(record.get("incident_code") or record.get("internal_id"))}</h3><p>{_e(location)}</p><small>{_e(record.get("incident_date") or "التاريخ غير متاح")} · {STATUS_AR.get(status, _e(status))}</small></a></article>'''


def _pagination(current: int, pages: int, prefix: str) -> str:
    links = []
    for number in range(1, pages + 1):
        if number == current:
            links.append(f'<span aria-current="page">{number}</span>')
        elif number == 1:
            links.append(f'<a href="{prefix}index.html">{number}</a>')
        else:
            links.append(f'<a href="{prefix}pages/page-{number:03d}.html">{number}</a>')
    return '<nav class="pagination" aria-label="صفحات الحوادث">' + "".join(links) + "</nav>"


def _index_html(records: list[dict[str, Any]], collection: dict[str, Any], map_report: dict[str, Any]) -> str:
    direct = collection.get("direct_collection") or {}
    counts = map_report.get("counts") or {}
    statuses = direct.get("status_counts") or {}
    direct_verified = sum(bool(item.get("page_extraction") or item.get("api_extraction")) for item in records)
    pages = math.ceil(len(records) / PAGE_SIZE)
    cards = "".join(_case_card(record, "") for record in records[:PAGE_SIZE])
    body = f'''<section class="hero"><div class="wrap hero-grid"><div><p class="eyebrow">سجل مدني مستقل</p><h1>الحوادث والمصادر والضحايا الموثقة عن سوريا</h1><p class="lead">واجهة عربية مستقلة مبنية من ملفات الجمع الموحّدة. تُعرض حالة المصدر بوضوح ولا يُقدَّم نجاح إنشاء الملف بوصفه تحققًا مباشرًا.</p></div><aside class="hero-note"><strong>{_fmt(len(records))}</strong><p>سجل موحّد متاح للتصفح، مع روابط المصدر الأصلي وبيانات المنشأ.</p><a class="button-link" href="map.html">فتح الخريطة الكاملة</a></aside></div></section>
<div class="wrap content-stack"><section class="stats-strip" aria-label="ملخص الأرشيف"><div class="stat"><strong>{_fmt(len(records))}</strong><span>إجمالي الملفات الموحّدة</span></div><div class="stat"><strong>{_fmt(direct_verified)}</strong><span>تحقق بمصدر حي أو مؤرشف</span></div><div class="stat"><strong>{_fmt(statuses.get("blocked", 0))}</strong><span>محجوب وقت الجمع</span></div><div class="stat"><strong>{_fmt(counts.get("incidents_included_on_map", 0))}</strong><span>نقطة على الخريطة</span></div></section>
<section class="panel notice"><h2>حالة الجمع الحديث</h2><p>كل سجل هنا ناتج من مسار البيانات الموحّد. حالة <strong>محجوب</strong> تعني أن المحرك حاول المصدر الحديث ولم يستطع الوصول إليه؛ راجع صفحة الحالة والمنشأ قبل الاستشهاد.</p></section>
{_search_panel("")}
<section><div class="section-heading"><div><p class="eyebrow">التصفح المتسلسل</p><h2>الحوادث 0001–{min(PAGE_SIZE, len(records)):04d}</h2></div><a href="data/reports/collection-summary.json">بيانات الجمع JSON</a></div><div class="case-grid">{cards}</div>{_pagination(1, pages, "")}</section></div>'''
    html = _document("الأرشيف السوري | الحوادث", "", "index", body)
    return html.replace("</body>", '<script src="assets/js/archive-search.js" defer></script></body>')


def _listing_html(records: list[dict[str, Any]], page: int, pages: int) -> str:
    first = (page - 1) * PAGE_SIZE + 1
    last = first + len(records) - 1
    cards = "".join(_case_card(record, "../") for record in records)
    body = f'''<div class="wrap content-stack">{_search_panel("../")}<section><div class="section-heading"><div><p class="eyebrow">التصفح المتسلسل</p><h1>الحوادث {first:04d}–{last:04d}</h1></div></div><div class="case-grid">{cards}</div>{_pagination(page, pages, "../")}</section></div>'''
    html = _document(f"الحوادث {first:04d}–{last:04d} | الأرشيف السوري", "../", "index", body)
    return html.replace("</body>", '<script src="../assets/js/archive-search.js" defer></script></body>')


def _value(value: Any) -> str:
    return _e(value) if value not in (None, "", [], {}) else '<span class="missing-value">غير متاح</span>'


def _case_html(record: dict[str, Any]) -> str:
    sequence = int(record.get("legacy_sequence") or 0)
    status = str(record.get("completeness_status") or "partial")
    fields = [
        ("التسلسل", f"{sequence:04d}"), ("المعرّف الداخلي", record.get("internal_id")),
        ("رمز Airwars", record.get("incident_code")), ("التاريخ", record.get("incident_date")),
        ("الموقع", record.get("location")), ("الموقع بالعربية", record.get("location_ar")),
        ("الإحداثيات", f"{record.get('latitude') or '—'}, {record.get('longitude') or '—'}"),
        ("الجهة المنسوبة", record.get("alleged_belligerent")), ("نوع الضربة", record.get("strike_type")),
        ("التقييم", record.get("assessment")),
        ("الوفيات المدنية", f"{record.get('civilian_deaths_min') or 0}–{record.get('civilian_deaths_max') or record.get('civilian_deaths_min') or 0}"),
        ("الإصابات المدنية", f"{record.get('civilian_injuries_min') or 0}–{record.get('civilian_injuries_max') or record.get('civilian_injuries_min') or 0}"),
    ]
    field_html = "".join(f'<div class="field"><dt>{_e(label)}</dt><dd>{_value(value)}</dd></div>' for label, value in fields)
    victims = record.get("victims") or []
    victim_html = "".join(f'<li class="source-card"><strong>{_e(item.get("name_local") or item.get("name_original") or "اسم غير متاح")}</strong><p>{_e(item.get("additional_information") or "")}</p></li>' for item in victims) or '<li class="missing-value">لا توجد أسماء ضحايا محفوظة في السجل الحالي.</li>'
    sources = record.get("sources") or []
    source_cards = []
    for item in sources:
        original_link = f'<a href="{_e(item.get("url"))}" target="_blank">الرابط الأصلي</a>' if item.get("url") else ""
        archive_link = f'<a href="{_e(item.get("archive_url"))}" target="_blank">الرابط المؤرشف</a>' if item.get("archive_url") else ""
        source_cards.append(
            f'<li class="source-card"><strong>{_e(item.get("publisher") or item.get("name") or "مصدر")}</strong>'
            f'<p>{_e(item.get("content") or item.get("description") or "لا يوجد وصف محفوظ")}</p>{original_link} {archive_link}</li>'
        )
    source_html = "".join(source_cards) or '<li class="missing-value">لا توجد مصادر مستخرجة في السجل الحالي.</li>'
    missing = list(record.get("missing_fields") or []) + list(record.get("missing_sections") or []) + list(record.get("review_flags") or [])
    missing_html = "".join(f"<li>{_e(item)}</li>" for item in missing) or "<li>لا توجد عناصر نقص مسجلة.</li>"
    narrative = record.get("narrative_original") or record.get("narrative") or ""
    direct = bool(record.get("page_extraction") or record.get("api_extraction"))
    source_label = "تحقق حي/مؤرشف محفوظ" if direct else "لم ينجح التحقق المباشر بعد"
    body = f'''<div class="wrap case-layout"><div class="case-heading"><div><p class="eyebrow">الحالة {sequence:04d}</p><h1 class="ltr">{_e(record.get("incident_code") or record.get("internal_id"))}</h1><p>{_e(record.get("location_ar") or record.get("location") or "الموقع غير متاح")}</p></div><span class="status-badge">{STATUS_AR.get(status, _e(status))}</span></div>
<section class="panel notice"><h2>منشأ السجل</h2><p><strong>{source_label}</strong> · آخر محاولة: <span class="ltr">{_e(record.get("retrieved_at") or (record.get("last_collection_attempt") or {}).get("attempted_at") or "غير متاح")}</span>.</p><p><a href="{_e(record.get("canonical_url"))}" target="_blank">صفحة Airwars الأصلية</a> · <a href="../../data/incidents/{_e(record.get("internal_id"))}.json">JSON الموحّد</a></p></section>
<section class="panel"><h2>تفاصيل الحادثة</h2><dl class="field-grid">{field_html}</dl></section>
<section class="panel"><h2>السرد المحفوظ</h2><div class="preserved-text" dir="auto">{_value(narrative)}</div></section>
<section class="panel"><h2>الضحايا ({len(victims)})</h2><ul class="victim-list">{victim_html}</ul></section>
<section class="panel"><h2>المصادر ({len(sources)})</h2><ul class="source-list">{source_html}</ul></section>
<section class="panel"><h2>النواقص والمراجعة</h2><ul>{missing_html}</ul></section></div>'''
    return _document(f"{record.get('incident_code') or record.get('internal_id')} | الأرشيف السوري", "../../", "index", body)


def _source_html(source: dict[str, Any]) -> str:
    source_id = str(source.get("source_id") or "source")
    incidents = "".join(f'<li><a href="../../cases/{int(sequence):04d}/">الحالة {int(sequence):04d}</a></li>' for sequence in source.get("incident_sequences") or []) or "<li>لا توجد علاقة حادثة محفوظة.</li>"
    archives = "".join(f'<li><a href="{_e(url)}" target="_blank">{_e(url)}</a></li>' for url in source.get("archived_urls") or []) or "<li>لا يوجد رابط مؤرشف.</li>"
    body = f'''<div class="wrap source-page"><p class="eyebrow">سجل مصدر موحّد</p><h1>{_e(source.get("publisher_ar") or source.get("publisher") or source_id)}</h1><section class="panel"><h2>بيانات المصدر</h2><dl class="field-grid"><div class="field"><dt>المعرّف</dt><dd>{_e(source_id)}</dd></div><div class="field"><dt>النوع</dt><dd>{_e(source.get("source_type"))}</dd></div><div class="field"><dt>حالة الاسترجاع</dt><dd>{_e(source.get("retrieval_status"))}</dd></div></dl><p><a href="{_e(source.get("original_url"))}" target="_blank">الرابط الأصلي</a></p></section><section class="panel"><h2>النص المحفوظ</h2><div class="preserved-text" dir="auto">{_value(source.get("text_original"))}</div></section><section class="panel"><h2>روابط مؤرشفة</h2><ul>{archives}</ul></section><section class="panel"><h2>الحوادث المرتبطة</h2><ul>{incidents}</ul></section></div>'''
    return _document(f"{source.get('publisher') or source_id} | مصدر", "../../", "index", body)


def _methodology_html(records: list[dict[str, Any]], collection: dict[str, Any], map_report: dict[str, Any]) -> str:
    statuses = Counter(str(item.get("completeness_status") or "partial") for item in records)
    direct = sum(bool(item.get("page_extraction") or item.get("api_extraction")) for item in records)
    map_counts = map_report.get("counts") or {}
    body = f'''<section class="hero"><div class="wrap hero-grid"><div><p class="eyebrow">منهجية قابلة للتدقيق</p><h1>ما الذي يعنيه وجود السجل؟</h1><p class="lead">وجود ملف موحّد يعني أن للحادثة هوية ومسارًا في المحرك. لا يعني ذلك وحده أن المصدر الحديث استجاب أو أن كل الحقول تحققت.</p></div><aside class="hero-note"><strong>{_fmt(len(records))}</strong><p>ملف موحّد؛ تحقق المصدر مباشرة في {_fmt(direct)} منها حتى آخر بناء.</p></aside></div></section><div class="wrap content-stack"><section class="stats-strip"><div class="stat"><strong>{_fmt(statuses.get("complete", 0))}</strong><span>مكتمل</span></div><div class="stat"><strong>{_fmt(statuses.get("partial", 0))}</strong><span>جزئي</span></div><div class="stat"><strong>{_fmt(statuses.get("blocked", 0))}</strong><span>محجوب</span></div><div class="stat"><strong>{_fmt(map_counts.get("incidents_included_on_map", 0))}</strong><span>على الخريطة</span></div></section><section class="panel"><h2>سلسلة الجمع</h2><ol><li>يقرأ المحرك هوية الحادثة والرابط القانوني.</li><li>يحاول نقطة البيانات العامة وصفحة Airwars الحية.</li><li>عند نقص المحتوى يحاول نسخة مؤرشفة مسجلة.</li><li>يحفظ حالة الطلب والمنشأ والنواقص دون تحويل HTTP ناجح إلى «سجل مكتمل» تلقائيًا.</li></ol></section><section class="panel notice"><h2>الحجب الحالي</h2><p>قد يعيد المصدر HTTP 403 أو تتعذر خدمة الأرشفة من عنوان الخادم. تُعرض هذه النتيجة كـ«محجوب»، وتبقى قابلة لإعادة المحاولة من لوحة التحكم.</p></section><section class="panel"><h2>الخريطة</h2><p>تُرسم كل النقاط ذات زوج إحداثيات صالح داخل نطاق التحقق الواسع. لا تُخترع إحداثيات للسجلات الناقصة، وتتوفر قائمة الاستبعادات في <a href="data/reports/map-coverage.json">تقرير تغطية الخريطة</a>.</p></section></div>'''
    return _document("المنهجية | الأرشيف السوري", "", "methodology", body)


def _copy_data(project_root: Path, site_root: Path) -> None:
    target = site_root / "data"
    target.mkdir(parents=True, exist_ok=True)
    for directory in ("incidents", "sources", "media", "relationships", "schema"):
        source = project_root / "data" / directory
        if source.is_dir():
            shutil.copytree(source, target / directory, dirs_exist_ok=True)
    reports_target = target / "reports"
    reports_target.mkdir(parents=True, exist_ok=True)
    for name in ("collection-summary.json", "collection-summary.md", "map-coverage.json", "map-coverage.md"):
        source = project_root / "data" / "reports" / name
        if source.is_file():
            shutil.copy2(source, reports_target / name)


def build_modern_site(site_root: Path, project_root: Path, *, resume: bool = False) -> dict[str, Any]:
    site_root = site_root.resolve()
    project_root = project_root.resolve()
    site_root.mkdir(parents=True, exist_ok=True)
    records = _load_records(project_root)
    if not records:
        raise FileNotFoundError("No normalized incident records were found")
    collection = load_json(project_root / "data" / "reports" / "collection-summary.json", {})
    map_report = load_json(project_root / "data" / "reports" / "map-coverage.json", {})
    points = load_json(project_root / "data" / "generated" / "map-points.json", [])
    if not collection or not map_report:
        raise FileNotFoundError("Generate modern-only reports before building the site")

    assets_css = site_root / "assets" / "css"
    assets_js = site_root / "assets" / "js"
    assets_css.mkdir(parents=True, exist_ok=True)
    assets_js.mkdir(parents=True, exist_ok=True)
    shutil.copy2(project_root / "web" / "site.css", assets_css / "style.css")
    shutil.copy2(project_root / "web" / "map.css", assets_css / "map.css")
    shutil.copy2(project_root / "web" / "site.js", assets_js / "site.js")
    shutil.copy2(project_root / "web" / "map.js", assets_js / "map.js")
    shutil.copy2(project_root / "web" / "archive-search.js", assets_js / "archive-search.js")

    _copy_data(project_root, site_root)
    summary = {"generated_at": utc_now(), "source": "normalized_records_only", "cases": [_summary_record(record) for record in records]}
    atomic_write_json(site_root / "data" / "cases-summary.json", summary)
    atomic_write_json(site_root / "data" / "map-points.json", points)
    atomic_write_text(assets_js / "map-points.js", "window.SYRIAN_ARCHIVE_MAP_POINTS=" + json.dumps(points, ensure_ascii=False, separators=(",", ":")) + ";\n")

    pages = math.ceil(len(records) / PAGE_SIZE)
    atomic_write_text(site_root / "index.html", _index_html(records, collection, map_report))
    for page in range(2, pages + 1):
        chunk = records[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
        atomic_write_text(site_root / "pages" / f"page-{page:03d}.html", _listing_html(chunk, page, pages))
    # The validator expects page 1 at the stable pagination path as well.
    atomic_write_text(site_root / "pages" / "page-001.html", _listing_html(records[:PAGE_SIZE], 1, pages))

    for record in records:
        sequence = int(record.get("legacy_sequence") or 0)
        case_root = site_root / "cases" / f"{sequence:04d}"
        if resume and (case_root / "index.html").is_file() and (case_root / "data.json").is_file():
            continue
        atomic_write_text(case_root / "index.html", _case_html(record))
        atomic_write_json(case_root / "data.json", record)

    source_pages = 0
    for path in sorted((project_root / "data" / "sources").glob("*.json")):
        source = load_json(path, {})
        if not source or not source.get("source_id"):
            continue
        source_page = site_root / "sources" / str(source["source_id"]) / "index.html"
        if not (resume and source_page.is_file()):
            atomic_write_text(source_page, _source_html(source))
        source_pages += 1

    atomic_write_text(site_root / "map.html", _patch_map("", map_report))
    atomic_write_text(site_root / "methodology.html", _methodology_html(records, collection, map_report))
    atomic_write_text(site_root / "README_AR.txt", "نسخة موقع مبنية حصريًا من ملفات data/incidents الموحّدة، دون فتح الحزمة التاريخية.\n")
    (site_root / ".nojekyll").touch()

    statuses = Counter(str(item.get("completeness_status") or "partial") for item in records)
    report = {
        "project": "الأرشيف السوري — سجل حوادث Airwars",
        "generated_at": utc_now(),
        "architecture": {"active_source_of_truth": "normalized_collection_records", "legacy_package_required": False, "static_site": True},
        "counts": {
            "total_incidents": len(records), "case_pages_created": len(records), "pagination_pages": pages,
            "source_pages_created": source_pages, "map_points": len(points), **dict(statuses),
        },
        "reports": {"collection": "data/reports/collection-summary.json", "map_coverage": "data/reports/map-coverage.json"},
    }
    atomic_write_json(site_root / "data" / "build-report.json", report)
    return report
