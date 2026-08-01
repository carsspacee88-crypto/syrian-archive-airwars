from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.resolve_v4_request import load_request, resolve_request, write_github_outputs


def valid_request() -> dict[str, object]:
    return {
        "request_id": "collect-0151-0250-r1",
        "operation": "collect",
        "first_sequence": 151,
        "last_sequence": 250,
        "workers": 64,
        "per_host_workers": 1,
        "delay": 0.75,
        "source_batch_size": 1000,
        "recovery_limit": 500,
    }


class V4RequestTests(unittest.TestCase):
    def test_push_request_is_loaded_and_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text(json.dumps(valid_request()), encoding="utf-8")
            result = resolve_request(load_request("push", path, {}))
        self.assertEqual(result["request_id"], "collect-0151-0250-r1")
        self.assertEqual(result["first_sequence"], "151")
        self.assertEqual(result["last_sequence"], "250")
        self.assertEqual(result["delay"], "0.75")

    def test_dispatch_inputs_do_not_depend_on_the_request_file(self) -> None:
        payload = {name: str(value) for name, value in valid_request().items()}
        result = resolve_request(load_request("workflow_dispatch", Path("missing.json"), payload))
        self.assertEqual(result["operation"], "collect")
        self.assertEqual(result["workers"], "64")

    def test_invalid_range_and_host_concurrency_are_rejected(self) -> None:
        payload = valid_request()
        payload["last_sequence"] = 150
        with self.assertRaisesRegex(ValueError, "last_sequence"):
            resolve_request(payload)
        payload = valid_request()
        payload["per_host_workers"] = 65
        with self.assertRaisesRegex(ValueError, "per_host_workers"):
            resolve_request(payload)

    def test_outputs_are_safe_single_line_values(self) -> None:
        outputs = resolve_request(valid_request())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "github-output.txt"
            write_github_outputs(outputs, path)
            lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), len(outputs))
        self.assertIn("operation=collect", lines)
        self.assertIn("request_id=collect-0151-0250-r1", lines)


if __name__ == "__main__":
    unittest.main()
