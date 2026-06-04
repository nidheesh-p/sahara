# Sahara Project Status

Last updated: 2026-06-03

## Current Repository State

- Active local branch: `feat/mcp-chat-agent-integration`
- Base branch: `main`
- Working tree: contains Phase 2 MCP/chat-agent implementation updates
- Latest merged PR: [#9 Document Sahara chat and agent integration plan](https://github.com/nidheesh-p/sahara/pull/9)
- Latest `main` state: local `main` is aligned with `origin/main` at merge commit `9682140`

## Verification

Latest local verification:

- `pytest`: 780 passed
- `ruff check .`: passed
- `mypy src`: passed
- Clean-venv install: `pip install -e ".[search,dev]"` passed
- Clean-venv CLI smoke: `sahara --version` prints `0.2.0`
- Backend validation tests: local drive, dual-write, S3, and S3-compatible client behavior passed
- Live AWS S3 validation: passed using a temporary bucket in account `825502798121`; bucket was deleted after validation
- Live MinIO validation: passed using a temporary Docker container on `127.0.0.1:19000`; bucket and container were deleted after validation
- Release artifact build: passed with `python3 -m build --outdir /tmp/sahara-dist-check`
- Artifact inspection: wheel and sdist are version `0.2.0`; installed wheel imports `sahara.__version__ == "0.2.0"`
- Release rehearsal: built wheel/sdist into `/tmp/sahara-release-rehearsal-dist`, installed the wheel into `/tmp/sahara-release-rehearsal-venv`, and verified `sahara --version` plus `sahara.__version__`
- MCP implementation checks: `pytest`, focused MCP/search tests, `ruff check`, `mypy src`, `python3 -m build --outdir /tmp/sahara-mcp-build-check`, and `python3 -m pip install --dry-run '.[mcp]'` passed
- Indexing hardening checks: targeted index/search/MCP tests, `ruff check`, and `mypy src` passed after adding skip reasons and `sahara index-report`
- Branding update: README/package metadata/CLI now position Sahara as "extended storage, searchable memory and instant retrieval"
- Remote MCP security checks: `pytest tests/test_mcp_server.py -q`, `mypy src/sahara/mcp_server.py src/sahara/cli.py`, `ruff check src/sahara/mcp_server.py src/sahara/cli.py tests/test_mcp_server.py`, and `git diff --check` passed
- Remote MCP CLI smoke: `sahara mcp serve --transport http` now rejects unauthenticated HTTP/SSE transports unless `--allow-insecure-http` is explicitly set
- Release artifact verification: built wheel/sdist into `/tmp/sahara-release-pr-dist`, confirmed updated MCP/docs files are included, installed the wheel into `/tmp/sahara-release-pr-venv`, and verified `sahara.__version__ == "0.2.0"`

Current CI coverage threshold:

- Keep `--cov-fail-under=80` for now. The implementation plan's 90% target remains aspirational until the suite is ready for that stricter gate.

Warning cleanup:

- `pathspec` deprecation warnings were fixed by switching ignore-rule parsing from deprecated `gitwildmatch` to `gitignore`.

## Completed Work

### Branch Hygiene

- Fast-forwarded local `main` to `origin/main`.
- Deleted stale merged local branches:
  - `feature/minio-local-storage`
  - `fix-mv-preserve-storage-class`
  - `distribution-readiness`
- Local branch set is now clean.

### Full-Suite Reliability

- Fixed the full-suite segfault caused by macOS watchdog/FSEvents observer teardown in unit tests.
- Added observer injection to `start_watching()` so tests can use a fake observer while production keeps the real watchdog observer by default.
- Made `Debouncer.stop()` join its worker thread briefly for cleaner teardown.
- Set `asyncio_default_fixture_loop_scope = "function"` to remove the pytest-asyncio deprecation warning.
- Merged PR #7 for this work.

### Phase 0 / Phase 1 Hardening

- Confirmed PR #8 merge CI passed on `main`.
- Kept CI coverage threshold at 80% by decision.
- Fixed `pathspec` deprecation warning noise.
- Ran a clean virtual-environment install with `[search,dev]` extras.
- Fixed the CLI/package version mismatch so `sahara --version` reports `0.2.0`.
- Validated local drive, dual-write, S3, and S3-compatible backend behavior through focused automated tests.
- Validated live AWS S3 upload/download/head/list/manifest/delete behavior with a temporary bucket.
- Validated live MinIO upload/download/head/list/manifest/delete behavior with a temporary Docker container.
- Built and inspected release artifacts in `/tmp/sahara-dist-check`.
- Added `RELEASE_CHECKLIST.md`.
- Completed local release rehearsal from built artifacts.
- TestPyPI publishing was not attempted because no `twine` install or TestPyPI credentials were present in the environment.

### Phase 0: Local Testing Ready

Status: mostly complete.

- Install metadata reflects local-first storage and semantic search.
- Local drive mode no longer requires an S3 bucket.
- MinIO, local drive, S3, and local+glacier storage modes are implemented.
- Chunked indexing and sqlite-vec search are implemented.
- `sahara ask` is implemented with local Ollama/OpenAI fallback behavior.
- Broad unit coverage exists across sync, storage, search, ask, daemon, and CLI.

### Phase 1: Open Source Ready

Status: mostly complete.

- README rewritten around extended storage, searchable memory, and instant retrieval.
- Architecture, contributing, security, roadmap, and changelog docs exist.
- GitHub CI workflow exists.
- Issue and PR templates exist.
- Package build checks are included in CI.

### Phase 2: MCP / Chat and Agent Integrations

Status: mostly complete on `feat/mcp-chat-agent-integration`.

- Read-only MCP server exists for search, ask, chunk reads, folder listing, and index status.
- `sahara mcp serve` supports stdio for local clients and authenticated HTTP/streamable transport for remote clients.
- Remote HTTP/SSE transports require `--auth-token` or `SAHARA_MCP_AUTH_TOKEN` unless `--allow-insecure-http` is explicitly set.
- The CLI warns when HTTP/SSE transports bind beyond loopback.
- MCP exposure can be narrowed with `--allow-tool`, `--allow-storage-prefix`, and `--max-snippet-chars`.
- Claude Desktop docs, Claude mobile/ngrok docs, and OpenClaw guidance exist under `docs/integrations/`.

## Remaining Work

### Release Readiness

1. Optional: publish to TestPyPI and install from the published TestPyPI artifact when TestPyPI credentials are available.
2. Tag the release after the PR is merged and public artifact verification is complete.

### Phase 2: MCP / Chat and Agent Integrations

1. Confirm Claude Desktop setup end-to-end on a clean machine.
2. Confirm Claude mobile remote MCP flow end-to-end through ngrok or another HTTPS tunnel with bearer auth.
3. Track OAuth support for clients that cannot send a static bearer token.
4. Track ChatGPT connector/MCP support as an optional future client path, with explicit privacy and data-flow warnings.
5. Keep Sahara out of the autonomous-agent runtime business; clients can act, Sahara retrieves and cites.

### Future Plugin Ecosystem

1. Add plugin interfaces for parsers, embedders, and rerankers.
2. Add plugin discovery and registry using `importlib.metadata.entry_points`.
3. Move built-in parsers behind the parser plugin interface.
4. Move the built-in fastembed integration behind the embedder interface.
5. Add `sahara plugins list`.
6. Add plugin enable/disable config support.
7. Write `PLUGIN_SYSTEM.md`.
8. Add tests for plugin discovery, built-in plugin registration, and CLI plugin commands.

### v0.3 Roadmap

1. Hybrid retrieval:
   - Add sqlite FTS5/BM25 keyword index.
   - Merge BM25 and vector results with Reciprocal Rank Fusion.
2. Optional reranking:
   - Add reranker plugin support.
   - Support cross-encoder reranking after retrieval merge.
3. Entity extraction:
   - Dates
   - Names
   - Amounts
   - Document types
4. OCR plugin:
   - Tesseract integration
   - Opt-in install path
   - Scanned document tests
5. Rucksack-style backend support:
   - Backblaze B2
   - Cloudflare R2
   - Wasabi

### Future Work

1. Image search with CLIP embeddings and EXIF metadata.
2. Audio/video transcription and indexing with Whisper.
3. Plugin marketplace or curated plugin install flow.
4. Incremental re-indexing improvements.

## Non-Goals

- Cloud SaaS
- Multi-user shared storage
- General-purpose autonomous agent framework; Sahara retrieves and cites, agent clients act
- Web UI or desktop GUI before the CLI and plugin ecosystem are stable
