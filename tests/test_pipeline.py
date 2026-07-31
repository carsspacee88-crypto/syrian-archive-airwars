from __future__ import annotations

import unittest
from unittest.mock import patch

from archive_pipeline.normalize import apply_page_extraction, build_legacy_record, finalize_status
from archive_pipeline.parser import parse_incident_html
from archive_pipeline.pilot import ArabicTranslator, _exact_pilot_sequence, normalize_source_url, stable_source_id
from archive_pipeline.reports import coordinate_reasons


CLASSIC_HTML = '''<!doctype html><html><head><title>اختبار – Airwars</title></head><body>
<article>
  <div class="meta-block code current"><h4>Incident Code</h4><p>TEST001</p></div>
  <div class="meta-block"><h4>Incident date</h4><p>June 19, 2017</p></div>
  <div class="meta-block location"><h4>Location</h4><p>الرقة, Raqqa, Syria</p></div>
  <div class="info-left"><h4>Geolocation</h4><p>35.9, 4239.00416</p></div>
  <div class="info-main-block"><h2>Airwars assessment</h2><p>Test narrative.</p></div>
  <div class="info-main-block sources"><h2>Sources (1)</h2><ul class="meta-list sources-list"><li>
    <div class="source-title"><a href="https://example.org/report">Example</a></div>
    <div class="source-tags"><ul><li class="tag">Arabic</li></ul></div>
    <div class="archive-link"><a class="archive" href="https://web.archive.org/example">Archive</a></div>
  </li></ul></div>
</article></body></html>'''.encode("utf-8")


class ParserTests(unittest.TestCase):
    def test_classic_page_preserves_utf8_and_sources(self) -> None:
        parsed = parse_incident_html(CLASSIC_HTML, "https://airwars.org/civilian-casualties/test/")
        self.assertIn("الرقة", next(row["value"] for row in parsed["fields"] if row["label"] == "Location"))
        self.assertTrue(parsed["sources_section_present"])
        self.assertEqual(parsed["sources_declared"], 1)
        self.assertEqual(parsed["sources"][0]["name"], "Example")
        self.assertEqual(parsed["sources"][0]["archive_url"], "https://web.archive.org/example")

    def test_invalid_coordinate_is_not_silently_corrected(self) -> None:
        summary = {
            "sequence": 1, "airwars_id": "99", "code": "TEST001",
            "airwars_url": "https://airwars.org/civilian-casualties/test/",
            "date": "2017-06-19", "location_original": "Raqqa", "completion": "partial",
        }
        legacy = {"incident": {"خط العرض": 35.9, "خط الطول": 4239.00416}}
        record = build_legacy_record(summary, legacy)
        apply_page_extraction(record, parse_incident_html(CLASSIC_HTML, summary["airwars_url"]), "airwars_archive", "https://web.archive.org/example", "2026-01-01T00:00:00+00:00", "abc")
        finalize_status(record)
        self.assertEqual(record["longitude"], 4239.00416)
        self.assertIn("invalid_longitude", record["review_flags"])


class CoordinateTests(unittest.TestCase):
    def test_reason_taxonomy(self) -> None:
        self.assertEqual(coordinate_reasons(None, None), ["missing_latitude", "missing_longitude"])
        self.assertIn("invalid_longitude", coordinate_reasons(35.9, 4239.0))
        self.assertIn("parsing_error", coordinate_reasons("not-a-number", 36.0))
        self.assertIn("outside_expected_region", coordinate_reasons(36.6, 45.0))
        self.assertEqual(coordinate_reasons(35.9, 36.0), [])


class PilotTests(unittest.TestCase):
    def test_pilot_scope_hard_stops_after_0100(self) -> None:
        self.assertEqual(_exact_pilot_sequence(100), 100)
        with self.assertRaisesRegex(ValueError, "pilot_scope_violation"):
            _exact_pilot_sequence(101)

    def test_source_id_uses_conservative_url_normalization(self) -> None:
        left = "HTTPS://Example.COM:443/a?b=1#fragment"
        right = "https://example.com/a?b=1"
        self.assertEqual(normalize_source_url(left), right)
        self.assertEqual(stable_source_id(left), stable_source_id(right))

    def test_translation_chunks_are_complete_and_ordered(self) -> None:
        text = "فقرة أولى\n\n" + ("x" * 8000) + "\n\nفقرة أخيرة"
        chunks = ArabicTranslator.chunks(text, max_chars=1000)
        self.assertGreater(len(chunks), 2)
        self.assertEqual("".join(chunks).replace("\n", ""), text.replace("\n", ""))

    def test_translation_provider_prefers_deepl_when_configured(self) -> None:
        with patch.dict("os.environ", {"DEEPL_API_KEY": "example:fx", "TRANSLATION_PROVIDER": "auto"}, clear=True):
            translator = ArabicTranslator()
        self.assertEqual(translator.provider, "deepl")
        self.assertEqual(translator.model, "deepl-text-v2")


if __name__ == "__main__":
    unittest.main()
