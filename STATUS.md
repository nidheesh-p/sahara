# Sahara Project Status

Last updated: 2026-06-03

## Current Repository State

- Active local branch: `main`
- Base branch: `main`
- Working tree: contains local hardening/status updates after PR #7 merge
- Latest merged PR: [#7 Fix full-suite file watcher reliability](https://github.com/nidheesh-p/sahara/pull/7)
- Latest `main` CI state: passed for merge commit `d6cf0a4`

## Verification

Latest local verification:

- `pytest`: 765 passed
- `ruff check .`: passed
- `mypy src`: passed
- Clean-venv install: `pip install -e ".[search,dev]"` passed
- Clean-venv CLI smoke: `sahara --version` prints `0.2.0`
- Backend validation tests: local drive, dual-write, S3, and S3-compatible client behavior passed

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

- Confirmed PR #7 merge CI passed on `main`.
- Kept CI coverage threshold at 80% by decision.
- Fixed `pathspec` deprecation warning noise.
- Ran a clean virtual-environment install with `[search,dev]` extras.
- Fixed the CLI/package version mismatch so `sahara --version` reports `0.2.0`.
- Validated local drive, dual-write, S3, and S3-compatible backend behavior through focused automated tests.
- Added `RELEASE_CHECKLIST.md`.

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

- README rewritten around local-first semantic search.
- Architecture, contributing, security, roadmap, and changelog docs exist.
- GitHub CI workflow exists.
- Issue and PR templates exist.
- Package build checks are included in CI.

## Remaining Work

### Release Readiness

1. Run live MinIO validation when Docker daemon is available.
2. Run live AWS S3 validation against a real test bucket before a public release.
3. Build and inspect release artifacts with `python -m build`.

### Phase 2: Plugin Ecosystem

1. Add plugin interfaces:
   - `FileParser`
   - `Embedder`
   - `Reranker`
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
- AI agent framework
- Web UI or desktop GUI before the CLI and plugin ecosystem are stable
