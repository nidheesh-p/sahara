# Sahara Cloud Storage — Product Specification v5.0

## 1. Overview

**Sahara** is a personal, self-hosted cloud storage system built on AWS S3 providing a Dropbox-like experience without monthly subscription costs. Users pay only for what they store/transfer directly to AWS.

### 1.1 Cost Comparison
| Service | 2TB/month |
|---------|-----------|
| Google One | $9.99 |
| iCloud+ | $9.99 |
| Sahara (mixed Hot+Cold) | ~$5–8 |
| Sahara (2TB Cold archive) | ~$2.00 |

### 1.2 Goals
- Bidirectional file sync between local folder and S3
- Two storage tiers: Hot (S3 Standard) and Cold (Glacier Deep Archive)
- Fast, intuitive CLI
- Incremental sync via checksums
- Single user, multiple machines

### 1.3 Non-Goals (v1)
- Multi-user, Web UI, Mobile apps
- Real-time collaboration / locking
- Full POSIX filesystem semantics
- File versioning (overwritten files are NOT recoverable)
- Automatic Glacier restore on file access

---

## 2. User Stories & Acceptance Criteria

| ID | Story | Acceptance Criteria |
|----|-------|---------------------|
| US-01 | Sync local folder to S3 | `push` uploads all new/modified; re-run on unchanged = zero uploads |
| US-02 | Download from any machine | `pull` on fresh machine downloads all; identical files skipped |
| US-03 | Archive to Glacier | `archive` moves to DEEP_ARCHIVE; appears in `ls --tier=cold` |
| US-04 | Restore from Glacier | `restore` initiates job; daemon notifies on completion |
| US-05 | List files with metadata | `ls` shows size, tier, modified date in table |
| US-06 | Conflict detection | Both-sides-changed reported; no silent data loss |
| US-07 | Exclude patterns | `.saharaignore` files excluded; gitignore syntax |
| US-08 | Cost estimates | `usage` shows storage/requests/egress + projected total |
| US-09 | Incremental sync | Only SHA-256-changed files transferred |
| US-10 | Encryption | HTTPS; SSE-S3 default; optional AES-256-GCM client-side |
| US-11 | Efficient rename/move | S3 copy+delete, no re-upload |
| US-12 | Safe deletion | `rm` prompts confirmation; `--force` to skip |
| US-13 | Restore notification | Daemon desktop notification on Glacier restore complete |

---

## 3. Architecture

### 3.1 System Diagram
```
┌─────────────────────────────────────────────────────────┐
│                     LOCAL MACHINE                        │
│  ┌──────────────┐    ┌──────────────────────────────┐   │
│  │  Sync Folder  │◄──►│     Sahara CLI / Daemon       │   │
│  │  (watched)    │    │  - FileWatcher (watchdog)    │   │
│  └──────────────┘    │  - SyncEngine                │   │
│                       │  - StateDB (SQLite WAL)      │   │
│                       │  - S3Client (boto3)          │   │
│                       │  - FileLock (filelock)       │   │
│                       └──────────────┬───────────────┘   │
└──────────────────────────────────────┼───────────────────┘
                                        │ HTTPS (TLS)
┌──────────────────────────────────────▼───────────────────┐
│                       AWS S3 Bucket                        │
│   Hot objects:  StorageClass=STANDARD                      │
│   Cold objects: StorageClass=DEEP_ARCHIVE                  │
│   .sahara/manifest.json  (remote state cache)              │
└───────────────────────────────────────────────────────────┘
```

### 3.2 Storage Strategy

| Tier | StorageClass | Retrieval | Cost |
|------|-------------|-----------|------|
| Hot | `STANDARD` | Instant | ~$0.023/GB/mo |
| Cold | `DEEP_ARCHIVE` | 12-48h | ~$0.00099/GB/mo |

**Egress**: $0.09/GB after 100 GB/month free.

### 3.3 Remote State Manifest

**Architecture decision (Eng Director feedback #1)**: `ListObjectsV2` does NOT return custom metadata. To avoid 100k `HeadObject` calls on every sync, Sahara maintains a single remote state file:

- Location: `s3://<bucket>/.sahara/manifest.json`
- Updated atomically after each successful sync (PUT with `StorageClass=STANDARD`)
- Downloaded at start of each sync; compared against local `files` table

**manifest.json schema**:
```json
{
  "version": 1,
  "updated_at": "2026-03-16T10:00:00Z",
  "files": {
    "relative/path/file.txt": {
      "sha256": "abc123...",
      "size": 1024,
      "tier": "hot",
      "modified_at": "2026-03-15T09:00:00Z",
      "etag": "\"d41d8cd98f00b204e9800998ecf8427e\""
    }
  }
}
```

**Consistency**: The manifest is written at the end of a sync. If a sync is interrupted before manifest update, the next sync re-fetches only objects not in the manifest via `HeadObject`. This is the only scenario requiring per-object `HeadObject` calls.

**First sync / bootstrap**: If `.sahara/manifest.json` does not exist, treat as bootstrap (see Section 4.1 init).

**Manifest size concern**: For 1M files, manifest ~200MB; acceptable given sync happens periodically. Compress with gzip if manifest > 10MB (store as `.sahara/manifest.json.gz`; detected by Content-Encoding).

### 3.4 Local State Database (SQLite)
Location: `~/.sahara/state.db`

**SQLite configuration** (applied at every connection):
```python
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;  # ms; prevents SQLITE_BUSY on concurrent access
PRAGMA synchronous = NORMAL;  # safe with WAL
PRAGMA foreign_keys = ON;
```

#### `files` table
```sql
CREATE TABLE files (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  relative_path       TEXT NOT NULL UNIQUE,
  sha256_checksum     TEXT,             -- pre-encryption plaintext; NULL = foreign object
  size_bytes          INTEGER,
  tier                TEXT NOT NULL
                      CHECK(tier IN ('hot','cold','hot_temp')),
  s3_etag             TEXT,             -- for out-of-band S3 change detection only; NOT used for integrity
  last_sync_at        TEXT,             -- ISO 8601 UTC; timestamp of last successful sync
  local_modified_at   TEXT,             -- local mtime at last sync (ISO 8601 UTC)
  remote_modified_at  TEXT,             -- x-amz-meta-sahara-modified-at at last sync
  archived_at         TEXT,
  restore_job_id      TEXT,
  restore_expires_at  TEXT,
  is_deleted          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_files_path ON files(relative_path);
CREATE INDEX idx_files_tier ON files(tier, is_deleted);
```

**`tier` state machine**:
```
hot ──archive──► cold
cold ──restore-initiate──► cold (restore pending; restore_job_id set)
cold (restore pending) ──restore-complete──► hot_temp
hot_temp ──expires──► cold  (restore_expires_at passed)
hot_temp ──user modifies + push──► hot  (promoted permanently)
```

**`s3_etag` note**: Stored for detecting out-of-band S3 modifications (e.g., another tool modified the object). NOT used as integrity check. For multipart uploads, ETag is `MD5(concat(part_MD5s))-N` and is not content-addressable. For encrypted objects, ETag is MD5 of ciphertext. **`sha256_checksum` is the sole integrity check.**

#### `sync_history` table
```sql
CREATE TABLE sync_history (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  operation         TEXT NOT NULL CHECK(operation IN ('upload','download','delete','archive','restore','move','skip')),
  path              TEXT NOT NULL,
  sha256_checksum   TEXT,
  s3_etag           TEXT,
  status            TEXT NOT NULL CHECK(status IN ('success','failed','skipped')),
  error_message     TEXT,
  started_at        TEXT NOT NULL,
  completed_at      TEXT,
  bytes_transferred INTEGER DEFAULT 0
);
CREATE INDEX idx_history_path ON sync_history(path, started_at DESC);
```

#### `pending_multipart` table
```sql
CREATE TABLE pending_multipart (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  relative_path TEXT NOT NULL UNIQUE,
  upload_id     TEXT NOT NULL,
  s3_key        TEXT NOT NULL,
  file_sha256   TEXT NOT NULL,   -- SHA-256 of file at upload start; used to detect file change during resume
  parts_json    TEXT DEFAULT '[]',  -- JSON: [{"PartNumber": 1, "ETag": "...", "StartByte": 0, "EndByte": 8388607}, ...]
  started_at    TEXT NOT NULL
);
```

**Multipart resume logic**:
1. Load `pending_multipart` entry
2. Recompute local file SHA-256; if differs from `file_sha256`: abandon old upload (`s3:AbortMultipartUpload`), delete `pending_multipart` row, start fresh
3. Call `s3:ListParts(upload_id)` — if `NoSuchUpload`: delete row, start fresh
4. Resume from first missing part number based on `parts_json`

#### `config` table
```sql
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

### 3.5 S3 Object Metadata
```
x-amz-meta-sahara-checksum:     SHA-256 of plaintext (pre-encryption) content
x-amz-meta-sahara-original-path: relative path within sync folder
x-amz-meta-sahara-modified-at:  local mtime at upload time (ISO 8601 UTC)
x-amz-meta-sahara-version:      "1"
```

Objects without `x-amz-meta-sahara-checksum` are **foreign objects** (not uploaded by Sahara). Imported with `sha256_checksum=NULL`; require `--verify` sync to establish checksum.

### 3.6 Encryption

**Server-side (default)**: SSE-S3 on all objects.

**Client-side (optional)**: AES-256-GCM.

**Key derivation**:
- Algorithm: PBKDF2-HMAC-SHA256
- Iterations: 600,000 (NIST 2023 recommendation)
- Salt: 32-byte random salt generated once at `sahara encryption setup`, stored in `config` table as `encryption.kdf_salt` (hex-encoded)
- Key length: 32 bytes

**Ciphertext binary layout** (exact byte format):
```
[nonce: 12 bytes random (os.urandom(12))]
[ciphertext: variable length]
[GCM authentication tag: 16 bytes]
```
Total overhead per file: 28 bytes.

**Checksum**: Computed on **plaintext** before encryption. Stored in `x-amz-meta-sahara-checksum`. This ensures incremental sync works correctly regardless of encryption.

**Passphrase lifecycle**:
1. **Setup** (`sahara encryption setup`): generate KDF salt, derive key, store passphrase in OS keychain via `keyring`. Print: *"WARNING: Lost passphrase = permanently inaccessible files. Store it securely (e.g., a password manager)."*
2. **Missing passphrase**: sync fails immediately: *"Client-side encryption enabled but passphrase not found. Run `sahara encryption setup`."*
3. **Rotation** (`sahara encryption rotate`): re-download all Hot files, re-encrypt with new key (new salt + new passphrase), re-upload. Cold files cannot be re-encrypted until restored. Prints: *"N Cold files will remain encrypted with old passphrase until restored."*
4. **Lost passphrase**: No recovery. Documented at setup and in README.

### 3.7 AWS Authentication

Credential resolution (boto3 chain):
1. `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env vars
2. `aws.profile` config → `~/.aws/credentials` named profile
3. `aws.access_key_id` + `aws.secret_access_key` in config (discouraged)
4. EC2 instance profile / ECS task role

**Auth failure in daemon**: Stop syncing; desktop notification: *"Sahara: AWS credentials expired or invalid. Re-authenticate and run `sahara doctor`."*; retry validation every 10 minutes. Daemon stays resident (does not exit).

### 3.8 Daemon / CLI Concurrency

**Lock**: `~/.sahara/sync.lock` via `filelock` (POSIX `fcntl` on Unix, Windows lockfile).
- Protects: full sync operations (scan + diff + execute)
- NOT held: between sync cycles (daemon idle)
- Daemon idle does NOT hold lock; CLI `sync` can run between daemon cycles

**SQLite WAL** (Section 3.4): Handles multiple readers + one writer concurrently. The filelock prevents two concurrent write-heavy sync operations. Read-only commands (`ls`, `status`, `history`) do NOT acquire the sync lock.

**Stale lock detection**: On lock acquisition attempt, if lock file exists and PID in file is not a running process → stale lock → delete and acquire.

---

## 4. CLI Specification

### 4.1 `sahara init` Flow

```
sahara init [--bucket=NAME] [--region=REGION] [--folder=PATH] [--non-interactive]
```

1. **Credentials**: Auto-detect from env/profile. If not found, prompt for method.
2. **Validate**: `sts:GetCallerIdentity`. Print account ID + ARN.
3. **Bucket**: Prompt for name. Check existence:
   - Not found → offer to create (Block Public Access ON, SSE-S3 ON, multipart lifecycle rule 7 days, versioning OFF)
   - Found, empty → use as-is
   - Found, has `.sahara/manifest.json` → download manifest, import into `files` table (bootstrap from manifest)
   - Found, no manifest, has Sahara-tagged objects → offer `HeadObject` import (expensive for large buckets; warn)
   - Found, no manifest, non-Sahara objects → warn; import with `sha256_checksum=NULL` or skip
4. **Bootstrap flow** (first sync from existing data):
   - All remote objects imported as `tier=hot` (or Cold based on StorageClass), `is_deleted=0`
   - `sha256_checksum` populated from `x-amz-meta-sahara-checksum` if present; else NULL
   - On first `sahara push` after bootstrap: three-way diff sees local files as "local-new" vs base=empty or base=manifest. Engineer note: when manifest is imported, local files matching manifest sha256 are "unchanged"; only divergent files are uploaded.
5. **Folder**: Prompt (default: `~/Sahara`); create if absent.
6. **Encryption**: Prompt `Enable client-side AES-256 encryption? [y/N]`. If yes: run passphrase setup sub-flow.
7. **Write** `~/.sahara/config.toml`; write `.saharaignore` template.
8. **Run** `sahara doctor`.
9. **Print summary**.

### 4.2 `sahara doctor [--repair]`

Checks (each reported pass/fail):
- AWS credentials valid (`sts:GetCallerIdentity`)
- Bucket accessible (`s3:HeadBucket`)
- IAM permissions (test with `s3:HeadObject` on a test key):
  - Required: `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket`, `s3:HeadObject`, `s3:CopyObject`, `s3:RestoreObject`, `s3:AbortMultipartUpload`, `s3:ListMultipartUploadParts`, `sts:GetCallerIdentity`
- Bucket region matches configured
- No versioning, no Object Lock (both unsupported by Sahara)
- Sync folder accessible
- SQLite integrity (`PRAGMA integrity_check`)
- Pending stale multipart uploads (list items older than 7 days)
- Available disk space

`--repair`: rename corrupted DB; rebuild from S3 manifest (or `ListObjectsV2` + `HeadObject` if manifest missing).

### 4.3 Bucket Requirements

| Setting | Required Value |
|---------|---------------|
| Block Public Access | ON (all 4 options) |
| Versioning | OFF (Sahara does not support versioned buckets in v1) |
| Object Lock | OFF (DeleteObject calls would fail) |
| SSE | SSE-S3 or SSE-KMS (client preference) |
| Multipart lifecycle rule | Abort incomplete uploads after 7 days |

**Minimum IAM policy** (documented in README):
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
      "s3:ListBucket", "s3:HeadObject", "s3:CopyObject",
      "s3:RestoreObject", "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts", "s3:GetObjectRestoreStatus"
    ],
    "Resource": ["arn:aws:s3:::BUCKET-NAME", "arn:aws:s3:::BUCKET-NAME/*"]
  }, {
    "Effect": "Allow",
    "Action": ["sts:GetCallerIdentity"],
    "Resource": "*"
  }]
}
```

### 4.4 Full Command Reference

```
# Setup
sahara init [--bucket] [--region] [--folder] [--non-interactive]
sahara doctor [--repair]
sahara encryption setup
sahara encryption rotate
sahara config set <key> <value>
sahara config get <key>
sahara config show

# Sync
sahara sync  [--dry-run] [--verify] [--wait]
sahara push  [path] [--dry-run] [--verify] [--wait]
sahara pull  [path] [--dry-run] [--verify] [--wait]
sahara status
sahara diff  [path]

# Conflict Management
sahara conflicts
sahara resolve <path> --keep=local|remote|backup

# File Operations
sahara ls   [path] [--tier=hot|cold|hot_temp] [--long]
sahara rm   <path> [--force]
sahara rm   <path> --local [--force]
sahara mv   <old-path> <new-path>

# Archive / Glacier
sahara archive <path|dir> [--older-than=DAYS] [--dry-run] [--force]
sahara restore <path> [--tier=bulk|standard|expedited] [--wait]
sahara restore-status <path>
sahara restore-download <path> [--overwrite]

# Information
sahara usage   [--simulate] [--month=YYYY-MM]
sahara history [path] [--limit=50]

# Daemon
sahara daemon start  [--on-login]
sahara daemon stop
sahara daemon status
sahara daemon pause   # sets ~/.sahara/daemon.paused; NOT persisted across daemon restart
sahara daemon resume
sahara daemon logs   [--tail=50]
```

### 4.5 Command Behavioral Contracts

#### `push` / `pull` / `sync`
- `push`: upload only; check for conflicts but do not overwrite remote.
- `pull`: download only; skip files where local SHA-256 = manifest SHA-256.
- `sync`: bidirectional; unified conflict resolution.
- None are "force" operations.

#### Three-way diff base
Base = `files` table (`sha256_checksum`, `last_sync_at`):
- **local-new**: in filesystem, NOT in `files` table → upload
- **remote-new**: in manifest, NOT in `files` table → download
- **local-modified**: local SHA-256 ≠ `files.sha256_checksum`; remote unchanged → upload
- **remote-modified**: manifest SHA-256 ≠ `files.sha256_checksum`; local unchanged → download
- **conflict**: both local AND remote changed from base → apply conflict strategy
- **local-deleted**: in `files` table (not is_deleted), absent locally → delete remote
- **remote-deleted**: in `files` table, absent from manifest → see remote-delete policy
- **empty DB bootstrap**: treat all manifest entries as "remote-new" → download all; treat all local files as "local-new" → upload all that aren't already in manifest

#### Remote delete policy
File in `files` table + present locally + absent from manifest:
- `newest-wins`/`backup`: remote delete is authoritative; delete local, set `is_deleted=1`, log
- `manual`: halt; report; require `sahara resolve`

#### `sahara mv <old> <new>`
- Both paths must be `tier=hot`. Cold: error "Restore first with `sahara restore <path>`."
- `s3:CopyObject` old→new, `s3:DeleteObject` old
- Update `files`: insert new record, set old `is_deleted=1`
- Distinct from implicit rename detection during sync

#### `sahara rm <path> [--force]`
- Deletes S3 object; sets `files.is_deleted=1`
- Prompts unless `--force`
- If `tier=cold`: additional warning about 180-day minimum charge; `--force` bypasses all prompts

#### `sahara archive [--older-than=DAYS] [--dry-run] [--force]`
- `--older-than=DAYS`: measured against `remote_modified_at` (from manifest, i.e., upload-time mtime)
- `--dry-run`: list candidates + savings; no action
- Pre-check: `archived_at` or `remote_modified_at` within 180 days → warn + require `--force`

#### `sahara restore-download <path> [--overwrite]`
- Restore must be complete (status check via manifest or `HeadObject`)
- If local file exists and SHA-256 differs: prompt unless `--overwrite`
- If SHA-256 matches: skip with *"Local copy already matches archive."*

#### `sahara resolve <path> --keep=local|remote|backup`
- `--keep=local`: push local as canonical; clear conflict state
- `--keep=remote`: download remote; clear conflict state
- `--keep=backup`: download remote as `<filename>.conflict-MACHINEID-YYYYMMDD-HHMMSS.<ext>` (local only, NOT pushed to S3); push local as canonical. Auto-add conflict file pattern to `.saharaignore`.

#### Conflict naming
Conflict copies named: `<stem>.conflict-<hostname>-<YYYYMMDD-HHMMSS>.<ext>`
Example: `report.conflict-macbook-pro-20260316-142300.docx`
These conflict files are surfaced in `sahara status` and `sahara conflicts`. Auto-excluded from sync via `.saharaignore` entry.

### 4.6 Conflict Resolution Detail

**Conflict detection condition**: local SHA-256 ≠ `files.sha256_checksum` AND manifest SHA-256 ≠ `files.sha256_checksum`

**Timestamp tolerance**: Timestamps within 2 seconds of each other are considered simultaneous. For simultaneous conflicts, `backup` strategy is always applied regardless of configured strategy (to prevent silent data loss).

**Conflict authority** (`newest-wins`): `x-amz-meta-sahara-modified-at` (upload-time local mtime). This is immune to sync-time clock skew. Tiebreaker for identical timestamps: prefer remote (download).

**`manual` mode UX**:
```
CONFLICT: documents/report.docx
  Local version:  modified 2026-03-16 14:20:00 UTC  (sha256: abc123...)
  Remote version: modified 2026-03-16 14:20:05 UTC  (sha256: def456...)

  Run: sahara resolve documents/report.docx --keep=local|remote|backup
```

### 4.7 Configuration (`~/.sahara/config.toml`)
```toml
[aws]
profile = ""
access_key_id = ""        # prefer env var or profile
secret_access_key = ""    # prefer env var or profile
region = "us-east-1"
bucket = ""
sse = "SSE-S3"            # "SSE-S3" | "SSE-KMS" | "none"
kms_key_id = ""

[sync]
folder = "~/Sahara"
exclude = []              # gitignore-syntax patterns
auto_archive_days = 0     # 0 = disabled
conflict_strategy = "backup"
bandwidth_limit_kbps = 0  # 0 = unlimited
debounce_seconds = 5

[encryption]
client_side = false
# kdf_salt stored in config table in state.db; passphrase in OS keychain

[restore]
default_tier = "bulk"
temp_expiry_days = 7
notify_on_complete = true
max_poll_hours = 72       # max time to wait for restore before marking failed

[performance]
multipart_threshold_mb = 100
multipart_part_size_mb = 8
max_concurrent_uploads = 4    # ThreadPoolExecutor workers
max_concurrent_downloads = 4
```

---

## 5. Sync Engine

### 5.1 Sync Algorithm
1. **Acquire lock** (`~/.sahara/sync.lock`; stale PID check; --wait polls)
2. **Download manifest** (`s3:GetObject .sahara/manifest.json`; decompress if gzip)
3. **Scan local**: walk sync folder; skip `.saharaignore` matches; for each file: if `mtime` or `size` changed since `last_sync_at` → recompute SHA-256; else use `files.sha256_checksum`
4. **Three-way diff** (base = `files` table; see Section 4.5)
5. **Rename detection**: match local-deleted + local-new by SHA-256. Disambiguation: prefer pairs sharing parent directory or filename stem. If multiple ambiguous matches: treat as separate delete + upload.
6. **Conflict resolution**: apply `conflict_strategy`. Simultaneous timestamps (within 2s): always `backup`.
7. **Execute** via `ThreadPoolExecutor(max_workers=max_concurrent_uploads)`:
   - Uploads: `PutObject` (<100MB) or multipart (≥100MB)
   - Downloads: `GetObject` to temp file → atomic move to final path
   - Moves: `CopyObject` + `DeleteObject`
   - Deletes: `DeleteObject`
   - Dequeue strategy: operations queued; when all workers busy, new operations wait in queue
8. **Update manifest**: rebuild manifest from `files` table; `PutObject .sahara/manifest.json`
9. **Update state DB**: `files` table and `sync_history`
10. **Release lock**

**S3 prefix distribution**: Object keys are stored as `<relative_path>` without sharding. For buckets expected to exceed 1M objects, a 2-char hash prefix is recommended (documented in README) but not enforced by Sahara.

### 5.2 Large File Support (Multipart)
- Threshold: `multipart_threshold_mb` (default 100MB)
- Part size: `multipart_part_size_mb` (default 8MB)
- `parts_json` schema: `[{"PartNumber": 1, "ETag": "...", "StartByte": 0, "EndByte": 8388607}]`
- Resume logic:
  1. Recompute file SHA-256; if ≠ `pending_multipart.file_sha256`: abort upload, delete row, start fresh
  2. `s3:ListParts(upload_id)`: if `NoSuchUpload` → delete row, start fresh
  3. Upload parts from first gap in `parts_json`
- On interruption: mark in `pending_multipart`, move to next file. Do NOT fail overall sync.
- S3 lifecycle rule (7-day abort) must be set during `sahara init`.

### 5.3 Exclusion Patterns

**Library**: `pathspec` (gitignore-compatible pattern matching).

**Sources** (both applied, merged):
1. `.saharaignore` in sync root only (single file; NOT per-directory in v1)
2. `sync.exclude` in config (global patterns)

**Built-in excludes** (non-configurable):
```
.DS_Store, Thumbs.db, desktop.ini, *.tmp, *.swp, ~$*, .Trash-*, *.conflict-*
```

**Behavior for already-synced files added to `.saharaignore`**: File remains in S3; not deleted by default. Use `sahara rm <path>` to explicitly remove.

**`.saharaignore` itself**: Never synced to S3.

**Negation patterns** (`!`): Supported (pathspec handles this).

### 5.4 Retry Policy

| Error | Policy |
|-------|--------|
| Network timeout / connection reset | Retry 5× exponential: 2,4,8,16,32s + jitter |
| S3 503 SlowDown | Retry 5× with jitter, cap 60s |
| S3 4xx (403, 404) | Fail immediately; log; no retry |
| Multipart interrupted | Mark `pending_multipart`; skip file; resume next sync |
| Disk full | Stop sync; daemon notification |
| AWS `ExpiredTokenException` | Stop sync; daemon notification; daemon retries auth every 10 min |

---

## 6. Glacier Archive Operations

### 6.1 Archive Flow
1. Verify `tier=hot`; confirm not already cold
2. Pre-check: if `remote_modified_at` < 180 days ago: warn + `--force` required (not just warn)
3. `s3:CopyObject` (same key, `StorageClass=DEEP_ARCHIVE`, same metadata)
4. `s3:DeleteObject` original
5. Update `files`: `tier=cold`, `archived_at=now`; update manifest; log `sync_history`

### 6.2 Restore Flow

**Initiate** (`sahara restore <path> [--tier=bulk] [--wait]`):
- Verify `tier=cold`
- If `tier=hot_temp` + not expired: print *"Already restored, expires <date>. Run `sahara restore-download <path>`."*
- `s3:RestoreObject(Days=temp_expiry_days, GlacierJobParameters.Tier=tier)`
- Store `restore_job_id` (AWS request ID from response header)
- `--wait`: poll `s3:HeadObject` every 30 min, up to `max_poll_hours` (default 72h). On timeout: log `sync_history` as `status=failed`, update `files.restore_job_id=NULL`, print error.

**Status** (`sahara restore-status <path>`):
- `s3:HeadObject`; parse `Restore` response header
- States: `not-initiated` | `in-progress` | `complete (expires <date>)` | `expired` | `timed-out`

**Download** (`sahara restore-download <path> [--overwrite]`):
- Restore must be complete; error with ETA if still in-progress
- Handle existing local file (see Section 4.5)
- `s3:GetObject` to temp file → atomic move
- Update `files`: `tier=hot_temp`, `restore_expires_at` from `Restore` header expiry date

### 6.3 Restore Expiry
- Daemon daily check on all `hot_temp` files:
  - 24h before `restore_expires_at`: notification + log
  - After expiry: `files.tier=cold`; `sahara status` flags local copy as "local only (archive expired)"
- Max poll duration: `max_poll_hours=72` (configurable). After timeout: `sync_history` records `failed`.

### 6.4 Restore Tiers
| Tier | Speed | Cost/GB |
|------|-------|---------|
| Bulk | 12-48h | ~$0.0025 |
| Standard | 3-5h | ~$0.01 |
| Expedited | 1-5min | ~$0.03 |

---

## 7. Daemon

### 7.1 Process Management
- **PID file**: `~/.sahara/daemon.pid` (written on start; deleted on clean stop)
- **Stale PID detection**: if PID file exists but PID is not running → stale; overwrite
- **Start**: `sahara daemon start` forks process; writes PID file; runs initial full sync before starting file watcher
- **Stop**: `sahara daemon stop` sends SIGTERM to PID; daemon catches signal, stops watcher, completes current sync, exits cleanly
- **Crash recovery**: On `daemon start`, if PID file is absent but sync was in progress (detected via stale lock file), run `sahara doctor` before starting
- **Platform startup** (`--on-login`):
  - macOS: create `~/Library/LaunchAgents/com.sahara.daemon.plist`
  - Linux: create `~/.config/systemd/user/sahara.service`
  - Windows: create Task Scheduler entry (documented; not auto-created)

### 7.2 File Watching
- Library: `watchdog` (inotify/Linux, FSEvents/macOS, ReadDirectoryChangesW/Windows)
- Debounce: 5s window; events within window coalesced
- On event: sync only affected paths (not full sync)
- Scheduled full sync: every 6 hours (catches missed events)
- **Event queue**: when all 4 workers busy, new events queued (unbounded queue; events deduplicated by path)
- **Daemon paused**: `~/.sahara/daemon.paused` flag file. Not persisted across `daemon start`.

### 7.3 Auth Failure Handling
- On `ExpiredTokenException` or `ClientError` with auth code: stop sync; desktop notification; mark `daemon.auth_failed=true`; retry credential validation every 10 min via `sts:GetCallerIdentity`
- On success: clear `auth_failed`; resume sync
- Daemon stays resident during auth failure (does not exit)

### 7.4 Restore Polling
- Every 30 min: `s3:HeadObject` for all rows where `restore_job_id IS NOT NULL AND tier='cold'`
- On completion: set `tier=hot_temp`, `restore_expires_at`; send desktop notification; update manifest
- On `max_poll_hours` exceeded: log failure; clear `restore_job_id`

---

## 8. Cost Estimation

### 8.1 `sahara usage` Output
```
Sahara Usage Report — March 2026
═══════════════════════════════════════════════════════════
Storage:
  Hot (S3 Standard):        45.3 GB   ~$1.04/month
  Cold (Glacier Deep):     892.1 GB   ~$0.88/month
  Total:                   937.4 GB   ~$1.92/month
  vs Google One 1TB: $9.99 | iCloud+ 1TB: $9.99 | Savings: ~$8/mo

Requests (this month):
  PUT/COPY: 1,203  ~$0.006  |  GET: 456  ~$0.002  |  Glacier: 12  ~$0.001

Data Transfer:
  Upload: 2.3 GB  Free  |  Download: 0.8 GB  ~$0.07
  ⚠ Egress: $0.09/GB after 100 GB/month free

Estimated Monthly Total: ~$2.00
Run 'sahara usage --simulate' for 3-month projection.
```

---

## 9. Error Handling Reference

| Scenario | Behavior |
|----------|----------|
| Network timeout | Retry 5× exponential (2,4,8,16,32s + jitter) |
| S3 rate limit | Retry 5× jitter, cap 60s |
| 403 Forbidden | Fail; log required IAM permission |
| Bucket not found | Fail; "Run `sahara doctor`" |
| DB corrupt | Rename; `sahara doctor --repair` rebuilds from manifest |
| Disk full | Stop; daemon notification |
| File locked (Windows) | Skip; retry next cycle |
| Multipart interrupted | Mark pending; resume next cycle |
| Auth expiry | Daemon pauses; notification; retry every 10 min |
| Missing encryption key | Fail; "Run `sahara encryption setup`" |
| Glacier 180-day warning | Abort unless `--force` |
| Restore timeout (72h) | Mark `failed` in sync_history; clear restore_job_id |
| Stale multipart upload ID | Abort old; restart from scratch |
| File modified during multipart resume | Abort old upload; restart fresh |

---

## 10. Performance Targets

| Metric | Target | Measurement Conditions |
|--------|--------|----------------------|
| Sync latency (event → upload start) | < 10s | Single file change, 100 Mbps, SSD |
| Full local scan (100k files) | < 30s | Mixed 1KB–10MB, SSD |
| Manifest download + parse (100k files) | < 5s | ~20MB manifest, 100 Mbps |
| Daemon idle memory | < 50MB RSS | macOS/Linux, watching 100k file folder |
| State DB lookup by path | < 10ms | Indexed, 100k rows |
| Concurrent transfers | 4 (configurable 1–16) | ThreadPoolExecutor |
| Memory per upload thread (at 8MB part) | ~8MB buffer | Total: ~32MB for 4 concurrent |

**Memory note**: 4 concurrent uploads at 8MB parts = ~32MB buffers + ~18MB baseline ≈ 50MB. This meets the target but leaves no headroom. Reduce to `max_concurrent_uploads=2` if RSS target is not met in practice.

---

## 11. Security

- AWS credentials: profile or env vars preferred; config stores profile name only
- Client-side passphrase: OS keychain (`keyring`); never in config
- KDF salt: `config` table in `state.db`; not in config file
- S3 bucket: Block Public Access ON; versioning OFF; Object Lock OFF
- All HTTPS via boto3
- IAM role auth supported
- Minimum IAM policy in README (see Section 4.3)

---

## 12. Technology Stack

| Component | Library | Version |
|-----------|---------|---------|
| Language | Python | 3.11+ |
| AWS SDK | boto3 | latest |
| CLI | Click | 8.x |
| SQLite | sqlite3 (stdlib) | — |
| File watching | watchdog | 3.x |
| Config | tomllib (stdlib) | Python 3.11+ |
| Encryption | cryptography (PyCA) | 41.x+ |
| Keychain | keyring | 24.x+ |
| Notifications | plyer | 2.x |
| File locking | filelock | 3.x |
| Ignore patterns | pathspec | 0.11+ |
| Testing | pytest + moto | 7.x / 5.x |
| Coverage | pytest-cov | 4.x |
| Packaging | pyproject.toml + hatchling | — |

---

## 13. Testing Strategy

| Layer | Tool | Notes |
|-------|------|-------|
| Unit (sync algorithm, DB, ignore rules) | pytest + SQLite in-memory | No AWS calls |
| S3 API integration | moto | Covers standard S3 ops |
| Retry logic (503 injection) | responses / pytest-httpserver | moto doesn't simulate throttling |
| Glacier restore timing | moto + manual state override | moto completes restores instantly; tests verify state transitions |
| Encryption round-trip | pytest | Encrypt → decrypt → verify checksum |
| CLI commands | Click test runner | Includes --dry-run verification |
| Daemon lifecycle | pytest + subprocess | Platform-specific; macOS + Linux CI |
| File watcher (platform-specific) | pytest + temp dirs | Run on macOS and Linux CI separately |
| Integration (real AWS) | Separate test suite | Optional; requires `SAHARA_TEST_BUCKET` env var |

**Coverage target**: ≥90% line coverage for `src/` modules.

---

## 14. Project File Structure

```
sahara/
├── pyproject.toml
├── README.md
├── .saharaignore.template
├── src/
│   └── sahara/
│       ├── __init__.py
│       ├── cli.py           # Click CLI: all commands
│       ├── config.py        # TOML config read/write
│       ├── sync_engine.py   # Three-way diff, sync algorithm, manifest
│       ├── s3_client.py     # boto3 wrapper: upload, download, archive, restore, mv
│       ├── state_db.py      # SQLite CRUD + schema (WAL mode)
│       ├── file_watcher.py  # watchdog handler + debounce
│       ├── daemon.py        # Daemon process + PID + platform startup
│       ├── encryption.py    # AES-256-GCM, PBKDF2, keyring integration
│       ├── cost_estimator.py
│       ├── ignore_rules.py  # pathspec-based .saharaignore matching
│       ├── notifier.py      # plyer notifications
│       └── models.py        # Dataclasses: FileRecord, SyncOperation, ManifestEntry
└── tests/
    ├── conftest.py          # Fixtures: moto mock, temp dirs, test DB, sample files
    ├── test_sync_engine.py  # Three-way diff, conflict, rename, bootstrap
    ├── test_s3_client.py    # Upload, download, multipart, archive, restore, mv
    ├── test_state_db.py     # Schema, CRUD, WAL, concurrent access
    ├── test_cli.py          # All CLI commands (Click test runner)
    ├── test_config.py       # Read/write, defaults, validation
    ├── test_encryption.py   # Encrypt/decrypt, PBKDF2, nonce uniqueness, keychain mock
    ├── test_cost_estimator.py
    ├── test_ignore_rules.py # gitignore patterns, negation, .saharaignore
    ├── test_daemon.py       # Start/stop/pause/resume/PID/crash recovery
    └── test_manifest.py     # Manifest download, parse, update, gzip
```

---

## 15. Resolved Design Decisions

| # | Decision |
|---|----------|
| 1 | Remote state via manifest.json (not per-object HeadObject) |
| 2 | ListObjectsV2 used only for bootstrap; manifest for all sync cycles |
| 3 | PBKDF2 salt: global, generated at init, stored in config table |
| 4 | Nonce: 12-byte random per file per upload (`os.urandom(12)`) |
| 5 | Ciphertext layout: `[nonce:12][ciphertext][tag:16]` |
| 6 | Conflict timestamp: `x-amz-meta-sahara-modified-at`; 2s tolerance window |
| 7 | Manual conflict mode: halt + report; resolve via `sahara resolve` |
| 8 | Conflict copy naming: `<stem>.conflict-<hostname>-<YYYYMMDD-HHMMSS>.<ext>` (local only) |
| 9 | Multipart resume: file SHA-256 check before resume; restart if changed |
| 10 | Rename detection: SHA-256 match; parent-dir tiebreaker; ambiguous = skip |
| 11 | SQLite: WAL mode, busy_timeout=5000ms, synchronous=NORMAL |
| 12 | .saharaignore: single root file, gitignore syntax via pathspec, no per-directory |
| 13 | Files added to .saharaignore: left in S3; not auto-deleted |
| 14 | Glacier restore max poll: 72h (configurable); after = failed state |
| 15 | Concurrency: ThreadPoolExecutor(4); event queue for daemon events |
| 16 | s3_etag: stored for out-of-band detection only; NOT used for integrity |
| 17 | Daemon PID file: ~/.sahara/daemon.pid; stale PID check on lock acquire |
| 18 | Daemon crash recovery: run full sync on next daemon start |
| 19 | Daemon auth failure: stay resident; retry every 10 min |
| 20 | Bucket requirements: versioning OFF, Object Lock OFF, Block Public Access ON |
| 21 | Simultaneous conflict timestamps (within 2s): always apply backup strategy |
| 22 | Bootstrap (empty DB vs existing S3): download all; upload divergent locals |
