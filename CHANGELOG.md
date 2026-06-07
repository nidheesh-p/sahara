# Changelog

All notable changes to Sahara are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Sahara uses [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

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
