# Sahara — Architecture

This document explains how Sahara is structured so contributors can find their way around quickly and extend the system without touching unrelated code.

---

## System overview

```
┌─────────────┐   ┌────────────────┐   ┌──────────────────────┐
│ CLI / MCP   │──▶│ IndexingService│──▶│ SearchEngine         │
│ Mobile API  │   │ library.py     │   │ fastembed + vec      │
└─────────────┘   └────────────────┘   └──────────────────────┘
       │                   │                      │
       │          ┌────────┴────────┐             ▼
       │          ▼                 ▼         AskEngine
       │    MemoryService    LocalIndexWatcher  none / Ollama / OpenAI
       │    memory/          index_watcher.py
       │          │                 │
       │          ▼                 ▼
       │    ┌──────────┐      content roots + inbox
       │    │ StateDB  │
       │    └──────────┘
       │          ▲
       ▼          │
┌─────────────┐   ┌──────────────────────┐
│ SyncEngine  │──▶│ Optional StorageBackend│
└─────────────┘   │ S3 / LocalDrive      │
                  └──────────────────────┘
```

The Click CLI, read-only MCP server (with optional stdio-only memory capture), and
private mobile capture API are Sahara's public surfaces. They compose the same
indexing, retrieval, memory, state, and optional storage services.

---

## Source layout

```
src/sahara/
├── cli.py                  # All Click commands — the public API
├── mcp_server.py           # MCP tools: search, ask, recall, optional remember
├── mobile_api.py           # Authenticated mobile capture and recall HTTP API
├── index_watcher.py        # Always-local incremental indexing and memory inbox
├── shortcuts.py            # iOS Shortcut artifact export helpers
├── config.py               # SaharaConfig dataclass + TOML I/O
├── library.py              # Content-root registration and IndexingService
├── models.py               # FileRecord, SyncOperation, ManifestEntry, ...
│
├── memory/
│   ├── format.py           # Markdown frontmatter parse/render for captured knowledge
│   └── service.py          # MemoryService — capture, recall, lifecycle
│
├── storage/
│   ├── backend.py          # StorageBackend Protocol (the interface)
│   ├── s3_client.py        # AWS S3 + MinIO implementation
│   ├── local_drive_client.py  # Local filesystem implementation
│   ├── dual_write_backend.py  # local+glacier dual-write wrapper
│   ├── state_db.py         # SQLite state — files, chunks, memory, mobile, ...
│   ├── lifecycle.py        # Offload/fetch with checksum verification
│   └── cost_estimator.py   # S3 pricing estimates
│
├── sync/
│   ├── sync_engine.py      # Three-way diff, conflict resolution, execution
│   ├── daemon.py           # Index watcher + optional sync loop
│   ├── file_watcher.py     # watchdog integration
│   └── ignore_rules.py     # .saharaignore parser
│
├── search/
│   ├── search_engine.py    # Text extraction, chunking, embedding, sqlite-vec KNN
│   └── ask_engine.py       # Retrieval-only ask + optional Ollama/OpenAI answers
│
├── data/
│   ├── saharaignore.template
│   └── shortcuts/          # Versioned iOS Shortcut JSON blueprints
│
└── utils/
    ├── encryption.py       # AES-256-GCM, PBKDF2, keyring
    ├── hash.py             # SHA-256 helpers (shared between sync and search)
    └── notifier.py         # OS desktop notification
```

---

## Onboarding

`sahara setup` orchestrates existing commands rather than duplicating business logic:

1. Create or reuse basic/index-only configuration via `init`
2. Register additional content roots via `folder add`
3. Optionally run `models prepare` and `index`
4. Optionally run `mcp install-claude` when Claude Desktop is detected

The flow is idempotent: existing configuration, content roots, indexes, and MCP
settings are preserved.

---

## Captured knowledge

Captured memories are portable Markdown files under a managed content root (default
`~/Sahara Memory/`). Markdown is the source of truth; SQLite caches metadata for
listing and filtering.

```
1. MemoryService.capture(CaptureRequest):
   a. Validate content, tags, URLs, and size limits
   b. Ensure managed memory root exists without overlapping other content roots
   c. Atomically write versioned Markdown with YAML frontmatter
   d. Update rebuildable memory_items cache
   e. Index only the new path via IndexingService.index_path()

2. sahara recall / sahara_recall MCP tool:
   a. Scope retrieval to the managed memory storage prefix
   b. Apply metadata filters (tags, source_type, date range)
   c. Return ranked chunks from the shared chunks/vec_chunks tables

3. Opt-in sahara_remember MCP tool (stdio only):
   a. Create-only; requires explicit installer opt-in
   b. Size limits, idempotency key, metadata-only audit events
```

All capture adapters (CLI, inbox, mobile API, MCP) call `MemoryService.capture()`;
adapters do not implement their own storage or indexing behavior.

See [specs/CAPTURED_KNOWLEDGE_PLAN.md](specs/CAPTURED_KNOWLEDGE_PLAN.md) for the full
design, security boundaries, and mobile/Siri rollout.

---

## Local index watcher

`index_watcher.py` provides always-local filesystem watching independent of storage
backends. The daemon starts `LocalIndexWatcherService` for every registered content
root and optionally adds sync handlers when a storage backend is configured.

```
1. watchdog debounces create/modify/delete/move events
2. LocalIndexWatcherService.handle_path():
   a. Memory inbox files → MemoryService.capture(), then delete inbox file
   b. Supported files under content roots → IndexingService.index_path()
   c. Deletions → remove chunks/embeddings and mark index_entries missing
```

This replaces the previous model where only sync-enabled folders were watched for
changes. Basic index-only users get incremental indexing when the daemon runs.

---

## Mobile capture API

`mobile_api.py` exposes a minimal authenticated HTTP API for trusted mobile devices.
It is not MCP; Shortcuts and future companion apps POST JSON to loopback (or a private
network address such as Tailscale).

- Device tokens stored as hashes with scoped permissions (`memory:capture`, `memory:recall`)
- Rate limiting, idempotency, and audit logging
- All writes routed through `MemoryService.capture()`

See [docs/mobile-capture-api.md](docs/mobile-capture-api.md) and
[docs/ios-shortcuts.md](docs/ios-shortcuts.md).

---

## Storage backends

### The `StorageBackend` Protocol

`src/sahara/storage/backend.py` defines the `StorageBackend` Protocol. Every backend must implement these methods:

| Method | Purpose |
|--------|---------|
| `upload_file` | Upload a local file, optionally encrypting it first |
| `download_file` | Download a key to a local path, optionally decrypting |
| `delete_object` | Delete a key |
| `copy_object` | Copy within the same backend (rename path) |
| `get_manifest` / `put_manifest` | Fetch / write the Sahara manifest atomically |
| `list_all_objects` | Bootstrap when no manifest exists yet |
| `head_object` | Return metadata (size, etag, storage class) |
| `validate_bucket_access` | Connectivity check |
| `check_conditional_put_support` | Whether atomic manifest writes are supported |
| `restore_object` | Glacier restore (S3 only; raise if unsupported) |

`SyncEngine` accepts any `StorageBackend` — it never imports a concrete backend class directly.

### Adding a new backend

1. Create `src/sahara/storage/mybackend_client.py`
2. Implement all methods in the `StorageBackend` Protocol (use `LocalDriveClient` as the simplest reference)
3. Add an `isinstance` check in `cli.py` where the backend is instantiated (search for `storage_mode`)
4. Add tests in `tests/test_mybackend.py` — mock the external service, do not require real network access

### Current backends

| Class | Module | Description |
|-------|--------|-------------|
| `S3Client` | `storage/s3_client.py` | AWS S3 and MinIO (via `endpoint_url`) |
| `LocalDriveClient` | `storage/local_drive_client.py` | Local filesystem or network mount |
| `DualWriteBackend` | `storage/dual_write_backend.py` | Writes to two backends simultaneously (local + glacier) |

---

## Sync pipeline

The sync pipeline lives in `sync/sync_engine.py`. The sequence for a full sync:

```
1. Load manifest from storage (single JSON object — avoids per-file HeadObject calls)
2. Scan local folder → build local snapshot {path → sha256}
3. Load last-known-good state from StateDB
4. Three-way diff(local, remote_manifest, last_known_good):
     - New local file  → upload
     - Deleted locally → delete from remote (or skip if remote was also changed = conflict)
     - New remote file → download
     - Deleted remotely → delete locally
     - Both modified   → conflict
5. For each operation: execute in thread pool (max_workers parallel)
6. Write updated manifest back to storage (atomic via If-Match ETag check)
7. Update StateDB with new sync state
```

### Why the manifest?

Without the manifest, every sync would need to call `HeadObject` on every file in the bucket to check its current state — at $0.0004 per 1,000 calls and 50k files, that is $0.02 per sync, $7/month. The manifest is a single JSON blob stored at `.sahara/manifest.json` in the bucket. One `GetObject` replaces thousands of `HeadObject` calls.

### Conflict resolution

Conflict strategy is set in config (`backup` / `local` / `remote` / `ask`). The `backup` strategy (default) renames the local copy to `filename.conflict-TIMESTAMP.ext` and downloads the remote version — no data loss.

---

## Search pipeline

The search pipeline runs entirely locally. `library.py` scans every registered content
root directly; it does not depend on sync records or a storage backend.

```
1. IndexingService.index():
   a. Load content roots from StateDB
   b. Walk each root with .saharaignore rules
   c. Maintain index_entries inventory and detect missing files
   d. Call SearchEngine for supported local files

2. SearchEngine.index_file(path):
   a. Extract text (TextExtractor) — PDF, DOCX, EPUB, plain text, code, and heuristics
   b. Chunk text: 1600-char chunks with 320-char overlap
   c. Embed each chunk with BAAI/bge-small-en-v1.5 (384-dim) via fastembed
   d. Insert rows into `chunks` and `vec_chunks` (sqlite-vec)

3. search(query):
   a. Embed the query string
   b. KNN query against vec_chunks (O(log n) ANN, not a Python cosine loop)
   c. Join against `chunks` to get file paths and snippet text
   d. Deduplicate: keep best chunk score per file
   e. Return ranked list of {relative_path, score, snippet}
```

Use `load_embedding_model()` or `sahara models prepare` to download the embedding model
before the first index run.

### Why chunked indexing?

A 50-page PDF has ~25,000 words. Embedding the whole document as one vector would mean the embedding averages over all content, making any specific detail on page 30 nearly unretrievable. By splitting into 400-token chunks with 80-token overlap, each chunk can be matched independently, so a query about page 30 will find the right chunk.

### Adding a new file parser

`TextExtractor.extract()` in `search/search_engine.py` dispatches on file extension. Add a new `elif suffix == ".xyz"` branch there. For heavier parsers (OCR, audio transcription) consider wrapping the import in a `try/except ImportError` so the base install does not require the dependency.

---

## Ask pipeline

`search/ask_engine.py` wraps `SearchEngine` with optional answer generation.

```
1. Run search(question, top_k)
2. If answer_provider is "none", return ranked sources with no generated answer
3. If Ollama or OpenAI is explicitly selected:
   a. Build context from the top chunks (capped at 6,000 chars)
   b. Call only the selected provider
   c. If the provider fails, return sources with a degraded result and error
4. Return AskResult(answer, sources, degraded, model_used, provider_used, error)
```

Retrieval-only mode is the default and is not an error. It makes `sahara ask` useful
without a standalone LLM and lets MCP clients reason over the cited snippets with their
own model. A configured provider failure is reported as degraded while preserving the
retrieved sources.

---

## Daemon and file watcher

`sync/daemon.py` runs two complementary watchers:

1. **Local index watcher** — `LocalIndexWatcherService` on all content roots (always)
2. **Storage sync watcher** — `SaharaEventHandler` on sync-enabled roots (optional)

A periodic poll loop handles scheduled syncs and Glacier restore polling when storage
is configured. The daemon writes a PID file to `~/.sahara/daemon.pid` and logs to
`~/.sahara/daemon.log`.

---

## SQLite schema

All state is stored in `~/.sahara/state.db`. WAL mode is enabled on every connection for safe concurrent reads.

| Table | Purpose |
|-------|---------|
| `files` | One row per synced file — sha256, size, tier, timestamps, is_deleted |
| `history` | Append-only log of every sync operation |
| `pending_multipart` | In-flight multipart upload state (crash recovery) |
| `sync_targets` | Registered (local_path, s3_prefix) pairs |
| `content_roots` | Canonical indexed folders with primary and sync-enabled flags |
| `index_entries` | Local indexing inventory and indexed/unsupported/missing status |
| `storage_residency` | Explicit present/offloaded/missing state for stored files |
| `config_kv` | Key-value store for runtime config values |
| `embeddings` | Legacy single-vector-per-file index (superseded by `chunks`) |
| `chunks` | One row per text chunk — path, chunk_index, content_hash, chunk_text |
| `vec_chunks` | sqlite-vec virtual table — one float[384] vector per chunk (rowid matches `chunks.id`) |
| `memory_items` | Rebuildable cache of captured Markdown memory metadata |
| `memory_delete_journal` | Tombstone journal for deleted memories during rebuild |
| `mcp_memory_audit` | Metadata-only audit log for opt-in MCP memory capture |
| `mobile_devices` | Hashed device tokens and scopes for mobile capture API |
| `mobile_memory_audit` | Audit log for mobile capture and recall requests |

The `chunks` and `vec_chunks` tables work as a pair. `vec_chunks` stores the raw vectors; `chunks` stores the text and metadata. A JOIN on `rowid = id` links them.

### Offload lifecycle

`StorageLifecycle.offload()` requires a synced, indexed file. It downloads the stored
object to temporary storage, decrypts it when needed, verifies the plaintext SHA-256,
marks the file offloaded, and then removes the local source. Chunks and embeddings are
retained. `fetch()` downloads atomically, verifies the same checksum, and marks the file
present again. Sync ignores intentional offloads so they cannot be mistaken for local
deletions.

---

## Configuration

Config lives at `~/.sahara/config.toml`. `storage_mode = "none"` is the fresh-install
default. Existing configuration files that predate `storage_mode` are loaded as S3
configurations for compatibility. The CLI reads configuration at startup and passes a
snapshot down to each subsystem.

The TOML format is stable and user-editable. Do not add auto-generated comments or machine-managed sections to the config file.

---

## Known limitations

- **No reranker yet.** Results from sqlite-vec KNN are re-sorted by score but not re-ranked by a cross-encoder. Precision is good but not state-of-the-art for ambiguous queries.
- **No hybrid keyword search yet.** Pure vector retrieval can miss exact identifiers and rare terms.
- **Single embedding model.** Only `BAAI/bge-small-en-v1.5` (384-dim) is supported. Switching models requires re-indexing all files.
- **Full index scan on manual `sahara index`.** Content-hash tracking skips unchanged files, but discovery is O(n). The daemon watcher indexes incrementally between runs.
- **Single-user only.** The manifest + SQLite architecture assumes one writer at a time. Multiple machines syncing to the same bucket will serialize through the manifest ETag check.
- **No OCR.** Scanned PDFs and images are not searchable yet.

---

## Where to start

| Contribution area | Start here |
|-------------------|-----------|
| New storage backend | `storage/backend.py` (Protocol) → `storage/local_drive_client.py` (simplest impl) |
| New file parser | `search/search_engine.py` `TextExtractor.extract()` |
| Improve search ranking | `search/search_engine.py` `SearchEngine.search()` |
| Captured knowledge | `memory/service.py`, `memory/format.py` |
| Mobile capture | `mobile_api.py`, `docs/mobile-capture-api.md` |
| Index watcher | `index_watcher.py`, `sync/daemon.py` |
| New CLI command | `cli.py` — add a `@main.command()` function |
| Sync bug | `sync/sync_engine.py` `DiffResult` and `_execute_operations()` |
| Encryption | `utils/encryption.py` |
