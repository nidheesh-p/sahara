# Changelog

All notable changes to Sahara are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Sahara uses [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Added

- Opt-in, local-only OCR for images and scanned PDFs (`ocr_enabled`, off by default;
  `ocr_backend`, default `auto`), with tesseract as the first backend via the `ocr`
  extra. Image-only PDFs fall back to OCR of their embedded page images (at most 50
  pages; images over 20 MB skipped). `sahara index` prints `OCR: N files read, M
  skipped` when OCR is enabled. Nothing is sent off the machine. (#137)
- Hybrid retrieval: search now fuses SQLite FTS5 BM25 keyword results with vector
  results using Reciprocal Rank Fusion, so exact identifiers, amounts and dates are
  found reliably. Results gain `rrf_score` and `match` fields; `score` keeps its
  vector-similarity meaning. Existing indexes migrate automatically. If FTS5 is
  unavailable, search falls back to vector-only. (#133)
- Offline retrieval evaluation harness (`sahara.eval`): fictional golden corpus, 44
  golden queries, recall@k, MRR and not-present false-positive rate, with a recorded
  baseline in `tests/eval/BASELINE.md`. On the golden set, hybrid raises recall@5
  from 0.944 to 1.000 and MRR@5 from 0.884 to 0.954. (#132)
- `sahara first-run` shows a per-folder file count and size ("Found N files (~X MB)
  to index") and points to `.saharaignore` before asking to continue. (#125)
- Search results carry a `confidence` label (high, medium, low) from term coverage,
  retriever agreement and vector score; keyword-only hits show "keyword match"
  instead of 0%, and the CLI says "No confident matches found" when nothing is
  high or medium. Measured on held-out queries: 25% of unanswerable queries got a
  confident top result, 80% of answerable ones were labelled confident. This is a
  heuristic tuned on a small set, not a guarantee. (#133 follow-up)
- Index freshness: `sahara status` and `sahara doctor` report last-indexed time and
  pending changes (new, modified, missing files) and warn when the index is stale
  with no watcher running. MCP gains `index_stale` on search results, an `index`
  block on ask, and freshness in the index status tool. (#134)
- `sahara index --changed`: a fast path that skips files whose size and modified time
  match an indexed inventory row, without reading or parsing them. New, modified,
  failed and missing files are handled as usual. Cannot be combined with `--force`.
  The stale-index hint now suggests it. (#134)

### Changed

- `sahara status` now works without sync storage configured and reports index
  freshness first; previously it exited with an error. Sync changes still appear when
  storage is configured.
- `sahara first-run` now defaults the automatic re-index prompt to Yes and states what
  declining means. Unattended `--yes` runs are unchanged. (#126)

### Fixed

- MCP `sahara_ask` no longer accepts a client-chosen answer provider; the saved
  `answer_provider` always decides, so a client can no longer cause a cloud call when
  it is `none`. (#145)

## [0.3.0] — 2026-08-09

### Added

- Signed and notarized macOS Apple Silicon native installer packaging built from the
  standalone Sahara runtime. The signed `.pkg` is attached to this release.
- Release-only native artifact workflows build one-folder bundles and installer
  packages for macOS, Windows x64, and Linux x86_64, with checksums, manifests,
  dependency inventories, and smoke logs. Only the macOS installer is signed and
  published in this release; Windows code-signing is tracked in #106 and Linux
  publishing in #48 — both build successfully today but are not yet attached to a
  release.
- `sahara first-run` onboarding for native installers, including native folder
  selection, setup/index confirmation, and opt-in Claude Desktop MCP configuration
- `sahara first-run` now asks whether to keep the index automatically current in the
  background going forward, instead of silently skipping the decision. Opting in
  starts the background index watcher and installs the platform autostart entry
  (LaunchAgent on macOS, systemd user service on Linux, registry Run key on Windows)
  so it survives reboot/login; opting out prints how to enable it later
  (`sahara daemon start --autostart`). Supports `--auto-index`/`--no-auto-index` for
  non-interactive runs.
- `sahara doctor` reports whether the background index watcher is running and whether
  autostart is installed.
- `sahara first-run` now asks whether to let Claude save things to Sahara memory when
  connecting Claude Desktop, instead of silently defaulting `sahara_remember` off with
  no indication the capability exists. Supports `--enable-memory-write`/
  `--no-enable-memory-write` for non-interactive runs; declining (or the unattended
  default) prints how to enable it later (`sahara mcp install-claude
  --enable-memory-write`).
- `sahara models prepare` for pre-downloading the local embedding model before
  indexing
- Guided `sahara setup` onboarding with setup health checks and optional automation
- iPhone mobile setup artifacts, companion-app foundation package, and mobile workflow
  planning for future app work

### Changed

- Standalone answer generation is now disabled by default on new configurations;
  semantic search, retrieval-only `sahara ask`, and MCP access no longer require
  Ollama or OpenAI
- User installation guidance now recommends `pipx` and documents a virtual-environment
  fallback for PEP 668 `externally-managed-environment` errors
- The MCP extra now requires MCP SDK 1.14.0 or newer for authenticated HTTP token
  verification, with a concise upgrade error for stale environments
- Release docs now describe native installer validation, first-run onboarding, upgrade,
  uninstall, clean-machine checks for macOS, Windows, and Linux, plus Homebrew and
  WinGet publishing templates

### Fixed

- `sahara setup --daemon` and `sahara first-run`'s new auto-index prompt no longer
  crash the background index watcher on macOS. Starting the watcher forked the
  current process; forking after the embedding model had already loaded for
  indexing tripped an Objective-C fork-safety check and crashed the forked child
  immediately, even though the CLI reported success. Both now start the watcher by
  spawning a fresh `sahara daemon start` process instead.
- `scripts/build_linux_bundle.py` and `scripts/build_windows_bundle.py` no longer
  crash with `ModuleNotFoundError: No module named 'scripts'` when run the documented
  way (`python scripts/build_<platform>_bundle.py`). Running a script by path only
  puts its own directory on `sys.path`, not the repo root, so the sibling import of
  `scripts.build_macos_bundle` could never resolve — the Linux and Windows native
  builds had never once succeeded in CI.
- The native bundle smoke test's `--with-index` path (embedding model download plus
  first index) now gets a 300s timeout instead of 180s, reducing flakiness on
  variable CI network throughput. The `native-artifacts` workflow now also uploads
  a debug artifact with the partial build/smoke output when a bundle job fails, so
  future failures are diagnosable without a local repro.
- Fresh installs of the `mcp` extra no longer break. `mcp` released a `2.0.0` that
  renamed `mcp.server.fastmcp` to `mcp.server.mcpserver`; Sahara's dependency was
  unbounded (`mcp>=1.14.0`), so any new install pulled the breaking release and every
  MCP-serving command failed. Pinned to `mcp>=1.14.0,<2.0.0` until Sahara migrates to
  the new module layout. This only affected fresh installs — existing environments
  with an older `mcp` already installed were unaffected, which is why it wasn't
  caught sooner.
- The native bundle's PyInstaller spec no longer silently drops every `mcp` hidden
  import (including the one Sahara actually needs, `mcp.server.fastmcp`) when
  collecting them. `collect_submodules("mcp")` tries to import each submodule to
  verify it, `mcp.cli.cli` hard-imports the unrelated `typer` package to support
  `mcp`'s own CLI tooling (which Sahara never uses), that import fails in a
  minimal build environment, and a broad `except Exception` swallowed the whole
  collection. Now filters out `mcp.cli.*` before collecting.
- The CLI no longer crashes on Windows consoles using a legacy codepage (e.g.
  cp1252). Unicode checkmarks and box-drawing characters in `_ok()`/`_section()`
  raised `UnicodeEncodeError` on any command that prints them; output now degrades
  to `?` placeholders instead of crashing the command.
- Mobile API request handling now uses request-scoped database sessions and more robust
  input validation
- Saved config list values now escape backslashes and quotes correctly in TOML
- Egress free-tier calculations now match the documented free tier
- Sync handling now tolerates portable path aliases and legacy non-portable entries
- Python 3.12 mypy coverage works with current NumPy stubs

## [0.2.1] — 2026-06-07

### Added

- Trusted Publishing workflows for verified TestPyPI and PyPI releases
- Contributor Covenant 3.0 code of conduct with private incident reporting
- Claude Desktop launch guide with platform configuration, exact MCP tool contracts,
  security boundaries, verification, and troubleshooting
- Three-step product plan for basic indexing with optional local-drive or AWS storage
- Basic index-only mode with non-interactive `sahara init --mode basic --folder <path>`
- Canonical content-root and index-inventory database tables
- `sahara folder add/list/remove/sync` commands for index and sync scope management
- `sahara storage configure local/aws` for upgrading an existing basic library
- Checksum-verified `sahara offload` and `sahara fetch` with retained search metadata
- Explicit storage residency in CLI search/list/status and MCP results
- Local indexing that scans content roots without requiring sync records or storage
- `sahara mcp install-claude` for merge-safe, one-command Claude Desktop setup on
  macOS and Windows

### Changed

- Restored full mypy checking for the daemon and filesystem watcher
- Renamed the Python distribution from `sahara` to `sahara-memory` to avoid the
  unrelated OpenStack Sahara project on PyPI; the product name, `sahara` CLI,
  and `sahara` import package are unchanged
- First-time indexing now explains the local embedding-model download and clarifies
  that Hugging Face authentication warnings do not require user action
- Package and license metadata now identify Nidheesh Puthalath as the maintainer
- README quick start now demonstrates both CLI retrieval and cited Claude Desktop use
- Added fictional, privacy-safe README, social, and reproducible terminal demo assets
- Ollama is the initial answer provider; OpenAI can be selected explicitly or saved
  as the user's default without installing Ollama
- Added first-run Ollama and optional OpenAI setup guidance
- Streamlined the README around local search first, with answers, MCP, and storage
  introduced as optional extensions
- Added a categorized reference covering every CLI command
- Documentation consolidated around current user, contributor, release, and architecture
  guidance; superseded specifications remain available through Git history
- Fresh installations default to local indexing; legacy configs without `storage_mode`
  retain their previous S3 behavior
- `index-report` now reads the local index inventory rather than the sync file table

---

## [0.2.0] — 2026-06-06

### Added

- **Semantic search** — `sahara index` extracts and embeds file content; `sahara search <query>` retrieves files by meaning using sqlite-vec KNN
- **Chunked indexing** — long documents (PDFs, DOCX) are split into overlapping 400-token chunks so content past the first page is retrievable
- **`sahara ask`** — natural language question answering; uses local Ollama or OpenAI when available, degrades gracefully to ranked snippets
- **MinIO backend** — S3-compatible self-hosted storage via `endpoint_url` configuration
- **Local drive backend** — sync to a second local drive or NAS with no cloud account required
- **`local+glacier` dual-write mode** — writes to a local drive and S3 Glacier simultaneously
- **`StorageBackend` Protocol** — formal structural interface for all storage backends; `SyncEngine` no longer imports concrete backend classes
- **`BAAI/bge-small-en-v1.5` embedding model** — 384-dim vectors via `fastembed`; fast enough for CPU-only indexing
- **PDF and DOCX extraction** — `pypdf` and `python-docx` are optional dependencies under `[search]`
- **`sahara doctor --repair`** — diagnose and auto-fix common configuration problems
- **SHA-256 utility** — shared `utils/hash.py` used by both sync and search (previously duplicated)
- **Read-only MCP server** — exposes search, ask, chunk reads, folder listing, and index status to Claude Desktop and other MCP clients
- **Authenticated remote MCP** — HTTP/streamable transport with bearer-token protection for secure tunnel and remote-client workflows
- **MCP exposure controls** — tool and storage-prefix allowlists, snippet-size limits, and warnings for non-loopback bindings
- **`sahara index-report`** — reports indexed/unindexed file counts, skip reasons, and sample indexing gaps
- **MIT license file** — included in the repository, wheel metadata, and source distribution

### Changed

- Public positioning updated to "Sahara: extended storage, searchable memory and instant retrieval"
- `_require_config` guard: local drive mode no longer requires a bucket to be configured
- Storage modules reorganised into `src/sahara/storage/`, sync modules into `src/sahara/sync/`
- Indexing skips unsupported binary media instead of attempting noisy text extraction

### Fixed

- Manifest locking race condition under concurrent syncs
- False abort in local drive mode due to missing bucket check
- Optional MCP dependency tests now skip cleanly when the `[mcp]` extra is not installed

---

## [0.1.0] — 2024-03-16

### Added

- **Bidirectional sync** to AWS S3 with three-way diff (local / remote / last-known-good)
- **Client-side AES-256-GCM encryption** with PBKDF2-HMAC-SHA256 key derivation (600,000 iterations)
- **Glacier archiving** — `sahara archive`, `sahara restore`, `sahara restore-download`
- **Background daemon** with file-watching via watchdog
- **Rename detection** — moves are tracked as copy + delete rather than delete + upload
- **Conflict resolution** — backup, local, remote, and ask strategies
- **Cost reporting** — `sahara usage` shows storage usage and estimated monthly S3 cost
- **`.saharaignore`** — gitignore-style rules for excluding files from sync
- **Multipart uploads** — automatic for files above a configurable threshold
- **`sahara doctor`** — connectivity and configuration diagnostics
- `sahara init` interactive setup wizard
- `sahara config show/get/set` configuration management
- `sahara history` sync operation log
- `sahara conflicts` and `sahara resolve`
