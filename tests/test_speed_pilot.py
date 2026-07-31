from __future__ import annotations

import asyncio
import unittest
from unittest.mock import Mock

import httpx

from archive_pipeline.fetcher import AsyncHostFetcher, CircuitBreakingFetcher, FetchResult, HostCircuitBreaker
from archive_pipeline.io_utils import utc_now
from archive_pipeline.speed_pilot import _merge_previous_source, _new_source_record, _scope_sequences, should_defer_archive


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
