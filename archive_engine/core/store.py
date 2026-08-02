from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"not_json_serializable:{type(value).__name__}")


class ProjectStore:
    """Atomic file store used by every connector and by checkpoint recovery."""

    def __init__(self, root: Path, before_replace: Callable[[Path], None] | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.before_replace = before_replace

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def write_json(self, relative: str, value: Any) -> Path:
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if self.before_replace:
                self.before_replace(target)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def read_json(self, relative: str, default: Any = None) -> Any:
        target = self.path(relative)
        if not target.is_file():
            return default
        with target.open(encoding="utf-8") as handle:
            return json.load(handle)

    def preserve_raw(self, run_id: str, identity: str, body: bytes, metadata: dict[str, Any]) -> dict[str, Any]:
        digest = hashlib.sha256(body).hexdigest()
        raw_root = self.path("runs", run_id, "raw")
        raw_root.mkdir(parents=True, exist_ok=True)
        body_path = raw_root / f"{digest}.bin"
        if not body_path.exists():
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", dir=raw_root)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, body_path)
            finally:
                temporary.unlink(missing_ok=True)
        identity_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        self.write_json(
            f"runs/{run_id}/raw/attempts/{identity_digest}-{digest}.json",
            {"identity": identity, "sha256": digest, **metadata},
        )
        return {"sha256": digest, "path": str(body_path.relative_to(self.root)), "bytes": len(body)}

    def append_audit(self, run_id: str, event: dict[str, Any]) -> None:
        target = self.path("runs", run_id, "audit.jsonl")
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, sort_keys=True, default=_json_default) + "\n"
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
