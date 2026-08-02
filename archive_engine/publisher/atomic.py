from __future__ import annotations

import os
from pathlib import Path
from typing import Callable


class PublishError(RuntimeError):
    pass


class AtomicPublisher:
    def __init__(self, releases_root: Path):
        self.releases_root = Path(releases_root).resolve()
        (self.releases_root / "releases").mkdir(parents=True, exist_ok=True)

    @property
    def current_link(self) -> Path:
        return self.releases_root / "current"

    def current_release(self) -> Path | None:
        try:
            return self.current_link.resolve(strict=True)
        except FileNotFoundError:
            return None

    def publish(self, release: Path, health_check: Callable[[Path], bool] | None = None) -> tuple[Path | None, Path]:
        release = Path(release).resolve(strict=True)
        allowed_root = (self.releases_root / "releases").resolve()
        if release.parent != allowed_root:
            raise PublishError("release_outside_managed_root")
        if not (release / "release.json").is_file() or not (release / "checksums" / "sha256.txt").is_file():
            raise PublishError("release_not_finalized")
        previous = self.current_release()
        temporary = self.releases_root / f".current-{release.name}"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(Path("releases") / release.name)
        os.replace(temporary, self.current_link)
        if health_check and not health_check(release):
            if previous is not None:
                rollback = self.releases_root / f".rollback-{previous.name}"
                rollback.unlink(missing_ok=True)
                rollback.symlink_to(Path("releases") / previous.name)
                os.replace(rollback, self.current_link)
            raise PublishError("atomic_health_check_failed_and_rolled_back")
        return previous, release

    def rollback(self, target: Path, health_check: Callable[[Path], bool] | None = None) -> Path:
        target = Path(target).resolve(strict=True)
        allowed_root = (self.releases_root / "releases").resolve()
        if target.parent != allowed_root:
            raise PublishError("rollback_target_outside_managed_root")
        temporary = self.releases_root / f".rollback-{target.name}"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(Path("releases") / target.name)
        os.replace(temporary, self.current_link)
        if health_check and not health_check(target):
            raise PublishError("rollback_health_check_failed")
        return target
