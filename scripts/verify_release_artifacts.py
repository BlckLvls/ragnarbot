#!/usr/bin/env python3
"""Verify release archives before they can be uploaded."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path

FORBIDDEN_PARTS = {
    ".agents",
    ".claude",
    ".codex",
    "__pycache__",
    "scratchpad",
    "webui",
}


def _normalized_names(archive: Path) -> list[str]:
    if archive.suffix == ".whl":
        with zipfile.ZipFile(archive) as wheel:
            return wheel.namelist()

    with tarfile.open(archive, mode="r:gz") as sdist:
        names = []
        for member in sdist.getnames():
            parts = Path(member).parts
            names.append("/".join(parts[1:]) if len(parts) > 1 else member)
        return names


def _verify_archive(archive: Path, version: str) -> None:
    names = _normalized_names(archive)
    lowered = archive.name.lower()
    normalized_version = version.replace("-", "_")

    if normalized_version not in lowered:
        raise SystemExit(f"{archive}: filename does not contain version {version}")
    if "ragnarbot/__init__.py" not in names:
        raise SystemExit(f"{archive}: package root is missing")
    if "ragnarbot/web/static/index.html" not in names:
        raise SystemExit(f"{archive}: Web UI index is missing")
    if not any(
        name.startswith("ragnarbot/web/static/assets/") and name.endswith(".js")
        for name in names
    ):
        raise SystemExit(f"{archive}: Web UI JavaScript assets are missing")
    if not any(
        name.startswith("ragnarbot/web/static/assets/") and name.endswith(".css")
        for name in names
    ):
        raise SystemExit(f"{archive}: Web UI CSS assets are missing")

    forbidden = []
    for name in names:
        parts = set(Path(name).parts)
        if parts & FORBIDDEN_PARTS or name.endswith((".pyc", ".pyo")):
            forbidden.append(name)
    if forbidden:
        preview = "\n".join(f"  - {name}" for name in forbidden[:20])
        raise SystemExit(f"{archive}: forbidden release content:\n{preview}")

    print(f"verified {archive} ({len(names)} entries)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist", type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()

    wheels = sorted(args.dist.glob("*.whl"))
    sdists = sorted(args.dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit(
            f"expected one wheel and one sdist, found {len(wheels)} wheel(s) "
            f"and {len(sdists)} sdist(s)"
        )

    for archive in [*wheels, *sdists]:
        _verify_archive(archive, args.version)


if __name__ == "__main__":
    main()
