from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any, Iterator


class LegacyArchive:
    """Read-only migration adapter for the historical Excel-generated site."""

    def __init__(self, archive_path: Path):
        self.archive_path = Path(archive_path)
        self._zip = zipfile.ZipFile(self.archive_path)
        self._summary: dict[str, Any] | None = None

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> "LegacyArchive":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _read_json(self, member: str) -> Any:
        with self._zip.open(member) as handle:
            return json.load(handle)

    @property
    def summary(self) -> dict[str, Any]:
        if self._summary is None:
            self._summary = self._read_json("data/cases-summary.json")
        return self._summary

    @property
    def build_report(self) -> dict[str, Any]:
        return self._read_json("data/build-report.json")

    def iter_summaries(self) -> Iterator[dict[str, Any]]:
        yield from self.summary.get("cases", [])

    def summary_by_sequence(self, sequence: int) -> dict[str, Any]:
        cases = self.summary.get("cases", [])
        if 1 <= sequence <= len(cases):
            candidate = cases[sequence - 1]
            if int(candidate.get("sequence", -1)) == sequence:
                return candidate
        for candidate in cases:
            if int(candidate.get("sequence", -1)) == sequence:
                return candidate
        raise KeyError(f"Legacy incident sequence not found: {sequence}")

    def case_data(self, sequence: int) -> dict[str, Any]:
        return self._read_json(f"cases/{sequence:04d}/data.json")


def assemble_legacy_zip(parts_dir: Path, output: Path) -> Path:
    parts = sorted(Path(parts_dir).glob("part-*"))
    if not parts:
        raise FileNotFoundError(f"No package parts found in {parts_dir}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as destination:
        for part in parts:
            with part.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    destination.write(chunk)
    return output

