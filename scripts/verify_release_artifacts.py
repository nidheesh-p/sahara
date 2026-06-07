"""Verify Sahara release artifacts before publishing them."""

from __future__ import annotations

import argparse
import email
import re
import tarfile
import tomllib
import zipfile
from pathlib import Path

EXPECTED_DISTRIBUTION = "sahara-memory"
EXPECTED_CONSOLE_SCRIPT = "sahara = sahara.cli:main"


def _normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _project_metadata(project_file: Path) -> tuple[str, str]:
    with project_file.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return str(project["name"]), str(project["version"])


def _parse_metadata(content: bytes) -> tuple[str, str]:
    metadata = email.message_from_bytes(content)
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not name or not version:
        raise ValueError("artifact metadata must include Name and Version")
    return name, version


def _verify_identity(name: str, version: str, expected_version: str, source: str) -> None:
    if _normalized_name(name) != _normalized_name(EXPECTED_DISTRIBUTION):
        raise ValueError(
            f"{source} reports distribution {name!r}; expected {EXPECTED_DISTRIBUTION!r}"
        )
    if version != expected_version:
        raise ValueError(f"{source} reports version {version!r}; expected {expected_version!r}")


def verify_wheel(path: Path, expected_version: str) -> None:
    with zipfile.ZipFile(path) as archive:
        metadata_files = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        entry_point_files = [
            name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")
        ]
        if len(metadata_files) != 1:
            raise ValueError(f"{path.name} must contain exactly one METADATA file")
        if len(entry_point_files) != 1:
            raise ValueError(f"{path.name} must contain exactly one entry_points.txt file")

        name, version = _parse_metadata(archive.read(metadata_files[0]))
        _verify_identity(name, version, expected_version, path.name)
        entry_points = archive.read(entry_point_files[0]).decode("utf-8")
        if EXPECTED_CONSOLE_SCRIPT not in entry_points:
            raise ValueError(f"{path.name} does not install the sahara console command")


def verify_sdist(path: Path, expected_version: str) -> None:
    with tarfile.open(path, "r:gz") as archive:
        metadata_files = [
            member for member in archive.getmembers() if member.name.endswith("/PKG-INFO")
        ]
        if len(metadata_files) != 1:
            raise ValueError(f"{path.name} must contain exactly one PKG-INFO file")
        metadata_handle = archive.extractfile(metadata_files[0])
        if metadata_handle is None:
            raise ValueError(f"could not read metadata from {path.name}")
        name, version = _parse_metadata(metadata_handle.read())
        _verify_identity(name, version, expected_version, path.name)


def verify_release_artifacts(
    project_file: Path,
    dist_dir: Path,
    expected_tag: str | None = None,
) -> tuple[Path, Path]:
    project_name, project_version = _project_metadata(project_file)
    _verify_identity(project_name, project_version, project_version, project_file.name)

    if expected_tag is not None and expected_tag != f"v{project_version}":
        raise ValueError(
            f"release tag {expected_tag!r} does not match package version v{project_version}"
        )

    artifact_stem = f"sahara_memory-{project_version}"
    wheel = dist_dir / f"{artifact_stem}-py3-none-any.whl"
    sdist = dist_dir / f"{artifact_stem}.tar.gz"
    missing = [str(path) for path in (wheel, sdist) if not path.is_file()]
    if missing:
        raise ValueError(f"missing expected release artifact(s): {', '.join(missing)}")

    unexpected = sorted(
        path.name
        for path in dist_dir.iterdir()
        if path.is_file() and path not in {wheel, sdist}
    )
    if unexpected:
        raise ValueError(f"unexpected files in release directory: {', '.join(unexpected)}")

    verify_wheel(wheel, project_version)
    verify_sdist(sdist, project_version)
    return wheel, sdist


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-file", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--expected-tag")
    args = parser.parse_args()

    wheel, sdist = verify_release_artifacts(
        args.project_file,
        args.dist_dir,
        expected_tag=args.expected_tag,
    )
    print(f"Verified {wheel.name}")
    print(f"Verified {sdist.name}")


if __name__ == "__main__":
    main()
