"""Tests for Windows native bundle and installer packaging."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from scripts.build_macos_bundle import PROJECT_FILE, project_version
from scripts.build_windows_bundle import PLATFORM_TAG, bundle_name, is_windows_x64
from scripts.build_windows_installer import (
    APP_ID,
    FIRST_RUN_COMMAND,
    INSTALL_LOCATION,
    PATH_ENTRY,
    PRESERVED_USER_DATA,
    build_windows_installer,
    installer_name,
    verify_windows_installer,
    write_inno_script,
)
from scripts.package_native_artifacts import package_native_artifact, verify_native_artifact

ROOT = Path(__file__).parents[1]
SPEC_FILE = ROOT / "packaging" / "pyinstaller" / "sahara_windows_x64.spec"
DOC_FILE = ROOT / "docs" / "windows-x64-installer.md"
INSTALL_DOC = ROOT / "docs" / "INSTALLATION.md"


def test_windows_bundle_name_is_versioned_and_platform_specific() -> None:
    version = project_version(PROJECT_FILE)

    assert bundle_name(version) == f"sahara-{version}-{PLATFORM_TAG}"


def test_windows_platform_detection_accepts_amd64_aliases() -> None:
    with (
        patch("scripts.build_windows_bundle.platform.system", return_value="Windows"),
        patch("scripts.build_windows_bundle.platform.machine", return_value="AMD64"),
    ):
        assert is_windows_x64()


def test_windows_pyinstaller_spec_collects_required_resource_families() -> None:
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
        "keyring.backends.Windows",
        "watchdog.observers.read_directory_changes",
        "name=\"sahara\"",
    ]
    for fragment in required_fragments:
        assert fragment in spec


def test_windows_installer_docs_cover_supported_install_without_bypasses() -> None:
    doc = DOC_FILE.read_text(encoding="utf-8")
    install_doc = INSTALL_DOC.read_text(encoding="utf-8")

    for text in (doc, install_doc):
        assert "sahara-0.2.1-windows-x64-setup.exe" in text
        assert "%LOCALAPPDATA%\\Programs\\Sahara" in text
        assert "%USERPROFILE%\\.sahara" in text
        assert "/VERYSILENT /NORESTART /SUPPRESSMSGBOXES" in text
        assert "execution-policy" not in text.lower()
        assert "bypass" not in text.lower()

    assert "python scripts/build_windows_bundle.py" in doc
    assert "python scripts/smoke_windows_bundle.py" in doc
    assert "python scripts/build_windows_installer.py --sign" in doc
    assert "protected `windows-installer` environment" in doc


def test_packages_and_verifies_windows_native_artifact(tmp_path: Path) -> None:
    bundle = tmp_path / "sahara-1.2.3-windows-x64"
    bundle.mkdir()
    (bundle / "sahara.exe").write_text("exe", encoding="utf-8")

    with (
        patch("scripts.package_native_artifacts.write_dependency_inventory") as inventory,
        patch("scripts.package_native_artifacts.run_smoke") as smoke,
    ):
        inventory.side_effect = lambda destination: destination.write_text(
            "name,version\nsahara-memory,1.2.3\n",
            encoding="utf-8",
        )
        smoke.side_effect = lambda _bundle, destination, **_kwargs: (
            destination.write_text(
                "command: smoke\nreturncode: 0\n",
                encoding="utf-8",
            )
        )
        artifact = package_native_artifact(
            bundle,
            tmp_path / "artifacts",
            platform_name="windows-x64",
        )

    assert artifact.archive.name == "sahara-1.2.3-windows-x64.zip"
    verify_native_artifact(
        tmp_path / "artifacts",
        "sahara-1.2.3-windows-x64",
        platform_name="windows-x64",
    )
    manifest = json.loads(artifact.manifest.read_text(encoding="utf-8"))
    assert manifest["platform_tag"] == "windows-x64"


def test_inno_script_is_per_user_quiet_capable_and_preserves_user_data(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "sahara-0.2.1-windows-x64"
    bundle.mkdir()
    (bundle / "sahara.exe").write_text("exe", encoding="utf-8")
    script = write_inno_script(
        bundle=bundle,
        script=tmp_path / "installer" / "sahara.iss",
        output_root=tmp_path / "output",
        version="0.2.1",
    )

    text = script.read_text(encoding="utf-8")
    assert f"AppId={APP_ID}" in text
    assert "PrivilegesRequired=lowest" in text
    assert "DefaultDirName={localappdata}\\Programs\\Sahara" in text
    assert "ValueName: \"Path\"" in text
    assert "UpdatedUserPath" in text
    assert "sahara.exe" in text
    assert 'Parameters: "first-run"' in text
    assert "postinstall skipifsilent nowait" in text
    assert "%USERPROFILE%" not in text
    assert ".sahara" not in text


def test_builds_and_verifies_windows_installer_metadata(tmp_path: Path) -> None:
    bundle = tmp_path / "sahara-0.2.1-windows-x64"
    bundle.mkdir()
    (bundle / "sahara.exe").write_text("exe", encoding="utf-8")
    installer_root = tmp_path / "installers"

    def fake_compile(_script: Path) -> None:
        (installer_root / installer_name("0.2.1")).write_text("installer", encoding="utf-8")

    with patch("scripts.build_windows_installer.compile_inno_script", side_effect=fake_compile):
        artifact = build_windows_installer(
            bundle,
            installer_root,
            script_root=tmp_path / "scripts",
            skip_platform_check=True,
        )

    assert artifact.installer.name == "sahara-0.2.1-windows-x64-setup.exe"
    verify_windows_installer(installer_root, artifact.installer.name)
    manifest = json.loads(artifact.manifest.read_text(encoding="utf-8"))
    assert manifest["install_location"] == INSTALL_LOCATION
    assert manifest["path_entry"] == PATH_ENTRY
    assert manifest["first_run_command"] == FIRST_RUN_COMMAND
    assert manifest["launches_first_run_after_gui_install"] is True
    assert manifest["per_user"] is True
    assert manifest["quiet_install_args"] == "/VERYSILENT /NORESTART /SUPPRESSMSGBOXES"
    assert manifest["signed"] is False
    assert PRESERVED_USER_DATA in manifest["preserves_user_data"]


def test_windows_installer_signing_requires_certificate_and_password(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "sahara-0.2.1-windows-x64"
    bundle.mkdir()
    (bundle / "sahara.exe").write_text("exe", encoding="utf-8")

    with pytest.raises(ValueError, match="certificate and certificate password"):
        build_windows_installer(
            bundle,
            tmp_path / "installers",
            sign=True,
            skip_platform_check=True,
        )


def test_windows_installer_signs_with_base64_certificate(tmp_path: Path) -> None:
    bundle = tmp_path / "sahara-0.2.1-windows-x64"
    bundle.mkdir()
    (bundle / "sahara.exe").write_text("exe", encoding="utf-8")
    installer_root = tmp_path / "installers"

    def fake_compile(_script: Path) -> None:
        (installer_root / installer_name("0.2.1")).write_text("installer", encoding="utf-8")

    with (
        patch("scripts.build_windows_installer.compile_inno_script", side_effect=fake_compile),
        patch("scripts.build_windows_installer.sign_installer") as sign_installer,
    ):
        artifact = build_windows_installer(
            bundle,
            installer_root,
            script_root=tmp_path / "scripts",
            sign=True,
            certificate_base64="ZmFrZS1wZng=",
            certificate_password="secret",
            timestamp_url="https://timestamp.example.test",
            skip_platform_check=True,
        )

    sign_installer.assert_called_once()
    assert artifact.manifest.read_text(encoding="utf-8").count('"signed": true') == 1


def test_windows_installer_verification_rejects_bad_checksum(tmp_path: Path) -> None:
    installer_root = tmp_path / "installers"
    installer_root.mkdir()
    name = "sahara-0.2.1-windows-x64-setup.exe"
    (installer_root / name).write_text("installer", encoding="utf-8")
    (installer_root / f"{name}.sha256").write_text(f"{'0' * 64}  {name}\n", encoding="utf-8")
    (installer_root / "sahara-0.2.1-windows-x64-installer-manifest.json").write_text(
        "{}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_windows_installer(installer_root, name)
