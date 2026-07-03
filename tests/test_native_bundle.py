"""Tests for native bundle packaging scaffolding."""

from __future__ import annotations

import tomllib
from pathlib import Path

from scripts.build_macos_bundle import PLATFORM_TAG, bundle_name, project_version

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
