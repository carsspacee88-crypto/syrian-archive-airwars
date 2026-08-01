from __future__ import annotations

import csv
import io
import json
import re
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

from .io_utils import clean_text


MAX_OFFICE_ENTRIES = 2_000
MAX_OFFICE_UNCOMPRESSED_BYTES = 64 * 1024 * 1024


def _charset(content_type: str) -> str:
    match = re.search(r"charset\s*=\s*['\"]?([^;'\"\s]+)", content_type, flags=re.I)
    return match.group(1).strip() if match else "utf-8"


def _decode(body: bytes, content_type: str) -> str:
    encodings = [_charset(content_type), "utf-8-sig", "utf-16", "windows-1256", "cp1252"]
    for encoding in dict.fromkeys(encodings):
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def _json_text(value: Any, path: str = "$") -> list[str]:
    lines: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            lines.extend(_json_text(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            lines.extend(_json_text(child, f"{path}[{index}]"))
    elif value is not None:
        rendered = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        lines.append(f"{path}: {rendered}")
    return lines


def _xml_text(body: bytes) -> str:
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=False)
    root = etree.fromstring(body, parser=parser)
    values = [clean_text(value) for value in root.itertext()]
    return "\n\n".join(value for value in values if value)


def _csv_text(text: str, delimiter: str | None = None) -> str:
    sample = text[:8192]
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    rows = csv.reader(io.StringIO(text), delimiter=delimiter)
    return "\n".join(" | ".join(clean_text(cell) for cell in row) for row in rows)


def _office_text(body: bytes) -> tuple[str, str]:
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_OFFICE_ENTRIES:
            raise ValueError("office_archive_has_too_many_entries")
        if sum(info.file_size for info in infos) > MAX_OFFICE_UNCOMPRESSED_BYTES:
            raise ValueError("office_archive_uncompressed_size_exceeded")
        names = {info.filename for info in infos}
        if "word/document.xml" in names:
            family = "docx"
            selected = sorted(
                name
                for name in names
                if name == "word/document.xml"
                or re.fullmatch(r"word/(header|footer)\d+\.xml", name)
            )
        elif "ppt/presentation.xml" in names:
            family = "pptx"
            selected = sorted(name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name))
        elif "xl/workbook.xml" in names:
            family = "xlsx"
            selected = sorted(
                name
                for name in names
                if name == "xl/sharedStrings.xml" or re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
            )
        else:
            raise ValueError("unsupported_zip_container")
        sections = [_xml_text(archive.read(name)) for name in selected]
        return family, "\n\n".join(section for section in sections if section)


def detect_format(body: bytes, content_type: str, final_url: str, source_type: str) -> str:
    mime = content_type.split(";", 1)[0].strip().casefold()
    suffix = Path(urllib.parse.urlsplit(final_url).path.casefold()).suffix
    stripped = body.lstrip()
    if body.startswith(b"%PDF-") or "pdf" in mime or source_type == "pdf_document":
        return "pdf"
    if body.startswith(b"PK\x03\x04") or suffix in {".docx", ".xlsx", ".pptx"}:
        return "office_open_xml"
    if mime in {"application/json", "application/ld+json", "application/geo+json"} or suffix == ".json":
        return "json"
    if mime in {"application/xml", "text/xml", "application/rss+xml", "application/atom+xml"} or suffix in {".xml", ".rss", ".atom"}:
        return "xml"
    if "html" in mime or stripped[:32].lower().startswith((b"<!doctype html", b"<html")):
        return "html"
    if mime in {"text/csv", "application/csv"} or suffix == ".csv":
        return "csv"
    if mime in {"text/tab-separated-values"} or suffix == ".tsv":
        return "tsv"
    if mime.startswith("text/") or suffix in {".txt", ".md", ".markdown", ".log"}:
        return "text"
    if stripped.startswith((b"{", b"[")):
        return "json"
    if stripped.startswith(b"<"):
        return "xml"
    return "unsupported"


def extract_payload(
    body: bytes,
    content_type: str,
    final_url: str,
    source_type: str,
) -> dict[str, Any]:
    """Extract textual content without retaining the downloaded binary."""

    detected = detect_format(body, content_type, final_url, source_type)
    base: dict[str, Any] = {
        "title": "",
        "author": "",
        "publication_date": "",
        "text": "",
        "format": detected,
    }
    if detected == "html":
        from .pilot import _extract_html

        base.update(_extract_html(body, final_url))
    elif detected == "pdf":
        from .pilot import _extract_pdf

        base.update(_extract_pdf(body))
    elif detected == "json":
        payload = json.loads(_decode(body, content_type))
        base["text"] = "\n".join(_json_text(payload))
    elif detected == "xml":
        base["text"] = _xml_text(body)
    elif detected in {"csv", "tsv"}:
        base["text"] = _csv_text(_decode(body, content_type), "\t" if detected == "tsv" else None)
    elif detected == "office_open_xml":
        family, text = _office_text(body)
        base["format"] = family
        base["text"] = text
    elif detected == "text":
        base["text"] = _decode(body, content_type)
    return base
