# Windows x64 Native Installer

This document describes the Windows x64 native installer path for Sahara. It creates
a one-folder PyInstaller runtime and packages it as a per-user Inno Setup installer
that does not require Git, Python, pip, pipx, or administrator access.

The installer includes Sahara's Python runtime and runtime dependencies. It does not
include user data, credentials, answer models, Ollama, or the local embedding model.

## Prerequisites

Build on a Windows x64 host. Cross-compilation is intentionally out of scope.

Install Sahara with the dependencies needed by the native build:

```powershell
python -m pip install -e ".[all,native]"
```

Install Inno Setup 6 before building the installer. The GitHub Actions release job
installs it with Chocolatey.

## Build

From the repository root:

```powershell
python scripts/build_windows_bundle.py
```

The output directory name is deterministic and versioned:

```powershell
python scripts/build_windows_bundle.py --print-name
# sahara-0.2.1-windows-x64
```

By default the bundle is written to:

```text
dist/native/sahara-0.2.1-windows-x64/
```

The executable is:

```text
dist/native/sahara-0.2.1-windows-x64/sahara.exe
```

## Smoke Test

Run the quick smoke test after building:

```powershell
python scripts/smoke_windows_bundle.py
```

The quick smoke test verifies:

- `sahara --version`
- non-interactive basic `sahara setup`
- stdio MCP server startup

To also exercise first-use embedding-model download, indexing, and search:

```powershell
python scripts/smoke_windows_bundle.py --with-index
```

The `--with-index` path may download the embedding model on first use. The model is
cached outside the bundle and is not shipped in the artifact.

## Release Artifact Packaging

Package the one-folder bundle into a versioned zip archive, checksum, bundled
dependency inventory, manifest, and smoke-test log:

```powershell
python scripts/package_native_artifacts.py --platform windows-x64 --with-index
```

The generated files are written under:

```text
dist/native-artifacts/
```

Verify an already-packaged artifact directory with:

```powershell
python scripts/package_native_artifacts.py --platform windows-x64 --verify-only
```

## Installer Package

Build the per-user installer from an existing one-folder bundle:

```powershell
python scripts/build_windows_installer.py
```

The unsigned development installer is written to:

```text
dist/native-installers/sahara-0.2.1-windows-x64-setup.exe
```

The installer uses the current user's application directory:

```text
%LOCALAPPDATA%\Programs\Sahara
```

It adds that directory to the current user's `PATH`, which exposes `sahara` in new
terminals. It does not write to the system PATH and does not require administrator
access.

At the end of a normal graphical install, the installer offers to launch:

```powershell
sahara first-run
```

The first-run flow lets the user choose one or more folders, builds the first index
with consent, and offers to connect Claude Desktop when it is detected. Silent
installs skip the first-run launch.

Quiet installation for clean-machine validation:

```powershell
.\sahara-0.2.1-windows-x64-setup.exe /VERYSILENT /NORESTART /SUPPRESSMSGBOXES
```

Upgrades replace only the installed application directory and PATH entry. User data,
configuration, indexes, model caches, and credentials remain outside the installer and
are preserved by default, including `%USERPROFILE%\.sahara`.

Default uninstall also preserves user data:

```powershell
$uninstall = Join-Path $env:LOCALAPPDATA "Programs\Sahara\unins000.exe"
& $uninstall /VERYSILENT /NORESTART /SUPPRESSMSGBOXES
```

Do not remove `%USERPROFILE%\.sahara` unless the user explicitly requests data
removal.

## Signing

Release builds sign the installer with Authenticode after Inno Setup creates it:

```powershell
python scripts/build_windows_installer.py --sign
```

The script reads these protected-environment variables when explicit flags are not
provided:

- `WINDOWS_CODESIGN_CERTIFICATE_BASE64`
- `WINDOWS_CODESIGN_CERTIFICATE_PASSWORD`
- `WINDOWS_CODESIGN_TIMESTAMP_URL`

The GitHub Actions `Native Artifacts` workflow builds the signed installer only for
`v*` release tags, inside the protected `windows-installer` environment. Pull requests
and ordinary branch pushes do not receive signing secrets.

Verify an existing installer artifact directory with:

```powershell
python scripts/build_windows_installer.py --verify-only
Get-AuthenticodeSignature dist/native-installers/*.exe
```

## Clean-Machine Check

For installer-level validation on a clean Windows x64 VM, install the signed package
and run:

```powershell
.\sahara-0.2.1-windows-x64-setup.exe /VERYSILENT /NORESTART /SUPPRESSMSGBOXES
sahara --version
$content = Join-Path $env:TEMP "sahara-content"
New-Item -ItemType Directory -Force -Path $content | Out-Null
Set-Content -Path (Join-Path $content "notes.txt") -Value "Windows Sahara installer smoke test document about lunar geology."
sahara setup --folder $content --yes --no-mcp --no-doctor --no-daemon
sahara index
sahara search "lunar geology"
sahara mcp serve --transport stdio
sahara mcp install-claude
```

The MCP stdio command waits for client input; stop it with Ctrl-C after confirming it
starts without an import or startup error.

Upgrade by installing the next package over the existing one, then confirm
`%USERPROFILE%\.sahara`, configured folders, and existing indexes are still usable.

Uninstall with the quiet uninstall command above and confirm `%USERPROFILE%\.sahara`
remains present by default.
