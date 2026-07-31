from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from .io_utils import atomic_write_text


def write_checksums(site_root: Path) -> int:
    site_root = site_root.resolve()
    output = site_root / "checksums.sha256"
    rows: list[str] = []
    for path in sorted(site_root.rglob("*")):
        if not path.is_file() or path == output:
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        rows.append(f"{digest.hexdigest()}  {path.relative_to(site_root).as_posix()}")
    atomic_write_text(output, "\n".join(rows) + "\n")
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Write deterministic SHA-256 checksums for the generated site")
    parser.add_argument("--site-root", required=True)
    args = parser.parse_args()
    count = write_checksums(Path(args.site_root))
    print(f"checksummed_files={count}")


if __name__ == "__main__":
    main()
