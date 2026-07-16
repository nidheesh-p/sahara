"""Tests for the native artifact release workflow."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "native-artifacts.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_native_artifacts_workflow_is_release_only() -> None:
    workflow = _workflow()
    triggers = workflow[True]

    assert "workflow_dispatch" in triggers
    assert triggers["push"] == {"tags": ["v*"]}
    assert "pull_request" not in triggers


def test_native_artifacts_workflow_limits_retention_and_verifies_package() -> None:
    workflow_text = WORKFLOW.read_text(encoding="utf-8")

    assert "scripts/build_macos_bundle.py" in workflow_text
    assert "scripts/package_native_artifacts.py" in workflow_text
    assert "retention-days: 7" in workflow_text
    assert "native-macos-arm64" in workflow_text
    assert "--with-index" in workflow_text
    assert "github.event.inputs.smoke_with_index" in workflow_text
    assert "${{ inputs.smoke_with_index }}" not in workflow_text


def test_native_artifacts_workflow_builds_signed_installer_in_protected_environment() -> None:
    workflow = _workflow()
    installer = workflow["jobs"]["macos-installer"]
    workflow_text = WORKFLOW.read_text(encoding="utf-8")

    assert installer["needs"] == "macos-apple-silicon"
    assert installer["if"] == "startsWith(github.ref, 'refs/tags/')"
    assert installer["environment"] == "macos-installer"
    assert "scripts/build_macos_installer.py --bundle \"$bundle\" --notarize" in workflow_text
    assert "pkgutil --check-signature" in workflow_text
    assert "spctl -a -vv -t install" in workflow_text
    assert "native-macos-arm64-installer" in workflow_text
    assert "MACOS_DEVELOPER_ID_APPLICATION_CERTIFICATE_BASE64" in workflow_text
    assert "MACOS_DEVELOPER_ID_INSTALLER_CERTIFICATE_BASE64" in workflow_text
    assert "APPLE_APP_SPECIFIC_PASSWORD" in workflow_text
