# Sahara Three-Step Product Model Plan

## Goal

Make Sahara useful in three progressively richer configurations:

1. **Basic: local indexing only**
   - No external drive, AWS account, bucket, or sync setup required.
   - Index one or more folders on the computer.
   - Search, ask, and MCP work against the local index.
2. **Local drive: index plus extended local storage**
   - Add one or more mounted drives, NAS paths, or network shares.
   - Sync selected indexed folders to the configured drives.
   - Explicitly offload local files and fetch them later.
3. **AWS: index plus cloud storage**
   - Add an AWS S3 backend instead of local drives.
   - Sync selected indexed folders to S3.
   - Explicitly offload local files and fetch them later.

Steps 2 and 3 are optional alternatives. A user can remain on Step 1 indefinitely or
upgrade an existing Step 1 library without rebuilding its semantic index.

## Product Principles

- Indexing is the baseline product capability; storage is optional.
- Folders Sahara indexes must be modeled separately from folders Sahara syncs.
- Adding storage must not silently upload every indexed folder.
- An indexed folder can remain index-only even when a storage backend is configured.
- "Extended storage" requires an explicit offload/fetch lifecycle. A backup copy alone
  does not free space on the local computer.
- Accidental deletion and intentional offload must be represented as different states.
- Existing configurations and indexes must continue to work after migration.
- Local-drive and AWS setup should share one storage abstraction and user vocabulary.

## User Experience

### Step 1: Basic Indexing

Interactive:

```bash
sahara init
```

The default path should be:

```text
Primary folder [~/Sahara]:
Setup:
  basic  - index locally, no storage destination (default)
  local  - index and use a local/external drive
  aws    - index and use AWS S3
Choice [basic]:
```

Non-interactive:

```bash
sahara init --mode basic --folder ~/Documents
sahara index
sahara search "passport expiry" --snippet
```

Expected behavior:

- Create the primary content root.
- Create local Sahara configuration and SQLite state.
- Require no bucket, credentials, drive path, or storage validation.
- Scan and index files directly from the content root.
- Enable search, ask, index-report, and MCP.
- Reject storage commands with a clear upgrade message.

### Step 2: Local Drive Storage

New setup:

```bash
sahara init \
  --mode local \
  --folder ~/Sahara \
  --storage-drive /Volumes/Archive/Sahara
```

Upgrade an existing basic library:

```bash
sahara storage configure local \
  --drive /Volumes/Archive/Sahara
sahara folder sync ~/Sahara --enable
sahara sync
```

Expected behavior:

- Indexing continues to read from content roots, never from storage-drive copies.
- Only roots explicitly marked for sync are copied.
- New or modified source files are copied to every configured drive.
- Local-drive deletion propagation remains disabled by default.
- `sahara offload <path>` verifies a durable storage copy, retains index metadata, and
  removes the local source file.
- `sahara fetch <path>` restores an offloaded file from storage.
- Search results identify offloaded files and remain useful before fetching.

### Step 3: AWS Storage

New setup:

```bash
sahara init \
  --mode aws \
  --folder ~/Sahara \
  --bucket my-sahara-bucket \
  --region us-east-1
```

Upgrade an existing basic library:

```bash
sahara storage configure aws \
  --bucket my-sahara-bucket \
  --region us-east-1
sahara folder sync ~/Sahara --enable
sahara sync
```

Expected behavior:

- Use the same content-root and sync-selection model as local drives.
- Preserve current AWS profile, environment, and explicit-key credential options.
- Support explicit offload/fetch after remote-copy verification.
- Keep Glacier/archive operations separate from normal offload/fetch semantics.

## Architecture Changes

### 1. Separate Content Roots from Sync Targets

Replace the assumption that every registered folder is a sync target.

Introduce a `content_roots` table:

```sql
CREATE TABLE content_roots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    local_path      TEXT NOT NULL UNIQUE,
    storage_prefix  TEXT NOT NULL UNIQUE,
    is_primary      INTEGER NOT NULL DEFAULT 0,
    sync_enabled    INTEGER NOT NULL DEFAULT 0,
    added_at        TEXT NOT NULL
);
```

All content roots are indexed. `sync_enabled` controls whether the root participates
in sync when a storage backend exists.

Add one shared helper that returns typed content roots. Index, search filters, folder
listing, MCP folder listing, daemon setup, and sync must use this helper instead of
reading `sync_targets` independently.

### 2. Make Storage Optional

Add a no-storage mode:

```toml
storage_mode = "none"  # none | local | s3 | minio | local+glacier
```

Add configuration helpers:

```python
config.has_storage_backend
config.is_index_only_mode
```

Split validation:

- `_require_library_config`: primary/content root exists.
- `_require_storage_config`: a storage backend is configured and valid.

Commands requiring only the library:

- `index`
- `index-report`
- `search`
- `ask`
- `mcp serve`
- content-root add/remove/list

Commands requiring storage:

- `sync`, `push`, `pull`, storage status/diff
- remote remove/move
- archive/restore
- offload/fetch

### 3. Decouple Index Inventory from Sync State

The current `files` table represents sync state and must not be populated with fake
remote records in basic mode.

Introduce an index inventory table:

```sql
CREATE TABLE index_entries (
    storage_prefix  TEXT NOT NULL DEFAULT '',
    relative_path   TEXT NOT NULL,
    content_hash    TEXT,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    modified_ns     INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    reason          TEXT,
    last_seen_at    TEXT NOT NULL,
    indexed_at      TEXT,
    PRIMARY KEY (storage_prefix, relative_path)
);
```

Responsibilities:

- Record every discovered candidate file independently of sync.
- Record `indexed`, `unsupported`, `no_text`, `failed`, and `offloaded` states.
- Drive `index-report` without consulting the sync `files` table.
- Identify stale entries when files disappear.
- Remove chunks/embeddings for true deletions.
- Retain chunks/embeddings for intentional offloads.

Create an `IndexingService` that:

1. Walks every content root using Sahara ignore rules.
2. Updates inventory and indexes changed supported files.
3. Marks unseen files as missing or deleted.
4. Removes stale vectors only when the file was deleted rather than offloaded.

### 4. Model Intentional Offload

Add an explicit storage lifecycle state rather than inferring offload from a missing
local file:

```sql
CREATE TABLE storage_residency (
    storage_prefix  TEXT NOT NULL DEFAULT '',
    relative_path   TEXT NOT NULL,
    local_state     TEXT NOT NULL,  -- present | offloaded | missing
    remote_state    TEXT NOT NULL,  -- present | missing | unknown
    offloaded_at    TEXT,
    fetched_at      TEXT,
    PRIMARY KEY (storage_prefix, relative_path)
);
```

`sahara offload` must:

1. Require a configured storage backend.
2. Require the root to be sync-enabled.
3. Verify the stored object's checksum.
4. Ensure searchable chunks already exist.
5. Mark the item offloaded transactionally.
6. Remove the local source only after all prior steps succeed.

`sahara fetch` must:

1. Locate the configured backend object.
2. Restore/decrypt it to the original content root.
3. Verify the checksum.
4. Mark it present locally.

An ordinary filesystem deletion must be reported as `missing`, not silently treated as
an offload. Local-drive storage copies remain retained by default.

### 5. Folder Commands

Introduce a canonical command group:

```bash
sahara folder add ~/Documents                 # index only
sahara folder add ~/Projects --sync           # index and sync
sahara folder list
sahara folder sync ~/Documents --enable
sahara folder sync ~/Documents --disable
sahara folder remove ~/Documents
```

Compatibility:

- Keep `sahara add`, `sahara remove`, and `sahara folders` during a deprecation window.
- Existing `sahara add` behavior remains sync-enabled when storage exists.
- New documentation uses the `sahara folder` group.

### 6. Storage Commands

Introduce:

```bash
sahara storage status
sahara storage configure local --drive <path>
sahara storage configure aws --bucket <name> --region <region>
sahara storage disable
```

Rules:

- Only one primary storage backend is active at a time.
- Switching or disabling storage never deletes existing stored data.
- Configuring storage does not automatically sync index-only roots.
- MinIO and `local+glacier` remain supported as advanced existing modes but are not
  part of the primary three-choice onboarding screen.

### 7. Daemon Behavior

Define two daemon responsibilities:

- Index watcher: available in all modes.
- Sync worker: enabled only when storage exists and at least one root is sync-enabled.

The first implementation may keep manual `sahara index` for basic mode, but daemon
status and docs must not imply that storage is required for indexing.

## Migration and Compatibility

Database migration:

1. Create `content_roots`, `index_entries`, and `storage_residency`.
2. Insert the configured primary `sync_folder` as the primary content root.
3. Migrate every `sync_targets` row to `content_roots` with `sync_enabled = 1`.
4. Keep existing `storage_prefix` values so current chunks and embeddings remain valid.
5. Backfill `index_entries` from existing embeddings/chunks where possible.
6. Leave `sync_targets` readable for one release, then remove it in a later migration.

Configuration migration:

- Existing `local`, `s3`, `minio`, and `local+glacier` configs retain their behavior.
- Missing `storage_mode` keeps the current compatibility default during migration.
- Fresh installs default to `storage_mode = "none"`.
- Continue reading `sync_folder`; consider renaming it to `primary_folder` only in a
  future breaking release.

CLI compatibility:

- Preserve current commands as wrappers.
- Add deprecation messages only after replacement commands are stable.
- Never reinterpret a previously synced folder as index-only during migration.

## Implementation Sequence

### Milestone A: Index-Only Foundation

Status: implemented on June 6, 2026.

- [x] Add `storage_mode = "none"`.
- [x] Split library and storage validation.
- [x] Add content-root and index-inventory migrations.
- [x] Refactor `sahara index` to scan content roots directly.
- [x] Refactor `index-report` to use index inventory.
- [x] Make search, ask, and MCP work with no backend.
- [x] Add basic interactive and non-interactive initialization.
- [x] Add canonical index-only folder registration.

Exit criterion:

```bash
sahara init --mode basic --folder ~/Documents
sahara index
sahara search "known phrase"
sahara mcp serve
```

works without a bucket or drive.

### Milestone B: Optional Storage Upgrade

- [x] Add `sahara storage configure`.
- [x] Add per-root sync selection.
- [x] Refactor sync to iterate only sync-enabled roots.
- [x] Add canonical folder commands and compatibility wrappers.
- [x] Preserve local, AWS, MinIO, and dual-write backend behavior.

Exit criterion: an existing basic library can add local-drive or AWS storage and sync
one selected root without rebuilding the index or uploading unrelated roots.

### Milestone C: True Extended Storage

Status: implemented on June 6, 2026.

- [x] Add residency state.
- [x] Add `sahara offload` and `sahara fetch`.
- [x] Keep indexed chunks after offload.
- [x] Show local/offloaded state in search, list, status, and MCP results.
- [x] Add checksum and failure-recovery guarantees.

Exit criterion: a user can safely free local disk space, still discover the file by
semantic search, and restore it from local-drive or S3 storage.

### Milestone D: Onboarding and Release Hardening

- [ ] Time all three clean-install paths.
- [x] Exercise migration from a current `v0.2.0` config and database in automated tests.
- [x] Validate local-drive behavior with temporary-drive tests.
- [ ] Validate live AWS behavior with a temporary bucket.
- [x] Update package and release documentation.
- [ ] Publish a prerelease for external testing.

## Test Plan

### Configuration and Migration

- Fresh basic config requires no storage fields.
- Existing configs retain their current backend.
- Existing primary and additional sync folders migrate as sync-enabled content roots.
- Existing embeddings remain searchable after migration.
- Migration is idempotent.

### Indexing

- Basic mode indexes a folder before any sync.
- Multiple index-only roots produce isolated prefixes.
- Added, modified, renamed, deleted, unsupported, and empty files update inventory.
- Intentional offload retains chunks; ordinary deletion removes stale chunks.
- `index-report` works with no sync records.

### Local Drive

- Selected roots sync; index-only roots do not.
- Files are written to all configured drives.
- Local deletion does not remove drive copies by default.
- Offload verifies storage before deleting local content.
- Fetch restores and checksum-verifies content.
- Missing/unmounted drives fail without local data loss.

### AWS

- Selected roots sync through moto tests.
- Credential modes and bucket validation remain covered.
- Offload/fetch round trips and checksum failures are tested.
- Archive/Glacier behavior remains separate.

### CLI and MCP

- Interactive setup covers basic, local, and AWS paths.
- Non-interactive flags reject incomplete combinations.
- Storage commands explain how to upgrade from basic mode.
- MCP search/ask/index-status work in basic mode.
- Folder-list output identifies index-only versus sync-enabled roots.
- MCP remains read-only and cannot offload or fetch files.

### End-to-End Acceptance

1. Basic: clean install to cited Claude Desktop answer in under five minutes.
2. Local: clean install, sync, offload, search, and fetch using a temporary drive.
3. AWS: clean install, sync, offload, search, and fetch using a temporary bucket.
4. Upgrade: current `v0.2.0` local and AWS users retain all indexed and synced data.

## Documentation Updates

Update these files as each milestone lands:

- `README.md`
  - Present Basic as the default quick start.
  - Show separate Basic, Local Drive, and AWS paths.
  - Explain that storage is optional.
  - Explain index-only versus sync-enabled folders.
  - Demonstrate offload/search/fetch before claiming extended storage.
- `docs/GETTING_STARTED.md` (new)
  - Three setup paths and non-interactive examples.
- `docs/STORAGE_MODES.md` (new)
  - No storage, local drive, AWS, MinIO, and local+glacier behavior.
  - Deletion, offload, fetch, encryption, and failure semantics.
- `docs/CLAUDE_DESKTOP.md`
  - Use Basic mode for the shortest onboarding flow.
  - Remove any implication that sync must happen before indexing.
- `docs/integrations/chat-agents.md`
  - State that MCP works in index-only mode.
- `ARCHITECTURE.md`
  - Content roots, index inventory, optional storage, residency state, and migrations.
- `SECURITY.md`
  - Data-flow differences for Basic, Local Drive, and AWS.
  - Offload verification and OpenAI snippet disclosure.
- `CONTRIBUTING.md`
  - New migrations, indexing service, storage guards, and test commands.
- `ROADMAP.md`
  - Make the three-step product model the next product milestone.
- `CHANGELOG.md`
  - Record new modes, migration behavior, commands, and compatibility notes.
- `RELEASE_CHECKLIST.md`
  - Build/install and smoke-test Basic, Local Drive, AWS, migration, offload, and fetch.
  - Record clean-install acceptance evidence for all three paths.

## Decisions to Keep Explicit

- Basic/index-only is the default for new users.
- Local drive and AWS are optional alternatives, not required sequential upgrades.
- Additional folders default to index-only in the new canonical folder command.
- Sync must be explicitly enabled per content root.
- Storage copies are not indexed as separate sources.
- Automatic local deletion propagation remains off for local drives.
- Intentional offload requires a dedicated command and state transition.
- ChatGPT, OpenClaw, Claude Code, and Cursor support are separate client-validation work
  and do not block this storage/indexing redesign.
