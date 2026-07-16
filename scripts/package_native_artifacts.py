"""Package and verify native Sahara bundle artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_macos_bundle import PROJECT_FILE, bundle_name, project_version  # noqa: E402
from scripts.build_windows_bundle import bundle_name as windows_bundle_name  # noqa: E402

DEFAULT_ARTIFACT_ROOT = Path("dist") / "native-artifacts"
MACOS_PLATFORM = "macos-arm64"
WINDOWS_PLATFORM = "windows-x64"


@dataclass(frozen=True)
class NativeArtifact:
    bundle: Path
    archive: Path
    checksum: Path
    inventory: Path
    smoke_log: Path
    manifest: Path


@dataclass(frozen=True)
class NativePlatform:
    name: str
    archive_suffix: str
    smoke_script: str


PLATFORMS = {
    MACOS_PLATFORM: NativePlatform(
        name=MACOS_PLATFORM,
        archive_suffix=".tar.gz",
        smoke_script="scripts/smoke_macos_bundle.py",
    ),
    WINDOWS_PLATFORM: NativePlatform(
        name=WINDOWS_PLATFORM,
        archive_suffix=".zip",
        smoke_script="scripts/smoke_windows_bundle.py",
    ),
}


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


def create_tarball(bundle: Path, destination: Path) -> Path:
    if not bundle.is_dir():
        raise ValueError(f"bundle directory does not exist: {bundle}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as archive:
        archive.add(bundle, arcname=bundle.name)
    return destination


def create_zip(bundle: Path, destination: Path) -> Path:
    if not bundle.is_dir():
        raise ValueError(f"bundle directory does not exist: {bundle}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            archive.write(path, path.relative_to(bundle.parent))
    return destination


def create_archive(bundle: Path, destination: Path, platform_config: NativePlatform) -> Path:
    if platform_config.archive_suffix == ".tar.gz":
        return create_tarball(bundle, destination)
    if platform_config.archive_suffix == ".zip":
        return create_zip(bundle, destination)
    raise ValueError(f"unsupported archive suffix: {platform_config.archive_suffix}")


def write_dependency_inventory(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "list", "--format=json"],
        check=True,
        text=True,
        capture_output=True,
    )
    packages = sorted(json.loads(result.stdout), key=lambda item: item["name"].lower())
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["name", "version"])
        writer.writeheader()
        writer.writerows({"name": item["name"], "version": item["version"]} for item in packages)
    return destination


def run_smoke(
    bundle: Path,
    destination: Path,
    *,
    smoke_script: str,
    with_index: bool = False,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, smoke_script, str(bundle)]
    if with_index:
        cmd.append("--with-index")
    result = subprocess.run(cmd, check=False, text=True, capture_output=True)
    destination.write_text(
        "\n".join(
            [
                f"command: {' '.join(cmd)}",
                f"returncode: {result.returncode}",
                "",
                "stdout:",
                result.stdout,
                "stderr:",
                result.stderr,
            ]
        ),
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise SystemExit(f"bundle smoke test failed; see {destination}")
    return destination


def write_manifest(
    destination: Path,
    *,
    bundle: Path,
    archive: Path,
    checksum: str,
    inventory: Path,
    smoke_log: Path,
    platform_name: str,
) -> Path:
    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "platform_tag": platform_name,
        "bundle": bundle.name,
        "archive": archive.name,
        "archive_sha256": checksum,
        "inventory": inventory.name,
        "smoke_log": smoke_log.name,
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
    }
    destination.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return destination


def package_native_artifact(
    bundle: Path,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    *,
    platform_name: str = MACOS_PLATFORM,
    with_index: bool = False,
) -> NativeArtifact:
    platform_config = PLATFORMS[platform_name]
    artifact_root.mkdir(parents=True, exist_ok=True)
    archive = artifact_root / f"{bundle.name}{platform_config.archive_suffix}"
    checksum = artifact_root / f"{archive.name}.sha256"
    inventory = artifact_root / f"{bundle.name}-dependencies.csv"
    smoke_log = artifact_root / f"{bundle.name}-smoke.txt"
    manifest = artifact_root / f"{bundle.name}-manifest.json"

    create_archive(bundle, archive, platform_config)
    digest = write_checksum(archive, checksum)
    write_dependency_inventory(inventory)
    run_smoke(
        bundle,
        smoke_log,
        smoke_script=platform_config.smoke_script,
        with_index=with_index,
    )
    write_manifest(
        manifest,
        bundle=bundle,
        archive=archive,
        checksum=digest,
        inventory=inventory,
        smoke_log=smoke_log,
        platform_name=platform_name,
    )
    return NativeArtifact(
        bundle=bundle,
        archive=archive,
        checksum=checksum,
        inventory=inventory,
        smoke_log=smoke_log,
        manifest=manifest,
    )


def verify_native_artifact(
    artifact_root: Path,
    expected_name: str,
    *,
    platform_name: str = MACOS_PLATFORM,
) -> None:
    platform_config = PLATFORMS[platform_name]
    archive = artifact_root / f"{expected_name}{platform_config.archive_suffix}"
    checksum = artifact_root / f"{archive.name}.sha256"
    inventory = artifact_root / f"{expected_name}-dependencies.csv"
    smoke_log = artifact_root / f"{expected_name}-smoke.txt"
    manifest = artifact_root / f"{expected_name}-manifest.json"
    missing = [
        path
        for path in (archive, checksum, inventory, smoke_log, manifest)
        if not path.is_file()
    ]
    if missing:
        raise ValueError(
            "missing native artifact file(s): "
            + ", ".join(str(path) for path in missing)
        )

    expected_digest = checksum.read_text(encoding="utf-8").split()[0]
    actual_digest = sha256_file(archive)
    if actual_digest != expected_digest:
        raise ValueError(
            f"checksum mismatch for {archive.name}: {actual_digest} != {expected_digest}"
        )

    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    if manifest_data.get("platform_tag") != platform_name:
        raise ValueError(f"manifest platform mismatch: {manifest_data.get('platform_tag')!r}")
    if manifest_data.get("bundle") != expected_name:
        raise ValueError(f"manifest bundle mismatch: {manifest_data.get('bundle')!r}")
    if manifest_data.get("archive") != archive.name:
        raise ValueError(f"manifest archive mismatch: {manifest_data.get('archive')!r}")
    if manifest_data.get("archive_sha256") != actual_digest:
        raise ValueError("manifest checksum does not match archive")
    if "returncode: 0" not in smoke_log.read_text(encoding="utf-8"):
        raise ValueError(f"smoke log does not show success: {smoke_log}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--platform",
        choices=sorted(PLATFORMS),
        default=MACOS_PLATFORM,
        help="Native platform artifact format to package or verify.",
    )
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument(
        "--with-index",
        action="store_true",
        help="Run the bundle smoke test with embedding/index/search validation.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify existing native artifact files instead of packaging a bundle.",
    )
    args = parser.parse_args()

    version = project_version(PROJECT_FILE)
    default_names = {
        MACOS_PLATFORM: bundle_name(version),
        WINDOWS_PLATFORM: windows_bundle_name(version),
    }
    bundle = args.bundle or Path("dist") / "native" / default_names[args.platform]

    expected_name = bundle.name
    if args.verify_only:
        verify_native_artifact(args.artifact_root, expected_name, platform_name=args.platform)
        print(f"Verified native artifact {expected_name}")
        return

    if args.artifact_root.exists():
        shutil.rmtree(args.artifact_root)
    artifact = package_native_artifact(
        bundle,
        args.artifact_root,
        platform_name=args.platform,
        with_index=args.with_index,
    )
    verify_native_artifact(args.artifact_root, expected_name, platform_name=args.platform)
    print(f"Packaged {artifact.archive}")
    print(f"Wrote {artifact.checksum}")
    print(f"Wrote {artifact.inventory}")
    print(f"Wrote {artifact.smoke_log}")
    print(f"Wrote {artifact.manifest}")


if __name__ == "__main__":
    main()
