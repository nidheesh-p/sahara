"""Build Sahara's Windows x64 PyInstaller bundle."""

from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import subprocess
import sys
from pathlib import Path

from scripts.build_macos_bundle import PROJECT_FILE, project_version

ROOT = Path(__file__).resolve().parents[1]
SPEC_FILE = ROOT / "packaging" / "pyinstaller" / "sahara_windows_x64.spec"
DIST_ROOT = ROOT / "dist" / "native"
WORK_ROOT = ROOT / "build" / "pyinstaller-windows"
PLATFORM_TAG = "windows-x64"


def bundle_name(version: str) -> str:
    return f"sahara-{version}-{PLATFORM_TAG}"


def is_windows_x64() -> bool:
    machine = platform.machine().lower()
    return platform.system() == "Windows" and machine in {"amd64", "x86_64"}


def require_pyinstaller() -> None:
    if importlib.util.find_spec("PyInstaller") is None:
        raise SystemExit(
            "PyInstaller is required. Install native build dependencies with:\n"
            "  python -m pip install -e '.[all,native]'"
        )


def build_bundle(
    *,
    project_file: Path = PROJECT_FILE,
    dist_root: Path = DIST_ROOT,
    work_root: Path = WORK_ROOT,
    skip_platform_check: bool = False,
) -> Path:
    if not skip_platform_check and not is_windows_x64():
        raise SystemExit(
            "Windows x64 bundles must be built on Windows x64. "
            "Pass --skip-platform-check only for spec smoke tests."
        )
    require_pyinstaller()

    version = project_version(project_file)
    name = bundle_name(version)
    dist_root.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["SAHARA_BUNDLE_NAME"] = name
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(dist_root),
        "--workpath",
        str(work_root),
        str(SPEC_FILE),
    ]
    subprocess.run(cmd, cwd=ROOT, env=env, check=True)
    return dist_root / name


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-file", type=Path, default=PROJECT_FILE)
    parser.add_argument("--dist-root", type=Path, default=DIST_ROOT)
    parser.add_argument("--work-root", type=Path, default=WORK_ROOT)
    parser.add_argument(
        "--skip-platform-check",
        action="store_true",
        help="Allow non-Windows-x64 execution for spec smoke tests.",
    )
    parser.add_argument(
        "--print-name",
        action="store_true",
        help="Print the deterministic bundle directory name and exit.",
    )
    args = parser.parse_args()

    version = project_version(args.project_file)
    if args.print_name:
        print(bundle_name(version))
        return

    bundle = build_bundle(
        project_file=args.project_file,
        dist_root=args.dist_root,
        work_root=args.work_root,
        skip_platform_check=args.skip_platform_check,
    )
    print(f"Built {bundle}")


if __name__ == "__main__":
    main()
