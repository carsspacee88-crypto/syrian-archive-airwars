from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable

from archive_engine.models import ValidationResult


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def iter_release_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.relative_to(root).as_posix() != "checksums/sha256.txt":
            yield path


def write_checksums(root: Path) -> Path:
    root = Path(root)
    target = root / "checksums" / "sha256.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{sha256_file(path)}  {path.relative_to(root).as_posix()}" for path in iter_release_files(root)]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


class ReleaseValidator:
    def validate_structure(self, root: Path) -> ValidationResult:
        root = Path(root)
        blocking: list[dict] = []
        checks: dict[str, object] = {}
        required = ["release.json", "data", "site", "reports", "logs", "checksums/sha256.txt"]
        missing = [name for name in required if not (root / name).exists()]
        if missing:
            blocking.append({"code": "missing_release_artifacts", "paths": missing})
        checks["required_artifacts"] = {"required": required, "missing": missing}
        return ValidationResult(not blocking, blocking, [], checks)

    def validate_checksums(self, root: Path) -> ValidationResult:
        root = Path(root)
        target = root / "checksums" / "sha256.txt"
        blocking: list[dict] = []
        checked = 0
        if not target.is_file():
            blocking.append({"code": "missing_checksum_manifest"})
        else:
            for line_number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    expected, relative = line.split("  ", 1)
                except ValueError:
                    blocking.append({"code": "malformed_checksum_line", "line": line_number})
                    continue
                path = root / relative
                if not path.is_file():
                    blocking.append({"code": "checksummed_file_missing", "path": relative})
                elif sha256_file(path) != expected:
                    blocking.append({"code": "checksum_mismatch", "path": relative})
                checked += 1
        return ValidationResult(not blocking, blocking, [], {"checksums": {"checked": checked}})

    def validate(self, root: Path) -> ValidationResult:
        structure = self.validate_structure(root)
        checksums = self.validate_checksums(root)
        blocking = structure.blocking_failures + checksums.blocking_failures
        return ValidationResult(not blocking, blocking, [], {**structure.checks, **checksums.checks})

    @staticmethod
    def write_report(path: Path, result: ValidationResult) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "result": "passed" if result.passed else "failed",
            "blocking_failures": result.blocking_failures,
            "non_blocking_failures": result.non_blocking_failures,
            "checks": result.checks,
        }, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
