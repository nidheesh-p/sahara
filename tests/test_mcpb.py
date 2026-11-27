"""Tests for the Claude Desktop extension (MCPB) packaging."""

from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from scripts.build_mcpb import (
    ICON_FILE,
    PLATFORMS,
    build_manifest,
    build_mcpb,
    mcpb_name,
    project_metadata,
    verify_mcpb,
)

from sahara.mcp_server import DEFAULT_MCP_TOOLS

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "native-artifacts.yml"


def _bundle(tmp_path: Path, executable: str) -> Path:
    bundle = tmp_path / "bundle"
    (bundle / "_internal").mkdir(parents=True)
    exe = bundle / executable
    exe.write_bytes(b"binary")
    exe.chmod(0o755)
    (bundle / "_internal" / "lib.dat").write_bytes(b"data")
    return bundle


@pytest.mark.parametrize("platform_tag", sorted(PLATFORMS))
def test_manifest_matches_runtime_contract(platform_tag: str) -> None:
    version = str(project_metadata()["version"])
    manifest = build_manifest(version, platform_tag)

    assert manifest["version"] == version
    assert manifest["server"]["type"] == "binary"
    assert [tool["name"] for tool in manifest["tools"]] == list(DEFAULT_MCP_TOOLS)
    assert "sahara_remember" not in json.dumps(manifest)
    assert manifest["user_config"]["folders"]["type"] == "directory"
    assert manifest["user_config"]["folders"]["required"] is True
    args = manifest["server"]["mcp_config"]["args"]
    assert args[:3] == ["mcp", "serve", "--extension"]
    assert "${user_config.folders}" in args
    assert ICON_FILE.is_file()


def test_only_macos_arm64_is_supported() -> None:
    assert sorted(PLATFORMS) == ["macos-arm64"]


def test_build_and_verify_round_trip(tmp_path: Path) -> None:
    platform_tag, executable = "macos-arm64", "sahara"
    bundle = _bundle(tmp_path, executable)

    mcpb, checksum = build_mcpb(
        bundle, platform_tag=platform_tag, output_root=tmp_path / "out"
    )

    version = str(project_metadata()["version"])
    assert mcpb.name == mcpb_name(version, platform_tag)
    assert checksum.read_text().split()[1] == mcpb.name
    manifest = verify_mcpb(mcpb, platform_tag=platform_tag, expected_version=version)
    with zipfile.ZipFile(mcpb) as archive:
        assert f"server/{executable}" in archive.namelist()
        assert "server/_internal/lib.dat" in archive.namelist()
        mode = archive.getinfo(f"server/{executable}").external_attr >> 16
        assert mode & stat.S_IXUSR
    assert manifest["server"]["entry_point"] == f"server/{executable}"


def test_verify_rejects_tampered_archive(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, "sahara")
    mcpb, _ = build_mcpb(
        bundle, platform_tag="macos-arm64", output_root=tmp_path / "out"
    )
    mcpb.write_bytes(mcpb.read_bytes() + b"x")

    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_mcpb(mcpb, platform_tag="macos-arm64")


def test_build_rejects_bundle_without_executable(tmp_path: Path) -> None:
    bundle = tmp_path / "empty"
    bundle.mkdir()

    with pytest.raises(ValueError, match="no sahara executable"):
        build_mcpb(bundle, platform_tag="macos-arm64", output_root=tmp_path / "out")


def test_signing_is_applied_to_a_copy_and_notarized(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, "sahara")
    with (
        patch("scripts.build_mcpb.sign_bundle") as sign,
        patch("scripts.build_mcpb.notarize_bundle") as notarize,
    ):
        mcpb, _ = build_mcpb(
            bundle,
            platform_tag="macos-arm64",
            output_root=tmp_path / "out",
            application_identity="Developer ID Application: Example",
            notarize=True,
            apple_id="id",
            team_id="team",
            apple_password="pw",
        )

    assert sign.call_args.args[0] != bundle
    assert sign.call_args.args[1] == "Developer ID Application: Example"
    notarize.assert_called_once()
    verify_mcpb(mcpb, platform_tag="macos-arm64")


def test_notarize_requires_credentials(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, "sahara")

    with pytest.raises(ValueError, match="notarization requires"):
        build_mcpb(
            bundle,
            platform_tag="macos-arm64",
            output_root=tmp_path / "out",
            application_identity="id",
            notarize=True,
        )


def test_workflow_builds_signed_extension_with_existing_secrets() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    jobs = yaml.safe_load(text)["jobs"]
    job = jobs["claude-desktop-extension"]

    assert job["needs"] == "macos-apple-silicon"
    assert job["if"] == (
        "startsWith(github.ref, 'refs/tags/') || github.event_name == 'workflow_dispatch'"
    )
    assert job["environment"] == "macos-installer"
    assert "claude-desktop-extension" in jobs["publish-release-assets"]["needs"]
    assert "build_mcpb.py --platform macos-arm64" in text
    assert "--sign --notarize" in text
    assert "windows-x64 --bundle" not in text
