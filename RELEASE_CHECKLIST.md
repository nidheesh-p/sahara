# Sahara Release Checklist

Use this checklist before publishing a Sahara release.

## Pre-Release

- Confirm `main` is up to date with `origin/main`.
- Confirm the working tree is clean.
- Confirm `pyproject.toml` version and `sahara --version` match.
- Confirm the distribution name is `sahara-memory`; the `sahara` PyPI project is
  unrelated and must never appear in public installation commands.
- Confirm installation docs state the Python 3.11 minimum.
- Test the `pipx install "sahara-memory[search,mcp]"` path with a clean,
  externally-managed Python such as Homebrew Python.
- Repeat with `pipx install "sahara-memory[search,mcp]" --python PATH` to verify
  explicit selection of a supported Python interpreter.
- Confirm public installation docs do not recommend `--break-system-packages`, global
  `sudo pip`, or direct installation into a managed interpreter.
- Confirm the README latest-release callout and badge point to the published release.
- Review `CHANGELOG.md` and add release notes for the target version.
- Review `README.md`, `ROADMAP.md`, `SECURITY.md`, and integration guides for stale claims.

## Launch Readiness

- Test `CONTRIBUTING.md` from a fresh clone and clean virtual environment.
- Confirm README status, limitations, supported-client claims, and comparison language
  match the implementation.
- Record both demo flows:
  - CLI initialization, indexing, and semantic search.
  - The same query in Claude Desktop with Sahara-provided citations.
- Time the clean-machine Claude Desktop setup on macOS or Windows.
- Confirm `sahara mcp install-claude` preserves existing Claude preferences and MCP
  servers, then successfully reconnects after a full Claude Desktop restart.
- Confirm a new user reaches one cited answer in under five minutes; revise the guide
  if the flow takes longer.
- Confirm the basic path works without Ollama, OpenAI, or an answer-provider network
  request; the local embedding-model download remains expected.
- Separately smoke-test explicit Ollama and OpenAI opt-in without making either one an
  onboarding prerequisite.
- State unvalidated MCP clients as unvalidated. Do not promise ChatGPT, Claude Code,
  Cursor, or OpenClaw support until each path has been tested and documented.

## Verification

- Run `python3 -m pip install -e ".[search,dev]"` in a clean virtual environment.
- Run `pipx install "sahara-memory[search,mcp]"` and `sahara --version` outside the
  contributor virtual environment.
- Run `sahara --version` from the clean virtual environment.
- Run `pytest`.
- Run `ruff check .`.
- Run `mypy src`.
- Confirm GitHub CI passes on the full matrix:
  - Ubuntu Python 3.11
  - Ubuntu Python 3.12
  - macOS Python 3.11
  - macOS Python 3.12
- Confirm CI package build succeeds.

## Backend Validation

- Validate basic/index-only mode without a drive or bucket.
- Validate local drive mode against a temporary folder.
- Validate S3 behavior with the moto-backed test suite.
- Validate MinIO against a local Docker MinIO instance when Docker is available.
- Validate AWS S3 against a real test bucket before publishing a public release.
- Validate `local+glacier` dual-write behavior against local storage plus an S3-compatible test target.
- Validate migration from the previous release without losing sync or index state.
- When offload/fetch is available, validate search-after-offload and checksum-verified fetch.

## Release

- Confirm pending Trusted Publishers exist on TestPyPI and PyPI for repository
  `nidheesh-p/sahara`, workflow `publish.yml`, and the matching `testpypi` or
  `pypi` GitHub environment.
- Run the **Publish** workflow manually from `main` to upload the verified wheel and
  source distribution to TestPyPI.
- Install `sahara-memory[search,mcp]` from the published artifact in a fresh
  environment and confirm `pip show sahara-memory` reports this repository.
- Run the **Native Artifacts** workflow manually from `main` and download the
  `native-macos-arm64` artifact.
- Confirm the native artifact directory contains a versioned archive, `.sha256`
  checksum, dependency inventory, manifest, and smoke-test log.
- Verify the native archive checksum locally from inside the artifact directory with
  `shasum -a 256 -c *.sha256`.
- For `v*` release tags, confirm the **Native Artifacts** workflow also runs the
  `macos-installer` job in the protected `macos-installer` environment.
- Confirm the `macos-installer` environment owns the Developer ID certificate and
  notarization secrets; repository-wide secrets must not be required for macOS
  signing.
- Download the `native-macos-arm64-installer` artifact and confirm it contains the
  versioned `.pkg`, `.pkg.sha256` checksum, and installer manifest.
- Verify the macOS installer checksum locally from inside the installer artifact
  directory with `shasum -a 256 -c *.sha256`.
- On a clean Apple Silicon Mac, run `pkgutil --check-signature` and
  `spctl -a -vv -t install` against the downloaded `.pkg`.
- Install the `.pkg` on a clean Apple Silicon Mac and confirm `command -v sahara`
  resolves to `/usr/local/bin/sahara`.
- Confirm the graphical macOS installer opens first-run setup as the logged-in user,
  not as `root`; for automated checks, set `SAHARA_SKIP_FIRST_RUN_LAUNCH=1` and run
  `sahara-first-run` manually.
- Run `sahara --version`, non-interactive `sahara setup`, `sahara index`,
  `sahara search`, and `sahara mcp install-claude` from the installed package.
- Run `sahara first-run`, select test folders, accept indexing, and opt in to Claude
  Desktop configuration when detected.
- Upgrade over the previous native package and confirm `~/.sahara`, configured
  folders, and existing indexes remain usable.
- Uninstall with the documented native package commands and confirm `~/.sahara`
  remains present by default.
- Download the `native-windows-x64` artifact and confirm it contains a versioned zip,
  `.sha256` checksum, dependency inventory, manifest, and smoke-test log.
- Verify the Windows native archive checksum with
  `Get-FileHash .\sahara-*-windows-x64.zip -Algorithm SHA256`.
- For `v*` release tags, confirm the **Native Artifacts** workflow also runs the
  `windows-installer` job in the protected `windows-installer` environment.
- Confirm the `windows-installer` environment owns the Authenticode certificate and
  timestamping secrets; repository-wide secrets must not be required for Windows
  signing.
- Download the `native-windows-x64-installer` artifact and confirm it contains the
  versioned setup `.exe`, `.exe.sha256` checksum, and installer manifest.
- Verify the Windows installer checksum with
  `Get-FileHash .\sahara-*-windows-x64-setup.exe -Algorithm SHA256`.
- On a clean Windows x64 VM, run `Get-AuthenticodeSignature` against the downloaded
  setup `.exe` and confirm the signature is valid.
- Install the setup `.exe` quietly with
  `/VERYSILENT /NORESTART /SUPPRESSMSGBOXES` and confirm `sahara --version` resolves
  from the user `PATH` in a new terminal.
- Confirm a normal graphical Windows install offers to launch `sahara first-run`, and
  confirm quiet installs skip the first-run launch.
- Run non-interactive `sahara setup`, `sahara index`, `sahara search`,
  `sahara mcp serve --transport stdio`, and `sahara mcp install-claude` from the
  installed Windows package.
- Run `sahara first-run`, select test folders, accept indexing, and opt in to Claude
  Desktop configuration when detected.
- Upgrade over the previous Windows package and confirm `%USERPROFILE%\.sahara`,
  configured folders, and existing indexes remain usable.
- Uninstall with the documented quiet uninstall command and confirm
  `%USERPROFILE%\.sahara` remains present by default.
- Create and publish a GitHub release whose tag is exactly `v<pyproject version>`.
  The release event builds, verifies, and publishes to PyPI through OIDC. Pushing the
  `v*` release tag also runs the native artifact workflow.
- Never publish the production package manually or with a long-lived API token.

## Post-Release

- Confirm the GitHub release page links to the changelog.
- Confirm `https://pypi.org/project/sahara-memory/` shows the expected version,
  files, metadata, and Trusted Publisher attestations.
- Confirm installation instructions still work.
- Confirm roadmap and release notes describe the next active milestone.
