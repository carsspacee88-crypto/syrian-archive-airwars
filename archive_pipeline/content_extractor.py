from __future__ import annotations

import io
import json
import re
import urllib.parse
import zipfile
from dataclasses import dataclass
from typing import Any, Iterable

from lxml import etree, html

from .io_utils import clean_text


_TEXT_KEYS = {
    "articlebody": 120,
    "article_body": 120,
    "body": 80,
    "content": 75,
    "text": 70,
    "description": 45,
    "summary": 45,
    "caption": 35,
    "message": 35,
}
_METADATA_KEYS = {
    "headline",
    "name",
    "title",
    "author",
    "datepublished",
    "datecreated",
    "datemodified",
    "published_at",
}
_SCRIPT_NOISE = re.compile(
    r"(?:webpack|chunk|manifest|polyfill|stylesheet|javascript|cookie|navigation|"
    r"tracking|analytics|advertisement|subscribe|sign\s*in|log\s*in)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TextCandidate:
    text: str
    method: str
    priority: int


def _decode_body(body: bytes, content_type: str) -> str:
    charset_match = re.search(r"charset\s*=\s*['\"]?([^;\s'\"]+)", content_type, re.I)
    charsets = [charset_match.group(1)] if charset_match else []
    # Older Arabic news sites frequently omit the HTTP charset but declare
    # windows-1256 in a meta tag. Read bytes as latin-1 only for declaration
    # discovery; the actual document is decoded with the declared codec.
    declaration = body[:8192].decode("latin-1", errors="ignore")
    meta_match = re.search(
        r"(?:charset\s*=\s*['\"]?([^\s'\";/>]+)|content\s*=\s*['\"][^'\"]*charset=([^\s'\";/>]+))",
        declaration,
        re.I,
    )
    if meta_match:
        charsets.append(meta_match.group(1) or meta_match.group(2))
    charsets.extend(["utf-8", "windows-1256", "windows-1252"])
    for charset in charsets:
        try:
            return body.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
    try:
        from charset_normalizer import from_bytes

        match = from_bytes(body).best()
        if match is not None:
            return str(match)
    except Exception:
        pass
    return body.decode("utf-8", errors="replace")


def _unique_text(values: Iterable[str]) -> str:
    paragraphs: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = clean_text(raw)
        fingerprint = re.sub(r"\s+", " ", value).casefold()
        if not value or fingerprint in seen:
            continue
        seen.add(fingerprint)
        paragraphs.append(value)
    return "\n\n".join(paragraphs)


def _json_candidates(payload: Any) -> tuple[list[TextCandidate], dict[str, str]]:
    candidates: list[TextCandidate] = []
    metadata: dict[str, str] = {}
    seen_objects: set[int] = set()
    stack: list[tuple[Any, int]] = [(payload, 0)]
    visited = 0
    while stack and visited < 20_000:
        value, depth = stack.pop()
        visited += 1
        if depth > 18:
            continue
        if isinstance(value, (dict, list)):
            identity = id(value)
            if identity in seen_objects:
                continue
            seen_objects.add(identity)
        if isinstance(value, list):
            stack.extend((item, depth + 1) for item in reversed(value[:2_000]))
            continue
        if not isinstance(value, dict):
            continue
        for raw_key, item in value.items():
            key = str(raw_key).casefold().replace("-", "_")
            compact_key = key.replace("_", "")
            if isinstance(item, str):
                text = clean_text(item)
                if not text or _SCRIPT_NOISE.search(key):
                    continue
                priority = _TEXT_KEYS.get(key, _TEXT_KEYS.get(compact_key))
                if priority and len(text) >= 24:
                    candidates.append(TextCandidate(text, f"json:{key}", priority))
                if compact_key in _METADATA_KEYS and len(text) <= 1_000:
                    metadata.setdefault(compact_key, text)
            elif isinstance(item, (dict, list)):
                stack.append((item, depth + 1))
    return candidates, metadata


def _safe_json(raw: str) -> Any | None:
    value = raw.strip().removeprefix("<!--").removesuffix("-->").strip()
    if not value or len(value) > 12 * 1024 * 1024:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _trafilatura_candidate(decoded: str, final_url: str) -> TextCandidate | None:
    try:
        import trafilatura

        text = trafilatura.extract(
            decoded,
            url=final_url,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
            no_fallback=False,
            output_format="txt",
        )
    except Exception:
        return None
    cleaned = clean_text(text or "")
    return TextCandidate(cleaned, "trafilatura", 110) if cleaned else None


def _html_document(body: bytes, content_type: str, final_url: str) -> dict[str, Any]:
    decoded = _decode_body(body, content_type)
    try:
        document = html.fromstring(decoded, base_url=final_url)
    except (etree.ParserError, ValueError):
        return {
            "title": "",
            "author": "",
            "publication_date": "",
            "text": clean_text(decoded),
            "method": "html_parse_fallback",
            "alternate_urls": [],
            "candidate_count": 1,
        }

    title = clean_text(" ".join(document.xpath(
        "//meta[@property='og:title']/@content | //meta[@name='twitter:title']/@content | //title/text()"
    )))
    author = clean_text(" ".join(document.xpath(
        "//meta[@name='author']/@content | //meta[@property='article:author']/@content | "
        "//*[@rel='author']/text()"
    )))
    publication_date = clean_text(" ".join(document.xpath(
        "//meta[@property='article:published_time']/@content | //meta[@name='date']/@content | "
        "//meta[@name='pubdate']/@content | //time/@datetime"
    )))
    description = clean_text(" ".join(document.xpath(
        "//meta[@property='og:description']/@content | //meta[@name='twitter:description']/@content | "
        "//meta[@name='description']/@content"
    )))

    alternate_urls: list[dict[str, str]] = []
    alternate_seen: set[str] = set()
    for node in document.xpath("//link[@href]"):
        relation = clean_text(node.get("rel") or "").casefold()
        mime = clean_text(node.get("type") or "").casefold()
        role = (
            "amp"
            if "amphtml" in relation
            else "feed"
            if "alternate" in relation and ("rss" in mime or "atom" in mime)
            else "json"
            if "alternate" in relation and "json" in mime
            else ""
        )
        if not role:
            continue
        resolved = urllib.parse.urljoin(final_url, str(node.get("href") or ""))
        if resolved.startswith(("http://", "https://")) and resolved not in alternate_seen:
            alternate_seen.add(resolved)
            alternate_urls.append({"url": resolved, "role": role})

    candidates: list[TextCandidate] = []
    if description:
        candidates.append(TextCandidate(description, "meta_description", 20))
    extracted = _trafilatura_candidate(decoded, final_url)
    if extracted:
        candidates.append(extracted)

    for raw in document.xpath("//script[@type='application/ld+json']/text()"):
        payload = _safe_json(str(raw))
        if payload is None:
            continue
        rows, metadata = _json_candidates(payload)
        candidates.extend(TextCandidate(row.text, "jsonld:" + row.method, row.priority + 30) for row in rows)
        title = title or metadata.get("headline") or metadata.get("title") or metadata.get("name") or ""
        author = author or metadata.get("author") or ""
        publication_date = publication_date or metadata.get("datepublished") or metadata.get("publishedat") or ""

    for raw in document.xpath(
        "//script[@id='__NEXT_DATA__']/text() | //script[@type='application/json']/text() | "
        "//script[@type='application/activity+json']/text()"
    ):
        payload = _safe_json(str(raw))
        if payload is None:
            continue
        rows, metadata = _json_candidates(payload)
        candidates.extend(TextCandidate(row.text, "embedded:" + row.method, row.priority) for row in rows)
        title = title or metadata.get("headline") or metadata.get("title") or ""
        author = author or metadata.get("author") or ""

    for unwanted in document.xpath(
        "//script|//style|//noscript|//template|//svg|//canvas|//nav|//footer|//header|//form|//aside"
    ):
        parent = unwanted.getparent()
        if parent is not None:
            parent.remove(unwanted)
    semantic = document.xpath("//article | //main | //*[@role='main'] | //*[@itemprop='articleBody']")
    if semantic:
        root = max(semantic, key=lambda node: len(clean_text(node.text_content())))
        blocks = _unique_text(
            node.text_content()
            for node in root.xpath(".//h1|.//h2|.//h3|.//p|.//blockquote|.//li|.//figcaption|.//pre|.//td")
        )
        semantic_text = blocks or clean_text(root.text_content())
        if semantic_text:
            candidates.append(TextCandidate(semantic_text, "semantic_dom", 100))
    body_nodes = document.xpath("//body")
    if body_nodes:
        body_text = clean_text(body_nodes[0].text_content())
        if body_text:
            candidates.append(TextCandidate(body_text, "document_body", 5))

    candidates = [candidate for candidate in candidates if clean_text(candidate.text)]
    if not candidates:
        selected = TextCandidate("", "empty_html", 0)
    else:
        # Priority protects a precise article/JSON-LD candidate from a much
        # longer navigation shell. Length only breaks ties within a method class.
        selected = max(candidates, key=lambda row: (row.priority, min(len(row.text), 250_000)))
    return {
        "title": title,
        "author": author,
        "publication_date": publication_date,
        "text": clean_text(selected.text),
        "method": selected.method,
        "alternate_urls": alternate_urls[:8],
        "candidate_count": len(candidates),
    }


def _json_document(body: bytes, content_type: str) -> dict[str, Any]:
    decoded = _decode_body(body, content_type)
    payload = _safe_json(decoded)
    if payload is None:
        return {"title": "", "author": "", "publication_date": "", "text": clean_text(decoded), "method": "json_text_fallback", "alternate_urls": [], "candidate_count": 1}
    candidates, metadata = _json_candidates(payload)
    selected = max(candidates, key=lambda row: (row.priority, min(len(row.text), 250_000)), default=TextCandidate("", "empty_json", 0))
    return {
        "title": metadata.get("headline") or metadata.get("title") or metadata.get("name") or "",
        "author": metadata.get("author") or "",
        "publication_date": metadata.get("datepublished") or metadata.get("publishedat") or "",
        "text": clean_text(selected.text),
        "method": selected.method,
        "alternate_urls": [],
        "candidate_count": len(candidates),
    }


def _xml_document(body: bytes, content_type: str) -> dict[str, Any]:
    decoded = _decode_body(body, content_type)
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=False)
    try:
        root = etree.fromstring(decoded.encode("utf-8", errors="replace"), parser=parser)
    except etree.XMLSyntaxError:
        return {"title": "", "author": "", "publication_date": "", "text": clean_text(decoded), "method": "xml_text_fallback", "alternate_urls": [], "candidate_count": 1}
    if root is None:
        return {"title": "", "author": "", "publication_date": "", "text": "", "method": "invalid_xml", "alternate_urls": [], "candidate_count": 0}
    values = root.xpath(
        "//*[local-name()='entry' or local-name()='item']/*[local-name()='content' or "
        "local-name()='encoded' or local-name()='description' or local-name()='summary']/text()"
    )
    title = clean_text(" ".join(root.xpath("(//*[local-name()='entry' or local-name()='item']/*[local-name()='title']/text())[1]")))
    text = _unique_text(str(value) for value in values)
    if "<" in text and ">" in text:
        try:
            text = clean_text(html.fromstring("<main>" + text + "</main>").text_content())
        except (etree.ParserError, ValueError):
            pass
    return {"title": title, "author": "", "publication_date": "", "text": text, "method": "rss_atom", "alternate_urls": [], "candidate_count": len(values)}


def _docx_document(body: bytes) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            raw = archive.read("word/document.xml")
        root = etree.fromstring(raw, parser=etree.XMLParser(resolve_entities=False, no_network=True))
        paragraphs = _unique_text(
            "".join(node.itertext())
            for node in root.xpath("//*[local-name()='p']")
        )
    except (KeyError, zipfile.BadZipFile, etree.XMLSyntaxError):
        paragraphs = ""
    return {"title": "", "author": "", "publication_date": "", "text": paragraphs, "method": "docx_xml", "alternate_urls": [], "candidate_count": int(bool(paragraphs))}


def extract_public_text(
    body: bytes,
    content_type: str,
    final_url: str,
) -> dict[str, Any]:
    """Extract public textual content without executing untrusted page code."""
    lowered_type = (content_type or "").casefold()
    path = urllib.parse.urlsplit(final_url).path.casefold()
    stripped = body.lstrip()
    if (
        "json" in lowered_type
        or path.endswith((".json", ".jsonld"))
        or stripped.startswith((b"{", b"["))
    ):
        return _json_document(body, content_type)
    if (
        "wordprocessingml" in lowered_type
        or path.endswith(".docx")
        or (body.startswith(b"PK") and b"word/" in body[:200_000])
    ):
        return _docx_document(body)
    if (
        any(token in lowered_type for token in ("rss", "atom", "xml"))
        or path.endswith((".rss", ".atom", ".xml"))
        or stripped[:80].lower().startswith((b"<?xml", b"<rss", b"<feed"))
    ):
        return _xml_document(body, content_type)
    if "html" in lowered_type or body.lstrip().startswith((b"<!doctype", b"<html", b"<HTML")):
        return _html_document(body, content_type, final_url)
    if "rtf" in lowered_type or path.endswith(".rtf"):
        decoded = _decode_body(body, content_type)
        text = re.sub(r"\\'[0-9a-fA-F]{2}|\\[a-zA-Z]+-?\d* ?|[{}]", " ", decoded)
        return {"title": "", "author": "", "publication_date": "", "text": clean_text(text), "method": "rtf_plain", "alternate_urls": [], "candidate_count": 1}
    binary_magic = (
        b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"RIFF", b"ID3",
        b"\x1aE\xdf\xa3", b"\x00\x00\x00\x18ftyp", b"\x00\x00\x00\x20ftyp",
    )
    if stripped.startswith(binary_magic) or b"\x00" in body[:4096]:
        return {"title": "", "author": "", "publication_date": "", "text": "", "method": "binary_no_public_text", "alternate_urls": [], "candidate_count": 0}
    decoded = _decode_body(body, content_type)
    return {"title": "", "author": "", "publication_date": "", "text": clean_text(decoded), "method": "plain_text", "alternate_urls": [], "candidate_count": 1}
