from __future__ import annotations

import argparse
import getpass
import hashlib
from pathlib import Path

from .auth import password_hash
from .db import init_db

LEGACY_SHA256 = "9f35a90d92f9fb0334cb61ffd8434aac03fd0ee61622908a11923dac40da35f3"


def hash_password() -> None:
    first = getpass.getpass("كلمة مرور مدير اللوحة: ")
    second = getpass.getpass("أعد كتابة كلمة المرور: ")
    if not first or first != second:
        raise SystemExit("كلمتا المرور غير متطابقتين")
    print(password_hash.hash(first))


def prepare_legacy(source: Path, output: Path) -> None:
    parts = sorted(source.glob("part-*"))
    if not parts:
        raise SystemExit(f"لا توجد أجزاء في {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with output.open("wb") as target:
        for part in parts:
            payload = part.read_bytes()
            digest.update(payload)
            target.write(payload)
    if digest.hexdigest() != LEGACY_SHA256:
        output.unlink(missing_ok=True)
        raise SystemExit("فشل تحقق SHA-256 للنسخة التاريخية")
    print(output)


def main() -> None:
    parser = argparse.ArgumentParser(description="أدوات إدارة نسخة VPS")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("hash-password")
    prepare = subparsers.add_parser("prepare-legacy")
    prepare.add_argument("--source", type=Path, default=Path("site-package"))
    prepare.add_argument("--output", type=Path, required=True)
    subparsers.add_parser("init-db")
    args = parser.parse_args()
    if args.command == "hash-password":
        hash_password()
    elif args.command == "prepare-legacy":
        prepare_legacy(args.source, args.output)
    else:
        init_db()


if __name__ == "__main__":
    main()
