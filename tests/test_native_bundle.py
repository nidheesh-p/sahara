"""Tests for native bundle packaging scaffolding."""

from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest
from scripts.build_macos_bundle import PLATFORM_TAG, bundle_name, project_version
from scripts.build_macos_installer import (
    FIRST_RUN_LINK,
    INSTALL_ROOT,
    PACKAGE_ID,
    build_macos_installer,
    build_pkg,
    is_codesign_candidate,
    prepare_payload,
    verify_macos_installer,
    write_installer_scripts,
)
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
    assert "python scripts/build_macos_installer.py --notarize" in doc
    assert "sahara-0.2.1-macos-arm64" in doc
    assert "not shipped in the artifact" in doc
    assert "repository checkout or system Python" in doc
    assert "/Library/Application Support/Sahara/sahara/" in doc
    assert "/usr/local/bin/sahara" in doc
    assert "~/.sahara" in doc
    assert "protected `macos-installer` environment" in doc
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
        smoke.side_effect = lambda _bundle, destination, **_kwargs: (
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


def test_macos_installer_payload_uses_stable_application_support_location(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "sahara-1.2.3-macos-arm64"
    bundle.mkdir()
    (bundle / "sahara").write_text("#!/bin/sh\n", encoding="utf-8")
    payload_root = tmp_path / "payload"

    prepare_payload(bundle, payload_root)

    assert (payload_root / INSTALL_ROOT / "sahara").read_text(encoding="utf-8") == "#!/bin/sh\n"


def test_macos_installer_scripts_create_path_link_and_preserve_user_data(
    tmp_path: Path,
) -> None:
    scripts_root = tmp_path / "scripts"

    write_installer_scripts(scripts_root)

    preinstall = (scripts_root / "preinstall").read_text(encoding="utf-8")
    postinstall = (scripts_root / "postinstall").read_text(encoding="utf-8")
    assert "/Library/Application Support/Sahara/sahara" in preinstall
    assert "~/.sahara" not in preinstall
    assert 'ln -sfn "$target" "$link_dir/sahara"' in postinstall
    assert "/usr/local/bin" in postinstall
    assert "/Library/Application Support/Sahara/sahara/sahara" in postinstall
    assert "sahara-first-run" in postinstall
    assert "first-run" in postinstall
    assert "SAHARA_SKIP_FIRST_RUN_LAUNCH" in postinstall
    assert "launchctl asuser" in postinstall


def test_macos_pkgbuild_requests_metadata_suppression(tmp_path: Path) -> None:
    with patch("scripts.build_macos_installer.subprocess.run") as run:
        build_pkg(
            payload_root=tmp_path / "payload",
            scripts_root=tmp_path / "scripts",
            package=tmp_path / "installers" / "sahara.pkg",
            version="1.2.3",
            installer_identity=None,
        )

    pkgbuild_cmd = run.call_args.args[0]
    _, kwargs = run.call_args
    assert kwargs["env"]["COPYFILE_DISABLE"] == "1"
    assert kwargs["env"]["DITTONORSRC"] == "1"
    assert r"^.*/._.*" in pkgbuild_cmd
    assert r"^.*/.DS_Store$" in pkgbuild_cmd


def test_macos_signed_pkgbuild_requests_trusted_timestamp(tmp_path: Path) -> None:
    with patch("scripts.build_macos_installer.subprocess.run") as run:
        build_pkg(
            payload_root=tmp_path / "payload",
            scripts_root=tmp_path / "scripts",
            package=tmp_path / "installers" / "sahara.pkg",
            version="1.2.3",
            installer_identity="Developer ID Installer: Example",
        )

    pkgbuild_cmd = run.call_args.args[0]
    assert "--sign" in pkgbuild_cmd
    assert "--timestamp" in pkgbuild_cmd


def test_macos_codesign_candidates_are_macho_files(tmp_path: Path) -> None:
    macho = tmp_path / "sahara"
    fat64 = tmp_path / "universal-helper"
    shell = tmp_path / "helper"
    macho.write_bytes(b"\xcf\xfa\xed\xfe\x00\x00")
    fat64.write_bytes(b"\xca\xfe\xba\xbf\x00\x00")
    shell.write_text("#!/bin/sh\n", encoding="utf-8")
    macho.chmod(0o755)
    fat64.chmod(0o755)
    shell.chmod(0o755)

    assert is_codesign_candidate(macho)
    assert is_codesign_candidate(fat64)
    assert not is_codesign_candidate(shell)


def test_builds_and_verifies_macos_installer_metadata(tmp_path: Path) -> None:
    bundle = tmp_path / "sahara-0.2.1-macos-arm64"
    bundle.mkdir()
    executable = bundle / "sahara"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    def fake_build_pkg(**kwargs: object) -> Path:
        package = kwargs["package"]
        assert isinstance(package, Path)
        package.write_text("pkg bytes\n", encoding="utf-8")
        assert kwargs["installer_identity"] == "Developer ID Installer: Example"
        return package

    with (
        patch("scripts.build_macos_installer.build_pkg", side_effect=fake_build_pkg),
        patch("scripts.build_macos_installer.sign_bundle") as sign_bundle,
        patch("scripts.build_macos_installer.strip_macos_metadata") as strip_metadata,
    ):
        artifact = build_macos_installer(
            bundle,
            tmp_path / "installers",
            application_identity="Developer ID Application: Example",
            installer_identity="Developer ID Installer: Example",
            skip_platform_check=True,
        )

    assert strip_metadata.call_count == 2
    sign_bundle.assert_called_once()
    assert artifact.package.name == "sahara-0.2.1-macos-arm64.pkg"
    verify_macos_installer(tmp_path / "installers", artifact.package.name)
    manifest = artifact.manifest.read_text(encoding="utf-8")
    assert PACKAGE_ID in manifest
    assert '"signed": true' in manifest
    assert '"notarized": false' in manifest
    assert "~/.sahara" in manifest
    assert "/" + str(FIRST_RUN_LINK) in manifest
    assert '"launches_first_run_after_gui_install": true' in manifest


def test_macos_installer_notarization_requires_release_credentials(tmp_path: Path) -> None:
    bundle = tmp_path / "sahara-0.2.1-macos-arm64"
    bundle.mkdir()
    (bundle / "sahara").write_text("#!/bin/sh\n", encoding="utf-8")

    with pytest.raises(ValueError, match="notarization requires"):
        build_macos_installer(
            bundle,
            tmp_path / "installers",
            notarize=True,
            skip_platform_check=True,
        )

    with pytest.raises(ValueError, match="Developer ID Application identity"):
        build_macos_installer(
            bundle,
            tmp_path / "installers",
            installer_identity="Developer ID Installer: Example",
            notarize=True,
            apple_id="release@example.com",
            team_id="TEAMID1234",
            apple_password="app-specific-password",
            skip_platform_check=True,
        )
