# Linux x86_64 Portable Artifact

This document describes the portable Linux x86_64 Sahara artifact. It creates a
one-folder PyInstaller runtime that can be unpacked under a user's home directory or
another writable application directory. Git, pip, pipx, or system Python are not
required.

## Supported Baseline

The release artifact is built on `ubuntu-22.04` GitHub Actions runners and supports
x86_64 Linux distributions with glibc 2.35 or newer. Other CPU architectures, musl
distributions such as Alpine Linux, older enterprise distributions, and systems without
standard writable home/cache directories are outside the first supported portable
scope.

Claude Desktop is not officially available on Linux, so the portable artifact supports
stdio MCP server startup but does not promise automatic Claude Desktop configuration.

## Build Locally

Install Sahara with the dependencies needed by the native build:

```bash
python -m pip install -e ".[all,native]"
```

Build the one-folder runtime on Linux x86_64:

```bash
python scripts/build_linux_bundle.py
```

The versioned output is:

```text
dist/native/sahara-0.3.0-linux-x86_64/
```

Print the deterministic output name without building:

```bash
python scripts/build_linux_bundle.py --print-name
# sahara-0.3.0-linux-x86_64
```

Run the bundled executable:

```bash
dist/native/sahara-0.3.0-linux-x86_64/sahara --version
```

## Smoke Test

Run the no-index smoke test:

```bash
python scripts/smoke_linux_bundle.py
```

Run the fuller smoke test that downloads the embedding model, indexes a fixture, and
searches it:

```bash
python scripts/smoke_linux_bundle.py --with-index
```

Both checks isolate `HOME`, `HF_HOME`, and XDG cache/config/data paths so the test does
not reuse the maintainer's normal Sahara configuration or model cache.

## Release Artifact Packaging

Release automation packages the one-folder bundle into a versioned `.tar.gz`, then
writes a SHA-256 checksum, dependency inventory, manifest, and smoke-test log:

```bash
python scripts/package_native_artifacts.py --platform linux-x86_64 --with-index
```

The artifact directory is:

```text
dist/native-artifacts/
```

Verify an existing artifact directory with:

```bash
python scripts/package_native_artifacts.py --platform linux-x86_64 --verify-only
shasum -a 256 -c sahara-0.3.0-linux-x86_64.tar.gz.sha256
```

## Install From Release

Download `sahara-0.3.0-linux-x86_64.tar.gz` and its `.sha256` file from the GitHub
release, then verify and unpack it:

```bash
shasum -a 256 -c sahara-0.3.0-linux-x86_64.tar.gz.sha256
mkdir -p "$HOME/.local/opt" "$HOME/.local/bin"
tar -xzf sahara-0.3.0-linux-x86_64.tar.gz -C "$HOME/.local/opt"
ln -sfn "$HOME/.local/opt/sahara-0.3.0-linux-x86_64/sahara" "$HOME/.local/bin/sahara"
sahara --version
```

Make sure `$HOME/.local/bin` is on `PATH`.

Upgrade by unpacking the newer archive beside the previous version and repointing the
symlink. Remove the portable runtime by deleting the extracted versioned directory and
symlink:

```bash
rm -f "$HOME/.local/bin/sahara"
rm -rf "$HOME/.local/opt/sahara-0.3.0-linux-x86_64"
```

These commands preserve `~/.sahara`, configuration, indexes, credentials, and model
caches by default.

## Clean-Machine Check

On a clean supported Linux x86_64 VM:

1. Verify the checksum with `shasum -a 256 -c`.
2. Unpack under `$HOME/.local/opt` and create a `$HOME/.local/bin/sahara` symlink.
3. Run `sahara --version`.
4. Run non-interactive setup against a test folder:

   ```bash
   mkdir -p "$HOME/sahara-smoke"
   printf 'Sahara Linux portable check about lunar geology.\n' > "$HOME/sahara-smoke/notes.txt"
   sahara setup --folder "$HOME/sahara-smoke" --yes --no-mcp --no-doctor --no-daemon
   sahara index
   sahara search "lunar geology" --snippet
   sahara mcp serve --transport stdio
   ```

5. Upgrade over a previous portable version and confirm `~/.sahara`, configured
   folders, and existing indexes remain usable.
6. Remove the symlink and extracted runtime directory and confirm `~/.sahara` remains
   present by default.
