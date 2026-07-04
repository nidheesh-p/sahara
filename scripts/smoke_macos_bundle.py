"""Smoke-test a Sahara macOS one-folder bundle."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_macos_bundle import (  # noqa: E402
    DIST_ROOT,
    PROJECT_FILE,
    bundle_name,
    project_version,
)


def default_bundle_path() -> Path:
    return DIST_ROOT / bundle_name(project_version(PROJECT_FILE))


def run_command(
    args: list[str],
    *,
    env: dict[str, str],
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"Command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def verify_mcp_starts(
    executable: Path,
    config_path: Path,
    *,
    env: dict[str, str],
) -> None:
    process = subprocess.Popen(
        [str(executable), "--config", str(config_path), "mcp", "serve", "--transport", "stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        time.sleep(2)
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=2)
            raise SystemExit(
                "MCP stdio server exited during startup.\n"
                f"stdout:\n{stdout}\n"
                f"stderr:\n{stderr}"
            )
    finally:
        if process.stdin is not None:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def smoke_bundle(bundle_path: Path, *, with_index: bool = False) -> None:
    executable = bundle_path / "sahara"
    if not executable.is_file():
        raise SystemExit(f"Missing bundled executable: {executable}")

    base_env = os.environ.copy()
    with tempfile.TemporaryDirectory(prefix="sahara-bundle-smoke-") as tmp:
        root = Path(tmp)
        home = root / "home"
        home.mkdir()
        env = base_env | {
            "HOME": str(home),
            "HF_HOME": str(root / "hf-cache"),
            "XDG_CACHE_HOME": str(root / "cache"),
        }
        config_path = root / "config.toml"
        content = root / "content"
        content.mkdir()
        (content / "notes.txt").write_text(
            "Sahara bundle smoke test document about lunar geology.\n",
            encoding="utf-8",
        )

        setup_args = [
            str(executable),
            "--config",
            str(config_path),
            "setup",
            "--folder",
            str(content),
            "--yes",
            "--no-mcp",
            "--no-doctor",
            "--no-daemon",
        ]
        if not with_index:
            setup_args.append("--no-index")
        run_command([str(executable), "--version"], env=env)
        run_command(setup_args, env=env, timeout=180)

        if with_index:
            search = run_command(
                [
                    str(executable),
                    "--config",
                    str(config_path),
                    "search",
                    "lunar geology",
                ],
                env=env,
                timeout=180,
            )
            if "notes.txt" not in search.stdout:
                raise SystemExit(f"Expected notes.txt in search output:\n{search.stdout}")

        verify_mcp_starts(executable, config_path, env=env)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path, nargs="?", default=default_bundle_path())
    parser.add_argument(
        "--with-index",
        action="store_true",
        help="Also download/prepare the embedding model, index a fixture, and search it.",
    )
    args = parser.parse_args()

    smoke_bundle(args.bundle, with_index=args.with_index)
    print(f"Smoke test passed for {args.bundle}")


if __name__ == "__main__":
    main()
