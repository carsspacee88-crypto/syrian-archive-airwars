from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

import httpx

from archive_pipeline.fetcher import AsyncHostFetcher, CircuitBreakingFetcher, FetchResult, HostCircuitBreaker
from archive_pipeline.io_utils import utc_now
from archive_pipeline.source_cache import SourceCacheStore
from archive_pipeline.speed_pilot import (
    SpeedPilotRunner,
    _inherit_airwars_circuit_classification,
    _merge_previous_source,
    _new_source_record,
    _record_exact_content_duplicate_groups,
    _scope_sequences,
    assess_content_quality,
    canonical_fetch_key,
    should_defer_archive,
)


def failed_result(url: str) -> FetchResult:
    return FetchResult(
        url=url,
        final_url=url,
        status=403,
        content_type="text/html",
        body=b"blocked",
        retrieved_at=utc_now(),
        elapsed_seconds=0.01,
        error="http_403",
    )


class CircuitBreakerTests(unittest.TestCase):
    def test_item_level_404_does_not_slow_the_entire_host(self) -> None:
        fetcher = AsyncHostFetcher(
            delay_seconds=0.05,
            workers=4,
            per_host_workers=2,
            adaptive_pacing=True,
        )
        fetcher._note_host_result(
            "facebook.com",
            FetchResult(
                url="https://facebook.com/missing",
                final_url="https://facebook.com/missing",
                status=404,
                content_type="text/html",
                body=b"missing",
                retrieved_at=utc_now(),
                elapsed_seconds=0.01,
                error="http_404",
            ),
        )
        self.assertNotIn("facebook.com", fetcher._adaptive_delays)
        self.assertEqual(fetcher._host_result_counts["facebook.com"]["item_miss"], 1)

    def test_sync_fetcher_opens_after_three_equivalent_host_failures(self) -> None:
        fetcher = CircuitBreakingFetcher(delay_seconds=0, retries=1, circuit_threshold=3)
        fetcher.inner.fetch = Mock(side_effect=lambda url, accept: failed_result(url))
        url = "https://airwars.org/example"
        self.assertEqual([fetcher.fetch(url).status for _ in range(3)], [403, 403, 403])
        skipped = fetcher.fetch(url)
        self.assertEqual(skipped.attempts, 0)
        self.assertIn("host_circuit_open", skipped.error or "")
        self.assertEqual(skipped.circuit_open_reason, "http_403")
        self.assertEqual(skipped.circuit_open_status, 403)
        self.assertEqual(fetcher.inner.fetch.call_count, 3)

    def test_circuit_state_survives_a_new_batch(self) -> None:
        breaker = HostCircuitBreaker(threshold=1)
        url = "https://example.org/a"
        breaker.note(url, True, "blocked")
        restored = HostCircuitBreaker(threshold=1, state=breaker.snapshot())
        self.assertFalse(restored.allow("https://example.org/b"))

    def test_async_queue_rechecks_circuit_after_acquiring_host_slot(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(403, request=request, text="blocked")

        async def scenario() -> tuple[list[FetchResult], int]:
            fetcher = AsyncHostFetcher(delay_seconds=0, retries=1, workers=6, per_host_workers=1, circuit_threshold=3)
            async with fetcher:
                assert fetcher._client is not None
                await fetcher._client.aclose()
                fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True, trust_env=False)
                results = await asyncio.gather(*[
                    fetcher.fetch(f"https://example.org/{index}")
                    for index in range(20)
                ])
            return results, fetcher.circuit_skips

        results, skipped = asyncio.run(scenario())
        self.assertEqual(calls, 3)
        self.assertEqual(skipped, 17)
        self.assertEqual(sum(result.attempts == 0 for result in results), 17)


class SourceCacheTests(unittest.TestCase):
    def test_sqlite_cache_imports_v2_json_once_and_persists_incrementally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "source-url-index.json"
            database = root / "source-cache-v3.sqlite3"
            legacy.write_text(json.dumps({
                "schema_version": "2.0.0",
                "urls": {"https://example.org/a": {"source_id": "source-a"}},
            }), encoding="utf-8")
            original_legacy = legacy.read_bytes()

            cache = SourceCacheStore(database, legacy)
            self.assertEqual(
                cache.get("https://example.org/a")["source_id"], "source-a"
            )
            cache.set("https://example.org/b", {"source_id": "source-b"})
            self.assertEqual(cache.flush(), 1)
            cache.close()

            reopened = SourceCacheStore(database, legacy)
            self.assertEqual(
                reopened.get("https://example.org/b")["source_id"], "source-b"
            )
            self.assertEqual(reopened.count(), 2)
            reopened.close()
            self.assertEqual(legacy.read_bytes(), original_legacy)

    def test_resolved_text_with_invalid_digest_is_not_promoted_to_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = SpeedPilotRunner(
                root, root / "missing.zip", first_sequence=1, last_sequence=1
            )
            record = {
                "source_id": "source-invalid-digest",
                "original_url": "https://example.org/article",
                "retrieval_status": "successful",
                "text_original": "Public evidence text whose digest must be verified.",
                "content_hash": "not-the-real-sha256",
                "content_quality": {"score": 100},
            }
            try:
                runner._update_cache(record)
                runner.cache.flush()
                self.assertEqual(
                    runner.cache.get(canonical_fetch_key(record["original_url"])),
                    {},
                )
            finally:
                runner.close()


class SpeedPilotPolicyTests(unittest.TestCase):
    def test_existing_preserved_text_skips_network_entirely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = SpeedPilotRunner(root, root / "missing.zip", first_sequence=1, last_sequence=1)
            seed = {
                "original_url": "https://facebook.com/example/posts/1",
                "publisher": "Example",
                "publisher_ar": "",
                "author": "",
                "publication_date": "",
                "content": "",
                "declared_language": "Arabic",
            }
            record = _new_source_record(seed, "source-existing")
            record["text_original"] = "هذا نص موثق محفوظ سابقًا من المصدر نفسه"
            record["content_hash"] = "abc"

            class NeverFetch:
                async def fetch(self, *_args, **_kwargs):
                    raise AssertionError("network must not be called")

            result, archive = asyncio.run(runner._live_source(NeverFetch(), record))
            self.assertFalse(archive)
            self.assertEqual(result["retrieval_status"], "embedded_text_preserved")

    def test_unavailable_listed_archive_enters_durable_recovery_not_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = SpeedPilotRunner(
                root, root / "missing.zip", first_sequence=1, last_sequence=1,
                inline_wayback=False,
            )
            seed = {
                "original_url": "https://facebook.com/example/posts/2",
                "publisher": "Example",
                "publisher_ar": "",
                "author": "",
                "publication_date": "",
                "content": "",
                "declared_language": "Arabic",
                "archived_urls": ["https://archive.is/example"],
            }
            record = _new_source_record(seed, "source-deferred")

            class FailedArchive:
                async def fetch(self, url, **_kwargs):
                    return failed_result(url)

                def note_application_failure(self, *_args, **_kwargs):
                    return None

            result = asyncio.run(runner._archive_source(FailedArchive(), record))
            self.assertEqual(result["retrieval_status"], "recovery_deferred")
            self.assertIsNone(result["failure_reason"])
            self.assertIn("source-deferred", runner.recovery["items"])

    def test_facebook_uses_lightweight_public_embed_before_full_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = SpeedPilotRunner(
                root, root / "missing.zip", first_sequence=1, last_sequence=1,
                fast_timeout=2, timeout=4,
            )
            seed = {
                "original_url": "https://www.facebook.com/example/posts/3",
                "publisher": "Example",
                "publisher_ar": "",
                "author": "",
                "publication_date": "",
                "content": "",
                "declared_language": "Arabic",
            }
            record = _new_source_record(seed, "source-facebook")
            requested: list[str] = []

            class EmbedFetcher:
                async def fetch(self, url, **_kwargs):
                    requested.append(url)
                    return FetchResult(
                        url=url, final_url=url, status=200, content_type="text/html",
                        body="<article><p>هذا نص منشور عام موثق وكامل للاختبار</p></article>".encode(),
                        retrieved_at=utc_now(), elapsed_seconds=0.01,
                    )

                def note_application_failure(self, *_args, **_kwargs):
                    return None

            result, archive = asyncio.run(runner._live_source(EmbedFetcher(), record))
            self.assertFalse(archive)
            self.assertEqual(result["retrieval_status"], "successful")
            self.assertEqual(len(requested), 1)
            self.assertIn("/plugins/post.php?", requested[0])

    def test_low_quality_facebook_item_does_not_open_host_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = SpeedPilotRunner(
                root, root / "missing.zip", first_sequence=1, last_sequence=1,
                fast_timeout=2, timeout=4,
            )
            seed = {
                "original_url": "https://www.facebook.com/example/posts/deleted",
                "publisher": "Example",
                "publisher_ar": "",
                "author": "",
                "publication_date": "",
                "content": "",
                "declared_language": "Arabic",
            }
            record = _new_source_record(seed, "source-facebook-shell")
            circuit_notes: list[tuple[bool, str]] = []

            class ShellFetcher:
                async def fetch(self, url, **_kwargs):
                    return FetchResult(
                        url=url, final_url=url, status=200, content_type="text/html",
                        body=b"<main>Log in to Facebook Create new account</main>",
                        retrieved_at=utc_now(), elapsed_seconds=0.01,
                    )

                def note_application_failure(self, _url, failed, reason):
                    circuit_notes.append((failed, reason))

            accepted = asyncio.run(runner._facebook_embed(ShellFetcher(), record))
            self.assertFalse(accepted)
            self.assertEqual(circuit_notes, [(False, "facebook_embed_item_low_quality")])

    def test_fetch_cache_unifies_x_and_twitter_without_changing_source_ids(self) -> None:
        twitter = "https://twitter.com/example/status/123?utm_source=test"
        x_url = "https://x.com/example/status/123"
        self.assertEqual(canonical_fetch_key(twitter), canonical_fetch_key(x_url))

    def test_quality_validator_rejects_platform_shell_and_keeps_short_social_text(self) -> None:
        shell = assess_content_quality(
            "Log in to Facebook Create new account See more on Facebook",
            "public_facebook_post",
        )
        post = assess_content_quality("قصف جوي استهدف البلدة مساء اليوم", "public_x_post")
        self.assertFalse(shell["accepted"])
        self.assertIn("access_or_platform_shell", shell["reasons"])
        self.assertTrue(post["accepted"])

    def test_archive_lane_starts_before_all_live_jobs_finish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            order: list[str] = []

            class StreamingRunner(SpeedPilotRunner):
                async def _live_source(self, fetcher, record):
                    if record["source_id"] == "source-a":
                        await asyncio.sleep(0.01)
                        return record, True
                    await asyncio.sleep(0.15)
                    order.append("slow_live_finished")
                    record["retrieval_status"] = "successful"
                    record["content_quality"] = {"accepted": True, "score": 100}
                    return record, False

                async def _archive_source(self, fetcher, record):
                    order.append("archive_started")
                    await asyncio.sleep(0.01)
                    record["retrieval_status"] = "successful"
                    record["content_quality"] = {"accepted": True, "score": 100}
                    return record

            runner = StreamingRunner(
                root, root / "missing.zip", first_sequence=1, last_sequence=1,
                delay=0, timeout=1, workers=2, per_host_workers=1,
                archive_workers=1, checkpoint_every=2,
            )
            records = [
                {
                    "source_id": source_id,
                    "source_type": "other_web_page",
                    "original_url": f"https://{source_id}.example/item",
                    "retrieval_status": "pending",
                    "failure_reason": None,
                    "attempt_history": [],
                    "text_original": "",
                    "content_hash": None,
                }
                for source_id in ("source-a", "source-b")
            ]
            asyncio.run(runner._collect_source_batch(records))
            self.assertLess(order.index("archive_started"), order.index("slow_live_finished"))

    def test_exact_content_duplicates_are_recorded_without_merging_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for source_id in ("source-a", "source-b"):
                (root / f"{source_id}.json").write_text(json.dumps({
                    "source_id": source_id,
                    "content_hash": "a" * 64,
                }), encoding="utf-8")
            groups, updated = _record_exact_content_duplicate_groups(root)
            self.assertEqual((groups, updated), (1, 2))
            first = json.loads((root / "source-a.json").read_text(encoding="utf-8"))
            second = json.loads((root / "source-b.json").read_text(encoding="utf-8"))
            self.assertEqual(first["exact_content_duplicate_group"], second["exact_content_duplicate_group"])

    def test_same_batch_403_evidence_classifies_circuit_skips_as_blocked(self) -> None:
        base = {
            "incident_code": "TEST",
            "canonical_url": "https://airwars.org/example",
            "location": "Syria",
            "incident_date": "2020-01-01",
            "narrative": "Legacy narrative",
            "sources": [],
            "review_flags": [],
            "retrieval_status": {},
        }
        observed = {
            **base,
            "retrieval_status": {
                "airwars_endpoint": {"ok": False, "status": 403, "error": "http_403"},
            },
        }
        skipped = {
            **base,
            "retrieval_status": {
                "airwars_endpoint": {
                    "ok": False,
                    "status": None,
                    "error": "host_circuit_open_after_repeated_block_or_timeout",
                },
            },
            "completeness_status": "failed",
        }
        self.assertEqual(_inherit_airwars_circuit_classification([observed, skipped]), 1)
        self.assertEqual(skipped["retrieval_status"]["airwars_endpoint"]["circuit_open_status"], 403)
        self.assertEqual(skipped["completeness_status"], "blocked")

    def test_airwars_circuit_inheritance_tolerates_empty_attempt_slots(self) -> None:
        records = [{
            "retrieval_status": {"airwars_endpoint": None, "live_page": None},
            "review_flags": [],
        }]
        self.assertEqual(_inherit_airwars_circuit_classification(records), 0)

    def test_v3_migration_keeps_resolved_v2_content_and_requeues_v2_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            progress_path = root / "data/pilot/speed-pilot-0001-0001-progress.json"
            source_root = root / "data/sources"
            progress_path.parent.mkdir(parents=True)
            source_root.mkdir(parents=True)
            progress_path.write_text(json.dumps({
                "engine_version": "2.0.0",
                "source_completed_ids": ["source-resolved", "source-gap"],
                "source_outcomes": {
                    "source-resolved": {"status": "successful"},
                    "source-gap": {"status": "archive_lookup_failed"},
                },
            }), encoding="utf-8")
            (source_root / "source-resolved.json").write_text(json.dumps({
                "source_id": "source-resolved",
                "text_original": "Preserved evidence text",
                "content_hash": "abc",
            }), encoding="utf-8")
            (source_root / "source-gap.json").write_text(json.dumps({
                "source_id": "source-gap",
                "text_original": "",
                "content_hash": None,
            }), encoding="utf-8")

            runner = SpeedPilotRunner(
                root, root / "missing.zip", first_sequence=1, last_sequence=1,
            )
            self.assertEqual(runner.progress["source_completed_ids"], ["source-resolved"])
            self.assertNotIn("source-gap", runner.progress["source_outcomes"])
            self.assertEqual(
                runner.progress["engine_migrations"][-1]["requeued_unresolved_sources"],
                1,
            )

    def test_default_comparison_scope_is_exactly_0101_through_0150(self) -> None:
        values = _scope_sequences(101, 150)
        self.assertEqual(len(values), 50)
        self.assertEqual(values[0], 101)
        self.assertEqual(values[-1], 150)

    def test_archive_is_deferred_only_when_original_text_exists(self) -> None:
        self.assertTrue(should_defer_archive({"text_original": "Preserved verbatim text"}))
        self.assertFalse(should_defer_archive({"text_original": ""}))

    def test_previous_successful_source_is_merged_without_losing_new_relationships(self) -> None:
        seed = {
            "original_url": "https://example.org/report",
            "publisher": "Example",
            "publisher_ar": "",
            "author": "",
            "publication_date": "",
            "content": "",
            "declared_language": "English",
        }
        current = _new_source_record(seed, "source-test")
        current["incident_ids"] = ["airwars-new"]
        previous = {
            **current,
            "incident_ids": ["airwars-old"],
            "text_original": "Cached text",
            "content_hash": "abc",
            "retrieval_status": "successful",
        }
        merged = _merge_previous_source(current, previous)
        self.assertEqual(merged["text_original"], "Cached text")
        self.assertEqual(merged["incident_ids"], ["airwars-old", "airwars-new"])


if __name__ == "__main__":
    unittest.main()
