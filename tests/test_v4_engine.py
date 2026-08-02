from __future__ import annotations

import asyncio
import io
import json
import tempfile
import time
import unittest
import urllib.parse
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx

from archive_pipeline.validator import PILOT_SOURCE_STATUSES

from archive_pipeline.content_extractor import extract_public_text
from archive_pipeline.fetcher import AsyncHostFetcher, FetchResult
from archive_pipeline.io_utils import utc_now
from archive_pipeline.speed_pilot import (
    SpeedPilotRunner,
    canonical_fetch_key,
    fair_source_order,
)


def _response_result(status: int) -> FetchResult:
    return FetchResult(
        url="https://publish.twitter.com/oembed",
        final_url="https://publish.twitter.com/oembed",
        status=status,
        content_type="application/json",
        body=b"{}",
        retrieved_at=utc_now(),
        elapsed_seconds=0.001,
        error=None if 200 <= status < 300 else f"http_{status}",
    )


async def _install_mock_transport(fetcher: AsyncHostFetcher, handler) -> None:
    assert fetcher._client is not None
    await fetcher._client.aclose()
    fetcher._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        trust_env=False,
    )


class ValidatorV4TaxonomyTests(unittest.TestCase):
    def test_v4_terminal_source_statuses_are_accepted(self) -> None:
        self.assertTrue(
            {
                "successful_partial",
                "cached",
                "embedded_text_preserved",
                "media_metadata_preserved",
                "recovery_deferred",
                "failed",
                "internal_error",
            }.issubset(PILOT_SOURCE_STATUSES)
        )


class PublicTextExtractionV4Tests(unittest.TestCase):
    def test_json_ld_is_extracted_before_script_cleanup(self) -> None:
        expected = (
            "هذا نص المقال الكامل الموجود حصراً داخل بيانات JSON-LD، "
            "ويجب ألا يختفي عند تنظيف عناصر script من الصفحة."
        )
        body = f"""<!doctype html>
        <html lang="ar"><head><title>عنوان تجريبي</title>
        <script type="application/ld+json">{json.dumps({
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            "headline": "خبر موثق",
            "articleBody": expected,
        }, ensure_ascii=False)}</script></head>
        <body><nav>الرئيسية الأخبار اتصل بنا</nav><div id="app"></div></body></html>"""

        extracted = extract_public_text(
            body.encode("utf-8"), "text/html; charset=utf-8", "https://example.org/story"
        )

        self.assertEqual(extracted["text"], expected)
        self.assertTrue(str(extracted["method"]).startswith("jsonld:"))

    def test_application_json_extracts_article_text_and_metadata(self) -> None:
        expected = "Public JSON article body with enough substantive text for extraction."
        payload = {
            "data": {
                "headline": "API headline",
                "author": "Archive Reporter",
                "datePublished": "2026-08-01",
                "articleBody": expected,
            }
        }

        extracted = extract_public_text(
            json.dumps(payload).encode("utf-8"),
            "application/json; charset=utf-8",
            "https://example.org/api/article/7",
        )

        self.assertEqual(extracted["text"], expected)
        self.assertEqual(extracted["title"], "API headline")
        self.assertEqual(extracted["author"], "Archive Reporter")
        self.assertEqual(extracted["publication_date"], "2026-08-01")

    def test_rss_and_atom_extract_entry_content(self) -> None:
        fixtures = (
            (
                "application/rss+xml",
                b"""<?xml version="1.0" encoding="UTF-8"?>
                <rss version="2.0"><channel><item><title>RSS title</title>
                <description><![CDATA[<p>RSS public article text preserved from the feed.</p>]]></description>
                </item></channel></rss>""",
                "RSS public article text preserved from the feed.",
                "RSS title",
            ),
            (
                "application/atom+xml",
                b"""<?xml version="1.0" encoding="UTF-8"?>
                <feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom title</title>
                <content type="html">Atom public article text preserved from the feed.</content>
                </entry></feed>""",
                "Atom public article text preserved from the feed.",
                "Atom title",
            ),
        )
        for content_type, body, expected, title in fixtures:
            with self.subTest(content_type=content_type):
                extracted = extract_public_text(body, content_type, "https://example.org/feed")
                self.assertIn(expected, extracted["text"])
                self.assertEqual(extracted["title"], title)
                self.assertEqual(extracted["method"], "rss_atom")

    def test_windows_1256_arabic_html_is_decoded_without_mojibake(self) -> None:
        expected = "هذا نص عربي محفوظ بترميز ويندوز 1256 ويجب استخراجه بصورة سليمة تماماً."
        body = (
            "<html><head><title>اختبار الترميز</title></head>"
            f"<body><article><p>{expected}</p></article></body></html>"
        ).encode("windows-1256")

        extracted = extract_public_text(
            body,
            "text/html; charset=windows-1256",
            "https://example.org/arabic",
        )

        self.assertEqual(extracted["text"], expected)
        self.assertNotIn("�", extracted["text"])

    def test_docx_wordprocessingml_text_is_extracted(self) -> None:
        expected = "نص وثيقة وورد عامة محفوظ حرفياً"
        document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body><w:p><w:r><w:t>{expected}</w:t></w:r></w:p></w:body>
        </w:document>""".encode("utf-8")
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document_xml)

        extracted = extract_public_text(
            archive_bytes.getvalue(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "https://example.org/report.docx",
        )

        self.assertEqual(extracted["text"], expected)
        self.assertEqual(extracted["method"], "docx_xml")


class SourceOrderingAndIdentityV4Tests(unittest.TestCase):
    def test_generic_cache_key_preserves_semantic_s_t_and_ref_parameters(self) -> None:
        original = (
            "https://www.example.org/article?s=section&t=edition&ref=primary"
            "&utm_source=campaign"
        )
        key = canonical_fetch_key(original)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(key).query))

        self.assertEqual(query["s"], "section")
        self.assertEqual(query["t"], "edition")
        self.assertEqual(query["ref"], "primary")
        self.assertNotIn("utm_source", query)

    def test_fair_source_order_does_not_starve_a_rare_host(self) -> None:
        records = [
            {
                "source_id": f"facebook-{index}",
                "source_type": "public_facebook_post",
                "original_url": f"https://www.facebook.com/page/posts/{index}",
            }
            for index in range(100)
        ]
        records.extend(
            {
                "source_id": f"twitter-{index}",
                "source_type": "public_x_post",
                "original_url": f"https://twitter.com/account/status/{index}",
            }
            for index in range(40)
        )
        rare = {
            "source_id": "rare-news",
            "source_type": "news_article",
            "original_url": "https://rare-news.example/investigation",
        }
        records.append(rare)

        ordered = fair_source_order(records)
        rare_index = next(index for index, row in enumerate(ordered) if row["source_id"] == "rare-news")

        self.assertLess(rare_index, 3)
        self.assertCountEqual(
            [row["source_id"] for row in ordered],
            [row["source_id"] for row in records],
        )


class AsyncFetcherV4Tests(unittest.IsolatedAsyncioTestCase):
    async def test_alternating_403_and_200_never_creates_adaptive_delay(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            status = 403 if calls % 2 else 200
            return httpx.Response(status, request=request, content=b"{}")

        fetcher = AsyncHostFetcher(
            delay_seconds=0,
            timeout_seconds=1,
            retries=1,
            workers=4,
            per_host_workers=2,
            circuit_threshold=100,
            adaptive_pacing=True,
        )
        async with fetcher:
            await _install_mock_transport(fetcher, handler)
            for index in range(20):
                await fetcher.fetch(f"https://publish.twitter.com/oembed?id={index}")

        self.assertEqual(fetcher._adaptive_delays, {})
        self.assertEqual(fetcher.stats()["adaptive_host_delays"], {})
        self.assertEqual(fetcher._host_result_counts["publish.twitter.com"]["blocked"], 10)
        self.assertEqual(fetcher._host_result_counts["publish.twitter.com"]["successful"], 10)

    async def test_retry_after_with_one_attempt_returns_without_sleeping(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                request=request,
                headers={"Retry-After": "5"},
                content=b"rate limited",
            )

        fetcher = AsyncHostFetcher(
            delay_seconds=0,
            timeout_seconds=1,
            retries=1,
            workers=1,
            per_host_workers=1,
            circuit_threshold=100,
        )
        started = time.monotonic()
        async with fetcher:
            await _install_mock_transport(fetcher, handler)
            result = await fetcher.fetch("https://rate.example/item")
        elapsed = time.monotonic() - started

        self.assertEqual(result.status, 429)
        self.assertLess(elapsed, 0.5)
        self.assertEqual(result.retry_waiting_seconds, 0)

    async def test_retry_backoff_releases_the_global_semaphore(self) -> None:
        first_failure_returned = asyncio.Event()
        retry_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal retry_calls
            if request.url.host == "retry.example":
                retry_calls += 1
                if retry_calls == 1:
                    first_failure_returned.set()
                    return httpx.Response(500, request=request, content=b"temporary")
                return httpx.Response(200, request=request, content=b"recovered")
            return httpx.Response(200, request=request, content=b"fast")

        fetcher = AsyncHostFetcher(
            delay_seconds=0,
            timeout_seconds=2,
            retries=2,
            workers=1,
            per_host_workers=1,
            circuit_threshold=100,
        )
        async with fetcher:
            await _install_mock_transport(fetcher, handler)
            with patch("archive_pipeline.fetcher.random.uniform", return_value=0.0):
                retry_task = asyncio.create_task(fetcher.fetch("https://retry.example/item"))
                await asyncio.wait_for(first_failure_returned.wait(), timeout=0.5)
                await asyncio.sleep(0)
                fast_started = time.monotonic()
                fast_result = await fetcher.fetch("https://fast.example/item")
                fast_elapsed = time.monotonic() - fast_started
                retry_result = await retry_task

        self.assertTrue(fast_result.ok)
        self.assertLess(fast_elapsed, 0.3)
        self.assertTrue(retry_result.ok)
        self.assertGreaterEqual(retry_result.retry_waiting_seconds, 0.9)

    async def test_slow_drip_response_obeys_a_total_wall_clock_deadline(self) -> None:
        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for _ in range(20):
                    await asyncio.sleep(0.1)
                    yield b"x"

            async def aclose(self) -> None:
                return None

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                request=request,
                headers={"Content-Type": "text/plain"},
                stream=SlowStream(),
            )

        fetcher = AsyncHostFetcher(
            delay_seconds=0,
            timeout_seconds=0.5,
            retries=1,
            workers=1,
            per_host_workers=1,
            circuit_threshold=100,
        )
        started = time.monotonic()
        async with fetcher:
            await _install_mock_transport(fetcher, handler)
            result = await fetcher.fetch(
                "https://slow.example/drip", timeout_seconds=0.5
            )
        elapsed = time.monotonic() - started

        self.assertIsNone(result.status)
        self.assertIn("TimeoutError", result.error or "")
        self.assertGreaterEqual(elapsed, 0.45)
        self.assertLess(elapsed, 1.2)


class CurrentRunTimingV4Tests(unittest.TestCase):
    def test_finish_source_ignores_attempts_from_previous_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            emitted: list[dict] = []
            runner = SpeedPilotRunner(
                root,
                root / "missing-legacy.zip",
                first_sequence=1,
                last_sequence=1,
                checkpoint_every=1000,
                progress_callback=emitted.append,
            )
            record = {
                "source_id": "source-current-run",
                "source_type": "news_article",
                "original_url": "https://example.org/current",
                "final_redirected_url": "https://example.org/current",
                "retrieval_status": "successful",
                "extraction_status": "text_extracted",
                "preservation_status": "live_text_preserved",
                "text_original": "Current run public article content preserved for timing.",
                "content_hash": "abc123",
                "content_quality": {"accepted": True, "score": 100, "provenance": "source_live"},
                "failure_reason": None,
                "attempt_history": [
                    {
                        "run_id": "v3-previous-run",
                        "attempts": 7,
                        "elapsed_seconds": 100.0,
                        "scheduler_waiting_seconds": 80.0,
                        "pacing_waiting_seconds": 15.0,
                        "retry_waiting_seconds": 5.0,
                    },
                    {
                        "run_id": runner.run_id,
                        "attempts": 1,
                        "elapsed_seconds": 0.2,
                        "scheduler_waiting_seconds": 0.1,
                        "pacing_waiting_seconds": 0.03,
                        "retry_waiting_seconds": 0.0,
                    },
                ],
            }
            try:
                runner._finish_source(
                    record,
                    time.monotonic() - 0.01,
                    {"cache_hit": False, "archive_deferred": False, "status": "successful"},
                )
                timing = runner.progress["source_timings"][-1]
                source_event = next(row for row in emitted if row.get("kind") == "source")
            finally:
                runner.close()

        self.assertEqual(timing["attempts"], 1)
        self.assertAlmostEqual(source_event["detail"]["network_seconds"], 0.2)
        self.assertAlmostEqual(source_event["detail"]["queue_seconds"], 0.1)
        self.assertAlmostEqual(source_event["detail"]["pacing_seconds"], 0.03)
        self.assertEqual(source_event["detail"]["retry_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
