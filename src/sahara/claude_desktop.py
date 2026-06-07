"""Claude Desktop MCP configuration helpers."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ClaudeDesktopInstallResult",
    "detect_claude_config_path",
    "install_claude_server",
    "resolve_sahara_executable",
]

_CONFIG_FILENAME = "claude_desktop_config.json"
_WINDOWS_PACKAGE_NAMES = (
    "Claude_pzs8sxrjxfjjc",
    "Anthropic.ClaudeDesktop_h6f0761",
)


@dataclass(frozen=True)
class ClaudeDesktopInstallResult:
    """Result of adding Sahara to Claude Desktop."""

    config_path: Path
    executable_path: Path
    backup_path: Path | None
    changed: bool


def detect_claude_config_path(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the Claude Desktop config path for macOS or Windows."""
    current_platform = platform or sys.platform
    env = os.environ if environ is None else environ
    user_home = home or Path.home()

    if current_platform == "darwin":
        return (
            user_home
            / "Library"
            / "Application Support"
            / "Claude"
            / _CONFIG_FILENAME
        )

    if current_platform == "win32":
        msix_path = _detect_windows_msix_config(env)
        if msix_path is not None:
            return msix_path

        appdata = env.get("APPDATA")
        if not appdata:
            raise RuntimeError(
                "APPDATA is not set, so the Claude Desktop config path "
                "cannot be detected."
            )
        return Path(appdata) / "Claude" / _CONFIG_FILENAME

    raise RuntimeError(
        "Claude Desktop local MCP installation is supported on macOS and Windows."
    )


def _detect_windows_msix_config(environ: Mapping[str, str]) -> Path | None:
    local_appdata = environ.get("LOCALAPPDATA")
    if not local_appdata:
        return None

    packages_dir = Path(local_appdata) / "Packages"
    if not packages_dir.is_dir():
        return None

    package_dirs: list[Path] = []
    for name in _WINDOWS_PACKAGE_NAMES:
        candidate = packages_dir / name
        if candidate.is_dir():
            package_dirs.append(candidate)

    known = {path.name.casefold() for path in package_dirs}
    try:
        discovered = sorted(
            (
                path
                for path in packages_dir.iterdir()
                if path.is_dir()
                and "claude" in path.name.casefold()
                and path.name.casefold() not in known
            ),
            key=lambda path: path.name.casefold(),
        )
    except OSError:
        discovered = []
    package_dirs.extend(discovered)

    config_paths = [
        package
        / "LocalCache"
        / "Roaming"
        / "Claude"
        / _CONFIG_FILENAME
        for package in package_dirs
    ]
    for config_path in config_paths:
        if config_path.is_file():
            return config_path
    return config_paths[0] if config_paths else None


def resolve_sahara_executable(
    executable: Path | None = None,
    *,
    argv0: str | None = None,
) -> Path:
    """Resolve the executable Claude Desktop should launch."""
    if executable is not None:
        candidate = executable.expanduser().resolve()
    else:
        invoked_as = argv0 or sys.argv[0]
        located = shutil.which(invoked_as) or shutil.which("sahara")
        candidate = Path(located or invoked_as).expanduser().resolve()

    if not candidate.is_file():
        raise RuntimeError(
            f"Could not find the Sahara executable at {candidate}. "
            "Reinstall Sahara or pass --executable with its absolute path."
        )
    return candidate


def install_claude_server(
    config_path: Path,
    executable_path: Path,
    *,
    sahara_config_path: Path | None = None,
) -> ClaudeDesktopInstallResult:
    """Merge Sahara's stdio MCP server into Claude Desktop configuration."""
    config_path = config_path.expanduser().resolve()
    executable_path = executable_path.expanduser().resolve()
    existing = _read_config(config_path)

    mcp_servers = existing.get("mcpServers")
    if mcp_servers is None:
        mcp_servers = {}
        existing["mcpServers"] = mcp_servers
    elif not isinstance(mcp_servers, dict):
        raise RuntimeError(
            f"{config_path} has a non-object 'mcpServers' value. "
            "Fix that value before installing Sahara."
        )

    args: list[str] = []
    if sahara_config_path is not None:
        args.extend(
            [
                "--config",
                str(sahara_config_path.expanduser().resolve()),
            ]
        )
    args.extend(["mcp", "serve", "--transport", "stdio"])

    server_config = {
        "command": str(executable_path),
        "args": args,
    }
    changed = mcp_servers.get("sahara") != server_config
    backup_path = None
    if changed:
        mcp_servers["sahara"] = server_config
        if config_path.is_file():
            backup_path = config_path.with_name(
                f"{config_path.name}.sahara-backup"
            )
            shutil.copy2(config_path, backup_path)
        _write_config(config_path, existing)

    return ClaudeDesktopInstallResult(
        config_path=config_path,
        executable_path=executable_path,
        backup_path=backup_path,
        changed=changed,
    )


def _read_config(config_path: Path) -> dict[str, object]:
    if not config_path.exists():
        return {}
    try:
        content = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not read {config_path}: {exc}") from exc
    if not content.strip():
        return {}
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{config_path} contains invalid JSON at line {exc.lineno}, "
            f"column {exc.colno}. It was not changed."
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"{config_path} must contain a JSON object. It was not changed."
        )
    return parsed


def _write_config(config_path: Path, config: dict[str, object]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=config_path.parent,
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            json.dump(config, temporary, indent=2, ensure_ascii=True)
            temporary.write("\n")
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, config_path)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise RuntimeError(f"Could not write {config_path}: {exc}") from exc
