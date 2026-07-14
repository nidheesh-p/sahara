"""Build and verify Sahara's macOS installer package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_macos_bundle import (  # noqa: E402
    PLATFORM_TAG,
    PROJECT_FILE,
    bundle_name,
    is_macos_arm64,
    project_version,
)

PACKAGE_ID = "io.github.nidheesh-p.sahara"
INSTALL_ROOT = Path("Library") / "Application Support" / "Sahara" / "sahara"
PATH_LINK = Path("usr") / "local" / "bin" / "sahara"
DEFAULT_INSTALLER_ROOT = Path("dist") / "native-installers"
MACHO_MAGICS = {
    b"\xca\xfe\xba\xbe",
    b"\xca\xfe\xba\xbf",
    b"\xbe\xba\xfe\xca",
    b"\xbf\xba\xfe\xca",
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
}


@dataclass(frozen=True)
class MacOSInstallerArtifact:
    package: Path
    checksum: Path
    manifest: Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksum(path: Path, checksum_path: Path) -> str:
    digest = sha256_file(path)
    checksum_path.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return digest


def require_macos_arm64(skip_platform_check: bool) -> None:
    if skip_platform_check:
        return
    if not is_macos_arm64():
        raise SystemExit(
            "macOS installer packages must be built on macOS arm64. "
            "Pass --skip-platform-check only for metadata tests."
        )


def prepare_payload(bundle: Path, payload_root: Path) -> None:
    if not bundle.is_dir():
        raise ValueError(f"bundle directory does not exist: {bundle}")
    install_target = payload_root / INSTALL_ROOT
    if install_target.exists():
        shutil.rmtree(install_target)
    install_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bundle, install_target, symlinks=True)


def write_installer_scripts(scripts_root: Path) -> None:
    scripts_root.mkdir(parents=True, exist_ok=True)
    preinstall = scripts_root / "preinstall"
    postinstall = scripts_root / "postinstall"
    preinstall.write_text(
        """#!/bin/sh
set -eu

target_volume="${3:-/}"
install_dir="$target_volume/Library/Application Support/Sahara/sahara"

if [ -d "$install_dir" ]; then
  rm -rf "$install_dir"
fi

exit 0
""",
        encoding="utf-8",
    )
    postinstall.write_text(
        """#!/bin/sh
set -eu

target_volume="${3:-/}"
link_dir="$target_volume/usr/local/bin"
target="/Library/Application Support/Sahara/sahara/sahara"

mkdir -p "$link_dir"
ln -sfn "$target" "$link_dir/sahara"

exit 0
""",
        encoding="utf-8",
    )
    for script in (preinstall, postinstall):
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def is_codesign_candidate(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    with path.open("rb") as handle:
        return handle.read(4) in MACHO_MAGICS


def sign_bundle(bundle: Path, identity: str) -> None:
    candidates = sorted(path for path in bundle.rglob("*") if is_codesign_candidate(path))
    for candidate in candidates:
        subprocess.run(
            [
                "codesign",
                "--force",
                "--timestamp",
                "--options",
                "runtime",
                "--sign",
                identity,
                str(candidate),
            ],
            check=True,
        )


def strip_macos_metadata(payload_root: Path) -> None:
    try:
        subprocess.run(["xattr", "-cr", str(payload_root)], check=False)
    except FileNotFoundError:
        return


def build_pkg(
    *,
    payload_root: Path,
    scripts_root: Path,
    package: Path,
    version: str,
    installer_identity: str | None,
) -> Path:
    package.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "pkgbuild",
        "--root",
        str(payload_root),
        "--identifier",
        PACKAGE_ID,
        "--version",
        version,
        "--install-location",
        "/",
        "--filter",
        r"^./._.*",
        "--filter",
        r"^.*/._.*",
        "--filter",
        r"^.*/.DS_Store$",
        "--scripts",
        str(scripts_root),
    ]
    if installer_identity:
        cmd.extend(["--sign", installer_identity, "--timestamp"])
    cmd.append(str(package))
    env = os.environ.copy()
    env["COPYFILE_DISABLE"] = "1"
    env["DITTONORSRC"] = "1"
    subprocess.run(cmd, check=True, env=env)
    return package


def notarize_pkg(
    package: Path,
    *,
    apple_id: str,
    team_id: str,
    password: str,
) -> None:
    subprocess.run(
        [
            "xcrun",
            "notarytool",
            "submit",
            str(package),
            "--apple-id",
            apple_id,
            "--team-id",
            team_id,
            "--password",
            password,
            "--wait",
        ],
        check=True,
    )
    subprocess.run(["xcrun", "stapler", "staple", str(package)], check=True)


def write_manifest(
    destination: Path,
    *,
    package: Path,
    bundle: Path,
    checksum: str,
    signed: bool,
    notarized: bool,
) -> Path:
    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "package": package.name,
        "package_id": PACKAGE_ID,
        "package_sha256": checksum,
        "bundle": bundle.name,
        "install_location": "/" + str(INSTALL_ROOT),
        "path_link": "/" + str(PATH_LINK),
        "signed": signed,
        "notarized": notarized,
        "preserves_user_data": ["~/.sahara"],
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
    }
    destination.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return destination


def build_macos_installer(
    bundle: Path,
    installer_root: Path = DEFAULT_INSTALLER_ROOT,
    *,
    application_identity: str | None = None,
    installer_identity: str | None = None,
    notarize: bool = False,
    apple_id: str | None = None,
    team_id: str | None = None,
    apple_password: str | None = None,
    skip_platform_check: bool = False,
) -> MacOSInstallerArtifact:
    require_macos_arm64(skip_platform_check)
    version = project_version(PROJECT_FILE)
    package = installer_root / f"sahara-{version}-{PLATFORM_TAG}.pkg"
    checksum = installer_root / f"{package.name}.sha256"
    manifest = installer_root / f"sahara-{version}-{PLATFORM_TAG}-installer-manifest.json"
    installer_root.mkdir(parents=True, exist_ok=True)

    if notarize and not (
        apple_id
        and team_id
        and apple_password
        and application_identity
        and installer_identity
    ):
        raise ValueError(
            "notarization requires Apple ID, team ID, app-specific password, "
            "a Developer ID Application identity, and a Developer ID Installer identity"
        )

    with tempfile.TemporaryDirectory(prefix="sahara-macos-installer-") as temp_dir:
        temp = Path(temp_dir)
        payload_root = temp / "payload"
        scripts_root = temp / "scripts"
        prepare_payload(bundle, payload_root)
        write_installer_scripts(scripts_root)
        strip_macos_metadata(payload_root)
        strip_macos_metadata(scripts_root)
        if application_identity:
            sign_bundle(payload_root / INSTALL_ROOT, application_identity)
        build_pkg(
            payload_root=payload_root,
            scripts_root=scripts_root,
            package=package,
            version=version,
            installer_identity=installer_identity,
        )
        if notarize:
            notarize_pkg(
                package,
                apple_id=apple_id or "",
                team_id=team_id or "",
                password=apple_password or "",
            )

    digest = write_checksum(package, checksum)
    write_manifest(
        manifest,
        package=package,
        bundle=bundle,
        checksum=digest,
        signed=bool(application_identity and installer_identity),
        notarized=notarize,
    )
    verify_macos_installer(installer_root, package.name)
    return MacOSInstallerArtifact(package=package, checksum=checksum, manifest=manifest)


def verify_macos_installer(installer_root: Path, package_name: str) -> None:
    package = installer_root / package_name
    checksum = installer_root / f"{package_name}.sha256"
    manifest = installer_root / package_name.replace(".pkg", "-installer-manifest.json")
    missing = [path for path in (package, checksum, manifest) if not path.is_file()]
    if missing:
        raise ValueError(
            "missing macOS installer file(s): "
            + ", ".join(str(path) for path in missing)
        )

    expected_digest = checksum.read_text(encoding="utf-8").split()[0]
    actual_digest = sha256_file(package)
    if actual_digest != expected_digest:
        raise ValueError(
            f"checksum mismatch for {package.name}: {actual_digest} != {expected_digest}"
        )

    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    if manifest_data.get("package") != package.name:
        raise ValueError(f"manifest package mismatch: {manifest_data.get('package')!r}")
    if manifest_data.get("package_id") != PACKAGE_ID:
        raise ValueError(f"manifest package id mismatch: {manifest_data.get('package_id')!r}")
    if manifest_data.get("package_sha256") != actual_digest:
        raise ValueError("manifest checksum does not match package")
    if manifest_data.get("install_location") != "/" + str(INSTALL_ROOT):
        raise ValueError("manifest install location does not match supported path")
    if "~/.sahara" not in manifest_data.get("preserves_user_data", []):
        raise ValueError("manifest does not document preserved user data")


def main() -> None:
    version = project_version(PROJECT_FILE)
    default_bundle = Path("dist") / "native" / bundle_name(version)

    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, default=default_bundle)
    parser.add_argument("--installer-root", type=Path, default=DEFAULT_INSTALLER_ROOT)
    parser.add_argument(
        "--application-identity",
        default=os.environ.get("MACOS_DEVELOPER_ID_APPLICATION_IDENTITY"),
        help="Developer ID Application identity used to sign bundled executables.",
    )
    parser.add_argument(
        "--installer-identity",
        default=os.environ.get("MACOS_DEVELOPER_ID_INSTALLER_IDENTITY"),
        help="Developer ID Installer identity used to sign the pkg.",
    )
    parser.add_argument(
        "--notarize",
        action="store_true",
        help="Submit the signed pkg to Apple notarytool and staple the ticket.",
    )
    parser.add_argument("--apple-id", default=os.environ.get("APPLE_ID"))
    parser.add_argument("--team-id", default=os.environ.get("APPLE_TEAM_ID"))
    parser.add_argument(
        "--apple-password",
        default=os.environ.get("APPLE_APP_SPECIFIC_PASSWORD"),
        help="Apple app-specific password for notarytool.",
    )
    parser.add_argument(
        "--skip-platform-check",
        action="store_true",
        help="Allow non-macOS-arm64 execution for metadata tests.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify existing macOS installer files instead of building a pkg.",
    )
    args = parser.parse_args()

    package_name = f"sahara-{version}-{PLATFORM_TAG}.pkg"
    if args.verify_only:
        verify_macos_installer(args.installer_root, package_name)
        print(f"Verified macOS installer {package_name}")
        return

    artifact = build_macos_installer(
        args.bundle,
        args.installer_root,
        application_identity=args.application_identity,
        installer_identity=args.installer_identity,
        notarize=args.notarize,
        apple_id=args.apple_id,
        team_id=args.team_id,
        apple_password=args.apple_password,
        skip_platform_check=args.skip_platform_check,
    )
    print(f"Built {artifact.package}")
    print(f"Wrote {artifact.checksum}")
    print(f"Wrote {artifact.manifest}")


if __name__ == "__main__":
    main()
