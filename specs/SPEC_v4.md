# Sahara Cloud Storage — Product Specification v4.0 (Engineering-Ready)

## 1. Overview

**Sahara** is a personal, self-hosted cloud storage system built on AWS S3 that provides a Dropbox-like experience without recurring monthly subscription costs. Users pay only for what they store and transfer, directly to AWS.

### 1.1 Problem Statement
Consumer cloud storage services charge fixed monthly subscriptions regardless of actual usage:
- Google One 2TB: $9.99/month
- iCloud+ 2TB: $9.99/month
- Dropbox Plus 2TB: $11.99/month

With Sahara on AWS S3:
- 2TB Hot Storage: ~$47/month (same instant-access speed)
- 2TB Cold (Glacier Deep Archive): ~$2.00/month (for rarely-accessed archives)
- Typical mixed 1TB workload: ~$5–8/month

### 1.2 Goals
- Seamless bidirectional file sync between a local folder and AWS S3
- Two storage tiers: Hot (S3 Standard) and Cold (Glacier Deep Archive)
- Fast, intuitive CLI for all operations
- Incremental sync using checksums (no unnecessary re-uploads)
- Personal use — single user, multiple machines

### 1.3 Non-Goals (v1)
- Multi-user/team collaboration
- Web UI
- Mobile apps
- Real-time collaboration / locking
- Full POSIX filesystem semantics
- File versioning / version history (overwritten files are NOT recoverable)
- Automatic Glacier restore on file access (always manual)

---

## 2. User Stories & Acceptance Criteria

| ID | Story | Acceptance Criteria |
|----|-------|---------------------|
| US-01 | Sync local folder to S3 | `sahara push` uploads all new/modified files; re-run on unchanged folder = zero uploads |
| US-02 | Download files on any machine | `sahara pull` on fresh machine downloads all remote files; identical files skipped |
| US-03 | Archive to Glacier | `sahara archive <path>` moves to DEEP_ARCHIVE; not in Hot tier; appears in `ls --tier=cold` |
| US-04 | Restore from Glacier | `sahara restore <path>` initiates job, returns ETA; daemon notifies on completion |
| US-05 | List files with metadata | `sahara ls` shows size, tier, modified date in tabular format |
| US-06 | Conflict detection | Both-sides-changed reported; no silent data loss |
| US-07 | Exclude patterns | `.saharaignore` files never uploaded; gitignore syntax |
| US-08 | Cost estimates | `sahara usage` shows storage/request/egress costs + projected monthly total |
| US-09 | Incremental sync | Only SHA-256-changed files transferred |
| US-10 | Encryption | HTTPS transit; SSE-S3 default at rest; optional AES-256-GCM client-side |
| US-11 | Efficient rename/move | Rename = S3 copy+delete, no re-upload |
| US-12 | Safe deletion | `sahara rm` prompts confirmation; `--force` to skip |
| US-13 | Restore notification | Daemon desktop notification when Glacier restore complete |

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
│  ┌──────────────────────────────────────────────────────┐ │
│  │              Single S3 Bucket                         │ │
│  │  Hot objects:  StorageClass=STANDARD                  │ │
│  │  Cold objects: StorageClass=DEEP_ARCHIVE              │ │
│  └──────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────┘
```

Note: Both tiers exist in the **same S3 bucket**, differentiated by the `StorageClass` attribute of each object.

### 3.2 Storage Strategy

| Tier | S3 StorageClass | Use Case | Retrieval | Cost |
|------|----------------|----------|-----------|------|
| Hot | `STANDARD` | Active files | Instant | ~$0.023/GB/mo |
| Cold | `DEEP_ARCHIVE` | Archives | 12-48h | ~$0.00099/GB/mo |

**Egress costs**: $0.09/GB after first 100 GB/month free. Displayed in `sahara usage`.

### 3.3 Local State Database (SQLite)
Location: `~/.sahara/state.db`

#### `files` table
```sql
CREATE TABLE files (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  relative_path       TEXT NOT NULL UNIQUE,
  sha256_checksum     TEXT,          -- pre-encryption plaintext checksum; NULL = foreign/unknown
  size_bytes          INTEGER,
  tier                TEXT NOT NULL  -- 'hot' | 'cold' | 'hot_temp'
                      CHECK(tier IN ('hot','cold','hot_temp')),
  s3_etag             TEXT,
  last_sync_at        TEXT,          -- ISO 8601 UTC; timestamp of last successful sync operation
  local_modified_at   TEXT,          -- mtime of local file at last sync (ISO 8601 UTC)
  remote_modified_at  TEXT,          -- value of x-amz-meta-sahara-modified-at from S3
  archived_at         TEXT,
  restore_job_id      TEXT,
  restore_expires_at  TEXT,          -- when S3 hot_temp copy expires
  is_deleted          INTEGER NOT NULL DEFAULT 0  -- 1 = soft-deleted
);
CREATE INDEX idx_files_path ON files(relative_path);
CREATE INDEX idx_files_tier ON files(tier);
```

**`tier` state machine**:
```
hot ──archive──► cold ──restore──► cold (restore pending)
                                    │ restore complete
                                    ▼
                              hot_temp ──expires──► cold
                                    │ user modifies file
                                    ▼
                                   hot (permanent, promoted on next push)
```

- `hot_temp` during sync: treated as `hot` for push purposes; if modified locally, re-upload and set `tier=hot` permanently.
- `hot_temp` during pull: skipped (local copy already present).
- After `restore_expires_at`: daemon sets `tier=cold`; local copy stays but is "local only (archive expired)".

#### `sync_history` table
```sql
CREATE TABLE sync_history (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  operation         TEXT NOT NULL,  -- 'upload'|'download'|'delete'|'archive'|'restore'|'move'
  path              TEXT NOT NULL,
  sha256_checksum   TEXT,           -- checksum at time of operation (for audit)
  s3_etag           TEXT,
  status            TEXT NOT NULL,  -- 'success'|'failed'|'skipped'
  error_message     TEXT,
  started_at        TEXT NOT NULL,
  completed_at      TEXT,
  bytes_transferred INTEGER DEFAULT 0
);
```

#### `pending_multipart` table
```sql
CREATE TABLE pending_multipart (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  relative_path TEXT NOT NULL UNIQUE,
  upload_id     TEXT NOT NULL,
  s3_key        TEXT NOT NULL,
  parts_json    TEXT DEFAULT '[]',  -- JSON: [{PartNumber, ETag}, ...]
  started_at    TEXT NOT NULL
);
```

#### `config` table
```sql
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

### 3.4 S3 Object Metadata Convention
Every object uploaded by Sahara carries:
```
x-amz-meta-sahara-checksum:      SHA-256 of plaintext (pre-encryption) content
x-amz-meta-sahara-original-path: relative path within sync folder
x-amz-meta-sahara-modified-at:   local file mtime at upload time (ISO 8601 UTC)
x-amz-meta-sahara-version:       "1" (spec version, for future migration)
```

Objects without `x-amz-meta-sahara-checksum` are **foreign objects** — see init import flow (Section 4.1).

### 3.5 Encryption

**In transit**: HTTPS enforced by boto3.

**At rest (server-side)**: SSE-S3 enabled by default on all objects.

**Client-side (optional)**: AES-256-GCM before upload.
- Checksum computed on **plaintext** before encryption; stored in S3 metadata.
- Random 12-byte nonce per file per upload; prepended to ciphertext: `[nonce(12B)][ciphertext]`.
- PBKDF2-HMAC-SHA256 key derivation from passphrase (iterations: 600,000; salt: stored in `~/.sahara/kdf_salt`).

**Passphrase lifecycle**:
1. **Setup**: `sahara encryption setup` — prompts passphrase + confirmation, stores in OS keychain via `keyring`.
   Prints: *"WARNING: This passphrase cannot be recovered. Files encrypted with it cannot be decrypted if lost."*
2. **Missing keychain entry on new machine**: `sahara push` fails with: *"Client-side encryption enabled but no passphrase found. Run `sahara encryption setup` to configure."*
3. **Rotation**: `sahara encryption rotate` — prompts old + new passphrase, re-downloads and re-encrypts all Hot tier files, re-uploads. Cold (Glacier) files are NOT re-encrypted (inaccessible until restored). Warns: *"N files in Glacier will remain encrypted with the old passphrase until restored and re-archived."*
4. **Lost passphrase**: No recovery. All encrypted files permanently inaccessible. Documented in README and during setup.

### 3.6 AWS Authentication

Credential resolution order (boto3 standard chain, in order):
1. `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` env vars
2. `aws.profile` config key → `~/.aws/credentials` named profile
3. `aws.access_key_id` + `aws.secret_access_key` config file values (discouraged)
4. EC2 instance profile / ECS task role (for server use)

**Auth failure handling**:
- On startup/sync: validate credentials with `sts:GetCallerIdentity`. On failure: log error + print actionable message.
- In daemon: on auth failure, stop syncing, log warning, retry credential validation every 10 minutes (credentials may be refreshed externally, e.g. SSO).
- Expired session tokens: detected by `ExpiredTokenException`; daemon pauses and notifies user to refresh credentials.

### 3.7 Daemon / CLI Concurrency
- Advisory lock file: `~/.sahara/sync.lock` (via `filelock` library)
- CLI command `sync`/`push`/`pull` without `--wait`: if lock is held, prints `"Sync already in progress (PID XXXXX). Use --wait to queue."` and exits with code 1.
- `--wait`: polls until lock is released (max 10 minutes), then acquires and runs.
- Daemon holds lock only for the duration of each sync operation; releases between operations.

---

## 4. CLI Specification

### 4.1 `sahara init` — Detailed Flow

```
sahara init [--bucket=NAME] [--region=REGION] [--folder=PATH] [--non-interactive]
```

Step-by-step:
1. **Credential detection**: Auto-detect from env vars / `~/.aws/credentials`. If not found, prompt: `[a] AWS profile name  [b] Enter access key + secret  [c] Use instance role`.
2. **Credential validation**: `sts:GetCallerIdentity`. Print account ID + ARN on success. Fail with specific error on invalid credentials.
3. **Bucket selection**: Prompt for bucket name (or `--bucket`).
4. **Bucket check**:
   - **Not found**: Offer to create. On yes: `s3:CreateBucket` → Block Public Access → enable SSE-S3 → set lifecycle rule (abort incomplete multipart uploads after 7 days).
   - **Found, empty**: Use as-is.
   - **Found, has Sahara objects** (any object with `x-amz-meta-sahara-checksum`): Offer import. Import: `ListObjectsV2` paginated → populate `files` table from metadata (`sha256_checksum`, `tier` from StorageClass, `remote_modified_at` from metadata, `s3_etag`).
   - **Found, has non-Sahara objects**: Warn. Offer: [a] skip foreign objects (default), [b] import with `sha256_checksum=NULL` (flagged for `--verify` on next sync), [c] abort.
5. **Folder**: Prompt for local sync folder (default: `~/Sahara`); create if absent.
6. **Client-side encryption**: Prompt `Enable client-side AES-256 encryption? [y/N]`. If yes: `sahara encryption setup` sub-flow.
7. **Write config**: `~/.sahara/config.toml`.
8. **Write `.saharaignore` template** in sync folder.
9. **Run `sahara doctor`** automatically.
10. **Print summary**: bucket, region, folder, encryption status, object count if imported.

### 4.2 `sahara doctor [--repair]`

Reports pass/fail for each check:
- [ ] AWS credentials valid (`sts:GetCallerIdentity`)
- [ ] Bucket exists and accessible (`s3:HeadBucket`)
- [ ] IAM permissions: `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`, `s3:ListBucket`, `s3:RestoreObject`, `s3:AbortMultipartUpload`, `sts:GetCallerIdentity`
- [ ] Bucket region matches configured region
- [ ] Local sync folder exists, readable, writable
- [ ] State DB integrity (`PRAGMA integrity_check`)
- [ ] Pending incomplete multipart uploads (list with ages)
- [ ] Available disk space vs. estimated download size

`--repair`: on corrupted DB → rename `state.db` to `state.db.bak.YYYYMMDD-HHMMSS` → rebuild from S3 listing.

### 4.3 Full Command Reference

```
# Setup & Config
sahara init [--bucket] [--region] [--folder] [--non-interactive]
sahara doctor [--repair]
sahara encryption setup                          Configure client-side encryption passphrase
sahara encryption rotate                         Rotate passphrase; re-encrypts Hot files
sahara config set <key> <value>
sahara config get <key>
sahara config show

# Sync Operations
sahara sync  [--dry-run] [--verify] [--wait]     Bidirectional sync
sahara push  [path] [--dry-run] [--verify] [--wait]
sahara pull  [path] [--dry-run] [--verify] [--wait]
sahara status                                    Show pending changes (read-only)
sahara diff  [path]                              Show diff metadata

# Conflict Management
sahara conflicts                                 List files with unresolved conflicts
sahara resolve <path> --keep=local|remote|backup Resolve a specific conflict

# File Operations
sahara ls   [path] [--tier=hot|cold|hot_temp] [--long]
sahara rm   <path> [--force]                     Delete remote (prompts unless --force)
sahara rm   <path> --local [--force]             Delete local copy only
sahara mv   <old-path> <new-path>               Rename/move in S3 (server-side copy+delete)

# Archive / Glacier
sahara archive <path|dir> [--older-than=DAYS] [--dry-run] [--force]
sahara restore <path> [--tier=bulk|standard|expedited] [--wait]
sahara restore-status <path>
sahara restore-download <path> [--overwrite]

# Information
sahara usage   [--simulate] [--month=YYYY-MM]
sahara history [path] [--limit=50]              Show sync_history log

# Daemon
sahara daemon start  [--on-login]
sahara daemon stop
sahara daemon status
sahara daemon pause                              Pause auto-sync (not persisted across restarts)
sahara daemon resume
sahara daemon logs   [--tail=50]
```

### 4.4 Command Behavioral Contracts

#### `sahara push` / `sahara pull` / `sahara sync`
- **push**: upload local changes only. Does NOT download. DOES check for conflicts; reports them without overwriting remote.
- **pull**: download remote changes only. Does NOT upload. Skips files where local checksum = remote checksum.
- **sync**: bidirectional; unified conflict resolution pass.
- None are "force" operations. All respect conflict strategy.

#### Three-way diff base
The **base** for three-way comparison is the last known state stored in `files` table (`sha256_checksum`, `s3_etag`, `last_sync_at`):
- **Local-only new**: path in filesystem, not in `files` table → upload
- **Remote-only new**: path in S3, not in `files` table → download
- **Both modified**: local SHA-256 ≠ `files.sha256_checksum` AND remote `x-amz-meta-sahara-checksum` ≠ `files.sha256_checksum` → conflict
- **Local modified only**: local SHA-256 ≠ `files.sha256_checksum`, remote unchanged → upload
- **Remote modified only**: remote metadata changed, local unchanged → download
- **Local deleted**: in `files` table, not on filesystem → delete remote (with `backup` strategy: prompt; with `manual`: report)
- **Remote deleted**: in `files` table, not in S3 → see remote delete policy

**First sync on re-init with imported state**: `files.sha256_checksum` populated from metadata = valid base for diff. `files.sha256_checksum=NULL` (foreign objects) → `--verify` re-fetches remote checksum; without `--verify`, foreign objects are downloaded on first pull.

#### Remote delete policy (multi-machine sync)
File exists in `files` table + present locally + absent from S3:
- `newest-wins` or `backup`: treat remote delete as authoritative → delete local file, update `files.is_deleted=1`, log to `sync_history`. No prompt.
- `manual`: halt file, report: *"<path> was deleted remotely but exists locally. Run `sahara resolve <path> --keep=local` to re-upload or `--keep=remote` to delete locally."*

#### `sahara mv <old-path> <new-path>`
- Explicit server-side rename: `s3:CopyObject` old → new, then `s3:DeleteObject` old.
- Both paths must be in Hot tier. If `old-path` is in Cold tier: error "Cannot move archived file. Restore it first with `sahara restore <path>`."
- Updates `files` table: insert new record, soft-delete old record.
- Does NOT interact with rename detection in sync engine (rename detection is for implicit filesystem renames detected during `push`).

#### `sahara rm <path> [--force]`
- Deletes remote S3 object; sets `files.is_deleted=1`.
- Default: confirmation prompt `"Permanently delete <path> from remote? [y/N]"`.
- `--force`: skips prompt.
- If `tier=cold`: additional warning: *"This file is in Glacier Deep Archive. Deleting before 180 days of storage will still incur the full 180-day minimum charge (~$X). Proceed? [y/N]"* (must confirm separately; `--force` bypasses both prompts).

#### `sahara archive [--older-than=DAYS] [--dry-run] [--force]`
- With `<path>`: archives specific file or all Hot files recursively under a directory.
- `--older-than=DAYS`: archives all Hot files where `remote_modified_at` (i.e., `x-amz-meta-sahara-modified-at`) is older than N days. Requires no `<path>` argument (operates on full sync folder).
- `--dry-run`: lists candidates + estimated savings, no action.
- Pre-check: if file `remote_modified_at` < 180 days ago: warn + require `--force`.

#### `sahara restore-download <path> [--overwrite]`
- If local file exists and local checksum ≠ remote restored checksum: prompt *"Local file <path> differs from archived version. Overwrite? [y/N]"*.
- `--overwrite`: skips prompt.
- If local file exists and checksums match: skip with message *"Local copy already matches archive."*

#### `sahara resolve <path> --keep=local|remote|backup`
- `--keep=local`: push local to remote as canonical; clear conflict state.
- `--keep=remote`: download remote to local; clear conflict state.
- `--keep=backup`: download remote as `<filename>.conflict-YYYYMMDD-HHMMSS.<ext>` (LOCAL ONLY — not pushed to S3). Push local as canonical remote. Add conflict file to `.saharaignore` automatically.

### 4.5 Configuration File (`~/.sahara/config.toml`)
```toml
[aws]
profile = ""                    # Preferred: AWS profile name
access_key_id = ""              # Discouraged; prefer env var or profile
secret_access_key = ""          # Discouraged; prefer env var or profile
region = "us-east-1"
bucket = ""                     # Required
sse = "SSE-S3"                  # "SSE-S3" | "SSE-KMS" | "none"
kms_key_id = ""                 # Required if sse = "SSE-KMS"

[sync]
folder = "~/Sahara"             # Absolute path or ~ expansion
exclude = []                    # Additional gitignore-syntax patterns
auto_archive_days = 0           # 0 = disabled; days since remote_modified_at
conflict_strategy = "backup"    # "newest-wins" | "manual" | "backup"
bandwidth_limit_kbps = 0        # 0 = unlimited
debounce_seconds = 5

[encryption]
client_side = false
# Passphrase in OS keychain; NEVER stored in this file
kdf_salt_path = "~/.sahara/kdf_salt"  # PBKDF2 salt file

[restore]
default_tier = "bulk"           # "bulk" | "standard" | "expedited"
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
1. **Acquire lock** (`~/.sahara/sync.lock`)
2. **Scan local**: Walk sync folder; skip files matching `.saharaignore` / `sync.exclude`. For each file: if `mtime` or `size` changed since `files.last_sync_at` → recompute SHA-256; else use `files.sha256_checksum`.
3. **Fetch remote index**: `s3:ListObjectsV2` paginated. Retrieve `StorageClass`, `ETag`, `LastModified`, and `x-amz-meta-sahara-*` metadata for each object.
4. **Three-way diff**: Classify each path as: unchanged | local-new | remote-new | local-modified | remote-modified | conflict | local-deleted | remote-deleted (using `files` table as base — see Section 4.4).
5. **Rename detection**: Match local-deleted + local-new pairs by SHA-256; promote to `move` operation.
6. **Resolve conflicts**: Apply `conflict_strategy`.
7. **Execute** (concurrent, up to `max_concurrent_uploads/downloads`):
   - Uploads: new/modified local files
   - Downloads: remote-new / remote-modified (non-conflict)
   - Moves: `s3:CopyObject` + `s3:DeleteObject`
   - Remote deletes: per policy
8. **Update state DB**: Write outcomes to `files` and `sync_history`.
9. **Release lock**

### 5.2 Change Detection & Timestamps
- **Local change**: `os.stat().st_mtime` or `st_size` changed since `files.last_sync_at` → recompute SHA-256.
- **Remote change**: `x-amz-meta-sahara-checksum` in S3 metadata differs from `files.sha256_checksum`.
- **Conflict authority**: `x-amz-meta-sahara-modified-at` is the authoritative timestamp for `newest-wins`. This is the local file mtime captured at upload time — immune to clock skew at sync time or across machines.
- **`--verify` mode**: Recomputes all local SHA-256s regardless of mtime. Use after migration or for integrity audit.

### 5.3 Conflict Resolution

| Strategy | Behavior |
|----------|----------|
| `newest-wins` | Higher `x-amz-meta-sahara-modified-at` wins; loser discarded silently |
| `manual` | File skipped; appears in `sahara conflicts`; resolved via `sahara resolve` |
| `backup` | Remote → local `.conflict-TIMESTAMP` (local only, NOT in S3); local → remote canonical |

Conflict backup files: auto-added to `.saharaignore`; surfaced in `sahara status` with warning.

### 5.4 Rename / Move Handling
1. Collect: local-deleted paths (A) + local-new paths (B)
2. Pair by matching SHA-256 checksum
3. For each pair: `s3:CopyObject(A→B)`, `s3:DeleteObject(A)` — no content re-upload
4. Unmatched: delete A, upload B as new

### 5.5 Large File Support
- Multipart upload for files > `multipart_threshold_mb`
- Parts tracked in `pending_multipart` table
- Resume: `s3:ListParts` to find completed parts; upload remaining
- Network timeout during multipart: mark as interrupted in state DB; resume on next sync cycle (do NOT fail the overall sync run)
- S3 lifecycle rule (set by `sahara init`): auto-abort incomplete multipart after 7 days

### 5.6 Retry Policy

| Error | Policy |
|-------|--------|
| Network timeout / connection error | Retry 5× with exponential backoff: 2, 4, 8, 16, 32s + jitter |
| S3 503 / slow down | Retry 5× with random jitter, cap 60s |
| S3 4xx (403, 404) | Fail immediately; log; no retry |
| Multipart interrupted | Mark pending; resume next sync cycle |
| Disk full | Stop sync immediately; send daemon notification |

### 5.7 Exclusion Patterns

Two sources merged (both always applied):
1. `.saharaignore` in sync root (gitignore syntax; per-directory support)
2. `sync.exclude` in config (gitignore syntax; global)

Built-in defaults (not user-configurable, always excluded):
```
.DS_Store
Thumbs.db
desktop.ini
*.tmp
*.swp
~$*
.Trash-*
*.conflict-*
```

---

## 6. Glacier Archive Operations

### 6.1 Archive Flow
1. Verify `tier=hot` in state DB + `s3:HeadObject` confirms `StorageClass=STANDARD`
2. If within 180-day minimum: warn with estimated charge. Without `--force`: abort.
3. `s3:CopyObject` (same key, `StorageClass=DEEP_ARCHIVE`, same metadata)
4. `s3:DeleteObject` original
5. Update `files`: `tier=cold`, `archived_at=now`
6. Log to `sync_history`

### 6.2 Restore Flow

**Initiate** (`sahara restore <path> [--tier=bulk] [--wait]`):
- Verify `tier=cold`
- If `tier=hot_temp` and not expired: *"File already restored, available until <date>. Run `sahara restore-download <path>` to download."*
- `s3:RestoreObject(Days=temp_expiry_days, Tier=tier)`
- Store request info in `files.restore_job_id`
- Print ETA based on tier
- `--wait`: poll `s3:HeadObject` every 30 min; print progress dots; desktop notification on complete

**Status** (`sahara restore-status <path>`):
- `s3:HeadObject`; parse `Restore` header: `ongoing-request="true"` | `ongoing-request="false", expiry-date="..."` | not present (not initiated/expired)
- Display status + expiry date if available

**Download** (`sahara restore-download <path> [--overwrite]`):
- Check restore complete (print ETA and exit with error code 1 if not)
- Handle existing local file (see Section 4.4)
- Download to sync folder
- Update `files`: `tier=hot_temp`, `restore_expires_at=expiry-date from Restore header`

### 6.3 Restore Expiry Handling
- Daemon polls daily: check all `hot_temp` files
  - 24h before `restore_expires_at`: desktop notification + log
  - After `restore_expires_at`: `files.tier=cold`; local copy flagged as "local only (archive expired)" in `sahara status`

### 6.4 Restore Tiers
| Tier | Speed | Cost/GB |
|------|-------|---------|
| Bulk | 12-48h | ~$0.0025 |
| Standard | 3-5h | ~$0.01 |
| Expedited | 1-5min | ~$0.03 |

---

## 7. Daemon / Background Sync

### 7.1 Daemon Operation
- File watching: `watchdog` (inotify/Linux, FSEvents/macOS, ReadDirectoryChangesW/Windows)
- Debounce: 5s window; on file event, sync only affected paths
- Scheduled full sync: every 6 hours
- Restore polling: every 30 minutes; `s3:HeadObject` for all pending `restore_job_id` entries
- Notifications: `plyer` (cross-platform desktop notifications)
- Logs: `~/.sahara/daemon.log` (rotating, 10MB max, keep 3 files)
- PID: `~/.sahara/daemon.pid`
- Pause: `~/.sahara/daemon.paused` flag file (does NOT persist across `daemon start`)
- Auth failure: log + notify; retry credential validation every 10 minutes

### 7.2 Network Awareness
- Test S3 reachability before sync; back off if unreachable
- Bandwidth throttle: `sync.bandwidth_limit_kbps`
- `sahara daemon pause` / `sahara daemon resume`

---

## 8. Cost Estimation

### 8.1 `sahara usage` Output
```
Sahara Usage Report — March 2026
═══════════════════════════════════════════════════════════

Storage (current):
  Hot (S3 Standard):        45.3 GB   ~$1.04/month
  Cold (Glacier Deep):     892.1 GB   ~$0.88/month
  Total:                   937.4 GB   ~$1.92/month

  vs Google One 1TB: $9.99/mo  |  iCloud+ 1TB: $9.99/mo
  Estimated savings: ~$8/month

API Requests (this month):
  PUT/COPY:                 1,203       ~$0.006
  GET:                        456       ~$0.002
  Glacier retrievals:          12       ~$0.001

Data Transfer (this month):
  Upload:                   2.3 GB    Free
  Download:                 0.8 GB    ~$0.07
  ⚠ Egress: $0.09/GB after 100 GB/month free

Estimated Monthly Total:    ~$2.00

Run 'sahara usage --simulate' for 3-month projection.
```

---

## 9. Error Handling & Resilience

| Scenario | Behavior |
|----------|----------|
| Network timeout | Retry 5× exponential backoff (2,4,8,16,32s + jitter) |
| S3 rate limit | Retry 5× jitter, cap 60s |
| 403 Forbidden | Fail; log required IAM permission |
| Bucket not found | Fail; "Run `sahara doctor`" |
| State DB corrupt | Rename; offer `sahara doctor --repair` |
| Disk full | Stop; notify daemon |
| File locked (Windows) | Skip; retry next cycle |
| Multipart interrupted | Mark pending; resume next cycle |
| Auth expiry | Daemon pauses; notify user; retry every 10 min |
| Missing encryption key | Fail; "Run `sahara encryption setup`" |
| Glacier 180-day early delete | Warn + require `--force` |

All errors logged: timestamp, operation, path, error code, stack trace → `~/.sahara/error.log` (rotating 10MB, keep 5)

---

## 10. Security

- AWS credentials: prefer profile or env vars; config stores only profile name
- Client-side passphrase: OS keychain only (`keyring`); never in config
- S3 bucket: Block Public Access enabled on creation
- All HTTPS via boto3
- IAM role auth supported
- Minimum IAM policy documented in README

---

## 11. Performance Targets

| Metric | Target | Measurement Conditions |
|--------|--------|----------------------|
| Sync latency (daemon event → upload start) | < 10s | Single file change, 100 Mbps connection |
| Full local scan (100k files) | < 30s | Average 1 KB–10 MB file mix, SSD |
| S3 listing (100k objects) | < 60s | Paginated ListObjectsV2 |
| Daemon idle memory | < 50MB RSS | macOS/Linux, watching 100k file folder |
| State DB lookup by path | < 10ms | Indexed query, 100k rows |
| Concurrent transfers | 4 (configurable 1–16) | — |

---

## 12. Technology Stack

| Component | Library |
|-----------|---------|
| Language | Python 3.11+ |
| AWS SDK | boto3 |
| CLI | Click |
| SQLite | sqlite3 (stdlib) + direct SQL |
| File watching | watchdog |
| Config | tomllib (stdlib 3.11+) |
| Encryption | cryptography (PyCA) |
| Keychain | keyring |
| Notifications | plyer |
| File locking | filelock |
| Testing | pytest + moto |
| Coverage | pytest-cov |
| Packaging | pyproject.toml / pip |

---

## 13. Project File Structure

```
sahara/
├── pyproject.toml
├── README.md
├── .saharaignore.template
├── src/
│   └── sahara/
│       ├── __init__.py
│       ├── cli.py              # Click CLI: all commands
│       ├── config.py           # TOML config read/write; defaults
│       ├── sync_engine.py      # Three-way diff, sync algorithm
│       ├── s3_client.py        # boto3 S3/Glacier wrapper
│       ├── state_db.py         # SQLite CRUD + schema migration
│       ├── file_watcher.py     # watchdog handler; debounce logic
│       ├── daemon.py           # Daemon process management
│       ├── encryption.py       # AES-256-GCM + PBKDF2 + keychain
│       ├── cost_estimator.py   # Cost calculation + formatting
│       ├── ignore_rules.py     # .saharaignore / gitignore matching
│       ├── notifier.py         # plyer desktop notifications
│       └── models.py           # Dataclasses: FileRecord, SyncOperation, etc.
└── tests/
    ├── conftest.py             # pytest fixtures, moto setup, temp dirs
    ├── test_sync_engine.py     # Three-way diff, conflict, rename detection
    ├── test_s3_client.py       # Upload, download, archive, restore, mv
    ├── test_state_db.py        # CRUD, schema, migrations
    ├── test_cli.py             # CLI command integration tests
    ├── test_config.py          # Config read/write, defaults, validation
    ├── test_encryption.py      # Encrypt/decrypt, key derivation, keychain
    ├── test_cost_estimator.py  # Cost calculations
    ├── test_ignore_rules.py    # Pattern matching
    └── test_daemon.py          # Daemon lifecycle, pause/resume
```

---

## 14. Resolved Design Decisions

| # | Decision |
|---|----------|
| 1 | No multiple sync folders in v1 |
| 2 | Glacier restore: always manual |
| 3 | No pre-signed URL sharing in v1 |
| 4 | No file versioning in v1; `sync_history` is audit-only |
| 5 | Rename detection via SHA-256 matching |
| 6 | Conflict timestamp: `x-amz-meta-sahara-modified-at` (upload-time mtime) |
| 7 | Daemon + CLI: advisory `sync.lock`; `--wait` to queue |
| 8 | Orphaned multipart: S3 lifecycle rule (7 days) + `sahara doctor` lists them |
| 9 | Glacier 180-day minimum: warn + `--force` required |
| 10 | Conflict backup files: local-only, not pushed to S3 |
| 11 | Remote delete (multi-machine): treated as authoritative for newest-wins/backup; halted for manual |
| 12 | `hot_temp` + local modification: promoted to permanent `hot` on next push |
| 13 | Daemon pause: NOT persisted across daemon restarts |
| 14 | `--older-than`: measured against `x-amz-meta-sahara-modified-at` |
| 15 | `sahara mv` on Cold tier: not allowed; restore first |
| 16 | Client-side encryption passphrase: PBKDF2-derived; in keychain; rotation re-encrypts Hot only |
| 17 | Three-way diff base: `files` table (`sha256_checksum`, `last_sync_at`) |
| 18 | Credential auth in daemon: pause on auth failure; retry every 10 min |
