from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from archive_engine.connectors.airwars.connector import (
    AirwarsConnector,
    classify_source_content,
    source_reachability,
    validate_full_source_text,
)
from archive_engine.connectors.base import DiscoveredTarget
from archive_engine.connectors.synthetic import SyntheticLibraryConnector
from archive_engine.core.engine import ArchiveEngine, EngineControl, RunPolicy
from archive_engine.core.store import ProjectStore
from archive_engine.fetchers.http import FetchResponse, classify_http_result
from archive_engine.models import ArchiveProject
from archive_engine.normalizers.urls import normalize_url
from archive_engine.publisher.atomic import AtomicPublisher, PublishError
from archive_engine.release_builder import GenericReleaseBuildInterrupted, GenericTextualReleaseBuilder
from archive_engine.statuses import CollectionStatus, RunStatus, SourceContentStatus
from archive_engine.validators.release import ReleaseValidator


LIBRARY_A = b'''<!doctype html><article data-accession="LIB-001"><h1 class="entry-title">River archives</h1><time class="published" datetime="2024-01-02"></time><section class="abstract">A complete abstract about river records.</section><a class="reference-link" href="https://sources.example/report?id=1">Report one</a></article>'''
LIBRARY_B = b'''<!doctype html><article data-accession="LIB-002"><h1 class="entry-title">Mountain archives</h1><section class="abstract">A second unrelated catalogue abstract.</section><a class="reference-link" href="https://sources.example/report?id=1">Same report, separate relation</a><a class="reference-link" href="not a url">Malformed evidence</a></article>'''


class MappingFetcher:
    def __init__(self, bodies: dict[str, bytes], *, interrupt_after: int | None = None):
        self.bodies = bodies
        self.interrupt_after = interrupt_after
        self.calls = 0

    def fetch(self, url: str) -> FetchResponse:
        self.calls += 1
        if self.interrupt_after and self.calls > self.interrupt_after:
            raise RuntimeError("simulated_worker_loss")
        body = self.bodies[url]
        return FetchResponse(url, url, 200, "text/html; charset=utf-8", body, CollectionStatus.FETCHED, [])


class ConcurrentFetcher(MappingFetcher):
    def __init__(self, bodies: dict[str, bytes]):
        super().__init__(bodies)
        self.barrier = threading.Barrier(2)

    def fetch(self, url: str) -> FetchResponse:
        self.barrier.wait(timeout=2)
        return super().fetch(url)


def synthetic_project() -> ArchiveProject:
    return ArchiveProject.create(
        "Synthetic public library",
        "https://library.example/catalogue",
        "synthetic_library",
        {"records": [
            {"id": "catalogue-1", "url": "https://library.example/records/1"},
            {"id": "catalogue-2", "url": "https://library.example/records/2"},
        ]},
        allowed_domains=["library.example"],
        release_name="library-text",
    )


class HttpAndStatusTests(unittest.TestCase):
    def test_http_403_is_blocked_and_never_success(self) -> None:
        self.assertEqual(classify_http_result(403), CollectionStatus.BLOCKED)
        self.assertNotEqual(classify_http_result(403), CollectionStatus.FETCHED)

    def test_http_404_is_dead_and_timeout_retryable(self) -> None:
        self.assertEqual(classify_http_result(404), CollectionStatus.DEAD)
        self.assertEqual(classify_http_result(None, "TimeoutError: timed out"), CollectionStatus.RETRYABLE_FAILURE)

    def test_exact_status_models(self) -> None:
        self.assertEqual([item.value for item in CollectionStatus], ["DISCOVERED", "QUEUED", "FETCH_ATTEMPTED", "FETCHED", "PARSED", "NORMALIZED", "PARTIAL", "BLOCKED", "DEAD", "MALFORMED", "RETRYABLE_FAILURE", "FINAL_FAILURE", "NEEDS_MANUAL_REVIEW"])
        self.assertEqual([item.value for item in SourceContentStatus], ["REFERENCE_ONLY", "URL_PRESERVED", "METADATA_ONLY", "PARTIAL_TEXT", "FULL_TEXT_DIRECT", "FULL_TEXT_ARCHIVED", "FULL_TEXT_LOCAL_SNAPSHOT", "BLOCKED", "DEAD", "MALFORMED", "RESTRICTED", "NEEDS_MANUAL_REVIEW"])
        self.assertEqual([item.value for item in RunStatus], ["CREATED", "ANALYZING", "PILOT_RUNNING", "PILOT_REVIEW", "READY", "QUEUED", "RUNNING", "PAUSED", "RETRYING", "VALIDATING", "BUILDING_RELEASE", "RELEASE_VALIDATION", "PUBLISHED", "COMPLETED", "COMPLETED_WITH_GAPS", "FAILED", "CANCELLED"])

    def test_malformed_url_preserves_raw_value_and_reason(self) -> None:
        result = normalize_url("not a url")
        self.assertEqual(result.raw_value, "not a url")
        self.assertEqual(result.normalization_status, "malformed")
        self.assertTrue(result.normalization_reason)


class SourceTruthfulnessTests(unittest.TestCase):
    def test_metadata_partial_and_full_are_distinct(self) -> None:
        metadata = {"original_url": "https://example.org/a", "page_title": "Known title"}
        partial = {"original_url": "https://example.org/b", "text_original": "Excerpt only", "content_quality": {"accepted": True, "completeness": "partial"}}
        full = {"original_url": "https://x.example/post", "source_type": "public_x_post", "text_original": "A complete short public post", "content_quality": {"accepted": True, "completeness": "full", "extraction_method": "semantic_dom", "provenance": "x_oembed"}}
        self.assertEqual(classify_source_content(metadata), SourceContentStatus.METADATA_ONLY)
        self.assertEqual(classify_source_content(partial), SourceContentStatus.PARTIAL_TEXT)
        self.assertEqual(classify_source_content(full), SourceContentStatus.FULL_TEXT_DIRECT)
        self.assertEqual(validate_full_source_text(full), (True, "structured_extractor_validated"))

    def test_login_shell_cannot_be_full_text(self) -> None:
        shell = {"original_url": "https://facebook.com/post", "source_type": "public_facebook_post", "text_original": "Познакомьтесь с тем, что вам нравится. Войдите на Facebook Электронный адрес или номер мобильного телефона Пароль", "content_quality": {"accepted": True, "completeness": "full", "extraction_method": "trafilatura", "provenance": "source_live"}}
        passed, reason = validate_full_source_text(shell)
        self.assertFalse(passed)
        self.assertEqual(reason, "facebook_login_shell")
        self.assertEqual(classify_source_content(shell), SourceContentStatus.PARTIAL_TEXT)

    def test_archived_provenance_is_not_confused_with_archive_url_metadata(self) -> None:
        direct = {"original_url": "https://x.example/post", "archived_urls": ["https://archive.example/copy"], "preservation_status": "archived_text_preserved", "source_type": "public_x_post", "text_original": "Complete public post text", "content_quality": {"accepted": True, "completeness": "full", "extraction_method": "semantic_dom", "provenance": "x_oembed"}}
        archived = {**direct, "content_quality": {**direct["content_quality"], "provenance": "listed_archive"}}
        self.assertEqual(classify_source_content(direct), SourceContentStatus.FULL_TEXT_DIRECT)
        self.assertEqual(classify_source_content(archived), SourceContentStatus.FULL_TEXT_ARCHIVED)

    def test_source_403_is_blocked_without_text(self) -> None:
        record = {"original_url": "https://example.org/blocked", "attempt_history": [{"status": 403, "error": "HTTPError:403"}]}
        self.assertEqual(source_reachability(record), CollectionStatus.BLOCKED)
        self.assertEqual(classify_source_content(record), SourceContentStatus.BLOCKED)


class ConnectorTests(unittest.TestCase):
    def test_archived_copy_does_not_override_direct_http_403(self) -> None:
        current = {
            "internal_id": "airwars-archive-fixture",
            "airwars_id": "fixture",
            "canonical_url": "https://airwars.org/civilian-casualties/fixture/",
            "page_extraction": {"source_type": "airwars_archive"},
            "retrieval_status": {
                "airwars_endpoint": {"ok": False, "status": 403},
                "live_page": {"ok": False, "circuit_open_status": 403},
                "archive_page": {"ok": True, "status": 200},
            },
            "narrative_original": "Archived incident narrative",
            "latitude": None,
            "longitude": None,
        }
        self.assertEqual(AirwarsConnector._direct_verification(current), "BLOCKED_HTTP_403")
        connector = object.__new__(AirwarsConnector)
        connector._records_by_sequence = {1: current}
        structured = connector.structured_incident(1, {"incident": {}, "sources": []})
        self.assertEqual(structured["direct_verification_status"], "BLOCKED_HTTP_403")
        self.assertEqual(structured["record_origin_status"], "mixed_historical_and_archived_airwars")

    def test_synthetic_analysis_and_parse_use_non_airwars_fields(self) -> None:
        connector = SyntheticLibraryConnector()
        project = synthetic_project()
        analysis = connector.analyze(project, {project.site.target_url: LIBRARY_A})
        self.assertIn("accession_number", analysis["detected_fields"])
        self.assertNotIn("incident_code", json.dumps(analysis))
        parsed = connector.parse(connector.discover(project)[0], LIBRARY_A, "text/html")
        self.assertEqual(parsed.record.fields["accession_number"].normalized_value, "LIB-001")
        self.assertEqual(len(parsed.record.source_references), 1)

    def test_malformed_html_and_missing_required_field_fail(self) -> None:
        connector = SyntheticLibraryConnector()
        target = connector.discover(synthetic_project())[0]
        with self.assertRaisesRegex(ValueError, "catalogue_entry_missing"):
            connector.parse(target, b"<html><p>not an entry</p></html>", "text/html")
        with self.assertRaisesRegex(ValueError, "missing_required_field:abstract"):
            connector.parse(target, b'<article data-accession="X"><h1 class="entry-title">Title</h1></article>', "text/html")

    def test_sanitized_airwars_fixture_parses_without_project_specific_core(self) -> None:
        connector = object.__new__(AirwarsConnector)
        target = DiscoveredTarget("airwars-fixture-1", "https://airwars.org/civilian-casualties/fixture/", "civilian_casualty_incident")
        body = b'<article><h1 data-incident-code="AW-1">AW-1</h1><time datetime="2020-01-02"></time><section class="assessment">Fixture narrative</section></article>'
        parsed = connector.parse(target, body, "text/html")
        self.assertEqual(parsed.record.fields["textual_description"].normalized_value, "Fixture narrative")


class DuplicateConnector(SyntheticLibraryConnector):
    def discover(self, project):
        target = super().discover(project)[0]
        return [target, target]


class EngineRecoveryAndReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = synthetic_project()
        self.bodies = {
            "https://library.example/records/1": LIBRARY_A,
            "https://library.example/records/2": LIBRARY_B,
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_duplicate_identifier_blocks_immutable_worklist(self) -> None:
        engine = ArchiveEngine(ProjectStore(self.root / "store"), DuplicateConnector(), MappingFetcher(self.bodies))
        run = engine.create_run(self.project, "full", "duplicate-run")
        with self.assertRaisesRegex(ValueError, "duplicate_discovered_identifier"):
            engine.execute(self.project, run)

    def test_interrupted_run_resumes_from_checkpoint(self) -> None:
        store = ProjectStore(self.root / "store")
        engine = ArchiveEngine(store, SyntheticLibraryConnector(), MappingFetcher(self.bodies, interrupt_after=1), RunPolicy(checkpoint_every=1))
        run = engine.create_run(self.project, "full", "resume-run")
        with self.assertRaisesRegex(RuntimeError, "simulated_worker_loss"):
            engine.execute(self.project, run)
        checkpoint = store.read_json("runs/resume-run/checkpoint.json")
        self.assertEqual(len(checkpoint["completed"]), 1)
        resumed = ArchiveEngine(store, SyntheticLibraryConnector(), MappingFetcher(self.bodies), RunPolicy(checkpoint_every=1)).execute(self.project, run)
        self.assertEqual(resumed.status, RunStatus.COMPLETED)
        self.assertEqual(resumed.counts["completed"], 2)
        self.assertEqual(len(store.read_json("runs/resume-run/checkpoint.json")["completed"]), 2)

    def test_max_workers_is_an_enforced_concurrency_boundary(self) -> None:
        store = ProjectStore(self.root / "concurrent-store")
        engine = ArchiveEngine(store, SyntheticLibraryConnector(), ConcurrentFetcher(self.bodies), RunPolicy(max_workers=2, checkpoint_every=1))
        run = engine.create_run(self.project, "full", "concurrent-run")
        result = engine.execute(self.project, run)
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(len(list((self.root / "concurrent-store" / "runs" / "concurrent-run" / "raw" / "attempts").glob("*.json"))), 2)

    def test_pause_resume_and_safe_cancel_control(self) -> None:
        store = ProjectStore(self.root / "store")
        engine = ArchiveEngine(store, SyntheticLibraryConnector(), MappingFetcher(self.bodies))
        run = engine.create_run(self.project, "full", "control-run")
        control = EngineControl(store, run.run_id)
        control.request("pause")
        self.assertEqual(engine.execute(self.project, run).status, RunStatus.PAUSED)
        control.request("resume")
        self.assertEqual(engine.execute(self.project, run).status, RunStatus.COMPLETED)
        cancelled = engine.create_run(self.project, "full", "cancel-run")
        EngineControl(store, cancelled.run_id).request("cancel")
        self.assertEqual(engine.execute(self.project, cancelled).status, RunStatus.CANCELLED)

    def test_atomic_json_interruption_keeps_previous_value(self) -> None:
        store = ProjectStore(self.root / "atomic")
        store.write_json("value.json", {"version": 1})
        store.before_replace = lambda _target: (_ for _ in ()).throw(RuntimeError("simulated_replace_failure"))
        with self.assertRaisesRegex(RuntimeError, "simulated_replace_failure"):
            store.write_json("value.json", {"version": 2})
        self.assertEqual(store.read_json("value.json"), {"version": 1})
        self.assertFalse(list((self.root / "atomic").glob(".value.json.*")))

    def _completed_store(self) -> ProjectStore:
        store = ProjectStore(self.root / "store")
        engine = ArchiveEngine(store, SyntheticLibraryConnector(), MappingFetcher(self.bodies), RunPolicy(checkpoint_every=1))
        run = engine.create_run(self.project, "full", "complete-run")
        result = engine.execute(self.project, run)
        self.assertEqual(result.status, RunStatus.COMPLETED)
        return store

    def test_non_airwars_end_to_end_relationships_release_publish_and_rollback(self) -> None:
        self._completed_store()
        managed = self.root / "managed"
        release1 = managed / "releases" / "library-r1"
        result = GenericTextualReleaseBuilder(self.root / "store", self.project, "complete-run", release1, release_id="library-r1").build()
        self.assertEqual(result["records"], 2)
        self.assertEqual(result["source_references"], 3)
        self.assertEqual(result["external_sources"], 2)  # one shared valid URL plus one malformed raw value
        self.assertEqual(result["relationships_lost"], 0)
        shared = [json.loads(path.read_text()) for path in (release1 / "data" / "external-sources").glob("*.json") if len(json.loads(path.read_text())["reference_ids"]) == 2]
        self.assertEqual(len(shared), 1)
        self.assertTrue(ReleaseValidator().validate(release1).passed)
        publisher = AtomicPublisher(managed)
        previous, current = publisher.publish(release1, lambda path: (path / "site" / "index.html").is_file())
        self.assertIsNone(previous)
        self.assertEqual(current.name, "library-r1")
        release2 = managed / "releases" / "library-r2"
        shutil.copytree(release1, release2)
        publisher.publish(release2, lambda _path: True)
        self.assertEqual(publisher.current_release().name, "library-r2")
        self.assertEqual(publisher.rollback(release1, lambda _path: True).name, "library-r1")
        self.assertEqual(publisher.current_release().name, "library-r1")

    def test_interrupted_release_build_resumes_without_losing_records(self) -> None:
        self._completed_store()
        release = self.root / "managed" / "releases" / "interrupted"
        with self.assertRaises(GenericReleaseBuildInterrupted):
            GenericTextualReleaseBuilder(self.root / "store", self.project, "complete-run", release, release_id="interrupted", interrupt_after_records=1).build()
        self.assertFalse((release / "release.json").exists())
        result = GenericTextualReleaseBuilder(self.root / "store", self.project, "complete-run", release, release_id="interrupted", resume=True).build()
        self.assertEqual(result["records"], 2)
        self.assertTrue(ReleaseValidator().validate(release).passed)

    def test_checksum_mismatch_is_blocking(self) -> None:
        self._completed_store()
        release = self.root / "managed" / "releases" / "checksum"
        GenericTextualReleaseBuilder(self.root / "store", self.project, "complete-run", release, release_id="checksum").build()
        (release / "site" / "index.html").write_text("tampered", encoding="utf-8")
        result = ReleaseValidator().validate(release)
        self.assertFalse(result.passed)
        self.assertIn("checksum_mismatch", {item["code"] for item in result.blocking_failures})

    def test_failed_atomic_deployment_restores_previous_release(self) -> None:
        self._completed_store()
        managed = self.root / "managed"
        first = managed / "releases" / "first"
        second = managed / "releases" / "second"
        GenericTextualReleaseBuilder(self.root / "store", self.project, "complete-run", first, release_id="first").build()
        shutil.copytree(first, second)
        publisher = AtomicPublisher(managed)
        publisher.publish(first, lambda _path: True)
        with self.assertRaisesRegex(PublishError, "rolled_back"):
            publisher.publish(second, lambda _path: False)
        self.assertEqual(publisher.current_release().name, "first")


if __name__ == "__main__":
    unittest.main()
