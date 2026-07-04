"""Tests for native bundle packaging scaffolding."""

from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest
from scripts.build_macos_bundle import PLATFORM_TAG, bundle_name, project_version
from scripts.package_native_artifacts import (
    create_tarball,
    package_native_artifact,
    verify_native_artifact,
)

ROOT = Path(__file__).parents[1]
SPEC_FILE = ROOT / "packaging" / "pyinstaller" / "sahara_macos_arm64.spec"
DOC_FILE = ROOT / "docs" / "macos-apple-silicon-bundle.md"
PROJECT_FILE = ROOT / "pyproject.toml"


def test_bundle_name_is_versioned_and_platform_specific() -> None:
    version = project_version(PROJECT_FILE)

    assert bundle_name(version) == f"sahara-{version}-{PLATFORM_TAG}"


def test_native_extra_includes_pyinstaller() -> None:
    with PROJECT_FILE.open("rb") as handle:
        optional = tomllib.load(handle)["project"]["optional-dependencies"]

    assert any(dep.startswith("pyinstaller>=") for dep in optional["native"])


def test_pyinstaller_spec_collects_required_resource_families() -> None:
    spec = SPEC_FILE.read_text(encoding="utf-8")

    required_fragments = [
        "collect_data_files",
        "collect_dynamic_libs",
        "copy_metadata",
        "sahara",
        "data/**",
        "fastembed",
        "onnxruntime",
        "sqlite_vec",
        "sqlite-vec",
        "mcp",
        "keyring",
        "cryptography",
        "pypdf",
        "python-docx",
        "watchdog.observers.fsevents",
        "target_arch=\"arm64\"",
    ]
    for fragment in required_fragments:
        assert fragment in spec


def test_bundle_docs_include_build_and_smoke_commands() -> None:
    doc = DOC_FILE.read_text(encoding="utf-8")

    assert "python scripts/build_macos_bundle.py" in doc
    assert "python scripts/smoke_macos_bundle.py" in doc
    assert "sahara-0.2.1-macos-arm64" in doc
    assert "not shipped in the artifact" in doc
    assert "repository checkout or system Python" in doc
    clean_machine_section = doc.split("## Clean-Machine Check", maxsplit=1)[1]
    assert "python /path/to/smoke_macos_bundle.py" not in clean_machine_section


def test_packages_and_verifies_native_artifact(tmp_path: Path) -> None:
    bundle = tmp_path / "sahara-1.2.3-macos-arm64"
    bundle.mkdir()
    executable = bundle / "sahara"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")

    with (
        patch("scripts.package_native_artifacts.write_dependency_inventory") as inventory,
        patch("scripts.package_native_artifacts.run_smoke") as smoke,
    ):
        inventory.side_effect = lambda destination: destination.write_text(
            "name,version\nsahara-memory,1.2.3\n",
            encoding="utf-8",
        )
        smoke.side_effect = lambda _bundle, destination, with_index=False: (
            destination.write_text(
                "command: smoke\nreturncode: 0\n",
                encoding="utf-8",
            )
        )
        artifact = package_native_artifact(bundle, tmp_path / "artifacts")

    assert artifact.archive.name == "sahara-1.2.3-macos-arm64.tar.gz"
    verify_native_artifact(tmp_path / "artifacts", "sahara-1.2.3-macos-arm64")


def test_native_artifact_verification_rejects_bad_checksum(tmp_path: Path) -> None:
    bundle = tmp_path / "sahara-1.2.3-macos-arm64"
    bundle.mkdir()
    (bundle / "sahara").write_text("#!/bin/sh\n", encoding="utf-8")
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    archive = create_tarball(bundle, artifact_root / "sahara-1.2.3-macos-arm64.tar.gz")
    (artifact_root / f"{archive.name}.sha256").write_text(
        f"{'0' * 64}  {archive.name}\n",
        encoding="utf-8",
    )
    (artifact_root / "sahara-1.2.3-macos-arm64-dependencies.csv").write_text(
        "name,version\n",
        encoding="utf-8",
    )
    (artifact_root / "sahara-1.2.3-macos-arm64-smoke.txt").write_text(
        "returncode: 0\n",
        encoding="utf-8",
    )
    (artifact_root / "sahara-1.2.3-macos-arm64-manifest.json").write_text(
        "{}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_native_artifact(artifact_root, "sahara-1.2.3-macos-arm64")
