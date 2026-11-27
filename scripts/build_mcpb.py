"""Package a verified native Sahara bundle as a Claude Desktop extension (MCPB).

An .mcpb file is a zip holding ``manifest.json``, an icon and the server
files. Sahara ships the PyInstaller one-folder runtime under ``server/`` so the
extension needs no system Python. The result is unsigned; see docs/CLAUDE_DESKTOP.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_macos_installer import sign_bundle  # noqa: E402

PROJECT_FILE = ROOT / "pyproject.toml"
ICON_FILE = ROOT / "packaging" / "mcpb" / "icon.png"
DEFAULT_OUTPUT_ROOT = Path("dist") / "mcpb"
MANIFEST_VERSION = "0.2"
SERVER_DIR = "server"

TOOL_DESCRIPTIONS = {
    "sahara_search": "Search the indexed folders and return ranked, cited snippets.",
    "sahara_ask": "Return cited source passages for a question.",
    "sahara_read_chunk": "Read one indexed chunk by id.",
    "sahara_list_folders": "List the folders Sahara indexes.",
    "sahara_index_status": "Report index size, freshness and background indexing progress.",
    "sahara_recall": "Recall saved memories (read-only).",
}


@dataclass(frozen=True)
class McpbPlatform:
    tag: str
    claude_platform: str
    executable: str


PLATFORMS = {
    "macos-arm64": McpbPlatform("macos-arm64", "darwin", "sahara"),
}
# Windows is deferred to #151. To re-add it, register
# McpbPlatform("windows-x64", "win32", "sahara.exe") above and add a CI job.


def project_metadata(project_file: Path = PROJECT_FILE) -> dict[str, Any]:
    with project_file.open("rb") as handle:
        project: dict[str, Any] = tomllib.load(handle)["project"]
    return project


def mcpb_name(version: str, platform_tag: str) -> str:
    return f"sahara-{version}-{platform_tag}.mcpb"


def build_manifest(version: str, platform_tag: str) -> dict[str, Any]:
    platform = PLATFORMS[platform_tag]
    command = f"${{__dirname}}/{SERVER_DIR}/{platform.executable}"
    return {
        "manifest_version": MANIFEST_VERSION,
        "name": "sahara",
        "display_name": "Sahara",
        "version": version,
        "description": "Search your own folders from Claude, locally, with cited answers.",
        "long_description": (
            "Sahara indexes the folders you choose on this computer and lets Claude "
            "search them. Indexing and search run locally; no file contents are sent "
            "anywhere except the snippets Claude reads in your conversation. The "
            "extension is read-only: it cannot change or delete your files."
        ),
        "author": {"name": "Nidheesh Puthalath"},
        "repository": {"type": "git", "url": "https://github.com/nidheesh-p/sahara"},
        "homepage": "https://github.com/nidheesh-p/sahara",
        "license": "MIT",
        "icon": "icon.png",
        "server": {
            "type": "binary",
            "entry_point": f"{SERVER_DIR}/{platform.executable}",
            "mcp_config": {
                "command": command,
                "args": ["mcp", "serve", "--extension", "${user_config.folders}"],
            },
        },
        "tools": [
            {"name": name, "description": text}
            for name, text in TOOL_DESCRIPTIONS.items()
        ],
        "user_config": {
            "folders": {
                "type": "directory",
                "multiple": True,
                "required": True,
                "title": "Folders to search",
                "description": (
                    "Folders Sahara may read and index. The first search can take a few "
                    "minutes while the index is built and a ~70 MB local model downloads."
                ),
            }
        },
        "compatibility": {"platforms": [platform.claude_platform]},
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _add_file(archive: zipfile.ZipFile, source: Path, arcname: str) -> None:
    info = zipfile.ZipInfo.from_file(source, arcname)
    mode = stat.S_IMODE(source.stat().st_mode) or 0o644
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    with source.open("rb") as handle, archive.open(info, "w") as out:
        while chunk := handle.read(1024 * 1024):
            out.write(chunk)


def notarize_bundle(bundle: Path, *, apple_id: str, team_id: str, password: str) -> None:
    """Submit the signed runtime to Apple notarization (a zip of it, ditto-made).

    Bare binaries cannot be stapled; Gatekeeper checks the notarization online.
    """
    with tempfile.TemporaryDirectory(prefix="sahara-mcpb-notarize-") as temp:
        archive = Path(temp) / f"{bundle.name}.zip"
        env = {**os.environ, "COPYFILE_DISABLE": "1"}
        subprocess.run(
            ["ditto", "-c", "-k", "--keepParent", str(bundle), str(archive)],
            check=True,
            env=env,
        )
        subprocess.run(
            [
                "xcrun", "notarytool", "submit", str(archive),
                "--apple-id", apple_id,
                "--team-id", team_id,
                "--password", password,
                "--wait",
            ],
            check=True,
        )


def build_mcpb(
    bundle: Path,
    *,
    platform_tag: str,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    project_file: Path = PROJECT_FILE,
    icon: Path = ICON_FILE,
    application_identity: str | None = None,
    notarize: bool = False,
    apple_id: str | None = None,
    team_id: str | None = None,
    apple_password: str | None = None,
) -> tuple[Path, Path]:
    """Write the .mcpb and its .sha256 file; return both paths.

    With *application_identity* the Mach-O files are Developer ID signed (on a
    temporary copy) before packaging; *notarize* also submits them to Apple.
    """
    if notarize and not (application_identity and apple_id and team_id and apple_password):
        raise ValueError(
            "notarization requires a Developer ID Application identity, Apple ID, "
            "team ID and app-specific password"
        )
    if application_identity:
        with tempfile.TemporaryDirectory(prefix="sahara-mcpb-") as temp:
            signed = Path(temp) / bundle.name
            shutil.copytree(bundle, signed, symlinks=True)
            sign_bundle(signed, application_identity)
            if notarize:
                notarize_bundle(
                    signed,
                    apple_id=apple_id or "",
                    team_id=team_id or "",
                    password=apple_password or "",
                )
            return build_mcpb(
                signed,
                platform_tag=platform_tag,
                output_root=output_root,
                project_file=project_file,
                icon=icon,
            )
    platform = PLATFORMS[platform_tag]
    if not (bundle / platform.executable).is_file():
        raise ValueError(f"bundle has no {platform.executable} executable: {bundle}")
    if not icon.is_file():
        raise ValueError(f"icon not found: {icon}")

    version = str(project_metadata(project_file)["version"])
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / mcpb_name(version, platform_tag)
    manifest = build_manifest(version, platform_tag)

    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr(
            "manifest.json", json.dumps(manifest, indent=2) + "\n", zipfile.ZIP_DEFLATED
        )
        _add_file(archive, icon, "icon.png")
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                relative = path.relative_to(bundle).as_posix()
                _add_file(archive, path, f"{SERVER_DIR}/{relative}")

    checksum = output_root / f"{destination.name}.sha256"
    checksum.write_text(f"{sha256_file(destination)}  {destination.name}\n", encoding="utf-8")
    return destination, checksum


def verify_mcpb(
    mcpb: Path,
    *,
    platform_tag: str,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Check the extension's structure and checksum; return its manifest."""
    platform = PLATFORMS[platform_tag]
    checksum = mcpb.with_name(f"{mcpb.name}.sha256")
    if not checksum.is_file():
        raise ValueError(f"missing checksum file: {checksum}")
    if checksum.read_text(encoding="utf-8").split()[0] != sha256_file(mcpb):
        raise ValueError(f"checksum mismatch for {mcpb.name}")

    with zipfile.ZipFile(mcpb) as archive:
        names = set(archive.namelist())
        manifest: dict[str, Any] = json.loads(archive.read("manifest.json"))
        entry = f"{SERVER_DIR}/{platform.executable}"
        for required in ("manifest.json", "icon.png", entry):
            if required not in names:
                raise ValueError(f"extension is missing {required}")
        if platform.executable == "sahara":
            mode = archive.getinfo(entry).external_attr >> 16
            if not mode & stat.S_IXUSR:
                raise ValueError(f"{entry} is not executable in the archive")

    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise ValueError("unexpected manifest_version")
    if manifest["server"]["entry_point"] != entry:
        raise ValueError("manifest entry_point does not match the packaged executable")
    if manifest["icon"] != "icon.png":
        raise ValueError("manifest icon does not match the packaged icon")
    if manifest["compatibility"]["platforms"] != [platform.claude_platform]:
        raise ValueError("manifest platform does not match the packaged runtime")
    if expected_version is not None and manifest["version"] != expected_version:
        raise ValueError(f"manifest version {manifest['version']!r} != {expected_version!r}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
    parser.add_argument("--bundle", type=Path, help="Native one-folder bundle directory.")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument(
        "--sign",
        action="store_true",
        help="Developer ID sign the runtime using MACOS_DEVELOPER_ID_APPLICATION_IDENTITY.",
    )
    parser.add_argument(
        "--notarize",
        action="store_true",
        help="Also notarize (needs APPLE_ID, APPLE_TEAM_ID, APPLE_APP_SPECIFIC_PASSWORD).",
    )
    args = parser.parse_args()

    version = str(project_metadata()["version"])
    if args.verify_only:
        mcpb = args.output_root / mcpb_name(version, args.platform)
    else:
        if args.bundle is None:
            parser.error("--bundle is required unless --verify-only is set")
        mcpb, _ = build_mcpb(
            args.bundle,
            platform_tag=args.platform,
            output_root=args.output_root,
            application_identity=(
                os.environ.get("MACOS_DEVELOPER_ID_APPLICATION_IDENTITY")
                if args.sign or args.notarize
                else None
            ),
            notarize=args.notarize,
            apple_id=os.environ.get("APPLE_ID"),
            team_id=os.environ.get("APPLE_TEAM_ID"),
            apple_password=os.environ.get("APPLE_APP_SPECIFIC_PASSWORD"),
        )
    verify_mcpb(mcpb, platform_tag=args.platform, expected_version=version)
    print(f"Verified {mcpb}")


if __name__ == "__main__":
    main()
