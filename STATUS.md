# Sahara Project Status

Last updated: 2026-06-03

## Current Repository State

- Active local branch: `fix/full-suite-file-watcher-reliability`
- Base branch: `main`
- Working tree: clean except for this status-page update
- Latest PR: [#7 Fix full-suite file watcher reliability](https://github.com/nidheesh-p/sahara/pull/7)
- PR state: open, mergeable
- PR CI state: in progress on GitHub at the time of this update

## Verification

Latest local verification:

- `pytest`: 765 passed
- `ruff check .`: passed
- `mypy src`: passed

Known warning noise:

- `pathspec` emits deprecation warnings for `GitWildMatchPattern` during ignore-rule-heavy tests.

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
- Created PR #7 for this work.

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

### Immediate

1. Merge PR #7 after GitHub CI completes successfully.
2. After merge, update local `main` from `origin/main`.
3. Confirm full CI matrix passes on:
   - Ubuntu Python 3.11
   - Ubuntu Python 3.12
   - macOS Python 3.11
   - macOS Python 3.12
4. Decide whether to address `pathspec` deprecation warnings now or leave them as dependency noise.

### Phase 0 / Phase 1 Hardening

1. Run a clean-machine or clean-venv install test with `pip install -e ".[search,dev]"`.
2. Manually validate the main backend flows:
   - Local drive sync
   - MinIO sync
   - AWS S3 sync
   - local+glacier dual-write
3. Decide whether CI coverage should move from the current 80% threshold to the plan's 90% target.
4. Add any missing release checklist steps before publishing a v0.2 package.

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
