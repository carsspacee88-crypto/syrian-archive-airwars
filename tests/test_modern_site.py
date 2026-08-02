import json
import shutil
import tempfile
import unittest
from pathlib import Path

from archive_pipeline import PARSER_VERSION, SCHEMA_VERSION
from archive_pipeline.modern_site_builder import build_modern_site
from archive_pipeline.normalized_archive import NormalizedArchive
from archive_pipeline.reports import generate_collection_summary, generate_map_coverage
from archive_pipeline.validator import validate


class ModernOnlySiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.site = self.root / "site"
        for directory in ("data/incidents", "data/sources", "data/reports", "data/generated", "web"):
            (self.project / directory).mkdir(parents=True, exist_ok=True)
        provenance = {
            key: [{"source_type": "airwars_live"}]
            for key in ("incident_code", "incident_date", "location", "latitude", "longitude")
        }
        self.record = {
            "legacy_sequence": 1,
            "internal_id": "airwars-1",
            "airwars_id": "1",
            "canonical_url": "https://airwars.org/civilian-casualties/test/",
            "incident_code": "T1",
            "incident_date": "2026-01-01",
            "location": "Test",
            "location_ar": "اختبار",
            "latitude": 35.0,
            "longitude": 38.0,
            "completeness_status": "partial",
            "retrieval_status": {"overall": "blocked"},
            "missing_fields": [],
            "missing_sections": [],
            "review_flags": [],
            "sources": [],
            "victims": [],
            "field_provenance": provenance,
            "schema_version": SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
        }
        (self.project / "data/incidents/airwars-1.json").write_text(
            json.dumps(self.record), encoding="utf-8"
        )
        web_root = Path(__file__).resolve().parents[1] / "web"
        for name in ("site.css", "map.css", "site.js", "map.js", "archive-search.js"):
            shutil.copy2(web_root / name, self.project / "web" / name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_normalized_archive_supplies_collection_identity(self) -> None:
        with NormalizedArchive(self.project) as archive:
            summary = archive.summary_by_sequence(1)
            case = archive.case_data(1)
        self.assertEqual(summary["airwars_id"], "1")
        self.assertEqual(summary["airwars_url"], self.record["canonical_url"])
        self.assertEqual(case["_normalized_record"]["internal_id"], "airwars-1")

    def test_modern_reports_build_and_validate_without_legacy_zip(self) -> None:
        collection = generate_collection_summary(None, self.project)
        map_report = generate_map_coverage(None, self.project)
        self.assertEqual(collection["total_incidents"], 1)
        self.assertEqual(collection["policy"]["report_source"], "normalized_records_only")
        self.assertEqual(map_report["source"], "normalized_records_only")
        self.assertEqual(map_report["counts"]["incidents_included_on_map"], 1)

        result = build_modern_site(self.site, self.project)
        self.assertFalse(result["architecture"]["legacy_package_required"])
        self.assertIn("map-screen", (self.site / "map.html").read_text(encoding="utf-8"))
        self.assertTrue((self.site / "cases/0001/data.json").is_file())

        report = validate(self.site, self.project, None, self.site / "data/reports")
        self.assertEqual(report["issue_counts"]["critical"], 0)
        self.assertEqual(report["checks"]["legacy_package"], {"required": False, "opened": False})


if __name__ == "__main__":
    unittest.main()
