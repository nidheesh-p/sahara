# macOS Apple Silicon Native Distribution

This document describes the local PyInstaller prototype for issue #47. It creates a
one-folder Sahara runtime for macOS Apple Silicon that includes Python and Sahara's
runtime dependencies, while leaving user data, credentials, Ollama, answer models, and
the embedding model outside the bundle.

The one-folder bundle remains an intermediate release artifact. Public macOS releases
are distributed as a signed and notarized installer package built from that bundle.

## Prerequisites

Build on a macOS Apple Silicon host. Cross-compilation is intentionally out of scope.

Install Sahara with the dependencies needed by the native prototype:

```bash
python -m pip install -e '.[all,native]'
```

## Build

From the repository root:

```bash
python scripts/build_macos_bundle.py
```

The output directory name is deterministic and versioned:

```bash
python scripts/build_macos_bundle.py --print-name
# sahara-0.2.1-macos-arm64
```

By default the bundle is written to:

```text
dist/native/sahara-0.2.1-macos-arm64/
```

The executable is:

```text
dist/native/sahara-0.2.1-macos-arm64/sahara
```

## Smoke Test

Run the quick smoke test after building:

```bash
python scripts/smoke_macos_bundle.py
```

The quick smoke test verifies:

- `sahara --version`
- non-interactive basic `sahara setup`
- stdio MCP server startup

To also exercise first-use embedding-model download, indexing, and search:

```bash
python scripts/smoke_macos_bundle.py --with-index
```

The `--with-index` path may download the embedding model on first use. The model is
cached outside the bundle by FastEmbed and is not shipped in the artifact.

## Release Artifact Packaging

Release automation packages the one-folder bundle into a versioned archive, then
generates a checksum, bundled dependency inventory, manifest, and smoke-test log:

```bash
python scripts/package_native_artifacts.py --with-index
```

The generated files are written under:

```text
dist/native-artifacts/
```

The GitHub Actions workflow for this path is `Native Artifacts`. It runs only from
`workflow_dispatch` or `v*` release tags, and it keeps uploaded artifacts for seven
days. Ordinary pull requests do not run the PyInstaller native build.

To verify an already-packaged artifact directory:

```bash
python scripts/package_native_artifacts.py --verify-only
```

## Installer Package

Build the macOS installer package from an existing one-folder bundle:

```bash
python scripts/build_macos_installer.py
```

The unsigned development package is written to:

```text
dist/native-installers/sahara-0.2.1-macos-arm64.pkg
```

The package installs the bundle at a stable location:

```text
/Library/Application Support/Sahara/sahara/
```

It also creates or updates the command shim:

```text
/usr/local/bin/sahara -> /Library/Application Support/Sahara/sahara/sahara
```

The package also installs:

```text
/usr/local/bin/sahara-first-run
```

At the end of a normal graphical install, `postinstall` attempts to open the
first-run setup in Terminal as the logged-in console user. It does not run folder
selection, indexing, or Claude Desktop configuration as `root`. Set
`SAHARA_SKIP_FIRST_RUN_LAUNCH=1` when installer automation must skip the launch.

The first-run flow lets the user choose folders to index, builds the first index with
consent, and offers to connect Claude Desktop when it is detected. It can be relaunched
with `sahara-first-run` or `sahara first-run`.

Upgrades replace only the installed bundle directory and command shim. User data,
configuration, indexes, model caches, and credentials remain outside the package and
are preserved by default, including `~/.sahara`.

For release builds, import Developer ID Application and Developer ID Installer
certificates into the temporary runner keychain, then build the signed and notarized
package:

```bash
python scripts/build_macos_installer.py --notarize
```

The script reads these protected-environment variables when explicit flags are not
provided:

- `MACOS_DEVELOPER_ID_APPLICATION_IDENTITY`
- `MACOS_DEVELOPER_ID_INSTALLER_IDENTITY`
- `APPLE_ID`
- `APPLE_TEAM_ID`
- `APPLE_APP_SPECIFIC_PASSWORD`

It generates the package, `.sha256` checksum, and installer manifest under
`dist/native-installers/`. Verify an existing installer artifact directory with:

```bash
python scripts/build_macos_installer.py --verify-only
pkgutil --check-signature dist/native-installers/*.pkg
spctl -a -vv -t install dist/native-installers/*.pkg
```

The GitHub Actions `Native Artifacts` workflow builds this installer only for `v*`
release tags, inside the protected `macos-installer` environment. Pull requests and
ordinary branch pushes do not receive signing secrets.

## Packaged Resources

The PyInstaller spec includes Sahara package data and metadata, plus hidden imports,
data files, or native libraries for the dependency families called out in the simple
installer plan:

- FastEmbed and ONNX Runtime
- sqlite-vec
- MCP SDK
- keyring and cryptography
- pypdf and python-docx
- Sahara's `.saharaignore` template and Shortcut artifacts

If a bundled smoke test fails because a runtime hook or resource is missing, update
`packaging/pyinstaller/sahara_macos_arm64.spec` and add a regression check in
`tests/test_native_bundle.py`.

## Clean-Machine Check

For bundle-level validation, copy `dist/native/sahara-0.2.1-macos-arm64/` to a clean
Apple Silicon account or VM without a Sahara checkout and run:

```bash
BUNDLE="$PWD/sahara-0.2.1-macos-arm64/sahara"
TMP="$(mktemp -d)"
mkdir -p "$TMP/home" "$TMP/content"
printf 'Sahara bundle smoke test document about lunar geology.\n' > "$TMP/content/notes.txt"

HOME="$TMP/home" HF_HOME="$TMP/hf-cache" XDG_CACHE_HOME="$TMP/cache" \
  "$BUNDLE" --version

HOME="$TMP/home" HF_HOME="$TMP/hf-cache" XDG_CACHE_HOME="$TMP/cache" \
  "$BUNDLE" --config "$TMP/config.toml" setup \
  --folder "$TMP/content" --yes --no-mcp --no-doctor --no-daemon

HOME="$TMP/home" HF_HOME="$TMP/hf-cache" XDG_CACHE_HOME="$TMP/cache" \
  "$BUNDLE" --config "$TMP/config.toml" search "lunar geology"

HOME="$TMP/home" HF_HOME="$TMP/hf-cache" XDG_CACHE_HOME="$TMP/cache" \
  "$BUNDLE" --config "$TMP/config.toml" mcp serve --transport stdio
```

The final MCP command starts the stdio server and waits for MCP client input; stop it
with Ctrl-C after confirming it starts without an import or startup error. These
commands use isolated temporary state and cache directories. The bundle itself must
not depend on a repository checkout or system Python.

For installer-level validation on a clean Apple Silicon Mac, install the signed and
notarized package and run:

```bash
sudo installer -pkg sahara-0.2.1-macos-arm64.pkg -target /
command -v sahara
sahara --version
sahara setup --folder "$HOME/Documents" --yes --no-mcp --no-doctor --no-daemon
sahara index
sahara search "known text from a test document"
sahara mcp install-claude
```

Upgrade by installing the next package over the existing one, then confirm
`~/.sahara`, the configured folders, and existing indexes are still usable.

Uninstall the installed bundle and command shim without deleting user data:

```bash
sudo rm -rf "/Library/Application Support/Sahara/sahara"
sudo rm -f /usr/local/bin/sahara
sudo pkgutil --forget io.github.nidheesh-p.sahara
```

Do not remove `~/.sahara` unless the user explicitly requests data removal.
