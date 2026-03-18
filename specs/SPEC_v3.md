# Sahara Cloud Storage — Product Specification v3.0

## 1. Overview

**Sahara** is a personal, self-hosted cloud storage system built on AWS S3 that provides a Dropbox-like experience without recurring subscription costs. Users pay only for what they store and transfer, directly to AWS.

### 1.1 Problem Statement
Consumer cloud storage services charge fixed monthly subscriptions regardless of actual usage:
- Google One 2TB: $9.99/month
- iCloud+ 2TB: $9.99/month
- Dropbox Plus 2TB: $11.99/month

With Sahara on AWS S3:
- 2TB Hot Storage: ~$47/month (same instant-access speed)
- 2TB Cold (Glacier Deep Archive): ~$2.00/month (for rarely-accessed archives)
- Typical mixed 1TB workload: ~$5–8/month

AWS S3 pricing is usage-based, making it significantly cheaper for users who store large amounts of cold/archival data or have variable storage needs.

### 1.2 Goals
- Provide seamless, bidirectional file sync between a local folder and AWS S3
- Support two storage tiers: Hot (S3 Standard) and Cold (Glacier Deep Archive)
- Offer a fast, intuitive CLI for all operations
- Track file changes efficiently using checksums (no unnecessary re-uploads)
- Support personal use — single user, multiple machines

### 1.3 Non-Goals (v1)
- Multi-user/team collaboration
- Web UI
- Mobile apps
- Real-time collaboration / locking
- Full POSIX filesystem semantics
- File versioning / version history (explicitly: overwritten files are NOT recoverable unless the user manually archived them first)
- Automatic Glacier restore when a file is accessed (always manual)

---

## 2. User Stories & Acceptance Criteria

| ID | Story | Acceptance Criteria |
|----|-------|---------------------|
| US-01 | As a user, I want to sync a local folder to S3 so my files are backed up in the cloud. | `sahara push` uploads all new/modified files; re-running push on unchanged folder produces zero uploads. |
| US-02 | As a user, I want to download synced files from any machine to restore my data. | `sahara pull` on a fresh machine downloads all remote files; existing identical files are skipped. |
| US-03 | As a user, I want to archive old files to Glacier Deep Archive to save money on cold data. | `sahara archive <path>` moves file to DEEP_ARCHIVE class; file no longer appears in Hot tier; `sahara ls --tier=cold` shows it. |
| US-04 | As a user, I want to restore archived files from Glacier when I need them. | `sahara restore <path>` initiates job and returns ETA; daemon notifies on completion; `sahara restore-download <path>` succeeds after restore completes. |
| US-05 | As a user, I want to see a list of all my files with tier, size, and last modified date. | `sahara ls` shows all files with size, tier, and modified date in tabular format. |
| US-06 | As a user, I want automatic conflict detection when syncing from multiple machines. | When both local and remote have changed, sync pauses and reports conflict with both versions' timestamps; no silent data loss. |
| US-07 | As a user, I want to configure which folders/files to exclude from sync. | Files matching `.saharaignore` patterns are never uploaded; patterns follow gitignore syntax. |
| US-08 | As a user, I want to see bandwidth and storage usage cost estimates. | `sahara usage` shows current month costs broken down by storage, requests, and egress; shows projected monthly total. |
| US-09 | As a user, I want incremental sync — only changed files are uploaded/downloaded. | Only files where SHA-256 has changed are transferred; unchanged files produce zero API calls beyond listing. |
| US-10 | As a user, I want encryption at rest and in transit. | All transfers use HTTPS; SSE-S3 is on by default; client-side encryption optional. |
| US-11 | As a user, I want to rename/move files and have sync handle it efficiently. | Rename/move tracked as move operation; does not re-upload content if checksum matches an existing remote object. |
| US-12 | As a user, I want safe deletion with confirmation before files are permanently removed. | `sahara rm` prompts for confirmation by default; requires `--force` flag to skip prompt. |
| US-13 | As a user, I want to be notified when a Glacier restore completes. | Daemon sends desktop notification and logs completion; `--wait` flag on `sahara restore` blocks and polls until complete. |

---

## 3. Architecture

### 3.1 Components

```
┌─────────────────────────────────────────────────────────┐
│                     LOCAL MACHINE                        │
│  ┌──────────────┐    ┌──────────────────────────────┐   │
│  │  Sync Folder  │◄──►│     Sahara CLI / Daemon       │   │
│  │  (watched)    │    │  - FileWatcher               │   │
│  └──────────────┘    │  - SyncEngine                │   │
│                       │  - StateManager (SQLite)     │   │
│                       │  - S3Client (boto3)          │   │
│                       │  - FileLock (advisory)       │   │
│                       └──────────────┬───────────────┘   │
└──────────────────────────────────────┼───────────────────┘
                                        │ HTTPS (TLS)
┌──────────────────────────────────────▼───────────────────┐
│                       AWS                                  │
│  ┌─────────────────────┐   ┌──────────────────────────┐  │
│  │   S3 Standard Bucket │   │  S3 Glacier Deep Archive │  │
│  │   (Hot Storage)      │   │  (Cold Storage)           │  │
│  │   - Active files     │   │  - Archived files         │  │
│  │   - .sahara/metadata │   │  - Restore: 12-48h        │  │
│  └─────────────────────┘   └──────────────────────────┘  │
└───────────────────────────────────────────────────────────┘
```

### 3.2 Storage Strategy

| Tier | AWS Class | Use Case | Retrieval Time | Cost Approx |
|------|-----------|----------|----------------|-------------|
| Hot | S3 Standard | Active files, recent data | Instant | ~$0.023/GB/mo |
| Cold | Glacier Deep Archive | Backups, archives, rarely accessed | 12-48 hours | ~$0.00099/GB/mo |

**Note on egress costs**: AWS charges $0.09/GB for data downloaded after first 100 GB/month free. For users who regularly download large datasets to multiple machines, egress costs may exceed storage costs. `sahara usage` displays projected egress costs.

### 3.3 Local State Database (SQLite)
Location: `~/.sahara/state.db`

#### Table: `files`
```sql
CREATE TABLE files (
  id              INTEGER PRIMARY KEY,
  relative_path   TEXT NOT NULL UNIQUE,
  sha256_checksum TEXT,               -- pre-encryption checksum; NULL for imported foreign objects
  size_bytes      INTEGER,
  tier            TEXT NOT NULL,      -- 'hot' | 'cold' | 'hot_temp'
  s3_etag         TEXT,
  last_sync_at    TEXT,               -- ISO 8601 UTC
  local_modified_at TEXT,             -- ISO 8601 UTC
  remote_modified_at TEXT,            -- x-amz-meta-sahara-modified-at value
  archived_at     TEXT,
  restore_job_id  TEXT,
  restore_expires_at TEXT,
  is_deleted      INTEGER DEFAULT 0   -- soft delete flag
);
```

Valid `tier` values:
- `hot` — file in S3 Standard storage, fully synced
- `cold` — file in Glacier Deep Archive, not directly accessible
- `hot_temp` — file was Glacier-restored; temporary Hot copy exists, expires at `restore_expires_at`

`hot_temp` behavior during sync:
- On `sahara push`: treated as `hot`; if modified locally, it is re-uploaded as permanent `hot` and tier promoted to `hot`
- On `sahara pull`: skipped (already present as temp copy)
- When `restore_expires_at` passes: tier reverts to `cold`; local copy remains but is marked "local only"

#### Table: `sync_history`
```sql
CREATE TABLE sync_history (
  id                INTEGER PRIMARY KEY,
  operation         TEXT,      -- 'upload' | 'download' | 'delete' | 'archive' | 'restore' | 'move'
  path              TEXT,
  sha256_checksum   TEXT,      -- checksum at time of operation (enables recovery audit)
  s3_etag           TEXT,
  status            TEXT,      -- 'success' | 'failed' | 'skipped'
  error_message     TEXT,
  started_at        TEXT,
  completed_at      TEXT,
  bytes_transferred INTEGER
);
```

#### Table: `pending_multipart`
```sql
CREATE TABLE pending_multipart (
  id            INTEGER PRIMARY KEY,
  relative_path TEXT NOT NULL,
  upload_id     TEXT NOT NULL,
  s3_key        TEXT NOT NULL,
  parts_json    TEXT,          -- JSON array of {PartNumber, ETag} for completed parts
  started_at    TEXT
);
```

#### Table: `config`
```sql
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT);
```

### 3.4 S3 Metadata Convention
Each file in S3 has object metadata:
- `x-amz-meta-sahara-checksum`: SHA-256 of **pre-encryption** original file content
- `x-amz-meta-sahara-original-path`: Original relative path
- `x-amz-meta-sahara-modified-at`: Local file mtime at upload time (ISO 8601 UTC)
- `x-amz-meta-sahara-tier`: `hot` or `cold`

Files without `x-amz-meta-sahara-checksum` are treated as "foreign objects" (imported, not uploaded by Sahara) and handled per Section 4.1 import flow.

### 3.5 Encryption

- **In transit**: HTTPS/TLS (enforced by boto3 / AWS SDK)
- **At rest**: AWS S3 Server-Side Encryption SSE-S3 enabled by default
- **Client-side** (optional): AES-256-GCM encryption before upload using user-provided passphrase
  - SHA-256 checksum is computed on **plaintext** content before encryption and stored in S3 metadata
  - This ensures incremental sync still works correctly (checksum is compared pre-encryption, not of the ciphertext)
  - Random IV per file per upload; IV stored prepended to ciphertext

**Passphrase management**:
- Set via `sahara encryption setup` (separate command from init, see 4.1)
- Stored in OS keychain (macOS Keychain, Linux Secret Service, Windows Credential Manager) via `keyring`
- If `client_side=true` and no passphrase in keychain: sync fails immediately with: `"Client-side encryption is enabled but no passphrase found. Run 'sahara encryption setup' to configure."`
- Passphrase rotation: `sahara encryption rotate` — re-encrypts all Hot tier files with new passphrase (Cold tier files are not re-encrypted until restored; user is warned)
- Lost passphrase: no recovery possible for encrypted files. User must understand this at setup time. `sahara encryption setup` prints explicit warning.

### 3.6 Daemon / CLI Concurrency
- Advisory file lock: `~/.sahara/sync.lock`
- If a sync (daemon or CLI) is running, subsequent CLI `sync`/`push`/`pull` commands detect the lock and exit with: `"Sync already in progress (PID 12345). Use --wait to queue."`
- `--wait` flag causes CLI to poll until the lock is released, then runs
- Daemon acquires lock for duration of sync operation, releases immediately after

---

## 4. CLI Specification

### 4.1 `sahara init` — Detailed Flow

```
sahara init [--bucket=NAME] [--region=us-east-1] [--folder=PATH] [--non-interactive]
```

Interactive setup wizard steps:
1. Prompt for AWS credentials method: [a] AWS profile, [b] access key + secret, [c] env vars (auto-detected)
2. Validate AWS credentials (`sts:GetCallerIdentity`); fail with specific error if invalid
3. Prompt for or accept S3 bucket name
4. Check if bucket exists:
   - **Does not exist**: Offer to create (`s3:CreateBucket`). If yes: enable Block Public Access, enable SSE-S3, set lifecycle rule for orphaned multipart cleanup (7 days)
   - **Exists, empty**: Use as-is
   - **Exists, has Sahara objects** (detected by `x-amz-meta-sahara-checksum` presence): Offer to import. Import: `ListObjectsV2`, populate `files` table with `sha256_checksum` from metadata, `tier` from storage class, `remote_modified_at` from metadata. Objects missing `x-amz-meta-sahara-checksum` are imported with `sha256_checksum=NULL` and flagged for full checksum computation on next `--verify` sync.
   - **Exists, has non-Sahara objects**: Warn "Bucket contains objects not managed by Sahara. These will NOT be synced or deleted by Sahara unless you import them." Offer to skip, import with null checksums, or abort.
5. Prompt for local sync folder (default: `~/Sahara`); create if not exists
6. Prompt for client-side encryption: [y/N]. If yes, run `sahara encryption setup` sub-flow:
   - Prompt for passphrase (with confirmation)
   - Store in OS keychain
   - Print: "WARNING: If you lose this passphrase, your encrypted files in Glacier cannot be recovered. Store it securely."
7. Create `~/.sahara/` directory, write `~/.sahara/config.toml`
8. Create `.saharaignore` template in sync folder
9. Run `sahara doctor` preflight check automatically
10. Print summary: bucket, region, folder, encryption status, estimated first sync size

### 4.2 `sahara doctor` — Preflight Check

```
sahara doctor [--repair]
```

Validates and reports pass/fail for each:
- AWS credentials valid and not expired
- Bucket exists and is accessible (`s3:HeadBucket`)
- Required IAM permissions: `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`, `s3:ListBucket`, `s3:RestoreObject`, `s3:GetObjectRestoreStatus`, `s3:AbortMultipartUpload`
- Bucket region matches configured region
- Local sync folder exists and is readable/writable
- State DB integrity (try `PRAGMA integrity_check`)
- Orphaned multipart uploads: list and warn

`--repair` flag: if state DB is corrupted, rename to `state.db.bak.TIMESTAMP` and rebuild from S3 listing.

### 4.3 Full Command Reference

```
# Setup
sahara init [--bucket=NAME] [--region=REGION] [--folder=PATH] [--non-interactive]
sahara doctor [--repair]
sahara encryption setup                      Set client-side encryption passphrase
sahara encryption rotate                     Rotate encryption passphrase (re-encrypts hot files)
sahara config set <key> <value>
sahara config get <key>
sahara config show

# Sync
sahara sync [--dry-run] [--verify] [--wait]              Bidirectional sync
sahara push [path] [--dry-run] [--verify] [--wait]       Upload local changes to S3
sahara pull [path] [--dry-run] [--verify] [--wait]       Download remote changes
sahara status                                             Show what would change (non-destructive)
sahara diff [path]                                        Show diff metadata

# Conflict Resolution (used with conflict_strategy=manual)
sahara conflicts                             List all files with unresolved conflicts
sahara resolve <path> --keep=local|remote|backup  Resolve a specific conflict

# File Operations
sahara ls [path] [--tier=hot|cold|hot_temp] [--long]  List remote files
sahara rm <path> [--force]                   Delete from remote (prompts unless --force)
sahara rm <path> --local [--force]           Delete local copy only
sahara mv <old-path> <new-path>              Rename/move file in remote (no re-upload)

# Archive / Glacier
sahara archive <path|dir] [--older-than=DAYS] [--dry-run] [--force]  Move to Glacier
sahara restore <path> [--tier=bulk|standard|expedited] [--wait]
sahara restore-status <path>
sahara restore-download <path> [--overwrite]

# Information
sahara usage [--simulate] [--month=YYYY-MM]
sahara history [path] [--limit=50]           Show sync history for a path or all files

# Daemon
sahara daemon start [--on-login]
sahara daemon stop
sahara daemon status
sahara daemon pause                          Pause automatic sync (persists across restarts: no)
sahara daemon resume
sahara daemon logs [--tail=50]
```

### 4.4 Command Behavioral Contracts

#### `sahara push` vs `sahara sync`
- `sahara push`: Upload only. Does NOT download remote-only files. Does check for conflicts — if a remote file has changed, reports conflict (does not overwrite remote).
- `sahara pull`: Download only. Does NOT upload local changes. Skips files where local checksum matches remote.
- `sahara sync`: Bidirectional. Equivalent to push + pull in a single pass with unified conflict resolution.
- None of these are "force" operations. All check for conflicts before overwriting.

#### `sahara rm <path> [--force]`
- Deletes file from remote S3 and marks as deleted in state DB
- Default: prompts `"Delete <path> from remote permanently? [y/N]"`. Aborts on N.
- `--force`: skips prompt (for scripting)
- Does NOT delete local file unless `--local` flag is specified
- If file is in `cold` tier: additional warning prompt: "This file is in Glacier Deep Archive. Deleting before 180 days of storage will still incur the full 180-day charge (~$X). Proceed? [y/N]"

#### `sahara archive <path|dir> [--older-than=DAYS] [--dry-run] [--force]`
- With `<path>`: archives single file or all files under a directory path
- With `--older-than=DAYS`: archives all Hot tier files where `remote_modified_at` is older than N days. "Older than" is measured against `x-amz-meta-sahara-modified-at` (upload-time local mtime).
- `--dry-run`: lists files that would be archived, prints estimated cost savings, takes no action
- If file was uploaded < 180 days ago: warns "Archiving this file before 180 days will still incur full 180-day storage charge (~$X). Use --force to proceed." Without `--force`: command aborts.
- `--force`: bypasses 180-day warning

#### `sahara restore-download <path> [--overwrite]`
- If local file exists at path AND local checksum differs from restored file: prompts "A local file already exists at <path> and differs from the archive. Overwrite? [y/N]"
- `--overwrite`: skips prompt
- If local file exists and checksums match: skips download, confirms already up to date

#### `sahara resolve <path> --keep=local|remote|backup`
- `--keep=local`: upload local to remote, delete conflict state
- `--keep=remote`: download remote to local, delete conflict state
- `--keep=backup`: download remote as `<filename>.conflict-TIMESTAMP.<ext>` (LOCAL ONLY — NOT pushed to S3), promote local version as canonical, push to remote

#### Remote delete handling (multi-machine sync)
When syncing and a file exists in local state DB (`is_deleted=0`) and is present locally but absent from S3:
- With `conflict_strategy=newest-wins` or `backup`: treat remote delete as authoritative; delete local file and state DB entry; log to `sync_history`
- With `conflict_strategy=manual`: halt and report: "File <path> was deleted on another machine but exists locally. Run `sahara resolve <path> --keep=local` to re-upload or `--keep=remote` to delete locally."

#### `sahara daemon pause`
- Sets a flag file `~/.sahara/daemon.paused`
- Does NOT persist across daemon restarts — on `daemon start`, daemon always starts in running (unpaused) state
- `sahara daemon status` shows paused state if applicable

### 4.5 Configuration Keys
```toml
[aws]
profile = ""                    # AWS profile name (alternative to key+secret)
access_key_id = ""              # Prefer env var AWS_ACCESS_KEY_ID or profile
secret_access_key = ""          # Prefer env var AWS_SECRET_ACCESS_KEY or profile
region = "us-east-1"
bucket = ""
sse = "SSE-S3"                  # SSE-S3 | SSE-KMS | none
kms_key_id = ""

[sync]
folder = "~/Sahara"
exclude = []                    # Additional glob patterns (gitignore syntax)
auto_archive_days = 0           # 0 = disabled; measured against remote_modified_at
conflict_strategy = "backup"    # newest-wins | manual | backup
bandwidth_limit_kbps = 0        # 0 = unlimited
debounce_seconds = 5

[encryption]
client_side = false
# passphrase stored in OS keychain, NOT in this config file

[restore]
default_tier = "bulk"           # bulk | standard | expedited
temp_expiry_days = 7
notify_on_complete = true

[performance]
multipart_threshold_mb = 100
multipart_part_size_mb = 8
max_concurrent_uploads = 4
max_concurrent_downloads = 4
```

---

## 5. Sync Engine

### 5.1 Sync Algorithm
1. **Acquire lock**: `~/.sahara/sync.lock` (fail or wait per `--wait` flag)
2. **Scan local**: Walk sync folder, skip `.saharaignore` matches; for each file: if mtime or size changed since `last_sync_at`, compute SHA-256; else reuse cached checksum from state DB
3. **Fetch remote index**: `s3:ListObjectsV2` with metadata; paginate until complete
4. **Three-way diff**: Compare (A) local state DB, (B) current local filesystem, (C) current remote S3 state. Categorize each file as: unchanged, local-only-new, remote-only-new, local-modified, remote-modified, both-modified (conflict), local-deleted, remote-deleted
5. **Detect renames**: Among local-deleted + local-only-new pairs, match by SHA-256; treat matches as moves
6. **Resolve conflicts**: Apply configured strategy
7. **Execute operations** (concurrent, up to `max_concurrent_uploads/downloads`):
   - Upload: new/modified local files (multipart if >threshold)
   - Download: remote-only-new / remote-modified (non-conflict) files
   - Move: `s3:CopyObject` + `s3:DeleteObject` for renames
   - Delete: apply remote-delete policy per Section 4.4
8. **Update state DB**: Record all outcomes in `files` and `sync_history`
9. **Release lock**

### 5.2 Change Detection
- **Local**: `mtime` + `size` change → recompute SHA-256; otherwise use cached checksum
- **Conflict timestamp authority**: `x-amz-meta-sahara-modified-at` (set at upload time from local mtime) is the authoritative timestamp for conflict resolution — NOT S3 `Last-Modified`, NOT sync-time clock
- **Full verify mode** (`--verify`): Recomputes all local SHA-256s regardless of mtime

### 5.3 Conflict Resolution

A **conflict** is detected when: local SHA-256 ≠ state DB checksum AND remote `x-amz-meta-sahara-checksum` ≠ state DB checksum (both sides changed since last sync).

| Strategy | Behavior |
|----------|----------|
| `newest-wins` | File with most recent `x-amz-meta-sahara-modified-at` wins; loser is discarded silently |
| `manual` | Conflicting files are skipped; reported by `sahara conflicts`; resolved via `sahara resolve` |
| `backup` | Remote downloaded as `<filename>.conflict-TIMESTAMP.<ext>` — **local only, NOT pushed to S3**; local version pushed as canonical remote |

Conflict files (`.conflict-TIMESTAMP`) are automatically added to `.saharaignore` to prevent them from being synced. `sahara status` surfaces any unresolved conflict files with a warning.

### 5.4 Rename / Move Handling
1. After diff, collect: local-deleted paths + local-only-new paths
2. For each pair where SHA-256 matches: `s3:CopyObject` old-key → new-key, `s3:DeleteObject` old-key
3. No content re-upload; 1 COPY + 1 DELETE per rename
4. No SHA-256 match: treated as delete + new upload

### 5.5 Large File Support
- Files > `multipart_threshold_mb` (default 100MB) use S3 multipart upload
- In-progress upload IDs stored in `pending_multipart` table
- On resume: fetch existing parts from S3 (`s3:ListParts`), continue from last uploaded part
- Network timeout during multipart: mark operation as interrupted; on next sync run, resume from `pending_multipart` table (do NOT fail the sync run — move on to next file)
- S3 lifecycle rule: auto-abort incomplete multipart uploads after 7 days (set by `sahara init`)

### 5.6 Retry Policy

| Error Type | Policy |
|------------|--------|
| Network timeout / connection error | Retry up to 5 times with exponential backoff: 2s, 4s, 8s, 16s, 32s |
| S3 503 / rate limiting | Retry up to 5 times with jitter backoff, cap 60s |
| S3 4xx client errors (403, 404) | Fail immediately, no retry; log specific error and required action |
| Multipart upload interrupted | Do NOT retry immediately; mark as interrupted; resume on next sync cycle |
| Disk full | Fail immediately; stop sync; notify via daemon |

All retries logged at DEBUG level.

### 5.7 Exclusion Patterns
Two sources, merged (both applied):
1. `.saharaignore` in sync root — gitignore syntax, per-directory support
2. `sync.exclude` in config — additional global patterns (gitignore syntax)

Default built-in excludes (always applied, not user-configurable):
```
.DS_Store
Thumbs.db
desktop.ini
*.tmp
*.swp
~$*
.Trash-*
*.conflict-*    # Sahara conflict files
```

---

## 6. Glacier Archive Operations

### 6.1 Archive Flow
1. Verify file is in `hot` tier (state DB + optional S3 check)
2. If `local_modified_at` or `remote_modified_at` is within 180 days: warn with estimated premature charge. Without `--force`: abort.
3. `s3:CopyObject` to same key with `StorageClass=DEEP_ARCHIVE`
4. `s3:DeleteObject` original Hot object
5. Update state DB: `tier=cold`, `archived_at=now`
6. Print: "Archived <path> (X MB) to Glacier Deep Archive. Retrieval will take 12-48 hours. Minimum storage: 180 days."

### 6.2 Restore Flow

**Step 1 — Initiate** (`sahara restore <path> [--tier=bulk] [--wait]`):
- Verify file is in `cold` or `hot_temp` tier
- If already `hot_temp` and not expired: print "File already restored, available until <date>. Run `sahara restore-download <path>` to download."
- Call `s3:RestoreObject` with `Days=temp_expiry_days`
- Store `restore_job_id` (S3 restore request ID) in state DB
- Print estimated completion time based on tier
- If `--wait`: poll `s3:HeadObject` every 30 minutes; print progress; on complete, print notification and exit

**Step 2 — Check Status** (`sahara restore-status <path>`):
- Call `s3:HeadObject`; parse `x-amz-restore` header
- Display: `Restore status: in-progress | complete | expired | not-initiated`
- If complete: print "Run `sahara restore-download <path>` to download."

**Step 3 — Download** (`sahara restore-download <path> [--overwrite]`):
- Check restore is complete (if not: print status and ETA, exit with error)
- If local file exists at path and checksums differ: prompt (or use `--overwrite`)
- Download to sync folder
- Update state DB: `tier=hot_temp`, `restore_expires_at=now + temp_expiry_days`

### 6.3 Restore Expiry Handling
- State DB tracks `restore_expires_at` for all `hot_temp` files
- Daemon runs daily check:
  - 24h before expiry: desktop notification + log warning
  - On/after expiry: update state DB `tier=cold`; local copy (if downloaded) remains but `sahara status` marks it "local only (archive expired)"
- `sahara status` displays all `hot_temp` files with expiry dates

### 6.4 Restore Tiers
| Tier | Speed | Cost per GB |
|------|-------|-------------|
| Bulk | 12-48h | ~$0.0025 |
| Standard | 3-5h | ~$0.01 |
| Expedited | 1-5min | ~$0.03 |

---

## 7. Daemon / Background Sync

### 7.1 Daemon Operation
- File system watching via `watchdog` library (inotify/Linux, FSEvents/macOS, ReadDirectoryChangesW/Windows)
- Debounces rapid changes: 5s default window before triggering partial sync for changed paths
- On file event: sync only affected paths (incremental)
- Scheduled full sync every 6 hours
- Restore polling: every 30 minutes, `s3:HeadObject` for all `restore_job_id` entries in state DB
- Desktop notifications via `plyer` (macOS, Linux, Windows)
- Logs: `~/.sahara/daemon.log` (rotating, max 10MB, keep 3)
- PID file: `~/.sahara/daemon.pid`
- Pause flag file: `~/.sahara/daemon.paused` (removed on `daemon stop`; daemon always starts unpaused)

### 7.2 Network Awareness
- Detect network availability before sync attempt; retry with backoff if offline
- Configurable bandwidth throttling (`sync.bandwidth_limit_kbps`)
- `sahara daemon pause` / `sahara daemon resume` for manual control

---

## 8. Cost Estimation

### 8.1 `sahara usage` Output
```
Sahara Usage Report — March 2026
═══════════════════════════════════════════════════════

Storage (current):
  Hot (S3 Standard):       45.3 GB   ~$1.04/month
  Cold (Glacier Deep):    892.1 GB   ~$0.88/month
  Total:                  937.4 GB   ~$1.92/month

  Comparison: 1TB Google One = $9.99/mo | 1TB iCloud+ = $9.99/mo

API Requests (this month):
  PUT/COPY:               1,203       ~$0.006
  GET:                      456       ~$0.002
  Glacier retrievals:        12       ~$0.001

Data Transfer (this month):
  Upload:                  2.3 GB    Free
  Download:                0.8 GB    ~$0.07
  ⚠ Note: First 100 GB/month egress free; $0.09/GB after.

Estimated Monthly Total:   ~$2.00
(vs Google One 2TB: $9.99/mo — you save ~$8/mo)

Run 'sahara usage --simulate' for projected costs based on sync frequency.
```

### 8.2 `sahara usage --simulate`
Projects costs based on average daily sync volume from last 30 days in `sync_history`. Shows 3-month projection.

---

## 9. Error Handling & Resilience

| Error Type | Behavior |
|------------|----------|
| Network timeout | Retry up to 5× with exponential backoff (2,4,8,16,32s) |
| S3 rate limiting (503) | Retry up to 5× with jitter, cap 60s |
| Missing permissions (403) | Fail immediately, log required IAM action |
| Bucket not found | Fail with "Run `sahara doctor` to diagnose" |
| State DB corrupted | Rename, rebuild from S3 listing (`sahara doctor --repair`) |
| Disk full | Fail immediately, notify daemon |
| File in use (Windows) | Skip, log, retry next cycle |
| Multipart interrupted | Mark pending, resume next sync cycle |
| Lost encryption passphrase | Files are unrecoverable. Print "Run `sahara encryption setup` to configure a new passphrase (existing encrypted files will be inaccessible)." |

All errors logged: timestamp, operation, path, error code, stack trace → `~/.sahara/error.log` (rotating 10MB, keep 5)

---

## 10. Security Considerations

- AWS credentials: prefer AWS profile or env vars; config file stores profile name only
- Passphrase stored in OS keychain via `keyring`; never in config file
- S3 bucket: Block Public Access enabled by default on creation
- All API calls via HTTPS (boto3 default)
- IAM role-based auth supported (for EC2/server use)
- Minimum required IAM policy in README

---

## 11. Performance Targets

| Metric | Target |
|--------|--------|
| Sync latency (daemon, file change event) | < 10s from event to upload start |
| Full scan of 100,000 files (local) | < 30s |
| S3 listing of 100,000 objects | < 60s (paginated) |
| Daemon memory usage (idle) | < 50MB RSS |
| State DB query (lookup by path) | < 10ms |
| Concurrent upload/download streams | 4 (configurable) |

---

## 12. Technology Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.11+ |
| AWS SDK | boto3 |
| CLI Framework | Click |
| Local DB | SQLite (direct, no ORM) |
| File Watching | watchdog |
| Config | TOML (tomllib stdlib Python 3.11+) |
| Encryption | cryptography (PyCA) |
| Keychain | keyring |
| Notifications | plyer |
| File Locking | filelock |
| Testing | pytest + moto (AWS mock) |
| Coverage | pytest-cov |
| Packaging | pip / pyproject.toml |

---

## 13. File Structure

```
sahara/
├── pyproject.toml
├── README.md
├── .saharaignore.template
├── src/
│   └── sahara/
│       ├── __init__.py
│       ├── cli.py              # Click CLI: all commands
│       ├── config.py           # TOML config read/write
│       ├── sync_engine.py      # Core sync algorithm + three-way diff
│       ├── s3_client.py        # boto3 S3/Glacier operations
│       ├── state_db.py         # SQLite state management
│       ├── file_watcher.py     # watchdog FS event handling
│       ├── daemon.py           # Background daemon process
│       ├── encryption.py       # AES-256-GCM client-side encryption
│       ├── cost_estimator.py   # Cost calculation + formatting
│       ├── ignore_rules.py     # .saharaignore / gitignore matching
│       ├── notifier.py         # Desktop notifications via plyer
│       └── models.py           # Dataclasses: FileRecord, SyncOp, etc.
└── tests/
    ├── conftest.py
    ├── test_sync_engine.py
    ├── test_s3_client.py
    ├── test_state_db.py
    ├── test_cli.py
    ├── test_config.py
    ├── test_encryption.py
    ├── test_cost_estimator.py
    ├── test_ignore_rules.py
    └── test_daemon.py
```

---

## 14. Resolved Design Decisions

1. **Multiple sync folders**: Not in v1. Single folder only.
2. **Glacier restore trigger**: Always manual.
3. **Pre-signed URL sharing**: Not in v1.
4. **File versioning**: Not in v1. `sync_history` includes `sha256_checksum` for audit; no content recovery.
5. **Rename detection**: SHA-256 matching (no re-upload).
6. **Conflict timestamp authority**: `x-amz-meta-sahara-modified-at`.
7. **Daemon + CLI locking**: Advisory `sync.lock`; CLI blocks (--wait) or fails with clear message.
8. **Orphaned multipart cleanup**: S3 lifecycle rule on bucket creation; listed in `sahara doctor`.
9. **Glacier 180-day minimum**: Warn + `--force` required to proceed early.
10. **Conflict backup files**: Local only — NOT pushed to S3. Auto-added to `.saharaignore`.
11. **Remote delete handling**: `newest-wins`/`backup` treat as authoritative; `manual` halts and reports.
12. **`hot_temp` modification**: If user modifies a `hot_temp` file, it is re-uploaded and promoted to permanent `hot` on next push.
13. **Daemon pause persistence**: Pause does NOT persist across daemon restarts.
14. **`--older-than` for archive**: Measured against `x-amz-meta-sahara-modified-at` (upload-time mtime).
