# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-folder bundle for macOS Apple Silicon."""

import os
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

ROOT = Path(SPECPATH).parents[1]
SRC = ROOT / "src"
BUNDLE_NAME = os.environ.get("SAHARA_BUNDLE_NAME", "sahara-dev-macos-arm64")


def _collect_metadata(distribution):
    try:
        return copy_metadata(distribution)
    except Exception:
        return []


def _collect_data(package, includes=None):
    try:
        return collect_data_files(package, includes=includes)
    except Exception:
        return []


def _collect_dynamic_libs(package):
    try:
        return collect_dynamic_libs(package)
    except Exception:
        return []


def _collect_submodules(package):
    try:
        return collect_submodules(package)
    except Exception:
        return []


datas = []
datas += _collect_data("sahara", includes=["data/**"])
datas += _collect_data("sahara.data.shortcuts", includes=["*.json"])
datas += _collect_data("fastembed")
datas += _collect_data("onnxruntime")
datas += _collect_data("sqlite_vec")
datas += _collect_data("mcp")
datas += _collect_metadata("sahara-memory")
datas += _collect_metadata("fastembed")
datas += _collect_metadata("onnxruntime")
datas += _collect_metadata("sqlite-vec")
datas += _collect_metadata("mcp")
datas += _collect_metadata("keyring")
datas += _collect_metadata("cryptography")
datas += _collect_metadata("pypdf")
datas += _collect_metadata("python-docx")

binaries = []
binaries += _collect_dynamic_libs("onnxruntime")
binaries += _collect_dynamic_libs("sqlite_vec")
binaries += _collect_dynamic_libs("cryptography")

hiddenimports = []
hiddenimports += _collect_submodules("fastembed")
hiddenimports += _collect_submodules("sqlite_vec")
hiddenimports += _collect_submodules("mcp")
hiddenimports += [
    "docx",
    "keyring.backends.macOS",
    "keyring.backends.null",
    "pypdf",
    "yaml",
    "watchdog.observers.fsevents",
]

a = Analysis(
    ["sahara_entry.py"],
    pathex=[str(Path(SPECPATH)), str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "keyring.backends.SecretService",
        "keyring.backends.Windows",
        "keyring.backends.kwallet",
        "keyring.backends.libsecret",
        "mypy",
        "onnx",
        "py",
        "pytest",
        "tensorflow",
        "torch",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sahara",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=BUNDLE_NAME,
)
