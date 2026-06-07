"""Tests for one-command Claude Desktop MCP installation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from sahara.claude_desktop import (
    detect_claude_config_path,
    install_claude_server,
    resolve_sahara_executable,
)
from sahara.cli import main


def test_detects_macos_config_path(tmp_path: Path) -> None:
    path = detect_claude_config_path(platform="darwin", home=tmp_path)

    assert path == (
        tmp_path
        / "Library"
        / "Application Support"
        / "Claude"
        / "claude_desktop_config.json"
    )


def test_detects_standard_windows_config_path(tmp_path: Path) -> None:
    appdata = tmp_path / "Roaming"

    path = detect_claude_config_path(
        platform="win32",
        environ={"APPDATA": str(appdata)},
    )

    assert path == appdata / "Claude" / "claude_desktop_config.json"


def test_windows_detection_requires_appdata() -> None:
    with pytest.raises(RuntimeError, match="APPDATA is not set"):
        detect_claude_config_path(platform="win32", environ={})


def test_prefers_existing_windows_msix_config(tmp_path: Path) -> None:
    local_appdata = tmp_path / "Local"
    config = (
        local_appdata
        / "Packages"
        / "Claude_pzs8sxrjxfjjc"
        / "LocalCache"
        / "Roaming"
        / "Claude"
        / "claude_desktop_config.json"
    )
    config.parent.mkdir(parents=True)
    config.write_text("{}", encoding="utf-8")

    path = detect_claude_config_path(
        platform="win32",
        environ={
            "APPDATA": str(tmp_path / "Roaming"),
            "LOCALAPPDATA": str(local_appdata),
        },
    )

    assert path == config


def test_detects_windows_msix_package_before_config_exists(tmp_path: Path) -> None:
    local_appdata = tmp_path / "Local"
    package = local_appdata / "Packages" / "Anthropic.ClaudeDesktop_h6f0761"
    package.mkdir(parents=True)

    path = detect_claude_config_path(
        platform="win32",
        environ={
            "APPDATA": str(tmp_path / "Roaming"),
            "LOCALAPPDATA": str(local_appdata),
        },
    )

    assert path == (
        package
        / "LocalCache"
        / "Roaming"
        / "Claude"
        / "claude_desktop_config.json"
    )


def test_rejects_unsupported_platform() -> None:
    with pytest.raises(RuntimeError, match="macOS and Windows"):
        detect_claude_config_path(platform="linux")


def test_resolves_explicit_sahara_executable(tmp_path: Path) -> None:
    executable = tmp_path / "sahara"
    executable.write_text("", encoding="utf-8")

    assert resolve_sahara_executable(executable) == executable.resolve()


def test_rejects_missing_sahara_executable(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Could not find"):
        resolve_sahara_executable(tmp_path / "missing")


def test_install_merges_existing_config_and_creates_backup(tmp_path: Path) -> None:
    config_path = tmp_path / "claude_desktop_config.json"
    executable = tmp_path / "bin" / "sahara"
    executable.parent.mkdir()
    executable.write_text("", encoding="utf-8")
    original = {
        "preferences": {"sidebarMode": "chat"},
        "mcpServers": {
            "filesystem": {
                "command": "npx",
                "args": ["server-filesystem"],
            }
        },
    }
    config_path.write_text(json.dumps(original), encoding="utf-8")

    result = install_claude_server(config_path, executable)

    installed = json.loads(config_path.read_text(encoding="utf-8"))
    assert installed["preferences"] == original["preferences"]
    assert installed["mcpServers"]["filesystem"] == original["mcpServers"]["filesystem"]
    assert installed["mcpServers"]["sahara"] == {
        "command": str(executable.resolve()),
        "args": ["mcp", "serve", "--transport", "stdio"],
    }
    assert result.changed is True
    assert result.backup_path is not None
    assert json.loads(result.backup_path.read_text(encoding="utf-8")) == original


def test_install_includes_non_default_sahara_config(tmp_path: Path) -> None:
    config_path = tmp_path / "claude_desktop_config.json"
    executable = tmp_path / "sahara"
    sahara_config = tmp_path / "custom.toml"
    executable.write_text("", encoding="utf-8")

    install_claude_server(
        config_path,
        executable,
        sahara_config_path=sahara_config,
    )

    installed = json.loads(config_path.read_text(encoding="utf-8"))
    assert installed["mcpServers"]["sahara"]["args"] == [
        "--config",
        str(sahara_config.resolve()),
        "mcp",
        "serve",
        "--transport",
        "stdio",
    ]


def test_install_is_idempotent(tmp_path: Path) -> None:
    config_path = tmp_path / "claude_desktop_config.json"
    executable = tmp_path / "sahara"
    executable.write_text("", encoding="utf-8")

    first = install_claude_server(config_path, executable)
    second = install_claude_server(config_path, executable)

    assert first.changed is True
    assert second.changed is False
    assert second.backup_path is None


def test_install_rejects_invalid_json_without_changing_file(tmp_path: Path) -> None:
    config_path = tmp_path / "claude_desktop_config.json"
    executable = tmp_path / "sahara"
    executable.write_text("", encoding="utf-8")
    invalid = '{"mcpServers":'
    config_path.write_text(invalid, encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid JSON"):
        install_claude_server(config_path, executable)

    assert config_path.read_text(encoding="utf-8") == invalid
    assert not config_path.with_name(
        "claude_desktop_config.json.sahara-backup"
    ).exists()


def test_install_rejects_non_object_mcp_servers(tmp_path: Path) -> None:
    config_path = tmp_path / "claude_desktop_config.json"
    executable = tmp_path / "sahara"
    executable.write_text("", encoding="utf-8")
    config_path.write_text('{"mcpServers": []}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="non-object"):
        install_claude_server(config_path, executable)


def test_install_rejects_non_object_root(tmp_path: Path) -> None:
    config_path = tmp_path / "claude_desktop_config.json"
    executable = tmp_path / "sahara"
    executable.write_text("", encoding="utf-8")
    config_path.write_text("[]", encoding="utf-8")

    with pytest.raises(RuntimeError, match="must contain a JSON object"):
        install_claude_server(config_path, executable)


def test_install_claude_cli_with_overrides(tmp_path: Path) -> None:
    claude_config = tmp_path / "Claude" / "claude_desktop_config.json"
    executable = tmp_path / "bin" / "sahara"
    executable.parent.mkdir()
    executable.write_text("", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "mcp",
            "install-claude",
            "--claude-config",
            str(claude_config),
            "--executable",
            str(executable),
        ],
    )

    assert result.exit_code == 0
    assert "Installed Sahara in Claude Desktop" in result.output
    assert "Fully quit and reopen Claude Desktop" in result.output
    installed = json.loads(claude_config.read_text(encoding="utf-8"))
    assert installed["mcpServers"]["sahara"]["command"] == str(
        executable.resolve()
    )


def test_install_claude_cli_preserves_custom_sahara_config(tmp_path: Path) -> None:
    claude_config = tmp_path / "claude_desktop_config.json"
    executable = tmp_path / "sahara"
    sahara_config = tmp_path / "sahara.toml"
    executable.write_text("", encoding="utf-8")
    sahara_config.write_text('storage_mode = "none"\n', encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "--config",
            str(sahara_config),
            "mcp",
            "install-claude",
            "--claude-config",
            str(claude_config),
            "--executable",
            str(executable),
        ],
    )

    assert result.exit_code == 0
    installed = json.loads(claude_config.read_text(encoding="utf-8"))
    assert installed["mcpServers"]["sahara"]["args"][:2] == [
        "--config",
        str(sahara_config.resolve()),
    ]


def test_install_claude_cli_reports_detection_error() -> None:
    with patch(
        "sahara.claude_desktop.detect_claude_config_path",
        side_effect=RuntimeError("unsupported test platform"),
    ):
        result = CliRunner().invoke(main, ["mcp", "install-claude"])

    assert result.exit_code != 0
    assert "unsupported test platform" in result.output
