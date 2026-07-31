from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from lxml import html

from . import PILOT_PARSER_VERSION, PILOT_SCHEMA_VERSION, TRANSLATION_VERSION
from .collector import collect_one
from .fetcher import FetchResult, RespectfulFetcher
from .io_utils import atomic_write_json, clean_text, load_json, sha256_bytes, sha256_text, utc_now
from .legacy import LegacyArchive


PILOT_FIRST_SEQUENCE = 1
PILOT_LAST_SEQUENCE = 100
PILOT_SEQUENCES = tuple(range(PILOT_FIRST_SEQUENCE, PILOT_LAST_SEQUENCE + 1))
TRANSLATION_MODEL = "gpt-5.6-sol"
SOURCE_ID_HEX_LENGTH = 24

DIRECT_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".tif", ".tiff", ".svg"}
DIRECT_VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
DIRECT_AUDIO_SUFFIXES = {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac"}
SOCIAL_DOMAINS = {
    "facebook.com": "public_facebook_post",
    "x.com": "public_x_post",
    "twitter.com": "public_x_post",
    "instagram.com": "public_instagram_post",
    "t.me": "public_telegram_post",
    "telegram.me": "public_telegram_post",
    "youtube.com": "youtube_page",
    "youtu.be": "youtube_page",
}
LOGIN_WALL_MARKERS = (
    "log in to continue", "login to continue", "sign in to continue",
    "log into facebook", "log in to facebook", "تسجيل الدخول للمتابعة",
    "سجّل الدخول للمتابعة", "قم بتسجيل الدخول للمتابعة",
)


def _exact_pilot_sequence(value: Any) -> int:
    sequence = int(value)
    if sequence not in PILOT_SEQUENCES:
        raise ValueError(f"pilot_scope_violation:{sequence:04d}; only 0001-0100 are allowed")
    return sequence


def normalize_source_url(raw_url: str) -> str:
    """Conservative normalization: preserve path/query; normalize only transport syntax."""
    value = (raw_url or "").strip()
    if not value:
        return ""
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return value
    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold().rstrip(".")
    port = parsed.port
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = parsed.path or "/"
    return urllib.parse.urlunsplit((scheme, netloc, path, parsed.query, ""))


def stable_source_id(raw_url: str) -> str:
    normalized = normalize_source_url(raw_url)
    if not normalized:
        raise ValueError("source_url_required_for_stable_id")
    return f"source-{sha256_text(normalized)[:SOURCE_ID_HEX_LENGTH]}"


def stable_media_id(source_id: str, url: str, fallback: str) -> str:
    identity = f"{source_id}\n{normalize_source_url(url) or fallback}"
    return f"media-{sha256_text(identity)[:SOURCE_ID_HEX_LENGTH]}"


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").casefold().removeprefix("www.")


def classify_source_type(url: str, publisher: str = "") -> str:
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path.casefold()
    suffix = Path(path).suffix
    host = _host(url)
    for domain, source_type in SOCIAL_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return source_type
    if suffix == ".pdf":
        return "pdf_document"
    if suffix in DIRECT_IMAGE_SUFFIXES:
        return "direct_image_url"
    if suffix in DIRECT_VIDEO_SUFFIXES:
        return "direct_video_url"
    if suffix in DIRECT_AUDIO_SUFFIXES:
        return "direct_audio_url"
    combined = f"{host} {publisher}".casefold()
    if any(token in combined for token in ("gov", "mil", "وزارة", "government", "defense", "ministry")):
        return "government_or_military_statement"
    if any(token in combined for token in ("hrw", "amnesty", "ngo", "observatory", "المرصد", "حقوق")):
        return "ngo_report"
    if any(token in combined for token in ("blog", "wordpress", "blogspot")):
        return "blog_post"
    if host:
        return "news_article" if any(token in combined for token in ("news", "media", "press", "أخبار", "اخبار", "agency")) else "other_web_page"
    return "unknown"


def detect_language(text: str, declared: str = "") -> str:
    declared_lower = clean_text(declared).casefold()
    declared_codes = (
        (("arab", "العرب"), "ar"), (("english",), "en"), (("turkish", "türk"), "tr"),
        (("kurdish", "kurdî"), "ku"), (("persian", "farsi"), "fa"),
        (("hebrew",), "he"), (("russian",), "ru"), (("french",), "fr"),
        (("spanish",), "es"), (("german",), "de"),
    )
    for tokens, code in declared_codes:
        if any(token in declared_lower for token in tokens) or declared_lower == code:
            return code
    letters = re.findall(r"[^\W\d_]", text, flags=re.UNICODE)
    if not letters:
        return "und"
    arabic = sum(1 for char in letters if "\u0600" <= char <= "\u06ff" or "\u0750" <= char <= "\u077f")
    if arabic / len(letters) >= 0.35:
        return "ar"
    hebrew = sum(1 for char in letters if "\u0590" <= char <= "\u05ff")
    if hebrew / len(letters) >= 0.35:
        return "he"
    cyrillic = sum(1 for char in letters if "\u0400" <= char <= "\u04ff")
    if cyrillic / len(letters) >= 0.35:
        return "ru"
    latin = sum(1 for char in letters if ("A" <= char <= "Z") or ("a" <= char <= "z"))
    return "en" if latin / len(letters) >= 0.7 else "und"


def _strip_input_marker(value: str) -> str:
    return re.sub(r"^\s*\[Input\]\s*", "", clean_text(value), flags=re.IGNORECASE)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return float(ordered[rank])


def _tree_bytes(root: Path, predicate: Callable[[Path], bool] | None = None) -> int:
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file() and (predicate is None or predicate(path)):
            total += path.stat().st_size
    return total


def _append_unique(items: list[Any], value: Any, key: Callable[[Any], Any] | None = None) -> None:
    identity = key(value) if key else value
    if all((key(item) if key else item) != identity for item in items):
        items.append(value)


def _record_exact_url_observations(
    exact_urls: dict[str, list[str]],
    legacy_seeds: list[dict[str, Any]],
    normalized_seeds: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Count each historical source row once, excluding its normalized mirror."""
    legacy_keys: set[tuple[str, str]] = set()
    for seed in legacy_seeds:
        original_url = seed.get("original_url") or ""
        normalized_url = normalize_source_url(original_url)
        if not normalized_url:
            continue
        legacy_keys.add((normalized_url, str(seed.get("airwars_source_id") or "")))
        exact_urls.setdefault(normalized_url, []).append(original_url)
    for seed in normalized_seeds:
        original_url = seed.get("original_url") or ""
        normalized_url = normalize_source_url(original_url)
        if not normalized_url:
            continue
        key = (normalized_url, str(seed.get("airwars_source_id") or ""))
        if key not in legacy_keys:
            exact_urls.setdefault(normalized_url, []).append(original_url)
    return legacy_seeds + normalized_seeds


def _legacy_source_seed(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "airwars_source_id": str(row.get("معرّف المصدر") or ""),
        "publisher": row.get("اسم المصدر — الأصل") or row.get("اسم المصدر — عربي") or "",
        "publisher_ar": row.get("اسم المصدر — عربي") or "",
        "author": row.get("كاتب/صاحب المصدر") or "",
        "publication_date": row.get("تاريخ المصدر") or "",
        "declared_language": row.get("اللغة/المنصة — الأصل") or row.get("اللغة/المنصة — عربي") or "",
        "original_url": row.get("رابط المصدر") or "",
        "archived_urls": [row["رابط الأرشيف"]] if row.get("رابط الأرشيف") else [],
        "legacy_metadata": deepcopy(row),
        "content": "",
        "content_translated": "",
        "provenance": "legacy_import",
    }


def _normalized_source_seed(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "airwars_source_id": str(row.get("source_id") or ""),
        "publisher": row.get("name") or row.get("name_original") or row.get("name_ar") or "",
        "publisher_ar": row.get("name_ar") or "",
        "author": row.get("author") or "",
        "publication_date": row.get("date") or row.get("published_date") or "",
        "declared_language": row.get("language") or row.get("platform_original") or row.get("platform_ar") or "",
        "original_url": row.get("url") or "",
        "archived_urls": [row["archive_url"]] if row.get("archive_url") else [],
        "content": _strip_input_marker(row.get("content") or ""),
        "content_translated": _strip_input_marker(row.get("content_translated") or ""),
        "provenance": row.get("provenance") or "airwars_live",
    }


def _source_variant(text: str, provenance: str, source_url: str, retrieved_at: str | None) -> dict[str, Any]:
    return {
        "text": text,
        "sha256": sha256_text(text),
        "bytes": len(text.encode("utf-8")),
        "provenance": provenance,
        "source_url": source_url,
        "retrieved_at": retrieved_at,
    }


def _classify_fetch_failure(status: int | None, error: str | None, body_text: str = "") -> str:
    lower = f"{error or ''} {body_text[:1000]}".casefold()
    if status == 404:
        return "not_found"
    if status == 410:
        return "gone"
    if status in {401} or any(token in lower for token in ("login required", "log in to continue", "sign in to continue")):
        return "login_required"
    if status in {403, 429, 451}:
        return "blocked"
    if "timeout" in lower or "timed out" in lower:
        return "timed_out"
    return "unavailable"


def _extract_html(body: bytes, final_url: str) -> dict[str, str]:
    decoded = body.decode("utf-8", errors="replace")
    document = html.fromstring(decoded, base_url=final_url)
    title = clean_text(" ".join(document.xpath("//title/text()")))
    author = clean_text(" ".join(document.xpath('//meta[@name="author"]/@content | //meta[@property="article:author"]/@content')))
    publication_date = clean_text(" ".join(document.xpath(
        '//meta[@property="article:published_time"]/@content | //meta[@name="date"]/@content | //time/@datetime'
    )))
    for node in document.xpath("//script|//style|//noscript|//template|//nav|//footer|//header|//form|//aside"):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
    candidates = document.xpath("//article | //main | //*[@role='main']")
    root = max(candidates, key=lambda node: len(clean_text(node.text_content())), default=document)
    blocks: list[str] = []
    for node in root.xpath(".//h1|.//h2|.//h3|.//p|.//blockquote|.//li|.//figcaption|.//pre"):
        value = clean_text(node.text_content())
        if value:
            blocks.append(value)
    text = "\n\n".join(blocks)
    if len(text) < 120:
        text = clean_text(root.text_content())
    return {"title": title, "author": author, "publication_date": publication_date, "text": text}


def _extract_pdf(body: bytes) -> dict[str, Any]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(body))
    text = "\n\n".join(clean_text(page.extract_text() or "") for page in reader.pages).strip()
    metadata = reader.metadata or {}
    return {
        "title": clean_text(metadata.get("/Title") or ""),
        "author": clean_text(metadata.get("/Author") or ""),
        "publication_date": clean_text(metadata.get("/CreationDate") or ""),
        "text": text,
        "page_count": len(reader.pages),
        "ocr_pending": not bool(text),
    }


class ArabicTranslator:
    def __init__(self, model: str = TRANSLATION_MODEL, timeout: float = 120.0, retries: int = 3):
        self.openai_api_key = os.environ.get("OPENAI_API_KEY", "")
        self.deepl_api_key = os.environ.get("DEEPL_API_KEY", "")
        requested_provider = os.environ.get("TRANSLATION_PROVIDER", "disabled").strip().casefold()
        if requested_provider not in {"auto", "openai", "deepl", "disabled"}:
            raise ValueError(f"unsupported_translation_provider:{requested_provider}")
        if requested_provider == "auto":
            requested_provider = "deepl" if self.deepl_api_key else "openai"
        self.provider = requested_provider
        self.model = "none" if self.provider == "disabled" else ("deepl-text-v2" if self.provider == "deepl" else model)
        self.timeout = timeout
        self.retries = retries
        self.retry_count = 0
        self.waiting_seconds = 0.0

    @staticmethod
    def chunks(text: str, max_chars: int = 7000) -> list[str]:
        paragraphs = re.split(r"\n\s*\n", clean_text(text))
        chunks: list[str] = []
        current = ""
        for paragraph in paragraphs:
            pieces = [paragraph[index:index + max_chars] for index in range(0, len(paragraph), max_chars)] or [""]
            for piece in pieces:
                candidate = f"{current}\n\n{piece}".strip() if current else piece
                if current and len(candidate) > max_chars:
                    chunks.append(current)
                    current = piece
                else:
                    current = candidate
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _output_text(payload: dict[str, Any]) -> str:
        if payload.get("output_text"):
            return clean_text(payload["output_text"])
        values: list[str] = []
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    values.append(content["text"])
        return clean_text("\n".join(values))

    def _translate_openai(self, text: str) -> tuple[str, dict[str, Any]]:
        if not self.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        request_body = {
            "model": self.model,
            "reasoning": {"effort": "none"},
            "text": {"verbosity": "low"},
            "input": [
                {"role": "system", "content": (
                    "Translate the supplied archival source text completely into Modern Standard Arabic. "
                    "Do not summarize, omit, interpret, add legal conclusions, or merge people. Preserve paragraph order, "
                    "names in their original spellings, dates, numbers, quotations, repetitions, conflicts, and uncertainty markers. "
                    "Return only the Arabic translation."
                )},
                {"role": "user", "content": text},
            ],
            "max_output_tokens": 12000,
        }
        encoded = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
        last_error = ""
        for attempt in range(1, self.retries + 1):
            request = urllib.request.Request(
                "https://api.openai.com/v1/responses",
                data=encoded,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.openai_api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "SyrianArchiveAirwars/first-100-pilot",
                },
            )
            started = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    translated = self._output_text(payload)
                    if not translated:
                        raise ValueError("empty_translation_response")
                    return translated, {
                        "provider": "openai",
                        "model": self.model,
                        "response_id": payload.get("id"),
                        "attempts": attempt,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "usage": payload.get("usage") or {},
                    }
            except urllib.error.HTTPError as error:
                body = error.read(4096).decode("utf-8", errors="replace")
                last_error = f"http_{error.code}:{body[:500]}"
                retry_after = float(error.headers.get("Retry-After", "0") or 0)
                if error.code not in {429, 500, 502, 503, 504}:
                    break
            except Exception as error:
                last_error = f"{type(error).__name__}:{error}"
                retry_after = 0.0
            if attempt < self.retries:
                self.retry_count += 1
                wait = max(retry_after, 2 ** (attempt - 1))
                time.sleep(min(wait, 60.0))
                self.waiting_seconds += min(wait, 60.0)
        raise RuntimeError(f"translation_failed:{last_error}")

    def _translate_deepl(self, text: str) -> tuple[str, dict[str, Any]]:
        if not self.deepl_api_key:
            raise RuntimeError("DEEPL_API_KEY is not configured")
        base_url = "https://api-free.deepl.com" if self.deepl_api_key.endswith(":fx") else "https://api.deepl.com"
        encoded = json.dumps({
            "text": [text],
            "target_lang": "AR",
            "preserve_formatting": True,
            "split_sentences": "1",
            "show_billed_characters": True,
        }, ensure_ascii=False).encode("utf-8")
        last_error = ""
        for attempt in range(1, self.retries + 1):
            request = urllib.request.Request(
                f"{base_url}/v2/translate",
                data=encoded,
                method="POST",
                headers={
                    "Authorization": f"DeepL-Auth-Key {self.deepl_api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "SyrianArchiveAirwars/first-100-pilot",
                },
            )
            started = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    translation = (payload.get("translations") or [{}])[0]
                    translated = clean_text(translation.get("text") or "")
                    if not translated:
                        raise ValueError("empty_translation_response")
                    return translated, {
                        "provider": "deepl",
                        "model": translation.get("model_type_used") or self.model,
                        "attempts": attempt,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "detected_source_language": translation.get("detected_source_language"),
                        "billed_characters": translation.get("billed_characters"),
                    }
            except urllib.error.HTTPError as error:
                body = error.read(4096).decode("utf-8", errors="replace")
                last_error = f"http_{error.code}:{body[:500]}"
                retry_after = float(error.headers.get("Retry-After", "0") or 0)
                if error.code not in {429, 500, 502, 503, 504, 529}:
                    break
            except Exception as error:
                last_error = f"{type(error).__name__}:{error}"
                retry_after = 0.0
            if attempt < self.retries:
                self.retry_count += 1
                wait = max(retry_after, 2 ** (attempt - 1))
                time.sleep(min(wait, 60.0))
                self.waiting_seconds += min(wait, 60.0)
        raise RuntimeError(f"translation_failed:{last_error}")

    def translate_chunk(self, text: str) -> tuple[str, dict[str, Any]]:
        if self.provider == "disabled":
            raise RuntimeError("translation_disabled_by_user")
        if self.provider == "deepl":
            return self._translate_deepl(text)
        return self._translate_openai(text)

    def preflight(self) -> dict[str, Any]:
        if self.provider == "disabled":
            return {
                "provider": "none",
                "model": "none",
                "result": "disabled_by_user",
                "checked_at": utc_now(),
            }
        translated, metadata = self.translate_chunk("This is a translation connectivity test.")
        return {
            "provider": self.provider,
            "model": self.model,
            "result": "successful",
            "translated_text_sha256": sha256_text(translated),
            "metadata": metadata,
            "checked_at": utc_now(),
        }

    def translate_record(
        self,
        record: dict[str, Any],
        text_field: str,
        arabic_field: str,
        state_field: str,
        checkpoint: Callable[[], None],
    ) -> None:
        if self.provider == "disabled":
            previous = record.get(state_field) or {}
            preexisting = bool(record.get(arabic_field))
            record[state_field] = {
                "status": "disabled_by_user",
                "version": "disabled-no-translation-v1",
                "provider": "none",
                "review_required": False,
                "generated_in_pilot": False,
                "preexisting_source_text_preserved": preexisting,
                "preexisting_version": previous.get("version") if preexisting else None,
                "reason": "Preserve retrieved text verbatim; no machine translation requested.",
                "chunks": [],
            }
            checkpoint()
            return
        text = clean_text(record.get(text_field) or "")
        if not text:
            record[arabic_field] = ""
            record[state_field] = {
                "status": "not_applicable",
                "version": TRANSLATION_VERSION,
                "provider": self.provider,
                "review_required": False,
                "chunks": [],
            }
            checkpoint()
            return
        language = detect_language(text, record.get("text_original_language") or record.get("narrative_original_language") or "")
        if language == "ar":
            record[arabic_field] = text
            record[state_field] = {
                "status": "not_required_arabic_original",
                "version": TRANSLATION_VERSION,
                "provider": self.provider,
                "review_required": False,
                "chunks": [],
            }
            checkpoint()
            return
        state = record.setdefault(state_field, {})
        state.setdefault("status", "in_progress")
        state.setdefault("version", TRANSLATION_VERSION)
        state.setdefault("provider", self.provider)
        state.setdefault("review_required", False)
        state.setdefault("chunks", [])
        outputs: list[str] = []
        chunks = self.chunks(text)
        for index, chunk in enumerate(chunks):
            input_hash = sha256_text(chunk)
            previous = next((item for item in state["chunks"] if item.get("index") == index and item.get("input_sha256") == input_hash and item.get("status") == "complete"), None)
            if previous:
                outputs.append(previous["text_ar"])
                continue
            started = time.monotonic()
            try:
                translated, metadata = self.translate_chunk(chunk)
                chunk_state = {
                    "index": index,
                    "input_sha256": input_hash,
                    "status": "complete",
                    "text_ar": translated,
                    "translated_at": utc_now(),
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "metadata": metadata,
                }
                outputs.append(translated)
            except Exception as error:
                chunk_state = {
                    "index": index,
                    "input_sha256": input_hash,
                    "status": "failed",
                    "error": f"{type(error).__name__}:{error}",
                    "failed_at": utc_now(),
                    "duration_seconds": round(time.monotonic() - started, 3),
                }
                state["status"] = "failed"
                state["review_required"] = True
            state["chunks"] = [item for item in state["chunks"] if item.get("index") != index]
            state["chunks"].append(chunk_state)
            state["chunks"].sort(key=lambda item: item["index"])
            checkpoint()
            if chunk_state["status"] != "complete":
                break
        complete = len(outputs) == len(chunks)
        record[arabic_field] = "\n\n".join(outputs)
        state["status"] = "complete" if complete else "incomplete"
        state["completed_at"] = utc_now() if complete else None
        state["review_required"] = not complete
        checkpoint()


class PilotRunner:
    def __init__(self, root: Path, legacy_zip: Path, delay: float, timeout: float, retries: int):
        self.root = root.resolve()
        self.legacy_zip = legacy_zip.resolve()
        self.progress_path = self.root / "data" / "pilot" / "first-100-progress.json"
        self.manifest_path = self.root / "data" / "pilot" / "first-100-manifest.json"
        self.progress = load_json(self.progress_path, {}) or {}
        self.progress.setdefault("pilot", "first-100-complete-content")
        self.progress.setdefault("scope", {"first_sequence": 1, "last_sequence": 100, "count": 100})
        baseline = load_json(self.root / "data" / "pilot" / "first-100-baseline.json", {}) or {}
        self.progress.setdefault("started_at", baseline.get("recorded_at_utc") or utc_now())
        self.progress.setdefault("incident_completed_sequences", [])
        self.progress.setdefault("source_completed_ids", [])
        self.progress.setdefault("translation_completed_records", [])
        self.progress.setdefault("incident_timings", [])
        self.progress.setdefault("source_timings", [])
        self.progress.setdefault("stage_runs", [])
        self.incident_fetcher = RespectfulFetcher(delay_seconds=delay, timeout_seconds=timeout, retries=retries)
        self.source_fetcher = RespectfulFetcher(delay_seconds=max(0.5, delay), timeout_seconds=min(timeout, 20.0), retries=max(1, min(retries, 2)), max_bytes=25 * 1024 * 1024)
        self.translator = ArabicTranslator(model=os.environ.get("OPENAI_TRANSLATION_MODEL", TRANSLATION_MODEL))
        self.source_host_failures: Counter[str] = Counter()
        self.suppressed_source_hosts: set[str] = set()

    def _source_fetch(self, url: str, accept: str, attempt_role: str) -> FetchResult:
        host = _host(url)
        if host in self.suppressed_source_hosts:
            return FetchResult(
                url=url,
                final_url=url,
                status=None,
                content_type="",
                body=b"",
                retrieved_at=utc_now(),
                elapsed_seconds=0.0,
                error="host_circuit_open_after_repeated_block_or_timeout",
                attempts=0,
                attempt_history=[{"attempt": 0, "result": "host_circuit_open", "host": host, "attempt_role": attempt_role}],
            )
        return self.source_fetcher.fetch(url, accept=accept)

    def _note_source_host_failure(self, url: str, failed: bool) -> None:
        host = _host(url)
        if not host:
            return
        if failed:
            self.source_host_failures[host] += 1
            if self.source_host_failures[host] >= 3:
                self.suppressed_source_hosts.add(host)
        else:
            self.source_host_failures[host] = 0

    def save_progress(self) -> None:
        self.progress["updated_at"] = utc_now()
        atomic_write_json(self.progress_path, self.progress)

    def _stage(self, name: str, function: Callable[[], Any]) -> Any:
        started_at = utc_now()
        started = time.monotonic()
        stage_result = "complete"
        payload: Any = None
        try:
            payload = function()
        except Exception:
            stage_result = "failed"
            raise
        finally:
            self.progress["stage_runs"].append({
                "stage": name,
                "started_at": started_at,
                "finished_at": utc_now(),
                "duration_seconds": round(time.monotonic() - started, 3),
                "result": stage_result,
            })
            self.save_progress()
        return payload

    def translation_preflight(self) -> dict[str, Any]:
        result = self.translator.preflight()
        self.progress["translation_preflight"] = result
        self.save_progress()
        return {"done": True, **result}

    def build_manifest(self) -> None:
        incidents: list[dict[str, Any]] = []
        with LegacyArchive(self.legacy_zip) as archive:
            for sequence in PILOT_SEQUENCES:
                summary = archive.summary_by_sequence(sequence)
                legacy = archive.case_data(sequence)
                airwars_id = str(summary.get("airwars_id") or legacy.get("case", {}).get("airwars_id") or "")
                canonical_url = summary.get("airwars_url") or legacy.get("incident", {}).get("رابط الحادثة") or ""
                if not airwars_id.isdigit() or not canonical_url.startswith("https://airwars.org/"):
                    raise ValueError(f"invalid_pilot_identifier:{sequence:04d}")
                incidents.append({
                    "sequence": sequence,
                    "sequence_padded": f"{sequence:04d}",
                    "internal_id": f"airwars-{airwars_id}",
                    "airwars_id": airwars_id,
                    "incident_code": summary.get("code") or legacy.get("incident", {}).get("رمز الحادثة"),
                    "canonical_url": canonical_url,
                })
        if [item["sequence"] for item in incidents] != list(PILOT_SEQUENCES):
            raise ValueError("pilot_manifest_not_exactly_0001_through_0100")
        manifest = {
            "schema_version": PILOT_SCHEMA_VERSION,
            "pilot": "first-100-complete-content",
            "scope": {"first_sequence": 1, "last_sequence": 100, "count": 100, "later_incidents_allowed": False},
            "created_at": utc_now(),
            "incidents": incidents,
        }
        atomic_write_json(self.manifest_path, manifest)

    def collect_incidents(self, max_items: int | None = None) -> dict[str, Any]:
        completed = {int(value) for value in self.progress["incident_completed_sequences"]}
        processed = 0
        with LegacyArchive(self.legacy_zip) as archive:
            for index, sequence in enumerate(PILOT_SEQUENCES, 1):
                _exact_pilot_sequence(sequence)
                manifest = load_json(self.manifest_path, {})
                item = manifest["incidents"][sequence - 1]
                path = self.root / "data" / "incidents" / f"{item['internal_id']}.json"
                if sequence in completed and path.is_file():
                    continue
                started = time.monotonic()
                status = "failed"
                error_text = ""
                try:
                    record = collect_one(archive, sequence, self.root, self.incident_fetcher)
                    legacy = archive.case_data(sequence)
                    record["pilot"] = {
                        "name": "first-100-complete-content",
                        "in_scope": True,
                        "sequence": sequence,
                        "parser_version": PILOT_PARSER_VERSION,
                    }
                    record["legacy_incident_fields"] = legacy.get("incident", {})
                    record["legacy_page_fields"] = legacy.get("page_fields", [])
                    record["legacy_page_sections"] = legacy.get("page_sections", [])
                    record["additional_notes"] = legacy.get("incident", {}).get("ملاحظات تصنيفية — الأصل") or ""
                    record["narrative_original"] = record.get("narrative") or ""
                    record["narrative_original_language"] = detect_language(record["narrative_original"])
                    record.setdefault("narrative_ar", "")
                    record.setdefault("translation", {})
                    atomic_write_json(path, record)
                    status = record.get("completeness_status") or "partial"
                except Exception as error:
                    error_text = f"{type(error).__name__}:{error}"
                duration = round(time.monotonic() - started, 3)
                self.progress["incident_timings"] = [row for row in self.progress["incident_timings"] if row.get("sequence") != sequence]
                self.progress["incident_timings"].append({
                    "sequence": sequence,
                    "internal_id": item["internal_id"],
                    "duration_seconds": duration,
                    "status": status,
                    "error": error_text or None,
                    "finished_at": utc_now(),
                })
                if sequence not in self.progress["incident_completed_sequences"]:
                    self.progress["incident_completed_sequences"].append(sequence)
                    self.progress["incident_completed_sequences"].sort()
                self.save_progress()
                print(f"incident [{index}/100] {sequence:04d} -> {status}", flush=True)
                processed += 1
                if max_items is not None and processed >= max_items:
                    break
        completed_count = len({int(value) for value in self.progress["incident_completed_sequences"]})
        return {"done": completed_count == len(PILOT_SEQUENCES), "processed": processed, "completed": completed_count, "total": len(PILOT_SEQUENCES)}

    def _source_seeds(self) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]], dict[str, list[str]]]:
        records: dict[str, dict[str, Any]] = {}
        incident_sources: dict[str, list[str]] = defaultdict(list)
        exact_urls: dict[str, list[str]] = defaultdict(list)
        with LegacyArchive(self.legacy_zip) as archive:
            for sequence in PILOT_SEQUENCES:
                manifest_item = load_json(self.manifest_path, {})["incidents"][sequence - 1]
                incident_id = manifest_item["internal_id"]
                normalized = load_json(self.root / "data" / "incidents" / f"{incident_id}.json", {})
                legacy_seeds = [
                    _legacy_source_seed(raw)
                    for raw in archive.case_data(sequence).get("sources", [])
                ]
                normalized_seeds = [
                    _normalized_source_seed(raw)
                    for raw in normalized.get("sources") or []
                ]
                source_seeds = _record_exact_url_observations(exact_urls, legacy_seeds, normalized_seeds)
                for seed in source_seeds:
                    original_url = seed["original_url"]
                    if not original_url:
                        continue
                    source_id = stable_source_id(original_url)
                    if source_id not in records:
                        records[source_id] = {
                            "schema_version": PILOT_SCHEMA_VERSION,
                            "parser_version": PILOT_PARSER_VERSION,
                            "translation_version": TRANSLATION_VERSION,
                            "source_id": source_id,
                            "stable_id_basis": normalize_source_url(original_url),
                            "incident_ids": [],
                            "incident_sequences": [],
                            "airwars_source_ids": [],
                            "publisher": seed["publisher"],
                            "publisher_ar": seed["publisher_ar"],
                            "source_type": classify_source_type(original_url, seed["publisher"]),
                            "original_url": original_url,
                            "normalized_url": normalize_source_url(original_url),
                            "observed_original_urls": [],
                            "final_redirected_url": "",
                            "archived_urls": [],
                            "page_title": "",
                            "author": seed["author"],
                            "publication_date": seed["publication_date"],
                            "text_original": "",
                            "text_original_language": detect_language(seed["content"], seed["declared_language"]),
                            "text_ar": "",
                            "content_hash": None,
                            "content_variants": [],
                            "captions": [],
                            "descriptions": [],
                            "retrieval_status": "pending",
                            "extraction_status": "pending",
                            "translation": {"status": "pending", "version": TRANSLATION_VERSION, "review_required": False, "chunks": []},
                            "preservation_status": "metadata_only",
                            "retrieved_at": None,
                            "attempt_history": [],
                            "provenance": [],
                            "failure_reason": None,
                            "review_flags": [],
                            "pdf": None,
                        }
                    record = records[source_id]
                    _append_unique(record["incident_ids"], incident_id)
                    _append_unique(record["incident_sequences"], sequence)
                    if seed["airwars_source_id"]:
                        _append_unique(record["airwars_source_ids"], seed["airwars_source_id"])
                    _append_unique(record["observed_original_urls"], original_url)
                    for archive_url in seed["archived_urls"]:
                        _append_unique(record["archived_urls"], archive_url)
                    content = seed["content"]
                    if content:
                        variant = _source_variant(content, seed["provenance"], manifest_item["canonical_url"], normalized.get("retrieved_at"))
                        _append_unique(record["content_variants"], variant, key=lambda item: item["sha256"])
                        if not record["text_original"]:
                            record["text_original"] = content
                            record["content_hash"] = variant["sha256"]
                            record["preservation_status"] = "preserved_in_airwars_incident_page"
                            record["extraction_status"] = "airwars_embedded_text"
                    translated = seed["content_translated"]
                    if translated and detect_language(translated) == "ar" and not record["text_ar"]:
                        record["text_ar"] = translated
                        record["translation"] = {
                            "status": "complete_existing_airwars_translation",
                            "version": "airwars_embedded",
                            "review_required": True,
                            "chunks": [],
                        }
                    _append_unique(record["provenance"], {
                        "source_type": seed["provenance"],
                        "incident_id": incident_id,
                        "incident_sequence": sequence,
                        "airwars_source_id": seed["airwars_source_id"] or None,
                        "observed_original_url": original_url,
                        "airwars_incident_url": manifest_item["canonical_url"],
                    }, key=lambda item: (item["incident_id"], item["source_type"], item.get("airwars_source_id"), item.get("observed_original_url")))
                    _append_unique(incident_sources[incident_id], source_id)
        return records, incident_sources, exact_urls

    def _collect_source(self, record: dict[str, Any]) -> None:
        source_type = record["source_type"]
        if source_type in {"direct_image_url", "direct_video_url", "direct_audio_url"}:
            record["retrieval_status"] = "unsupported_content_type"
            record["extraction_status"] = "media_metadata_only"
            record["preservation_status"] = "external_only"
            record["failure_reason"] = "media_binary_download_prohibited"
            record["attempt_history"].append({
                "attempted_at": utc_now(), "url": record["original_url"], "result": "not_downloaded_by_media_policy"
            })
            return
        accept = "text/html,application/pdf,text/plain;q=0.9,*/*;q=0.1"
        result = self._source_fetch(record["original_url"], accept, "live_source")
        metadata = result.metadata()
        metadata["ok"] = result.ok
        metadata["attempt_role"] = "live_source"
        record["attempt_history"].append(metadata)
        record["retrieved_at"] = result.retrieved_at
        record["final_redirected_url"] = result.final_url
        retrieval_provenance = "source_live"
        live_preview = result.body[:128 * 1024].decode("utf-8", errors="ignore").casefold() if result.body else ""
        access_wall = source_type.startswith("public_") and any(marker in live_preview for marker in LOGIN_WALL_MARKERS)
        live_failure = (not result.ok) or access_wall
        live_taxonomy = "login_required" if access_wall else _classify_fetch_failure(result.status, result.error, live_preview)
        self._note_source_host_failure(record["original_url"], live_failure and live_taxonomy in {"blocked", "timed_out", "login_required", "unavailable"})
        if not result.ok or access_wall:
            for archive_url in record.get("archived_urls") or []:
                archive_result = self._source_fetch(archive_url, accept, "listed_archive")
                archive_metadata = archive_result.metadata()
                archive_metadata["ok"] = archive_result.ok
                archive_metadata["attempt_role"] = "listed_archive"
                record["attempt_history"].append(archive_metadata)
                archive_taxonomy = _classify_fetch_failure(archive_result.status, archive_result.error)
                self._note_source_host_failure(archive_url, (not archive_result.ok) and archive_taxonomy in {"blocked", "timed_out", "unavailable"})
                if archive_result.ok:
                    result = archive_result
                    retrieval_provenance = "listed_archive"
                    access_wall = False
                    record["retrieved_at"] = result.retrieved_at
                    break
        if (not result.ok or access_wall) and not record["text_original"]:
            if "web.archive.org" in self.suppressed_source_hosts:
                capture = None
                lookup = {"ok": False, "error": "host_circuit_open_after_repeated_block_or_timeout", "retrieved_at": utc_now()}
            else:
                capture = self.source_fetcher.latest_wayback_capture(record["original_url"])
                lookup = deepcopy(self.source_fetcher.last_wayback_lookup or {})
                lookup_taxonomy = _classify_fetch_failure(lookup.get("status"), lookup.get("error"))
                self._note_source_host_failure("https://web.archive.org/", (not lookup.get("ok")) and lookup_taxonomy in {"blocked", "timed_out", "unavailable"})
            lookup["attempt_role"] = "wayback_lookup"
            record["attempt_history"].append(lookup)
            if capture:
                archive_result = self._source_fetch(capture["replay_url"], accept, "wayback_capture")
                archive_metadata = archive_result.metadata()
                archive_metadata["ok"] = archive_result.ok
                archive_metadata["attempt_role"] = "wayback_capture"
                archive_metadata["capture"] = capture
                record["attempt_history"].append(archive_metadata)
                if archive_result.ok:
                    result = archive_result
                    retrieval_provenance = "wayback_capture"
                    access_wall = False
                    _append_unique(record["archived_urls"], capture["replay_url"])
                    record["retrieved_at"] = result.retrieved_at
            elif lookup.get("ok"):
                record["retrieval_status"] = "no_archive_capture"
            else:
                record["retrieval_status"] = "archive_lookup_failed"
        if not result.ok or access_wall:
            if record.get("retrieval_status") not in {"no_archive_capture", "archive_lookup_failed"}:
                record["retrieval_status"] = "login_required" if access_wall else _classify_fetch_failure(result.status, result.error, result.body.decode("utf-8", errors="ignore"))
            record["failure_reason"] = result.error or record["retrieval_status"]
            if record["text_original"]:
                record["preservation_status"] = "preserved_in_airwars_incident_page"
            return
        record["retrieval_status"] = "successful"
        record["final_redirected_url"] = result.final_url
        content_type = result.content_type.casefold()
        extracted: dict[str, Any]
        extraction_started = time.monotonic()
        try:
            if "pdf" in content_type or source_type == "pdf_document":
                extracted = _extract_pdf(result.body)
                record["pdf"] = {
                    "byte_size": len(result.body),
                    "sha256": sha256_bytes(result.body),
                    "page_count": extracted.get("page_count"),
                    "ocr_pending": extracted.get("ocr_pending", False),
                    "binary_committed": False,
                }
                record["extraction_status"] = "ocr_pending" if extracted.get("ocr_pending") else "text_extracted"
            elif "html" in content_type or result.body.lstrip().startswith((b"<!doctype", b"<html", b"<HTML")):
                extracted = _extract_html(result.body, result.final_url)
                record["extraction_status"] = "text_extracted" if extracted["text"] else "parsing_failed"
            elif "text/" in content_type:
                extracted = {"title": "", "author": "", "publication_date": "", "text": clean_text(result.body.decode("utf-8", errors="replace"))}
                record["extraction_status"] = "text_extracted"
            else:
                record["retrieval_status"] = "unsupported_content_type"
                record["extraction_status"] = "media_metadata_only"
                record["failure_reason"] = f"unsupported_content_type:{result.content_type}"
                return
        except Exception as error:
            record["extraction_status"] = "parsing_failed"
            record["failure_reason"] = f"{type(error).__name__}:{error}"
            record["review_flags"].append("source_parser_failed")
            record.setdefault("timing", {})["text_extraction_seconds"] = round(time.monotonic() - extraction_started, 6)
            return
        record.setdefault("timing", {})["text_extraction_seconds"] = round(time.monotonic() - extraction_started, 6)
        record["page_title"] = extracted.get("title") or record["page_title"]
        record["author"] = extracted.get("author") or record["author"]
        record["publication_date"] = extracted.get("publication_date") or record["publication_date"]
        live_text = clean_text(extracted.get("text") or "")
        if live_text:
            variant = _source_variant(live_text, retrieval_provenance, result.final_url, result.retrieved_at)
            _append_unique(record["content_variants"], variant, key=lambda item: item["sha256"])
            if not record["text_original"]:
                record["text_original"] = live_text
                record["content_hash"] = variant["sha256"]
                record["preservation_status"] = "live_text_preserved" if retrieval_provenance == "source_live" else "archived_text_preserved"
            elif record["content_hash"] != variant["sha256"]:
                record["review_flags"].append("content_variants_require_review")
        if record["text_original"]:
            record["text_original_language"] = detect_language(record["text_original"], record["text_original_language"])

    def collect_sources(self, max_items: int | None = None) -> dict[str, Any]:
        seeds, incident_sources, exact_urls = self._source_seeds()
        completed = set(self.progress["source_completed_ids"])
        sources_root = self.root / "data" / "sources"
        processed = 0
        for index, source_id in enumerate(sorted(seeds), 1):
            path = sources_root / f"{source_id}.json"
            if source_id in completed and path.is_file():
                continue
            previous = load_json(path, {}) or {}
            if previous.get("retrieval_status") == "successful" and previous.get("text_original"):
                if source_id not in self.progress["source_completed_ids"]:
                    self.progress["source_completed_ids"].append(source_id)
                    self.save_progress()
                continue
            record = seeds[source_id]
            if previous:
                record["attempt_history"] = list(previous.get("attempt_history") or [])
                for field in ("text_original", "text_ar", "content_hash", "translation", "content_variants"):
                    if previous.get(field):
                        record[field] = deepcopy(previous[field])
            started = time.monotonic()
            self._collect_source(record)
            duration = round(time.monotonic() - started, 3)
            atomic_write_json(path, record)
            self.progress["source_timings"] = [row for row in self.progress["source_timings"] if row.get("source_id") != source_id]
            self.progress["source_timings"].append({
                "source_id": source_id,
                "duration_seconds": duration,
                "status": record["retrieval_status"],
                "attempts": sum(item.get("attempts", 0) for item in record["attempt_history"] if isinstance(item, dict)),
                "finished_at": utc_now(),
            })
            if source_id not in self.progress["source_completed_ids"]:
                self.progress["source_completed_ids"].append(source_id)
            self.save_progress()
            print(f"source [{index}/{len(seeds)}] {source_id} -> {record['retrieval_status']}", flush=True)
            processed += 1
            if max_items is not None and processed >= max_items:
                break
        completed_count = len(set(self.progress["source_completed_ids"]) & set(seeds))
        done = completed_count == len(seeds)
        if not done:
            return {"done": False, "processed": processed, "completed": completed_count, "total": len(seeds)}
        relationships_root = self.root / "data" / "relationships"
        atomic_write_json(relationships_root / "incident-sources.json", {
            "schema_version": PILOT_SCHEMA_VERSION,
            "scope": "0001-0100",
            "relationships": [
                {
                    "incident_id": incident_id,
                    "source_id": source_id,
                    "airwars_source_ids": sorted({
                        str(item.get("airwars_source_id"))
                        for item in (load_json(sources_root / f"{source_id}.json", {}) or {}).get("provenance", [])
                        if item.get("incident_id") == incident_id and item.get("airwars_source_id")
                    }),
                    "occurrence_count": max(1, len({
                        (item.get("airwars_source_id"), item.get("observed_original_url"))
                        for item in (load_json(sources_root / f"{source_id}.json", {}) or {}).get("provenance", [])
                        if item.get("incident_id") == incident_id
                    })),
                }
                for incident_id, source_ids in sorted(incident_sources.items())
                for source_id in source_ids
            ],
        })
        atomic_write_json(self.root / "data" / "reports" / "first-100-exact-url-duplicates.json", {
            "exact_duplicate_relationship_count": sum(max(0, len(values) - 1) for values in exact_urls.values()),
            "urls": {key: values for key, values in exact_urls.items() if len(values) > 1},
        })
        return {"done": True, "processed": processed, "completed": completed_count, "total": len(seeds)}

    def create_media(self) -> None:
        source_by_url: dict[str, str] = {}
        for path in (self.root / "data" / "sources").glob("*.json"):
            source = load_json(path, {})
            source_by_url[normalize_source_url(source.get("original_url") or "")] = source.get("source_id")
        source_media: list[dict[str, str]] = []
        incident_media: dict[str, list[str]] = defaultdict(list)
        for sequence in PILOT_SEQUENCES:
            manifest_item = load_json(self.manifest_path, {})["incidents"][sequence - 1]
            incident_id = manifest_item["internal_id"]
            record = load_json(self.root / "data" / "incidents" / f"{incident_id}.json", {})
            for index, media in enumerate(record.get("media_metadata") or [], 1):
                source_url = media.get("source_url") or ""
                source_id = source_by_url.get(normalize_source_url(source_url), "")
                url = media.get("url") or media.get("thumbnail_url") or ""
                media_type = media.get("type") or media.get("type_original") or media.get("type_ar") or "unknown"
                media_id = stable_media_id(source_id or incident_id, url, f"{incident_id}:{index}:{media_type}")
                path = self.root / "data" / "media" / f"{media_id}.json"
                existing = load_json(path, {}) or {}
                placeholder = existing or {
                    "schema_version": PILOT_SCHEMA_VERSION,
                    "media_id": media_id,
                    "incident_ids": [],
                    "source_id": source_id or None,
                    "media_type": media_type,
                    "original_url": url,
                    "archived_urls": [],
                    "caption": media.get("caption") or media.get("caption_original") or media.get("alt") or "",
                    "description": media.get("description") or "",
                    "publisher": media.get("source_name") or "",
                    "publication_date": None,
                    "sensitivity_flag": bool(media.get("sensitive")),
                    "preservation_status": "external_only" if url else "unknown",
                    "local_path": None,
                    "sha256": None,
                    "estimated_or_reported_byte_size": None,
                    "pending_action": "pending_review" if not url else "pending_download",
                    "provenance": media.get("provenance") or "airwars_incident_page",
                }
                _append_unique(placeholder["incident_ids"], incident_id)
                atomic_write_json(path, placeholder)
                _append_unique(incident_media[incident_id], media_id)
                if source_id:
                    _append_unique(source_media, {"source_id": source_id, "media_id": media_id}, key=lambda item: (item["source_id"], item["media_id"]))
            record["media_placeholder_ids"] = incident_media[incident_id]
            atomic_write_json(self.root / "data" / "incidents" / f"{incident_id}.json", record)
            self.save_progress()
        for source_path in sorted((self.root / "data" / "sources").glob("*.json")):
            source = load_json(source_path, {})
            if source.get("source_type") not in {"direct_image_url", "direct_video_url", "direct_audio_url", "pdf_document"}:
                continue
            media_type = "document" if source["source_type"] == "pdf_document" else source["source_type"].removeprefix("direct_").removesuffix("_url")
            media_id = stable_media_id(source["source_id"], source["original_url"], source["source_id"])
            path = self.root / "data" / "media" / f"{media_id}.json"
            placeholder = load_json(path, {}) or {
                "schema_version": PILOT_SCHEMA_VERSION,
                "media_id": media_id,
                "incident_ids": list(source.get("incident_ids") or []),
                "source_id": source["source_id"],
                "media_type": media_type,
                "original_url": source["original_url"],
                "archived_urls": list(source.get("archived_urls") or []),
                "caption": "",
                "description": "Document/media source; binary intentionally not committed.",
                "publisher": source.get("publisher") or "",
                "publication_date": source.get("publication_date") or None,
                "sensitivity_flag": False,
                "preservation_status": "external_only",
                "local_path": None,
                "sha256": None,
                "estimated_or_reported_byte_size": None,
                "pending_action": "ocr_pending" if (source.get("pdf") or {}).get("ocr_pending") else "pending_review",
                "provenance": "source_url",
            }
            atomic_write_json(path, placeholder)
            _append_unique(source_media, {"source_id": source["source_id"], "media_id": media_id}, key=lambda item: (item["source_id"], item["media_id"]))
        atomic_write_json(self.root / "data" / "relationships" / "source-media.json", {
            "schema_version": PILOT_SCHEMA_VERSION,
            "scope": "0001-0100",
            "relationships": source_media,
        })

    def translate(self, max_items: int | None = None) -> dict[str, Any]:
        manifest = load_json(self.manifest_path, {})
        work: list[tuple[str, Path, str, str, str]] = []
        for item in manifest.get("incidents", []):
            path = self.root / "data" / "incidents" / f"{item['internal_id']}.json"
            work.append((f"incident:{item['internal_id']}", path, "narrative_original", "narrative_ar", "translation"))
        for path in sorted((self.root / "data" / "sources").glob("source-*.json")):
            work.append((f"source:{path.stem}", path, "text_original", "text_ar", "translation"))
        completed = set(self.progress["translation_completed_records"])
        processed = 0
        terminal = {"complete", "not_required_arabic_original", "complete_existing_airwars_translation", "not_applicable", "incomplete", "failed", "disabled_by_user"}
        for key, path, text_field, arabic_field, state_field in work:
            if key in completed:
                continue
            record = load_json(path, {})
            if key.startswith("incident:"):
                record["narrative_original"] = record.get("narrative_original") or record.get("narrative") or ""
                record["narrative_original_language"] = detect_language(record["narrative_original"])
            status = (record.get(state_field) or {}).get("status")
            if status not in terminal:
                self.translator.translate_record(
                    record, text_field, arabic_field, state_field,
                    lambda path=path, record=record: (atomic_write_json(path, record), self.save_progress()),
                )
            if key not in self.progress["translation_completed_records"]:
                self.progress["translation_completed_records"].append(key)
            self.save_progress()
            processed += 1
            print(f"translation [{len(self.progress['translation_completed_records'])}/{len(work)}] {key} -> {(record.get(state_field) or {}).get('status')}", flush=True)
            if max_items is not None and processed >= max_items:
                break
        completed_count = len(set(self.progress["translation_completed_records"]) & {item[0] for item in work})
        return {"done": completed_count == len(work), "processed": processed, "completed": completed_count, "total": len(work)}

    def reports(self) -> None:
        manifest = load_json(self.manifest_path, {})
        incidents = [load_json(self.root / "data" / "incidents" / f"{item['internal_id']}.json", {}) for item in manifest["incidents"]]
        source_paths = sorted((self.root / "data" / "sources").glob("*.json"))
        sources = [load_json(path, {}) for path in source_paths]
        for source in sources:
            if not str(source.get("source_type") or "").startswith("public_"):
                continue
            live_variants = [item for item in source.get("content_variants") or [] if item.get("provenance") == "source_live"]
            login_variants = [item for item in live_variants if any(marker in str(item.get("text") or "").casefold() for marker in LOGIN_WALL_MARKERS)]
            if not login_variants:
                continue
            source["review_flags"] = list(dict.fromkeys((source.get("review_flags") or []) + ["live_response_was_login_wall"]))
            if source.get("content_hash") in {item.get("sha256") for item in login_variants}:
                preserved = next((item for item in source.get("content_variants") or [] if item.get("provenance") in {"airwars_live", "airwars_archive"} and item not in login_variants), None)
                source["text_original"] = preserved.get("text", "") if preserved else ""
                source["content_hash"] = preserved.get("sha256") if preserved else None
            if source.get("retrieval_status") == "successful" and not any(item.get("provenance") in {"listed_archive", "wayback_capture"} for item in source.get("content_variants") or []):
                source["retrieval_status"] = "login_required"
                source["failure_reason"] = "live_response_was_login_wall"
            atomic_write_json(self.root / "data" / "sources" / f"{source['source_id']}.json", source)
        content_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for source in sources:
            if source.get("content_hash"):
                content_groups[source["content_hash"]].append(source)
        for digest, group in content_groups.items():
            if len(group) < 2:
                continue
            canonical_id = sorted(item["source_id"] for item in group)[0]
            group_id = f"exact-content-{digest[:24]}"
            for source in group:
                source["exact_content_duplicate_group"] = group_id
                source["exact_content_duplicate_of"] = None if source["source_id"] == canonical_id else canonical_id
                atomic_write_json(self.root / "data" / "sources" / f"{source['source_id']}.json", source)
        media = [load_json(path, {}) for path in sorted((self.root / "data" / "media").glob("*.json"))]
        relationships = load_json(self.root / "data" / "relationships" / "incident-sources.json", {}).get("relationships", [])
        incident_statuses = Counter(item.get("completeness_status") or "failed" for item in incidents)
        source_statuses = Counter(item.get("retrieval_status") or "pending" for item in sources)
        translation_statuses = Counter(item.get("translation", {}).get("status") or "pending" for item in incidents + sources)
        summary = {
            "generated_at": utc_now(),
            "scope": {"first_sequence": 1, "last_sequence": 100, "count": 100, "later_incidents_processed": 0},
            "translation_policy": {
                "enabled": False,
                "status": "disabled_by_user",
                "generated_translations": 0,
                "original_text_preserved": True,
            },
            "incidents": {
                "attempted": 100,
                "records": len(incidents),
                "status_counts": dict(incident_statuses),
                "full_narratives": sum(bool(item.get("narrative_original")) for item in incidents),
                "narratives_translated": 0,
                "victim_records": sum(len(item.get("victims") or []) for item in incidents),
                "conflicting_fields": sum(len(item.get("conflicts") or []) for item in incidents),
                "legacy_dependent": sum(not bool(item.get("page_extraction") or item.get("api_extraction")) for item in incidents),
            },
            "sources": {
                "relationships": sum(int(item.get("occurrence_count") or 1) for item in relationships),
                "stable_relationship_edges": len(relationships),
                "unique_records": len(sources),
                "status_counts": dict(source_statuses),
                "texts_extracted": sum(bool(item.get("text_original")) for item in sources),
                "texts_translated": 0,
                "exact_duplicate_content": sum(max(0, len(group) - 1) for group in content_groups.values()),
                "review_required": sum(bool(item.get("review_flags") or item.get("translation", {}).get("review_required")) for item in sources),
                "type_counts": dict(Counter(item.get("source_type") for item in sources)),
            },
            "media": {
                "placeholders": len(media),
                "type_counts": dict(Counter(item.get("media_type") for item in media)),
                "binaries_downloaded": 0,
                "repository_binary_bytes": 0,
            },
            "translation_status_counts": dict(translation_statuses),
            "versions": {"pilot_schema": PILOT_SCHEMA_VERSION, "pilot_parser": PILOT_PARSER_VERSION, "translation": TRANSLATION_VERSION},
        }
        reports = self.root / "data" / "reports"
        atomic_write_json(reports / "first-100-summary.json", summary)
        atomic_write_json(reports / "first-100-sources.json", {
            "generated_at": utc_now(),
            "counts": summary["sources"],
            "sources": [{
                "source_id": item.get("source_id"),
                "incident_sequences": item.get("incident_sequences"),
                "source_type": item.get("source_type"),
                "original_url": item.get("original_url"),
                "retrieval_status": item.get("retrieval_status"),
                "extraction_status": item.get("extraction_status"),
                "translation_status": item.get("translation", {}).get("status"),
            } for item in sources],
        })
        failures = []
        for item in incidents:
            if item.get("completeness_status") not in {"complete"}:
                failures.append({"record_type": "incident", "id": item.get("internal_id"), "sequence": item.get("legacy_sequence"), "status": item.get("completeness_status"), "missing": (item.get("missing_fields") or []) + (item.get("missing_sections") or [])})
        for item in sources:
            if item.get("retrieval_status") != "successful" or not item.get("text_original"):
                failures.append({"record_type": "source", "id": item.get("source_id"), "status": item.get("retrieval_status"), "failure_reason": item.get("failure_reason")})
        atomic_write_json(reports / "first-100-failures.json", {"generated_at": utc_now(), "count": len(failures), "failures": failures})
        self._write_timing_report()
        self._write_storage_report()
        self._write_projections()
        lines = [
            "# تقرير الطيار: أول 100 حادثة",
            "",
            f"- النطاق: **0001–0100 فقط**",
            f"- سجلات الحوادث: **{len(incidents)}**",
            f"- علاقات المصادر: **{len(relationships)}**",
            f"- المصادر الفريدة: **{len(sources)}**",
            f"- عناصر الوسائط الوصفية: **{len(media)}**",
            f"- ملفات الوسائط الثنائية: **0**",
            "- الترجمة الآلية: **معطلة بطلب المستخدم؛ النص الأصلي محفوظ دون ترجمة**",
            "",
            "التفاصيل الآلية الكاملة موجودة في ملفات `first-100-*.json` داخل هذا المجلد.",
        ]
        from .io_utils import atomic_write_text
        atomic_write_text(reports / "first-100-summary.md", "\n".join(lines) + "\n")

    def _write_timing_report(self) -> None:
        incident_values = [float(row.get("duration_seconds") or 0) for row in self.progress["incident_timings"]]
        source_values = [float(row.get("duration_seconds") or 0) for row in self.progress["source_timings"]]
        translation_chunks: list[float] = []
        incident_extraction = 0.0
        source_extraction = 0.0
        persistent_waiting = 0.0
        persistent_retries = 0
        persistent_timeouts = 0
        persistent_blocked = 0
        for folder in ("incidents", "sources"):
            for path in (self.root / "data" / folder).glob("*.json"):
                record = load_json(path, {})
                translation_chunks.extend(float(chunk.get("duration_seconds") or 0) for chunk in record.get("translation", {}).get("chunks", []))
                if folder == "incidents":
                    incident_extraction += float(record.get("timing", {}).get("text_extraction_seconds") or 0)
                    attempts = [item for item in (record.get("retrieval_status") or {}).values() if isinstance(item, dict)]
                else:
                    source_extraction += float(record.get("timing", {}).get("text_extraction_seconds") or 0)
                    attempts = [item for item in record.get("attempt_history") or [] if isinstance(item, dict)]
                for attempt in attempts:
                    persistent_waiting += float(attempt.get("waiting_seconds") or 0)
                    persistent_retries += max(0, int(attempt.get("attempts") or 1) - 1)
                    status = attempt.get("status")
                    error = str(attempt.get("error") or "").casefold()
                    persistent_timeouts += int("timeout" in error or "timed out" in error)
                    persistent_blocked += int(status in {403, 429, 451})
        finish = utc_now()
        start_text = self.progress.get("started_at")
        try:
            wall_clock = (datetime.fromisoformat(finish.replace("Z", "+00:00")) - datetime.fromisoformat(str(start_text).replace("Z", "+00:00"))).total_seconds()
        except (TypeError, ValueError):
            wall_clock = 0.0
        report = {
            "generated_at": utc_now(),
            "pilot_start_utc": start_text,
            "pilot_finish_utc": finish,
            "total_wall_clock_seconds": round(wall_clock, 3),
            "stage_runs": self.progress["stage_runs"],
            "incident": {
                "count": len(incident_values),
                "total_seconds": round(sum(incident_values), 3),
                "mean_seconds": round(statistics.mean(incident_values), 3) if incident_values else 0,
                "median_seconds": round(statistics.median(incident_values), 3) if incident_values else 0,
                "p90_seconds": round(_percentile(incident_values, 0.9), 3),
            },
            "source": {
                "count": len(source_values),
                "total_seconds": round(sum(source_values), 3),
                "mean_seconds": round(statistics.mean(source_values), 3) if source_values else 0,
                "median_seconds": round(statistics.median(source_values), 3) if source_values else 0,
                "p90_seconds": round(_percentile(source_values, 0.9), 3),
            },
            "translation": {"chunk_count": len(translation_chunks), "total_seconds": round(sum(translation_chunks), 3)},
            "active_collection_seconds": round(sum(incident_values) + sum(source_values), 3),
            "text_extraction_seconds": round(incident_extraction + source_extraction, 3),
            "incident_text_extraction_seconds": round(incident_extraction, 3),
            "source_text_extraction_seconds": round(source_extraction, 3),
            "retrieval": {
                "waiting_and_rate_limit_seconds": round(persistent_waiting + self.translator.waiting_seconds, 3),
                "retries": persistent_retries + self.translator.retry_count,
                "timeouts": persistent_timeouts,
                "blocked_requests": persistent_blocked,
            },
        }
        atomic_write_json(self.root / "data" / "reports" / "first-100-timing.json", report)

    def _write_storage_report(self) -> None:
        baseline = load_json(self.root / "data" / "pilot" / "first-100-baseline.json", {}) or {}
        directories = ["incidents", "sources", "media", "relationships", "reports"]
        current = {f"data/{name}": _tree_bytes(self.root / "data" / name) for name in directories}
        current["data"] = _tree_bytes(self.root / "data")
        current["working_tree_excluding_git"] = _tree_bytes(self.root, lambda path: ".git" not in path.parts)
        current["git_directory"] = _tree_bytes(self.root / ".git")
        source_sizes = [path.stat().st_size for path in (self.root / "data" / "sources").glob("*.json")]
        report = {
            "generated_at": utc_now(),
            "baseline": baseline,
            "current_bytes": current,
            "added_bytes": {key: current[key] - int((baseline.get("sizes_bytes") or {}).get(key, 0)) for key in current},
            "source_record_bytes": {
                "count": len(source_sizes),
                "mean": round(statistics.mean(source_sizes), 1) if source_sizes else 0,
                "median": round(statistics.median(source_sizes), 1) if source_sizes else 0,
                "p90": round(_percentile([float(value) for value in source_sizes], 0.9), 1),
            },
        }
        atomic_write_json(self.root / "data" / "reports" / "first-100-storage.json", report)

    def _write_projections(self) -> None:
        summary = load_json(self.root / "data" / "reports" / "first-100-summary.json", {})
        timing = load_json(self.root / "data" / "reports" / "first-100-timing.json", {})
        sources = [load_json(path, {}) for path in (self.root / "data" / "sources").glob("*.json")]
        incidents = [load_json(path, {}) for path in (self.root / "data" / "incidents").glob("*.json") if 1 <= int((load_json(path, {}) or {}).get("legacy_sequence") or 0) <= 100]
        relationships = summary.get("sources", {}).get("relationships", 0)
        original_bytes = sum(len((item.get("text_original") or "").encode("utf-8")) for item in sources) + sum(len((item.get("narrative_original") or "").encode("utf-8")) for item in incidents)
        arabic_bytes = 0
        json_bytes = sum(path.stat().st_size for folder in ("incidents", "sources", "media", "relationships") for path in (self.root / "data" / folder).glob("*.json"))
        source_count = max(1, len(sources))
        incident_mean = float(timing.get("incident", {}).get("mean_seconds") or 0)
        incident_median = float(timing.get("incident", {}).get("median_seconds") or 0)
        incident_p90 = float(timing.get("incident", {}).get("p90_seconds") or 0)
        source_mean = float(timing.get("source", {}).get("mean_seconds") or 0)
        source_median = float(timing.get("source", {}).get("median_seconds") or 0)
        source_p90 = float(timing.get("source", {}).get("p90_seconds") or 0)
        translation_per_incident = float(timing.get("translation", {}).get("total_seconds") or 0) / 100
        scenarios = {
            "low": {"source_ratio": source_count / 100 * 0.8, "relationship_ratio": relationships / 100 * 0.85, "incident_seconds": incident_median, "source_seconds": source_median, "size_factor": 0.75},
            "central": {"source_ratio": source_count / 100, "relationship_ratio": relationships / 100, "incident_seconds": incident_mean, "source_seconds": source_mean, "size_factor": 1.0},
            "high": {"source_ratio": source_count / 100 * 1.25, "relationship_ratio": relationships / 100 * 1.25, "incident_seconds": incident_p90, "source_seconds": source_p90, "size_factor": 1.5},
        }
        projections: list[dict[str, Any]] = []
        for count in (500, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 8114):
            row: dict[str, Any] = {"incidents": count, "estimates": {}}
            for name, scenario in scenarios.items():
                estimated_sources = round(count * scenario["source_ratio"])
                factor = count / 100 * scenario["size_factor"]
                collection = count * scenario["incident_seconds"] + estimated_sources * scenario["source_seconds"]
                translation = count * translation_per_incident * scenario["size_factor"]
                row["estimates"][name] = {
                    "unique_sources": estimated_sources,
                    "source_relationships": round(count * scenario["relationship_ratio"]),
                    "original_text_bytes": round(original_bytes * factor),
                    "arabic_translation_bytes": round(arabic_bytes * factor),
                    "json_data_bytes": round(json_bytes * factor),
                    "generated_html_bytes": None,
                    "git_working_tree_bytes": None,
                    "estimated_git_history_growth_bytes": round(json_bytes * factor * 1.25),
                    "compressed_artifact_bytes": None,
                    "collection_seconds": round(collection),
                    "translation_seconds": round(translation),
                    "total_processing_seconds": round(collection + translation),
                }
            projections.append(row)
        atomic_write_json(self.root / "data" / "reports" / "first-100-projections.json", {
            "generated_at": utc_now(),
            "sample_size": 100,
            "warning": "The first 100 incidents may not represent all 8,114 incidents; estimates are scenarios, not promises.",
            "method": "Low uses median timing and 0.75x bytes; central uses means; high uses P90 timing and 1.5x bytes, adjusted for duplicate-source rate.",
            "sample_basis": {
                "unique_sources": source_count,
                "source_relationships": relationships,
                "duplicate_source_relationship_rate": round(max(0.0, 1.0 - source_count / max(1, relationships)), 4),
                "source_retrieval_success_rate": round(sum(item.get("retrieval_status") == "successful" for item in sources) / source_count, 4),
                "source_text_success_rate": round(sum(bool(item.get("text_original")) for item in sources) / source_count, 4),
                "source_language_distribution": dict(Counter(item.get("text_original_language") or "und" for item in sources)),
                "incident_language_distribution": dict(Counter(item.get("narrative_original_language") or "und" for item in incidents)),
                "original_text_bytes": original_bytes,
                "arabic_translation_bytes": arabic_bytes,
                "translation_policy": "disabled_by_user",
            },
            "projections": projections,
        })

    def run(self, stage: str = "all", max_items: int | None = None) -> dict[str, Any]:
        stages: dict[str, tuple[str, Callable[[], Any]]] = {
            "preflight": ("translation_preflight", self.translation_preflight),
            "manifest": ("manifest", lambda: (self.build_manifest() or {"done": True})),
            "incidents": ("incident_collection", lambda: self.collect_incidents(max_items)),
            "sources": ("source_collection", lambda: self.collect_sources(max_items)),
            "media": ("media_placeholders", lambda: (self.create_media() or {"done": True})),
            "translation": ("translation", lambda: self.translate(max_items)),
            "reports": ("reports", lambda: (self.reports() or {"done": True})),
        }
        if stage == "all":
            order = ("manifest", "incidents", "sources", "media", "translation", "reports")
            result: dict[str, Any] = {"done": True, "stage": "all"}
            for item in order:
                stage_name, function = stages[item]
                payload = self._stage(stage_name, function) or {"done": True}
                if not payload.get("done", True):
                    result = {"stage": item, **payload}
                    break
        else:
            stage_name, function = stages[stage]
            payload = self._stage(stage_name, function) or {"done": True}
            result = {"stage": stage, **payload}
        result["recorded_at"] = utc_now()
        atomic_write_json(self.root / "data" / "pilot" / "first-100-stage-result.json", result)
        if stage in {"all", "reports"} and result.get("done"):
            self.progress["finished_at"] = utc_now()
            self.progress["result"] = "complete"
            self.save_progress()
        print("PILOT_STAGE_RESULT=" + json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the resumable, strictly scoped first-100 incident pilot")
    parser.add_argument("--legacy-zip", required=True)
    parser.add_argument("--output-root", default=".")
    parser.add_argument("--delay", type=float, default=0.75)
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--stage", choices=("all", "preflight", "manifest", "incidents", "sources", "media", "translation", "reports"), default="all")
    parser.add_argument("--max-items", type=int)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.max_items is not None and args.max_items < 1:
        raise SystemExit("--max-items must be at least 1")
    PilotRunner(Path(args.output_root), Path(args.legacy_zip), args.delay, args.timeout, args.retries).run(args.stage, args.max_items)


if __name__ == "__main__":
    main()


def finalize_site_measurements(root: Path, site_root: Path, compressed_artifact: Path) -> dict[str, Any]:
    root = root.resolve()
    site_root = site_root.resolve()
    artifact = compressed_artifact.resolve()
    storage_path = root / "data" / "reports" / "first-100-storage.json"
    storage = load_json(storage_path, {}) or {}
    source_html_sizes = [path.stat().st_size for path in (site_root / "sources").glob("*/index.html")]
    incident_html_sizes = [
        (site_root / "cases" / f"{sequence:04d}" / "index.html").stat().st_size
        for sequence in PILOT_SEQUENCES
        if (site_root / "cases" / f"{sequence:04d}" / "index.html").is_file()
    ]
    current = storage.setdefault("current_bytes", {})
    current.update({
        "working_tree_excluding_git": _tree_bytes(root, lambda path: ".git" not in path.parts and "_site" not in path.parts),
        "git_directory": _tree_bytes(root / ".git"),
        "generated_pilot_incident_html": sum(incident_html_sizes),
        "generated_source_html": sum(source_html_sizes),
        "generated_static_site": _tree_bytes(site_root),
        "compressed_site_artifact": artifact.stat().st_size,
    })
    baseline = storage.get("baseline", {})
    baseline_sizes = baseline.get("sizes_bytes", {})
    storage["added_bytes"] = {
        key: value - int(baseline_sizes.get(key, 0))
        for key, value in current.items()
    }
    storage["generated_html"] = {
        "incident_count": len(incident_html_sizes),
        "incident_total_bytes": sum(incident_html_sizes),
        "incident_mean_bytes": round(statistics.mean(incident_html_sizes), 1) if incident_html_sizes else 0,
        "source_count": len(source_html_sizes),
        "source_total_bytes": sum(source_html_sizes),
        "source_mean_bytes": round(statistics.mean(source_html_sizes), 1) if source_html_sizes else 0,
        "source_median_bytes": round(statistics.median(source_html_sizes), 1) if source_html_sizes else 0,
        "source_p90_bytes": round(_percentile([float(value) for value in source_html_sizes], 0.9), 1),
    }
    storage.setdefault("site_measurement_finalized_at", utc_now())
    atomic_write_json(storage_path, storage)

    projections_path = root / "data" / "reports" / "first-100-projections.json"
    projections = load_json(projections_path, {}) or {}
    baseline_site = int(baseline_sizes.get("generated_site_uncompressed", 0))
    baseline_zip = int(baseline_sizes.get("generated_site_compressed", 0))
    baseline_worktree = int(baseline_sizes.get("working_tree_excluding_git", 0))
    pilot_html = sum(incident_html_sizes) + sum(source_html_sizes)
    json_data = sum(path.stat().st_size for folder in ("incidents", "sources", "media", "relationships") for path in (root / "data" / folder).glob("*.json"))
    zip_growth = max(0, artifact.stat().st_size - baseline_zip)
    worktree_growth = max(0, current["working_tree_excluding_git"] - baseline_worktree)
    for row in projections.get("projections", []):
        count = int(row["incidents"])
        for scenario_name, estimate in row.get("estimates", {}).items():
            size_factor = {"low": 0.75, "central": 1.0, "high": 1.5}[scenario_name]
            scale = count / 100 * size_factor
            estimate["generated_html_bytes"] = round(pilot_html * scale)
            estimate["git_working_tree_bytes"] = round(baseline_worktree + worktree_growth * scale)
            estimate["estimated_git_history_growth_bytes"] = round((json_data + pilot_html) * scale * 1.25)
            estimate["compressed_artifact_bytes"] = round(baseline_zip + zip_growth * scale)
            estimate["total_generated_site_bytes"] = round(baseline_site + pilot_html * scale)
    projections.setdefault("site_measurement_finalized_at", utc_now())
    atomic_write_json(projections_path, projections)
    return {"storage": storage, "projections": projections}
