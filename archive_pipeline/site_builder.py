from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from pathlib import Path
from typing import Any

from .io_utils import atomic_write_json, atomic_write_text, load_json, utc_now


STATUS_AR = {
    "complete": "مكتمل وفق معيار الاستخراج الحالي",
    "partial": "جزئي",
    "unavailable": "غير متاح",
    "blocked": "محجوب وقت الجمع",
    "failed": "فشل الجمع أو التحليل",
    "pending_review": "بانتظار المراجعة",
    "conflicting_sources": "تعارض يحتاج إلى مراجعة",
    "pending": "بانتظار الجمع المباشر",
}


def _e(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _fmt(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def _prefix(relative_path: Path) -> str:
    return "../" * len(relative_path.parent.parts)


def _navigation(prefix: str, current: str) -> str:
    links = [
        ("index", f"{prefix}index.html", "الحوادث"),
        ("map", f"{prefix}map.html", "خريطة الحوادث"),
        ("methodology", f"{prefix}methodology.html", "المنهجية وحالة الأرشيف"),
        ("report", f"{prefix}data/reports/collection-summary.json", "تقرير الجمع"),
    ]
    rendered = []
    for key, href, label in links:
        current_attr = ' aria-current="page"' if key == current else ""
        rendered.append(f'      <a href="{href}"{current_attr}>{label}</a>')
    return '<nav class="site-nav" aria-label="التنقل الرئيسي">\n' + "\n".join(rendered) + "\n    </nav>"


def _patch_navigation(text: str, prefix: str, current: str) -> str:
    text, count = re.subn(
        r'<nav class="site-nav" aria-label="التنقل الرئيسي">.*?</nav>',
        _navigation(prefix, current),
        text,
        count=1,
        flags=re.DOTALL,
    )
    if count != 1:
        raise ValueError("site_navigation_not_found")
    methodology_link = f'{prefix}methodology.html'
    if f'href="{methodology_link}"' not in text[text.find('<footer class="site-footer">'):]:
        text = text.replace(
            '<div class="footer-links">',
            f'<div class="footer-links">\n        <a href="{methodology_link}">المنهجية وحالة الأرشيف</a>',
            1,
        )
    return text


def _status_counts(collection: dict[str, Any]) -> dict[str, int]:
    direct = collection.get("direct_collection", {})
    result = {key: int(value or 0) for key, value in direct.get("status_counts", {}).items()}
    result["pending"] = int(direct.get("pending") or 0)
    return result


def _methodology_html(
    collection: dict[str, Any],
    map_report: dict[str, Any],
    build_report: dict[str, Any],
) -> str:
    total = int(collection.get("total_incidents") or build_report.get("counts", {}).get("total_incidents") or 0)
    direct = collection.get("direct_collection", {})
    statuses = _status_counts(collection)
    build_date = build_report.get("generated_at_utc") or build_report.get("generated_at") or "غير متاح"
    collection_date = direct.get("latest_successful_collection") or "لم يكتمل جمع مباشر ناجح بعد"
    map_counts = map_report.get("counts", {})
    status_cards = "".join(
        f'<div class="stat"><strong>{_fmt(statuses.get(key, 0))}</strong><span>{label}</span></div>'
        for key, label in [
            ("complete", "مكتملة مباشرة"),
            ("partial", "جزئية بعد الجمع"),
            ("failed", "فشل"),
            ("unavailable", "غير متاحة"),
            ("blocked", "محجوبة وقت الجمع"),
            ("pending", "بانتظار الجمع"),
            ("pending_review", "بانتظار المراجعة"),
        ]
    )
    return f'''<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="منهجية الأرشيف السوري وتعريف اكتمال السجلات وحالة الجمع المباشر من Airwars">
  <meta name="referrer" content="strict-origin-when-cross-origin">
  <title>المنهجية وحالة الأرشيف | الأرشيف السوري</title>
  <link rel="stylesheet" href="assets/css/style.css">
</head>
<body>
<a class="skip-link" href="#main-content">انتقل إلى المحتوى</a>
<header class="site-header"><div class="wrap header-inner">
  <a class="brand" href="index.html"><span class="brand-mark" aria-hidden="true">س</span><span class="brand-copy"><strong>الأرشيف السوري</strong><small>واجهة عربية مستقلة للبيانات الموثقة</small></span></a>
  {_navigation("", "methodology")}
</div></header>
<main id="main-content">
  <section class="hero methodology-hero"><div class="wrap hero-grid"><div>
    <p class="eyebrow">منهجية قابلة للتدقيق</p><h1>كيف تُجمع البيانات؟ وماذا تعني حالة السجل؟</h1>
    <p class="lead">تُجمع السجلات الجديدة من صفحة Airwars العامة أو نقطة البيانات العامة، ثم من نسخة مؤرشفة عند تعذر المصدر الحي. تبقى لقطة Excel القديمة مرجع هجرة فقط، وتُوسم قيمها داخليًا <span class="ltr">legacy_import</span> إلى أن تُراجع مستقلًا.</p>
  </div><aside class="hero-note"><strong>{_fmt(total)} حادثة</strong><p>آخر بناء: <span class="ltr">{_e(build_date)}</span><br>آخر جمع مباشر ناجح: <span class="ltr">{_e(collection_date)}</span></p></aside></div></section>
  <div class="wrap methodology-layout">
    <section class="stats-strip stats-strip--six" aria-label="حالة الجمع">{status_cards}</section>
    <section class="content-section"><h2>تعريف الاكتمال</h2>
      <div class="definition-grid">
        <article><h3>حادثة مكتملة</h3><p>سجل جرى تحليله من مصدر Airwars حي أو مؤرشف، ويتضمن المعرّف والرابط القانوني والتاريخ والموقع والسرد وقسم المصادر وفق معيار المحلل الحالي. نجاح طلب HTTP وحده لا يجعل السجل مكتملًا.</p></article>
        <article><h3>حادثة جزئية</h3><p>سجل صالح وله صفحة HTML وبيانات مفيدة، لكن حقلًا مطلوبًا أو قسمًا متوقعًا لم يُستخرج بعد. قد يظل يحتوي على الرمز والتاريخ والموقع وأعداد الضحايا والإحداثيات والمصادر أو الروابط المؤرشفة.</p></article>
      </div>
      <aside class="notice notice--data"><p><strong>«جزئي» لا يعني أن صفحة الموقع معطلة.</strong> كان الوصف الجزئي السابق ناتجًا عن استخراج غير مكتمل، وليس دليلًا على غياب المعلومة من المصدر الأصلي. الأقسام الأكثر عرضة للنقص هي السرد الكامل، تفاصيل المصادر، الضحايا، الوسائط، أو حقول الصفحة الداخلية.</p></aside>
    </section>
    <section class="content-section"><h2>هرمية المصادر وحفظ المنشأ</h2>
      <ol class="method-list"><li><strong>Airwars المنظم:</strong> نقطة عامة موثوقة إن كانت متاحة.</li><li><strong>صفحة Airwars الحية:</strong> المصدر الأول للنص والأقسام.</li><li><strong>نسخة Airwars المؤرشفة:</strong> بديل محافظ عند الحجب أو النقص.</li><li><strong>روابط مصادر خارجية مؤرشفة:</strong> تُحفظ مع نسبها إلى الناشر الأصلي.</li><li><strong>لقطة الهجرة القديمة:</strong> لا تُحذف، لكنها ليست المصدر النشط للحقيقة.</li></ol>
      <p><strong>بيانات المصدر الوصفية</strong> هي الاسم والتاريخ والرابط والوصف. <strong>الرابط المؤرشف</strong> مؤشر إلى نسخة لدى خدمة أرشفة خارجية. <strong>المحتوى المحفوظ محليًا</strong> هو نص الاستجابة أو لقطة نصية مسموحة وموسومة بالمصدر. لا تُنزّل الصور أو الفيديوهات في هذه المرحلة؛ تُحفظ روابطها وبياناتها الوصفية فقط، وتبقى حقوق ونِسب المحتوى لناشريه الأصليين.</p>
      <p>عند التعارض لا تُدمج القيم بصمت: تُحفظ القيمتان، وتُسجّل أصولهما، ويُحال السجل إلى مراجعة بشرية.</p>
    </section>
    <section class="content-section"><h2>حالة الخريطة</h2><p>تُمثَّل <strong>{_fmt(map_counts.get("incidents_included_on_map"))}</strong> حادثة من أصل <strong>{_fmt(map_counts.get("total_incidents"))}</strong> ({map_counts.get("map_coverage_percentage", 0)}%). استُبعدت القيم الناقصة أو غير الصالحة أو الواقعة خارج نطاق التحقق الواسع من دون تصحيح صامت.</p><p><a class="button-link" href="map.html">فتح الخريطة وتفاصيل التغطية</a> <a class="button-link button-link--secondary" href="data/reports/map-coverage.json">فتح التقرير الآلي</a></p></section>
    <section class="content-section"><h2>الشفافية وإعادة الاستخدام</h2><p>الموقع مستقل وغير تابع لـAirwars. الادعاءات منسوبة إلى سجلاتها أو إلى المصادر الأصلية ولا تُعرض كأحكام قضائية. لكل قيمة رئيسية سجل منشأ يبين هل جاءت من صفحة حية، أو نقطة منظمة، أو نسخة مؤرشفة، أو لقطة الهجرة، أو تصحيح يدوي.</p><p><a href="data/reports/collection-summary.json">تقرير حالة الجمع بصيغة JSON</a> · <a href="data/build-report.json">تقرير البناء</a></p></section>
  </div>
</main>
<footer class="site-footer"><div class="wrap footer-grid"><div><h2>عن هذه النسخة</h2><p>واجهة عربية مستقلة لتقريب بيانات الضرر المدني المتعلقة بسوريا مع الحفاظ على الإسناد والمنشأ.</p></div><div><h2>روابط وملفات</h2><div class="footer-links"><a href="methodology.html">المنهجية وحالة الأرشيف</a><a href="https://airwars.org/country/syria/?post_type=civ" target="_blank" rel="noopener noreferrer">سجل سوريا في Airwars</a><a href="data/reports/collection-summary.json">تقرير الجمع</a><a href="data/reports/map-coverage.json">تقرير الخريطة</a></div></div></div></footer>
<script src="assets/js/site.js" defer></script>
</body></html>'''


def _homepage_callout(collection: dict[str, Any]) -> str:
    direct = collection.get("direct_collection", {})
    return f'''<section class="archive-status-callout" aria-labelledby="archive-status-title">
      <div><p class="eyebrow">حالة الأرشيف</p><h2 id="archive-status-title">الصفحة موجودة لا تعني أن استخراج المصدر مكتمل</h2><p>جُمعت مباشرة حتى الآن {_fmt(direct.get("processed"))} حادثة، وتنتظر {_fmt(direct.get("pending"))} حادثة دورها في خط الجمع القابل للاستئناف. السجلات القديمة محفوظة ولا تُحذف.</p></div>
      <a class="button-link" href="methodology.html">اقرأ المنهجية وتعريف «جزئي»</a>
    </section>'''


def _search_panel(prefix: str) -> str:
    return f'''<section class="archive-search" aria-labelledby="search-title">
      <div class="section-heading"><div><p class="eyebrow">بحث محلي ثابت</p><h2 id="search-title">البحث والتصفية في 8,114 حادثة</h2></div><p>لا تُرسل عبارة البحث إلى أي خادم.</p></div>
      <form class="search-form" data-archive-search data-summary-url="{prefix}data/cases-summary.json" data-case-root="{prefix}">
        <label><span>الرمز أو الموقع أو التاريخ أو الجهة</span><input type="search" name="q" autocomplete="off" placeholder="مثال: الرقة أو CS1033"></label>
        <label><span>حالة لقطة الهجرة</span><select name="status"><option value="">الكل</option><option value="complete">تفاصيل محفوظة</option><option value="partial">بيانات جزئية</option></select></label>
        <label><span>الإحداثيات</span><select name="coordinates"><option value="">الكل</option><option value="yes">موجودة رقميًا</option><option value="no">ناقصة</option></select></label>
        <button type="submit">بحث</button>
      </form>
      <div class="search-results" data-search-results hidden aria-live="polite"></div>
    </section>'''


def _current_stats(collection: dict[str, Any], map_report: dict[str, Any]) -> str:
    direct = collection.get("direct_collection", {})
    map_points = map_report.get("counts", {}).get("incidents_included_on_map", 0)
    return f'''<section class="stats-strip" aria-label="ملخص الأرشيف">
      <div class="stat"><strong>{_fmt(collection.get("total_incidents"))}</strong><span>إجمالي الحوادث</span></div>
      <div class="stat"><strong>{_fmt(direct.get("processed"))}</strong><span>جُمعت مباشرة</span></div>
      <div class="stat"><strong>{_fmt(direct.get("pending"))}</strong><span>بانتظار الجمع المباشر</span></div>
      <div class="stat"><strong>{_fmt(map_points)}</strong><span>نقطة معروضة على الخريطة</span></div>
    </section>'''


def _patch_stats(text: str, collection: dict[str, Any], map_report: dict[str, Any]) -> str:
    text, count = re.subn(r'<section class="stats-strip" aria-label="ملخص الأرشيف">.*?</section>', _current_stats(collection, map_report), text, count=1, flags=re.DOTALL)
    if count != 1:
        raise ValueError("homepage_stats_not_found")
    return text


def _patch_homepage(text: str, collection: dict[str, Any], map_report: dict[str, Any]) -> str:
    text = _patch_stats(text, collection, map_report)
    marker = '    <div class="section-heading">'
    if marker not in text:
        raise ValueError("homepage_section_heading_not_found")
    return text.replace(marker, f"    {_homepage_callout(collection)}\n    {_search_panel('')}\n{marker}", 1)


def _patch_listing(text: str, prefix: str, collection: dict[str, Any], map_report: dict[str, Any]) -> str:
    text = _patch_stats(text, collection, map_report)
    marker = '    <div class="section-heading">'
    if marker not in text:
        raise ValueError("listing_section_heading_not_found")
    return text.replace(marker, f"    {_search_panel(prefix)}\n{marker}", 1)


def _add_search_script(text: str, prefix: str) -> str:
    tag = f'  <script src="{prefix}assets/js/archive-search.js" defer></script>\n'
    if "archive-search.js" not in text:
        text = text.replace("</body>", tag + "</body>", 1)
    return text


def _patch_map(text: str, report: dict[str, Any]) -> str:
    counts = report.get("counts", {})
    hero = f'''<section class="hero"><div class="wrap hero-grid"><div>
        <p class="eyebrow">خريطة أولية لسوريا</p><h1>مواقع الحوادث ذات الإحداثيات المقبولة</h1>
        <p class="lead">تعرض الخريطة الإحداثيات الموجودة في مجموعة البيانات الحالية فقط. لا تُخترع الإحداثيات ولا تُصحح القيم المشبوهة بصمت؛ تُحفظ القيمة الأصلية ويُسجل سبب الاستبعاد.</p>
      </div><aside class="hero-note"><strong>{_fmt(counts.get("incidents_included_on_map"))} نقطة</strong><p>تمثل {counts.get("map_coverage_percentage", 0)}% من إجمالي الحوادث. الضغط على النقطة يفتح ملخصًا ورابط صفحة الحالة.</p></aside></div></section>'''
    text, count = re.subn(r'<section class="hero">.*?</section>', hero, text, count=1, flags=re.DOTALL)
    if count != 1:
        raise ValueError("map_hero_not_found")
    coverage = f'''<section class="content-section map-coverage" aria-labelledby="coverage-title"><div class="section-heading"><div><p class="eyebrow">تغطية قابلة للتدقيق</p><h2 id="coverage-title">تغطية الخريطة</h2></div><a href="data/reports/map-coverage.json">تنزيل تقرير JSON</a></div>
      <div class="stats-strip stats-strip--six"><div class="stat"><strong>{_fmt(counts.get("total_incidents"))}</strong><span>إجمالي الحوادث</span></div><div class="stat"><strong>{_fmt(counts.get("incidents_with_world_valid_latitude_and_longitude"))}</strong><span>زوج صالح عالميًا</span></div><div class="stat"><strong>{_fmt(counts.get("incidents_missing_coordinates"))}</strong><span>إحداثيات ناقصة</span></div><div class="stat"><strong>{_fmt(counts.get("incidents_with_invalid_coordinates"))}</strong><span>إحداثيات غير صالحة</span></div><div class="stat"><strong>{_fmt(counts.get("incidents_outside_expected_region"))}</strong><span>خارج نطاق التحقق</span></div><div class="stat"><strong>{_fmt(counts.get("incidents_excluded_from_map"))}</strong><span>مستبعدة من الخريطة</span></div></div>
      <p>نسبة التمثيل: <strong>{counts.get("map_coverage_percentage", 0)}%</strong>. يتضمن التقرير معرّفات الحالات والقيم الأصلية وأسباب الاستبعاد.</p></section>'''
    marker = '    <section class="map-shell" aria-label="خريطة الحوادث">'
    if marker not in text:
        raise ValueError("map_shell_not_found")
    text = text.replace(marker, f"    {coverage}\n{marker}", 1)
    text = text.replace("في ملف Excel فقط", "في مجموعة البيانات الحالية فقط")
    return text


def _case_provenance(record: dict[str, Any] | None, sequence: int) -> str:
    if record:
        status = record.get("completeness_status") or "pending"
        extraction = record.get("page_extraction") or record.get("api_extraction") or {}
        source_type = extraction.get("source_type") or ("airwars_endpoint" if record.get("api_extraction") else "legacy_import")
        source_labels = {
            "airwars_live": "صفحة Airwars الحية",
            "airwars_archive": "نسخة Airwars المؤرشفة",
            "airwars_endpoint": "نقطة Airwars المنظمة",
            "legacy_import": "لقطة الهجرة القديمة",
        }
        missing = record.get("missing_sections", []) + record.get("missing_fields", [])
        missing_text = "، ".join(_e(item) for item in missing) if missing else "لا أقسام مطلوبة مفقودة وفق المعيار الحالي"
        archive_url = next((url for url in record.get("archived_urls", []) if "web.archive.org" in url), "")
        archive_link = f' · <a href="{_e(archive_url)}" target="_blank" rel="noopener noreferrer">النسخة المؤرشفة المستخدمة</a>' if archive_url else ""
        return f'''<aside class="provenance-panel" aria-label="منشأ بيانات الحالة"><div><strong>حالة الاستخراج: {STATUS_AR.get(status, _e(status))}</strong><p>المصدر المستخدم: {source_labels.get(source_type, _e(source_type))} · تاريخ الاسترجاع: <span class="ltr">{_e(record.get("retrieved_at") or "غير متاح")}</span></p><p>الحقول أو الأقسام المفقودة: {missing_text}</p></div><div class="provenance-links"><a href="{_e(record.get('canonical_url'))}" target="_blank" rel="noopener noreferrer">صفحة Airwars الأصلية</a>{archive_link}<a href="../../data/incidents/{_e(record.get('internal_id'))}.json">JSON الموحّد</a><a href="data.json">بيانات لقطة الهجرة</a></div></aside>'''
    return f'''<aside class="provenance-panel provenance-panel--pending" aria-label="منشأ بيانات الحالة"><div><strong>بانتظار التحقق المباشر</strong><p>هذه الصفحة تعرض لقطة الهجرة القديمة المتاحة، وليست معطلة. ستبقى القيم موسومة <span class="ltr">legacy_import</span> إلى أن تُراجع من Airwars أو نسخة مؤرشفة.</p></div><div class="provenance-links"><a href="../../methodology.html">شرح حالة الاكتمال</a><a href="data.json">بيانات لقطة الهجرة</a></div></aside>'''


def _patch_case(text: str, record: dict[str, Any] | None, sequence: int) -> str:
    marker = '<main id="main-content">'
    if marker not in text:
        raise ValueError(f"case_main_not_found:{sequence}")
    panel = f'\n  <div class="wrap provenance-wrap">{_case_provenance(record, sequence)}</div>'
    return text.replace(marker, marker + panel, 1)


CSS_ENHANCEMENTS = r'''

/* Direct-ingestion and methodology enhancements. */
.archive-status-callout,
.provenance-panel {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1.25rem;
  margin-block: 1.5rem 2rem;
  padding: 1.25rem 1.4rem;
  border: 1px solid var(--line);
  border-inline-start: .35rem solid var(--teal);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow);
}
.archive-status-callout h2,
.archive-status-callout p,
.provenance-panel p { margin-bottom: .4rem; }
.button-link {
  display: inline-flex;
  min-height: 2.8rem;
  align-items: center;
  justify-content: center;
  padding: .55rem 1rem;
  border-radius: .65rem;
  background: var(--teal);
  color: #fff;
  font-weight: 700;
  text-decoration: none;
}
.button-link:hover { background: var(--clay); color: #fff; }
.button-link--secondary { background: var(--surface-muted); color: var(--teal-dark); }
.methodology-layout { display: grid; gap: 1.4rem; padding-bottom: 3rem; }
.methodology-layout .content-section { margin: 0; }
.definition-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1rem; }
.definition-grid article { padding: 1rem; border: 1px solid var(--line); border-radius: var(--radius-sm); background: var(--surface-muted); }
.definition-grid article p { margin-bottom: 0; }
.method-list { display: grid; gap: .65rem; padding-inline-start: 1.4rem; }
.stats-strip--six { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.map-coverage { margin-block: 1.4rem; }
.archive-search { margin-block: 1.5rem 2rem; padding: 1.2rem; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); }
.search-form { display: grid; grid-template-columns: minmax(14rem, 2fr) 1fr 1fr auto; gap: .8rem; align-items: end; }
.search-form label { display: grid; gap: .25rem; color: var(--ink-soft); font-size: .82rem; font-weight: 700; }
.search-form input,
.search-form select { width: 100%; min-height: 2.8rem; padding: .45rem .65rem; border: 1px solid var(--line); border-radius: .55rem; background: #fff; color: var(--ink); }
.search-form button { min-height: 2.8rem; padding: .45rem 1rem; border: 0; border-radius: .55rem; background: var(--teal); color: #fff; font-weight: 700; cursor: pointer; }
.search-results { margin-top: 1rem; }
.search-summary { color: var(--ink-soft); }
.search-results-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: .65rem; }
.search-result-card { border: 1px solid var(--line); border-radius: .65rem; background: var(--surface-muted); }
.search-result-link { display: grid; gap: .2rem; height: 100%; padding: .8rem; color: var(--ink); text-decoration: none; }
.search-result-link small { color: var(--ink-soft); }
.provenance-wrap { padding-top: 1rem; }
.provenance-panel { align-items: flex-start; margin-bottom: 0; border-inline-start-color: var(--clay); }
.provenance-panel--pending { border-inline-start-color: var(--gold); }
.provenance-links { display: flex; flex: 0 0 min(22rem, 38%); flex-wrap: wrap; gap: .45rem .8rem; }
.provenance-links a { font-weight: 700; }
@media (max-width: 760px) {
  .archive-status-callout,
  .provenance-panel { display: grid; }
  .definition-grid,
  .stats-strip--six { grid-template-columns: 1fr 1fr; }
  .provenance-links { width: 100%; }
  .search-form { grid-template-columns: 1fr 1fr; }
  .search-results-grid { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 480px) {
  .definition-grid,
  .stats-strip--six { grid-template-columns: 1fr; }
  .search-form,
  .search-results-grid { grid-template-columns: 1fr; }
}
'''


def _load_normalized(root: Path) -> dict[int, dict[str, Any]]:
    records: dict[int, dict[str, Any]] = {}
    for path in sorted((root / "data" / "incidents").glob("*.json")):
        record = load_json(path, {})
        if record.get("legacy_sequence"):
            records[int(record["legacy_sequence"])] = record
    return records


def _write_current_build_report(
    site_root: Path,
    legacy_report: dict[str, Any],
    collection: dict[str, Any],
    map_report: dict[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    legacy_counts = legacy_report.get("counts", {})
    statuses = _status_counts(collection)
    report = {
        "project": "الأرشيف السوري — سجل حوادث Airwars",
        "generated_at": generated_at,
        "architecture": {
            "active_source_of_truth": "normalized_direct_airwars_ingestion",
            "legacy_excel_role": "historical_migration_snapshot_only",
            "static_site": True,
            "database_required": False,
        },
        "counts": {
            "total_incidents": collection.get("total_incidents", legacy_counts.get("total_incidents", 0)),
            "case_pages_created": legacy_counts.get("case_pages_created", 0),
            "direct_records_processed": collection.get("direct_collection", {}).get("processed", 0),
            "complete": statuses.get("complete", 0),
            "partial": statuses.get("partial", 0),
            "failed": statuses.get("failed", 0),
            "unavailable": statuses.get("unavailable", 0),
            "blocked": statuses.get("blocked", 0),
            "pending_review": statuses.get("pending_review", 0) + statuses.get("conflicting_sources", 0),
            "pending": statuses.get("pending", 0),
            "sources_in_legacy_snapshot": legacy_counts.get("sources", 0),
            "archive_links_in_legacy_snapshot": legacy_counts.get("archive_links", 0),
            "media_metadata_in_legacy_snapshot": legacy_counts.get("media", 0),
            "victims_in_legacy_snapshot": legacy_counts.get("victims", 0),
            "map_points": map_report.get("counts", {}).get("incidents_included_on_map", 0),
        },
        "latest_successful_source_collection": collection.get("direct_collection", {}).get("latest_successful_collection"),
        "media_policy": {"binary_files_downloaded": 0, "urls_and_metadata_only": True},
        "reports": {
            "collection": "data/reports/collection-summary.json",
            "map_coverage": "data/reports/map-coverage.json",
            "legacy_build": "data/reports/legacy-build-report.json",
        },
    }
    reports_dir = site_root / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(reports_dir / "legacy-build-report.json", legacy_report)
    atomic_write_json(site_root / "data" / "build-report.json", report)
    return report


def build_site(site_root: Path, project_root: Path) -> dict[str, Any]:
    site_root = site_root.resolve()
    project_root = project_root.resolve()
    if not (site_root / "index.html").exists():
        raise FileNotFoundError(f"Site root has no index.html: {site_root}")
    collection = load_json(project_root / "data" / "reports" / "collection-summary.json", {})
    map_report = load_json(project_root / "data" / "reports" / "map-coverage.json", {})
    if not collection or not map_report:
        raise FileNotFoundError("Generate collection-summary.json and map-coverage.json before building the site")
    legacy_report = load_json(site_root / "data" / "build-report.json", {})
    build_timestamp = utc_now()
    normalized = _load_normalized(project_root)

    reports_target = site_root / "data" / "reports"
    reports_target.mkdir(parents=True, exist_ok=True)
    for source in sorted((project_root / "data" / "reports").glob("*")):
        if source.is_file():
            shutil.copy2(source, reports_target / source.name)
    legacy_checksums = site_root / "checksums.sha256"
    if legacy_checksums.is_file():
        shutil.copy2(legacy_checksums, reports_target / "legacy-checksums.sha256")
    generated_points = load_json(project_root / "data" / "generated" / "map-points.json", [])
    atomic_write_json(site_root / "data" / "map-points.json", generated_points)
    atomic_write_text(
        site_root / "assets" / "js" / "map-points.js",
        "window.SYRIAN_ARCHIVE_MAP_POINTS=" + json.dumps(generated_points, ensure_ascii=False, separators=(",", ":")) + ";\n",
    )

    incidents_target = site_root / "data" / "incidents"
    incidents_target.mkdir(parents=True, exist_ok=True)
    for source in sorted((project_root / "data" / "incidents").glob("*.json")):
        shutil.copy2(source, incidents_target / source.name)
    schema_source = project_root / "data" / "schema"
    if schema_source.is_dir():
        shutil.copytree(schema_source, site_root / "data" / "schema", dirs_exist_ok=True)

    for html_path in sorted(site_root.rglob("*.html")):
        relative = html_path.relative_to(site_root)
        prefix = _prefix(relative)
        current = "map" if relative == Path("map.html") else "index"
        text = html_path.read_text(encoding="utf-8")
        text = _patch_navigation(text, prefix, current)
        if relative == Path("index.html"):
            text = _patch_homepage(text, collection, map_report)
            text = _add_search_script(text, prefix)
        elif len(relative.parts) == 2 and relative.parts[0] == "pages":
            text = _patch_listing(text, prefix, collection, map_report)
            text = _add_search_script(text, prefix)
        elif relative == Path("map.html"):
            text = _patch_map(text, map_report)
        elif len(relative.parts) == 3 and relative.parts[0] == "cases" and relative.name == "index.html":
            sequence = int(relative.parts[1])
            text = _patch_case(text, normalized.get(sequence), sequence)
        atomic_write_text(html_path, text)

    methodology_path = site_root / "methodology.html"
    methodology = _methodology_html(collection, map_report, {**legacy_report, "generated_at": build_timestamp, "generated_at_utc": build_timestamp})
    atomic_write_text(methodology_path, methodology)
    shutil.copy2(project_root / "web" / "archive-search.js", site_root / "assets" / "js" / "archive-search.js")
    css_path = site_root / "assets" / "css" / "style.css"
    css = css_path.read_text(encoding="utf-8")
    if "Direct-ingestion and methodology enhancements" not in css:
        atomic_write_text(css_path, css.rstrip() + "\n" + CSS_ENHANCEMENTS.lstrip())
    (site_root / ".nojekyll").touch()
    current_report = _write_current_build_report(site_root, legacy_report, collection, map_report, build_timestamp)
    return {
        "html_pages_patched": sum(1 for _ in site_root.rglob("*.html")),
        "normalized_incidents_published": len(normalized),
        "map_points_published": len(generated_points),
        "build_report": current_report,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Overlay the direct-ingestion archive UI on the legacy static snapshot")
    parser.add_argument("--site-root", required=True)
    parser.add_argument("--project-root", default=".")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = build_site(Path(args.site_root), Path(args.project_root))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
