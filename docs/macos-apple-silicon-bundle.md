# macOS Apple Silicon Bundle Prototype

This document describes the local PyInstaller prototype for issue #47. It creates a
one-folder Sahara runtime for macOS Apple Silicon that includes Python and Sahara's
runtime dependencies, while leaving user data, credentials, Ollama, answer models, and
the embedding model outside the bundle.

The bundle is a development artifact, not a signed or notarized installer.

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

For #47 acceptance, copy `dist/native/sahara-0.2.1-macos-arm64/` to a clean Apple
Silicon account or VM without a Sahara checkout and run:

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
