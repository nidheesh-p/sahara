# Sahara Release Checklist

Use this checklist before publishing a Sahara release.

## Pre-Release

- Confirm `main` is up to date with `origin/main`.
- Confirm the working tree is clean.
- Confirm `pyproject.toml` version and `sahara --version` match.
- Confirm the distribution name is `sahara-memory`; the `sahara` PyPI project is
  unrelated and must never appear in public installation commands.
- Confirm installation docs state the Python 3.11 minimum.
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
- Confirm a new user reaches one cited answer in under five minutes; revise the guide
  if the flow takes longer.
- State unvalidated MCP clients as unvalidated. Do not promise ChatGPT, Claude Code,
  Cursor, or OpenClaw support until each path has been tested and documented.

## Verification

- Run `python3 -m pip install -e ".[search,dev]"` in a clean virtual environment.
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

- Build the package with `python3 -m build`.
- Inspect the generated sdist and wheel.
- Confirm the built metadata reports `Name: sahara-memory` and still provides the
  `sahara` console command.
- Publish to TestPyPI first if this is the first release in a line.
- Install `sahara-memory[search,mcp]` from the published artifact in a fresh
  environment and confirm `pip show sahara-memory` reports this repository.
- Tag the release after artifacts are verified.

## Post-Release

- Confirm the GitHub release page links to the changelog.
- Confirm installation instructions still work.
- Confirm roadmap and release notes describe the next active milestone.
