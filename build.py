"""
Build the release files: the .sdkmod and the Nexus zip around it.

    python build.py            -> build/dmgtracker.sdkmod and build/dmgtracker-<version>.zip

The .sdkmod is a zip whose single root entry is the mod folder, named like the file
(the SDK loader checks exactly that and ignores anything else). The Nexus zip contains
`sdk_mods/dmgtracker.sdkmod` and nothing else: Vortex strips the leading folder and drops
the .sdkmod into sdk_mods, a manual installer extracts the zip into the game root, and any
loose file next to the .sdkmod would be reported as an invalid mod by the loader.

Standard library only; runs with any Python 3.11+.
"""

from __future__ import annotations

import sys
import tomllib
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
MOD_NAME = "dmgtracker"
MOD_DIR = REPO / MOD_NAME
BUILD_DIR = REPO / "build"
PYPROJECT = MOD_DIR / "pyproject.toml"
EXCLUDED_DIRS = {"__pycache__"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
SDK_MODS_FOLDER = "sdk_mods"


def mod_version() -> str:
    with PYPROJECT.open("rb") as file:
        return str(tomllib.load(file)["project"]["version"])


def package_files() -> list[Path]:
    """Every file of the mod folder that belongs in the release, in a stable order."""
    files = []
    for path in sorted(MOD_DIR.rglob("*")):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in path.relative_to(MOD_DIR).parts):
            continue
        if path.suffix in EXCLUDED_SUFFIXES:
            continue
        files.append(path)
    return files


def write_sdkmod(destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in package_files():
            archive.write(path, f"{MOD_NAME}/{path.relative_to(MOD_DIR).as_posix()}")


def write_release_zip(sdkmod: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(sdkmod, f"{SDK_MODS_FOLDER}/{sdkmod.name}")


def check_sdkmod(path: Path) -> None:
    """The same check the SDK loader makes: one root entry, named like the file."""
    with zipfile.ZipFile(path) as archive:
        roots = {name.split("/", 1)[0] for name in archive.namelist()}
    if roots != {path.stem}:
        raise SystemExit(f"{path.name}: root entries {sorted(roots)}, expected exactly {path.stem!r}")


def main() -> int:
    BUILD_DIR.mkdir(exist_ok=True)
    version = mod_version()
    sdkmod = BUILD_DIR / f"{MOD_NAME}.sdkmod"
    release = BUILD_DIR / f"{MOD_NAME}-{version}.zip"
    write_sdkmod(sdkmod)
    check_sdkmod(sdkmod)
    write_release_zip(sdkmod, release)
    print(f"{sdkmod} ({sdkmod.stat().st_size:,} bytes, {len(package_files())} files)")
    print(f"{release} ({release.stat().st_size:,} bytes) containing {SDK_MODS_FOLDER}/{sdkmod.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
