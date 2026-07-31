from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import httpx

from archive_pipeline.fetcher import AsyncHostFetcher, CircuitBreakingFetcher, FetchResult, HostCircuitBreaker
from archive_pipeline.io_utils import utc_now
from archive_pipeline.speed_pilot import (
    _inherit_airwars_circuit_classification,
    _merge_previous_source,
    _new_source_record,
    _record_exact_content_duplicate_groups,
    _scope_sequences,
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


class SpeedPilotPolicyTests(unittest.TestCase):
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
