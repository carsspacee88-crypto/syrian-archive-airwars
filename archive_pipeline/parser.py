from __future__ import annotations

import copy
import json
import re
from html import unescape
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

from lxml import html

from .io_utils import clean_text


BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "details",
    "div", "dl", "dt", "figcaption", "figure", "footer", "h1", "h2",
    "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav",
    "ol", "p", "pre", "section", "summary", "table", "td", "th", "tr", "ul",
}


def normalize_url(raw_url: str | None) -> str:
    value = unescape((raw_url or "").strip())
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.netloc.startswith("translate.google.") and parsed.path in {"/website", "/translate"}:
        return unquote(parse_qs(parsed.query).get("u", [""])[0])
    if parsed.netloc == "airwars-org.translate.goog":
        return urlunparse(("https", "airwars.org", parsed.path, "", "", parsed.fragment))
    if parsed.netloc == "translated.turbopages.org" and parsed.path.startswith("/proxy_u/"):
        parts = parsed.path.split("/", 3)
        remainder = parts[3] if len(parts) > 3 else ""
        if remainder.startswith("https/"):
            target = "https://" + remainder[6:]
        elif remainder.startswith("http/"):
            target = "http://" + remainder[5:]
        else:
            target = "http://" + remainder
        return target + (("?" + parsed.query) if parsed.query else "")
    return value


def element_text(element: Any) -> str:
    if element is None:
        return ""
    node = copy.deepcopy(element)
    for unwanted in node.xpath(".//script|.//style|.//noscript|.//template"):
        parent = unwanted.getparent()
        if parent is not None:
            parent.remove(unwanted)
    for item in node.iter():
        tag = item.tag.lower() if isinstance(item.tag, str) else ""
        if tag == "li" and item.text:
            item.text = "• " + item.text
        if tag in BLOCK_TAGS:
            if item.text and not item.text.startswith("\n"):
                item.text = "\n" + item.text
            item.tail = (item.tail or "") + "\n"
    return clean_text(node.text_content())


def first_text(nodes: list[Any]) -> str:
    for node in nodes:
        value = clean_text(node.text_content())
        if value:
            return value
    return ""


def value_without_heading(element: Any, heading: str) -> str:
    node = copy.deepcopy(element)
    for target in node.xpath(".//h4|.//input|.//label"):
        parent = target.getparent()
        if parent is not None:
            parent.remove(target)
    value = element_text(node)
    if value.startswith(heading):
        value = value[len(heading):].strip()
    return value


def declared_count(text: str) -> int | None:
    match = re.search(r"\((\d[\d,]*)\)", text or "")
    return int(match.group(1).replace(",", "")) if match else None


def _header_fields(article: Any) -> list[dict[str, str]]:
    sections = article.xpath('.//div[contains(concat(" ", normalize-space(@class), " "), " sections ")]')
    sections_node = sections[0] if sections else None
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for heading_node in article.xpath(".//h4"):
        if sections_node is not None and sections_node in heading_node.iterancestors():
            continue
        label = clean_text(heading_node.text_content())
        if label.casefold() not in {"conflict", "conflicts", "incident code", "incident date", "location", "geolocation"}:
            continue
        parent = heading_node.getparent()
        value = value_without_heading(parent, label)
        key = (label, value)
        if not value or key in seen:
            continue
        seen.add(key)
        anchors = parent.xpath(".//a[@href]")
        rows.append({
            "scope": "header",
            "label": label,
            "value": value,
            "url": normalize_url(anchors[0].get("href")) if anchors else "",
        })
    return rows


def _key_information(document: Any) -> tuple[Any | None, list[dict[str, str]]]:
    headings = document.xpath('//h3[normalize-space()="Key Information"]')
    if not headings:
        return None, []
    container = headings[0].getparent()
    rows: list[dict[str, str]] = []
    for row in container.xpath("./div"):
        children = [child for child in row if isinstance(child.tag, str)]
        if len(children) < 2:
            continue
        label = element_text(children[0])
        value = "\n".join(filter(None, (element_text(child) for child in children[1:])))
        if not label or not value:
            continue
        anchors = row.xpath(".//a[@href]")
        rows.append({
            "scope": "key_information",
            "label": label,
            "value": value,
            "url": normalize_url(anchors[0].get("href")) if anchors else "",
        })
    return container, rows


def _structured_source(details: Any, order: int) -> dict[str, Any]:
    summary_nodes = details.xpath("./summary")
    summary = summary_nodes[0] if summary_nodes else None
    source_name = summary_date = summary_language = summary_url = ""
    if summary is not None:
        divs = summary.xpath("./div")
        if divs:
            source_name = element_text(divs[0])
        if len(divs) > 1:
            summary_date = element_text(divs[1])
        summary_language = first_text(summary.xpath(".//h4"))
        links = summary.xpath(".//a[@href]")
        if links:
            summary_url = normalize_url(links[0].get("href"))
    grid_nodes = details.xpath("./div")
    grid = grid_nodes[0] if grid_nodes else None
    values: dict[str, str] = {}
    urls: dict[str, str] = {}
    if grid is not None:
        for item in grid.xpath("./div"):
            headings = item.xpath("./h4|.//h4[1]")
            if not headings:
                continue
            label = clean_text(headings[0].text_content())
            content_boxes = item.xpath('./div[contains(concat(" ", normalize-space(@class), " "), " italic ")]')
            values[label] = element_text(content_boxes[0]) if content_boxes else value_without_heading(item, label)
            anchors = item.xpath(".//a[@href]")
            if anchors:
                urls[label] = normalize_url(anchors[0].get("href"))
    return {
        "order": order,
        "source_id": values.get("Source ID", ""),
        "format": "structured",
        "name": source_name,
        "date": values.get("Date", "") or summary_date,
        "language": values.get("Languages", "") or summary_language,
        "author": values.get("Source Author", ""),
        "author_translated": values.get("Source Author Translated", ""),
        "url": urls.get("Source URL", "") or summary_url,
        "archive_url": urls.get("Archive URL", ""),
        "includes_video": values.get("Includes Video", ""),
        "content": values.get("Content", ""),
        "content_translated": values.get("Translated Content", ""),
        "raw_text": element_text(details),
    }


def _legacy_sources(container: Any, start_order: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    candidates = container.xpath(
        './/div[contains(concat(" ", normalize-space(@class), " "), " grid ")'
        ' and contains(concat(" ", normalize-space(@class), " "), " grid-cols-5 ") and .//a[@href]]'
    )
    for candidate in candidates:
        if candidate.xpath('ancestor::details[contains(concat(" ", normalize-space(@class), " "), " sourcedetails ")]'):
            continue
        source_anchor = archive_anchor = None
        for anchor in candidate.xpath(".//a[@href]"):
            if clean_text(anchor.text_content()).lower() == "archive":
                archive_anchor = anchor
            elif source_anchor is None:
                source_anchor = anchor
        if source_anchor is None:
            continue
        name = clean_text(source_anchor.text_content()).replace("↗", "").strip()
        rows.append({
            "order": start_order + len(rows),
            "source_id": "",
            "format": "legacy_airwars_page",
            "name": name,
            "date": "",
            "language": first_text(candidate.xpath(".//h4")),
            "author": name,
            "author_translated": "",
            "url": normalize_url(source_anchor.get("href")),
            "archive_url": normalize_url(archive_anchor.get("href")) if archive_anchor is not None else "",
            "includes_video": "",
            "content": "",
            "content_translated": "",
            "raw_text": element_text(candidate),
        })
    return rows


def _classic_sources(container: Any, start_order: int) -> list[dict[str, Any]]:
    """Parse Airwars' pre-2024 source-list markup."""
    rows: list[dict[str, Any]] = []
    for item in container.xpath('.//ul[contains(concat(" ", normalize-space(@class), " "), " sources-list ")]/li'):
        source_links = item.xpath('.//div[contains(concat(" ", normalize-space(@class), " "), " source-title ")]//a[@href]')
        if not source_links:
            continue
        source_link = source_links[0]
        archive_links = item.xpath('.//a[contains(concat(" ", normalize-space(@class), " "), " archive ")][@href]')
        tags: list[str] = []
        for tag in item.xpath('.//div[contains(concat(" ", normalize-space(@class), " "), " source-tags ") and not(contains(concat(" ", normalize-space(@class), " "), " source-tags-mobile "))]//li'):
            value = element_text(tag)
            if value and value not in tags:
                tags.append(value)
        name = element_text(source_link)
        rows.append({
            "order": start_order + len(rows),
            "source_id": "",
            "format": "classic_airwars_page",
            "name": name,
            "date": "",
            "language": ", ".join(tags),
            "author": name,
            "author_translated": "",
            "url": normalize_url(source_link.get("href")),
            "archive_url": normalize_url(archive_links[0].get("href")) if archive_links else "",
            "includes_video": "",
            "content": "",
            "content_translated": "",
            "raw_text": element_text(item),
        })
    return rows


def _media(article: Any) -> list[dict[str, Any]]:
    buttons = article.xpath('.//button[contains(concat(" ", normalize-space(@class), " "), " media-thumb ")]')
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for button in buttons:
        images = button.xpath(".//img")
        if images:
            image = images[0]
            media_type = "image"
            media_url = normalize_url(image.get("data-src") or image.get("src"))
            thumbnail_url = normalize_url(image.get("src"))
            alt = clean_text(image.get("alt"))
            srcset = clean_text(image.get("data-srcset"))
        else:
            media_type = "external_embed"
            media_url = ""
            thumbnail_url = ""
            alt = ""
            srcset = ""
        key = (media_type, media_url or normalize_url(button.get("data-source-url")))
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "order": len(rows) + 1,
            "type": media_type,
            "url": media_url,
            "thumbnail_url": thumbnail_url,
            "source_url": normalize_url(button.get("data-source-url")),
            "caption": clean_text(button.get("data-caption")),
            "description": clean_text(button.get("data-desc")),
            "alt": alt,
            "sensitive": button.get("data-graphic") == "1" or "graphic" in button.get("class", ""),
            "srcset": srcset,
            "storage_status": "external_only",
        })
    return rows


def _classify_link(url: str) -> str:
    parsed = urlparse(url)
    lower = url.lower()
    if "web.archive.org" in lower or "archive.is" in lower or "archive.ph" in lower:
        return "archive"
    if parsed.netloc.endswith("airwars.org"):
        return "airwars_internal"
    if any(token in lower for token in ("youtube.com", "youtu.be", "vimeo.com")):
        return "video"
    return "external"


def parse_incident_html(document: bytes, incident_url: str) -> dict[str, Any]:
    # Airwars pages are UTF-8; passing bytes alone makes lxml ignore the HTTP
    # charset and can turn Arabic into mojibake when the archived markup lacks
    # a usable meta declaration.
    decoded = document.decode("utf-8", errors="replace")
    parsed = html.fromstring(decoded)
    articles = parsed.xpath("//article")
    if not articles:
        raise ValueError("article_element_not_found")
    article = articles[0]
    title = first_text(parsed.xpath("//title"))
    canonical_nodes = parsed.xpath('//link[@rel="canonical"]/@href')
    canonical_url = normalize_url(canonical_nodes[0]) if canonical_nodes else incident_url
    fields = _header_fields(article)
    key_container, key_fields = _key_information(parsed)
    fields.extend(key_fields)

    source_nodes = article.xpath('//*[@data-anchor-id="sources"]')
    if not source_nodes:
        source_nodes = article.xpath('.//div[contains(concat(" ", normalize-space(@class), " "), " sources ") and .//h2]')
    source_container = source_nodes[0] if source_nodes else None
    source_heading = first_text(source_container.xpath("./h3|./h2")) if source_container is not None else ""
    sources = [
        _structured_source(details, index + 1)
        for index, details in enumerate(article.xpath('//details[contains(concat(" ", normalize-space(@class), " "), " sourcedetails ")]'))
    ]
    if source_container is not None:
        sources.extend(_legacy_sources(source_container, len(sources) + 1))
        sources.extend(_classic_sources(source_container, len(sources) + 1))

    media = _media(article)
    media_nodes = article.xpath('//*[@data-anchor-id="media"]')
    media_heading = first_text(media_nodes[0].xpath("./h3")) if media_nodes else ""

    sections: list[dict[str, Any]] = []
    if fields:
        sections.append({"id": "incident-header", "heading": "Incident Header", "text": "\n\n".join(f"{f['label']}\n{f['value']}" for f in fields if f["scope"] == "header")})
    if key_container is not None:
        sections.append({"id": "key-information", "heading": "Key Information", "text": element_text(key_container)})
    for container in article.xpath("//*[@data-anchor-id]"):
        section_id = container.get("data-anchor-id") or ""
        if section_id in {"sources", "media"}:
            continue
        text = element_text(container)
        if not text:
            continue
        sections.append({
            "id": section_id,
            "heading": first_text(container.xpath("./h3|.//h3[1]")) or section_id,
            "text": text,
        })
    if not article.xpath("//*[@data-anchor-id]"):
        seen_section_text: set[str] = set()
        for index, container in enumerate(article.xpath('.//div[contains(concat(" ", normalize-space(@class), " "), " info-main-block ") and .//h2]'), 1):
            if source_container is not None and (container is source_container or source_container in container.iterancestors()):
                continue
            heading = first_text(container.xpath("./h2|.//h2[1]"))
            text = element_text(container)
            if not text or text in seen_section_text:
                continue
            seen_section_text.add(text)
            section_id = re.sub(r"[^a-z0-9]+", "-", heading.casefold()).strip("-") or f"classic-section-{index}"
            sections.append({"id": section_id, "heading": heading or section_id, "text": text})

    links: list[dict[str, str]] = []
    seen_links: set[tuple[str, str]] = set()
    for anchor in article.xpath(".//a[@href]"):
        url = normalize_url(anchor.get("href"))
        if not url or url.rstrip("/") == incident_url.rstrip("/"):
            continue
        text = element_text(anchor)
        key = (text, url)
        if key in seen_links:
            continue
        seen_links.add(key)
        links.append({"text": text, "url": url, "type": _classify_link(url)})

    json_ld: list[Any] = []
    for script in parsed.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            json_ld.append(json.loads(script))
        except json.JSONDecodeError:
            continue

    return {
        "article_found": True,
        "title": title,
        "canonical_url": canonical_url,
        "fields": fields,
        "sections": sections,
        "sources_section_present": source_container is not None,
        "sources_declared": declared_count(source_heading),
        "sources": sources,
        "media_section_present": bool(media_nodes),
        "media_declared": declared_count(media_heading),
        "media_metadata": media,
        "links": links,
        "json_ld": json_ld,
        "snapshot_text": element_text(article),
    }
