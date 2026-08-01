from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from archive_pipeline.extractors import extract_payload
from archive_pipeline.v4 import (
    ENGINE_VERSION,
    RecoveryQueue,
    fair_host_order,
    timing_summary,
)


def source(source_id: str, url: str, text: str = "") -> dict[str, object]:
    return {
        "source_id": source_id,
        "original_url": url,
        "incident_ids": ["airwars-1"],
        "incident_sequences": [1],
        "retrieval_status": "blocked",
        "failure_reason": "http_403",
        "text_original": text,
    }


class V4SchedulingTests(unittest.TestCase):
    def test_engine_version_is_stable(self) -> None:
        self.assertEqual(ENGINE_VERSION, "4.0.0")

    def test_fair_scheduler_round_robins_hosts(self) -> None:
        records = [
            source("a1", "https://a.example/1"),
            source("a2", "https://a.example/2"),
            source("a3", "https://a.example/3"),
            source("b1", "https://b.example/1"),
            source("c1", "https://c.example/1"),
        ]
        ordered = fair_host_order(records)
        self.assertEqual([row["source_id"] for row in ordered], ["a1", "b1", "c1", "a2", "a3"])

    def test_performance_summary_reports_p50_and_p90(self) -> None:
        summary = timing_summary([
            {"duration_seconds": 1, "network_seconds": 0.1},
            {"duration_seconds": 2, "network_seconds": 0.2},
            {"duration_seconds": 10, "network_seconds": 0.3},
        ])
        self.assertEqual(summary["sample_size"], 3)
        self.assertEqual(summary["total"]["p50_seconds"], 2.0)
        self.assertGreater(summary["total"]["p90_seconds"], 8.0)


class V4RecoveryQueueTests(unittest.TestCase):
    def test_queue_is_atomic_persistent_and_resolvable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "recovery" / "v4-0001-0002.json"
            scope = {"first_sequence": 1, "last_sequence": 2, "count": 2}
            queue = RecoveryQueue(path, scope)
            queue.defer(source("source-a", "https://a.example/post"))
            queue.defer(source("source-b", "https://b.example/post"))
            restored = RecoveryQueue(path, scope)
            self.assertEqual(restored.summary()["pending"], 2)
            restored.note_attempt("source-a", "archive_lookup_failed")
            restored.resolve(source("source-a", "https://a.example/post", "preserved"), "successful")
            final = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("source-a", final["pending"])
            self.assertEqual(final["resolved"]["source-a"]["recovery_attempts"], 1)
            self.assertEqual(final["pending"]["source-b"]["host"], "b.example")

    def test_queue_rejects_a_different_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.json"
            RecoveryQueue(path, {"first_sequence": 1, "last_sequence": 1, "count": 1}).save()
            with self.assertRaises(ValueError):
                RecoveryQueue(path, {"first_sequence": 2, "last_sequence": 2, "count": 1})


class V4ExtractionTests(unittest.TestCase):
    def test_json_and_csv_are_textually_preserved(self) -> None:
        json_result = extract_payload(
            json.dumps({"title": "شهادة", "count": 3}, ensure_ascii=False).encode(),
            "application/json; charset=utf-8",
            "https://example.org/report.json",
            "other_web_page",
        )
        self.assertEqual(json_result["format"], "json")
        self.assertIn("شهادة", json_result["text"])
        csv_result = extract_payload(
            "name,value\nalpha,1\nbeta,2\n".encode(),
            "text/csv",
            "https://example.org/report.csv",
            "other_web_page",
        )
        self.assertIn("alpha | 1", csv_result["text"])

    def test_docx_text_is_extracted_without_committing_the_binary(self) -> None:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr(
                "word/document.xml",
                "<?xml version='1.0' encoding='UTF-8'?>"
                "<w:document xmlns:w='urn:test'><w:body><w:p><w:r><w:t>Archived testimony</w:t>"
                "</w:r></w:p></w:body></w:document>",
            )
        result = extract_payload(
            payload.getvalue(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "https://example.org/testimony.docx",
            "other_web_page",
        )
        self.assertEqual(result["format"], "docx")
        self.assertIn("Archived testimony", result["text"])


if __name__ == "__main__":
    unittest.main()
