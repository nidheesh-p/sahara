# Open Source Readiness Checklist

Last reviewed: 2026-06-06

This checklist tracks contributor-facing polish and trust signals. Core implementation,
testing, packaging, and the first GitHub release are already complete.

## Required Before Wider Promotion

- [x] Add a root `CODE_OF_CONDUCT.md` using Contributor Covenant.
- [x] Replace the generic `Sahara Authors` package metadata with the maintainer's name
      and preferred public contact.
- [x] Run a dedicated history scan with `gitleaks` or `trufflehog` and document the
      result.
- [x] Review scanner findings and distinguish real secrets from test fixtures.
- [ ] Test `CONTRIBUTING.md` from a fresh clone and clean environment as though joining
      the project for the first time.
- [x] Publish `docs/CLAUDE_DESKTOP.md` with the exact stdio command, macOS and Windows
      config locations, copy-pasteable JSON, verification steps, and common fixes.
- [x] Document every MCP tool's inputs and outputs plus the read-only/indexed-corpus
      security boundary.
- [ ] Time a cold-start Claude Desktop dry run on a clean macOS or Windows
      account/machine: install, initialize, index a known document, connect Claude,
      and receive one cited answer.
- [ ] Record the cold-start OS, install method, elapsed time, friction, and result.
      Target: under five minutes end-to-end; revise the guide if it takes longer.

## README Polish

- [ ] Add badges for CI, GitHub release, MIT license, and supported Python versions.
- [ ] Add a short beta-status notice with the areas where users should expect rough
      edges.
- [ ] Add a concise "Sahara vs. rclone/restic" section. Explain that Sahara combines
      storage and sync with local semantic indexing, cited answers, and read-only MCP;
      do not imply that it replaces mature backup/versioning tools.
- [ ] Add a short known-limitations section linking to `ARCHITECTURE.md` and
      `ROADMAP.md`.
- [ ] Record a two-flow demo:
      (a) `sahara init` → `sahara index` → `sahara search` in the CLI, and
      (b) the same query in Claude Desktop with Sahara-provided citations.
      Prefer an asciinema recording plus a compact Claude Desktop screenshot.
- [ ] Add a PyPI badge only after the package is actually published to PyPI.

## Distribution Follow-Up

- [ ] Publish to TestPyPI and install the published wheel in a clean environment.
- [ ] Decide whether `v0.2.0` is ready for PyPI or whether the first PyPI release should
      be a later patch version.
- [ ] Verify package metadata on the selected package index, including author, license,
      Python versions, project links, and rendered README.

## Already Complete

- [x] Root MIT `LICENSE` file included in source and wheel distributions.
- [x] `CONTRIBUTING.md` with setup, tests, Ruff, mypy, and PR guidance.
- [x] Contributor Covenant 3.0 code of conduct with a private reporting path.
- [x] `SECURITY.md` with threat model and private vulnerability reporting.
- [x] `CHANGELOG.md` with `v0.1.0` and `v0.2.0` history.
- [x] CI on Linux and macOS for Python 3.11 and 3.12.
- [x] CI coverage requirement and PR checklist aligned at 85%.
- [x] Package build verification in CI.
- [x] Bug-report and feature-request issue templates.
- [x] Pull-request template.
- [x] Root `.gitignore` and user-facing `.saharaignore.template`.
- [x] Version aligned at `0.2.0` in package metadata and runtime output.
- [x] Beta classifier retained intentionally for the `0.2.0` maturity level.
- [x] First tagged GitHub release published with wheel, source distribution, and
      checksums.

## Audit Notes

- The earlier feedback was based on an older repository state. Most of the reported
  missing files and automation now exist.
- Current measured coverage is 89.40%; the enforced project requirement is 85%, not
  90%.
- Gitleaks `v8.30.1` scanned all 34 reachable commits on 2026-06-06 and reported zero
  leaks. The downloaded macOS arm64 scanner was verified against its published SHA-256
  checksum before use.
- Reproduce the history audit with:
  `gitleaks git . --log-opts="--all" --redact=100`.
- A separate lightweight pattern scan found the public AWS documentation example
  credential pair in a test; Gitleaks correctly did not classify it as a leak.
- Git commit author emails are public repository metadata by design. Maintainers should
  use a GitHub noreply address if they do not want a personal email exposed in future
  commits.
- Supported today: Claude Desktop using a local stdio MCP server. "Supported" means
  Sahara documents the configuration, exposes a stable read-only tool surface, and
  covers server behavior with automated tests. The clean-machine timed acceptance test
  remains pending.
- Claude mobile remote MCP is documented but still awaiting end-to-end validation.
- Claude Code and Cursor consume MCP in general, but Sahara has not completed
  client-specific validation or support documentation for either one.
- OpenClaw remains on the future roadmap.
- ChatGPT remains a future client path because MCP availability and configuration vary
  by ChatGPT mode; do not promise ChatGPT support in launch materials.
