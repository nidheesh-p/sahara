"""Tests for release artifact verification."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest
from scripts.verify_release_artifacts import verify_release_artifacts


def _write_project(path: Path, name: str = "sahara-memory", version: str = "1.2.3") -> None:
    path.write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n',
        encoding="utf-8",
    )


def _write_artifacts(
    dist_dir: Path,
    name: str = "sahara-memory",
    version: str = "1.2.3",
) -> None:
    dist_dir.mkdir()
    metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n\n".encode()
    wheel = dist_dir / f"sahara_memory-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        prefix = f"sahara_memory-{version}.dist-info"
        archive.writestr(f"{prefix}/METADATA", metadata)
        archive.writestr(
            f"{prefix}/entry_points.txt",
            "[console_scripts]\nsahara = sahara.cli:main\n",
        )

    sdist = dist_dir / f"sahara_memory-{version}.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        info = tarfile.TarInfo(f"sahara_memory-{version}/PKG-INFO")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))


def test_verifies_expected_artifacts(tmp_path: Path) -> None:
    project_file = tmp_path / "pyproject.toml"
    dist_dir = tmp_path / "dist"
    _write_project(project_file)
    _write_artifacts(dist_dir)

    wheel, sdist = verify_release_artifacts(project_file, dist_dir, expected_tag="v1.2.3")

    assert wheel.name.endswith(".whl")
    assert sdist.name.endswith(".tar.gz")


def test_rejects_wrong_distribution_name(tmp_path: Path) -> None:
    project_file = tmp_path / "pyproject.toml"
    _write_project(project_file, name="sahara")

    with pytest.raises(ValueError, match="expected 'sahara-memory'"):
        verify_release_artifacts(project_file, tmp_path / "dist")


def test_rejects_tag_version_mismatch(tmp_path: Path) -> None:
    project_file = tmp_path / "pyproject.toml"
    dist_dir = tmp_path / "dist"
    _write_project(project_file)
    _write_artifacts(dist_dir)

    with pytest.raises(ValueError, match="does not match"):
        verify_release_artifacts(project_file, dist_dir, expected_tag="v1.2.4")


def test_rejects_unexpected_release_files(tmp_path: Path) -> None:
    project_file = tmp_path / "pyproject.toml"
    dist_dir = tmp_path / "dist"
    _write_project(project_file)
    _write_artifacts(dist_dir)
    (dist_dir / "old-package.whl").write_bytes(b"stale")

    with pytest.raises(ValueError, match="unexpected files"):
        verify_release_artifacts(project_file, dist_dir)
