# Sahara Release Checklist

Use this checklist before publishing a Sahara release.

## Pre-Release

- Confirm `main` is up to date with `origin/main`.
- Confirm the working tree is clean.
- Confirm `pyproject.toml` version and `sahara --version` match.
- Review `CHANGELOG.md` and add release notes for the target version.
- Review `README.md`, `STATUS.md`, `ROADMAP.md`, and `SECURITY.md` for stale claims.

## Verification

- Run `pip install -e ".[search,dev]"` in a clean virtual environment.
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

- Validate local drive mode against a temporary folder.
- Validate S3 behavior with the moto-backed test suite.
- Validate MinIO against a local Docker MinIO instance when Docker is available.
- Validate AWS S3 against a real test bucket before publishing a public release.
- Validate `local+glacier` dual-write behavior against local storage plus an S3-compatible test target.

## Release

- Build the package with `python -m build`.
- Inspect the generated sdist and wheel.
- Publish to TestPyPI first if this is the first release in a line.
- Install from the published artifact in a fresh environment.
- Tag the release after artifacts are verified.

## Post-Release

- Confirm the GitHub release page links to the changelog.
- Confirm installation instructions still work.
- Update `STATUS.md` with release outcome and next active milestone.
