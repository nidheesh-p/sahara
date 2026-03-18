# Sahara Cloud Storage — Product Specification v6.0 (Final Engineering-Ready)

## 1. Overview

**Sahara** is a personal, self-hosted cloud storage system built on AWS S3 providing a Dropbox-like experience without monthly subscription costs.

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
│   .sahara/manifest.json  (remote state; conditional PUTs)  │
└───────────────────────────────────────────────────────────┘
```

### 3.2 Storage Strategy

| Tier | StorageClass | Retrieval | Cost |
|------|-------------|-----------|------|
| Hot | `STANDARD` | Instant | ~$0.023/GB/mo |
| Cold | `DEEP_ARCHIVE` | 12-48h | ~$0.00099/GB/mo |

### 3.3 Remote State Manifest

The remote manifest serves as the authoritative remote state cache, avoiding per-object `HeadObject` calls during normal sync. `ListObjectsV2` is used ONLY during bootstrap / `--repair`.

**S3 key**: `.sahara/manifest.json` (`.sahara/manifest.json.gz` if compressed)

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
      "etag": "\"d41d8cd98f00b204e9800998ecf8427e\"",
      "ignored": false
    }
  }
}
```

`ignored` field: `true` if file matches current `.saharaignore` patterns. Set by sync engine during manifest update. Files with `ignored: true` are excluded from the diff phase on all subsequent syncs.

**Compression**: If manifest > 10MB, store as gzip (Content-Encoding: gzip; S3 key: `.sahara/manifest.json.gz`). Presence of `.gz` key is checked first on download.

#### 3.3.1 Manifest Atomicity and Cross-Machine Consistency

**Problem**: A plain PUT to manifest.json is non-atomic and susceptible to concurrent overwrites from two machines syncing simultaneously.

**Solution**: Optimistic concurrency using S3 conditional writes.

**Protocol**:
1. Download manifest: `s3:GetObject(.sahara/manifest.json)` → record `ETag` of downloaded manifest as `manifest_etag`
2. Perform sync operations
3. Upload updated manifest: `s3:PutObject` with `If-Match: <manifest_etag>` header
4. If `412 PreconditionFailed`: another machine updated the manifest concurrently → reload manifest, re-run diff against new manifest (re-use already-completed upload/download operations where safe), retry manifest upload with new `If-Match`
5. Maximum 3 retries on 412 before aborting sync with error

**Interim crash (upload interrupted)**: Manifest is not written. On next sync, stale local `files` table may diverge from S3 actual state. Mitigation: download manifest first and compare against `files` table; divergent entries are reconciled via `HeadObject` before proceeding.

**Bootstrap (no manifest in S3)**: Use `ListObjectsV2` + `HeadObject` for each object to build initial manifest. This is the only scenario requiring bulk `HeadObject` calls.

#### 3.3.2 Manifest ETag Tracking
- `manifest_etag` stored in `config` table as `manifest.etag` after each successful manifest upload
- Used as `If-Match` value for next manifest PUT

### 3.4 Local State Database (SQLite)
Location: `~/.sahara/state.db`

**SQLite connection settings** (applied on every connection open):
```python
conn.execute("PRAGMA journal_mode = WAL")
conn.execute("PRAGMA busy_timeout = 5000")   # ms
conn.execute("PRAGMA synchronous = NORMAL")   # safe with WAL
conn.execute("PRAGMA foreign_keys = ON")
```

#### `files` table
```sql
CREATE TABLE files (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  relative_path       TEXT NOT NULL UNIQUE,
  sha256_checksum     TEXT,             -- pre-encryption plaintext; NULL = foreign
  size_bytes          INTEGER,
  tier                TEXT NOT NULL CHECK(tier IN ('hot','cold','hot_temp')),
  s3_etag             TEXT,             -- out-of-band detection ONLY; not integrity
  last_sync_at        TEXT,
  local_modified_at   TEXT,
  remote_modified_at  TEXT,
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
cold ──restore-initiate──► cold (restore_job_id set)
cold (job set) ──restore-complete──► hot_temp (restore_expires_at set)
hot_temp ──expires──► cold
hot_temp ──modified locally + push──► hot (permanent)
```

**`s3_etag` note**: NOT used for content integrity. Used only to detect out-of-band S3 modifications. For multipart uploads, ETag is `MD5(concat_part_MD5s)-N` (not content-addressable). For encrypted objects, ETag is MD5 of ciphertext. `sha256_checksum` is the sole integrity check.

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
  file_sha256   TEXT NOT NULL,   -- SHA-256 at upload start; change detection on resume
  parts_json    TEXT DEFAULT '[]',
  -- parts_json schema: [{"PartNumber":1,"ETag":"...","StartByte":0,"EndByte":8388607}, ...]
  started_at    TEXT NOT NULL
);
```

**Multipart resume**:
1. Recompute local SHA-256; if ≠ `file_sha256`: `s3:AbortMultipartUpload`, delete row, start fresh
2. `s3:ListParts(upload_id)`: if `NoSuchUpload` → delete row, start fresh
3. Resume from first missing part using `parts_json` byte ranges
4. Interrupted: mark in `pending_multipart`, skip file, continue sync

#### `config` table
```sql
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Notable keys:
-- manifest.etag          : ETag of last successfully written manifest
-- encryption.kdf_salt    : hex-encoded 32-byte PBKDF2 salt (NOT in config file)
```

### 3.5 S3 Object Metadata
```
x-amz-meta-sahara-checksum:     SHA-256 of plaintext (pre-encryption) content
x-amz-meta-sahara-original-path: relative path within sync folder
x-amz-meta-sahara-modified-at:  local mtime at upload time (ISO 8601 UTC)
x-amz-meta-sahara-version:      "1"
```

### 3.6 Encryption

**Server-side**: SSE-S3 on all objects by default.

**Client-side (optional)**: AES-256-GCM.

#### Key Derivation
- Algorithm: PBKDF2-HMAC-SHA256, 600,000 iterations
- Salt: 32-byte random, generated once at `sahara encryption setup`
- **Storage**: stored in `config` table (`encryption.kdf_salt`) AND in OS keychain as backup. If DB is lost, salt can be recovered from keychain.
- Key length: 32 bytes
- Key rotation: `sahara encryption rotate` generates new salt + passphrase, re-encrypts all Hot files, stores new salt in both DB and keychain.

#### Ciphertext Format (Streaming / Chunked)

To avoid loading entire large files into memory, AES-256-GCM is applied in **authenticated chunks**:

```
File ciphertext format:
  [global_header: 4 bytes "SAHA"]
  [version: 1 byte = 0x01]
  [salt: 32 bytes]    <- included in file for self-contained decryption
  For each chunk (default 4MB plaintext chunks):
    [nonce: 12 bytes random (os.urandom(12))]
    [chunk_len: 4 bytes uint32 big-endian (encrypted+tag length)]
    [encrypted_chunk: chunk_len - 16 bytes]
    [GCM tag: 16 bytes]
  [terminator: nonce(12) + chunk_len(4)=0x00000000]
```

- Each chunk is independently authenticated
- Maximum memory usage: 1 chunk (~4MB plaintext + ~4MB ciphertext) + buffers
- Supports streaming upload via multipart (each multipart part = N complete chunks)
- Decryption can be parallelized per chunk

**Plaintext checksum**: computed on plaintext stream before encryption. Stored in `x-amz-meta-sahara-checksum`. Used for incremental sync comparison.

#### Passphrase Lifecycle
1. **Setup** (`sahara encryption setup`): generate salt; derive key; store passphrase + salt in OS keychain; store salt in `config` table. Print loss warning.
2. **Missing keychain entry**: sync fails with actionable message.
3. **Rotation** (`sahara encryption rotate`): new salt + passphrase; re-encrypt all Hot files (download → decrypt → re-encrypt → re-upload); update salt in keychain + config table. Cold files: warn that N files remain encrypted with old key.
4. **Lost passphrase**: No recovery. Documented at setup.

### 3.7 AWS Authentication
Credential resolution (boto3 chain order):
1. `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env vars
2. `aws.profile` config → `~/.aws/credentials`
3. `aws.access_key_id` + `aws.secret_access_key` in config (discouraged)
4. EC2/ECS instance role

**Auth failure in daemon**: Stop syncing; notify; retry credential check every 10 min. Daemon stays resident.

### 3.8 Daemon / CLI Concurrency

**Local sync lock**: `~/.sahara/sync.lock` via `filelock` (prevents two sync operations on the SAME machine from running concurrently). Does not provide cross-machine coordination.

**Cross-machine coordination**: S3 conditional PUT (`If-Match`) on manifest (see 3.3.1). This is the only cross-machine serialization mechanism.

**SQLite WAL**: Multiple readers, one writer; `busy_timeout=5000ms` handles contention.

**Stale lock**: If lock file exists and PID is not running → stale; delete and acquire.

---

## 4. CLI Specification

### 4.1 `sahara init` Flow

```
sahara init [--bucket=NAME] [--region=REGION] [--folder=PATH] [--non-interactive]
```

1. Credentials: auto-detect or prompt for method
2. Validate: `sts:GetCallerIdentity`
3. Bucket: check existence → create or import (see Section 4.3)
4. Folder: prompt (default: `~/Sahara`)
5. Encryption: prompt; if yes → `sahara encryption setup`
6. Write `~/.sahara/config.toml`; write `.saharaignore` template
7. Run `sahara doctor`
8. Print summary

#### Bootstrap from existing S3 bucket with manifest:
- Download manifest → populate `files` table
- Objects in manifest with `sha256_checksum` ≠ local file SHA-256 → conflict/upload on next sync
- Objects without `x-amz-meta-sahara-checksum` → imported with `sha256_checksum=NULL`

#### Bootstrap from existing S3 bucket without manifest:
- `ListObjectsV2` → `HeadObject` per object to read metadata → build manifest → import
- Warn user this is a slow one-time operation

### 4.2 `sahara doctor [--repair]`

| Check | Method |
|-------|--------|
| AWS credentials valid | `sts:GetCallerIdentity` |
| Bucket accessible | `s3:HeadBucket` |
| IAM permissions | Test key ops on `.sahara/doctor-test` object |
| Bucket region matches config | Compare bucket region to config |
| Versioning OFF | `s3:GetBucketVersioning` |
| Object Lock OFF | `s3:GetObjectLockConfiguration` |
| Block Public Access ON | `s3:GetPublicAccessBlock` |
| Sync folder accessible | `os.access()` |
| SQLite integrity | `PRAGMA integrity_check` |
| Stale multipart uploads | `s3:ListMultipartUploads` (list >7 days old) |
| Available disk space | `shutil.disk_usage()` vs. manifest total size |

`--repair`: rename corrupted DB; rebuild from manifest.

### 4.3 Bucket Requirements

| Setting | Required |
|---------|----------|
| Block Public Access | ON |
| Versioning | OFF |
| Object Lock | OFF |
| SSE | SSE-S3 (default) or SSE-KMS |
| Multipart lifecycle | Abort incomplete after 7 days |

**Minimum IAM policy**:
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket",
      "s3:HeadObject", "s3:CopyObject", "s3:RestoreObject",
      "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts",
      "s3:GetObjectRestoreStatus", "s3:GetBucketVersioning",
      "s3:GetObjectLockConfiguration", "s3:GetPublicAccessBlock"
    ],
    "Resource": [
      "arn:aws:s3:::BUCKET-NAME",
      "arn:aws:s3:::BUCKET-NAME/*"
    ]
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
sahara daemon pause   # ~/.sahara/daemon.paused; not persisted across restart
sahara daemon resume
sahara daemon logs   [--tail=50]
```

### 4.5 Command Behavioral Contracts

#### `push` / `pull` / `sync`
- `push`: upload only; check for conflicts but do not overwrite remote
- `pull`: download only; skip where local SHA-256 = manifest SHA-256
- `sync`: bidirectional; unified conflict resolution

#### Three-way diff base
Base = `files` table (`sha256_checksum`):
- **local-new**: in filesystem, NOT in `files` → upload
- **remote-new**: in manifest (not ignored), NOT in `files` → download
- **local-modified**: local SHA-256 ≠ `files.sha256_checksum`; manifest unchanged → upload
- **remote-modified**: manifest SHA-256 ≠ `files.sha256_checksum`; local unchanged → download
- **conflict**: both changed → apply conflict strategy
- **local-deleted**: in `files` (not is_deleted), absent locally → delete remote
- **remote-deleted**: in `files`, absent from manifest → remote delete policy
- **bootstrap (empty DB)**: all manifest entries = remote-new; all local files = local-new; reconciled

#### Remote delete policy
- `newest-wins`/`backup`: delete local, set `is_deleted=1`, log
- `manual`: halt; report; require `sahara resolve`

#### Ignore + already-synced files
When a file is added to `.saharaignore` AFTER being synced:
- File remains in S3 (not auto-deleted)
- Manifest entry updated with `ignored: true` on next sync
- File excluded from diff on all future syncs
- User must explicitly `sahara rm <path>` to remove from S3

#### Concurrent sync (multi-machine)
1. Machine A and B both download manifest (same ETag)
2. Both compute diffs and execute uploads/downloads
3. A finishes first: `PutObject(manifest, If-Match: <original-etag>)` → succeeds
4. B finishes: `PutObject(manifest, If-Match: <original-etag>)` → 412 PreconditionFailed
5. B: reload manifest (now includes A's changes), re-run diff (only B's changes not yet in new manifest), retry PUT with new ETag
6. Max 3 retries; then abort with error message

#### `sahara rm <path> [--force]`
- Delete S3 object; `is_deleted=1`; prompt unless `--force`
- Cold tier: additional 180-day charge warning; `--force` bypasses all prompts

#### `sahara archive`
- `--older-than=DAYS`: measured against `remote_modified_at`
- `--dry-run`: list + savings; no action
- Within 180 days: warn + require `--force`

#### `sahara restore-download <path> [--overwrite]`
- Must be complete; error with ETA if not
- Existing local + differing SHA-256: prompt or `--overwrite`

#### `sahara resolve <path> --keep=local|remote|backup`
- `backup`: downloads remote as `<stem>.conflict-<hostname>-<YYYYMMDD-HHMMSS>.<ext>` (LOCAL ONLY); pushes local as canonical. Auto-adds conflict pattern to `.saharaignore`.

#### Restore expiry proactive handling
- At start of each daemon sync cycle: query all `hot_temp` files where `restore_expires_at < now() + 24h`
- Emit desktop notification: *"Glacier restore for <path> expires in <time>. Download it with `sahara restore-download <path>` or run `sahara restore <path>` to re-request."*

#### Conflict resolution
- Detection: local SHA-256 ≠ `files.sha256_checksum` AND manifest SHA-256 ≠ `files.sha256_checksum`
- Timestamp tolerance: within 2s → simultaneous; always apply `backup` regardless of strategy
- Authority: `x-amz-meta-sahara-modified-at` (upload-time mtime; immune to sync-time clock skew)
- Copy naming: `<stem>.conflict-<hostname>-<YYYYMMDD-HHMMSS>.<ext>`

### 4.6 Configuration (`~/.sahara/config.toml`)
```toml
[aws]
profile = ""                   # Preferred
access_key_id = ""             # Discouraged
secret_access_key = ""         # Discouraged
region = "us-east-1"
bucket = ""
sse = "SSE-S3"
kms_key_id = ""

[sync]
folder = "~/Sahara"
exclude = []
auto_archive_days = 0
conflict_strategy = "backup"   # newest-wins | manual | backup
bandwidth_limit_kbps = 0
debounce_seconds = 5

[encryption]
client_side = false
# kdf_salt in DB config table + keychain; passphrase in keychain only

[restore]
default_tier = "bulk"
temp_expiry_days = 7
notify_on_complete = true
max_poll_hours = 72

[performance]
multipart_threshold_mb = 100
multipart_part_size_mb = 8
encryption_chunk_mb = 4        # plaintext chunk size for streaming AES-GCM
max_concurrent_uploads = 4
max_concurrent_downloads = 4
```

---

## 5. Sync Engine

### 5.1 Sync Algorithm
1. **Acquire lock** (`~/.sahara/sync.lock`; stale PID check)
2. **Download manifest** with ETag; store `manifest_etag`
3. **Scan local**: walk folder, skip `.saharaignore` matches; recompute SHA-256 only on mtime/size change
4. **Three-way diff** (base = `files` table)
5. **Rename detection**: match local-deleted + local-new by SHA-256; tiebreaker: prefer pairs sharing parent dir or filename stem; if still ambiguous: treat as separate delete + upload; log warning
6. **Conflict resolution**: apply strategy; simultaneous timestamps (within 2s) → always `backup`
7. **Execute** via `ThreadPoolExecutor(max_workers=max_concurrent_uploads)`:
   - Submit all operations as futures
   - Collect results via `concurrent.futures.as_completed()` (NOT `executor.map`)
   - **For each completed future** (within the loop): catch exception individually; on success: **immediately update `files` table and append to `sync_history`**; on failure: log `sync_history` as `failed`; continue
   - This ensures `files` table is fully up-to-date BEFORE manifest rebuild
   - End-of-sync: count failures; if any failed → exit non-zero; print summary
8. **Update manifest**: rebuild manifest from `files` table (now fully updated); `PutObject` with `If-Match: <manifest_etag>`
   - On 412: reload manifest, re-diff, retry (max 3)
9. **Release lock**

### 5.2 Encryption (Streaming)

#### Ciphertext Binary Format (precise definition)

```
File layout:
  Offset 0:  b'SAHA'              (4 bytes, magic header)
  Offset 4:  b'\x01'              (1 byte, format version)
  Offset 5:  salt                 (32 bytes, PBKDF2 salt from config table)
  Offset 37: [chunk records ...]
  Final:     terminator record

Chunk record (for each 4MB plaintext chunk):
  nonce              : 12 bytes  (random, os.urandom(12))
  blob_len           : 4 bytes   (uint32 big-endian)
                                  = len of the blob field below
  blob               : blob_len bytes
                                  = PyCA AESGCM.encrypt() output,
                                    which is ciphertext concatenated
                                    with 16-byte GCM tag as ONE unit.
                                    Do NOT split ciphertext and tag.
                                    Pass entire blob to AESGCM.decrypt().

Terminator record:
  nonce              : 12 bytes  (random, for format consistency)
  blob_len           : 4 bytes   = 0x00000000  (signals end of file)
```

**Decoder contract**: Read `nonce(12) + blob_len(4)`. If `blob_len == 0`: end of file. Else: read `blob_len` bytes and pass entire blob (ciphertext+tag combined) to `AESGCM(key).decrypt(nonce, blob, None)`. The GCM tag is embedded at the end of the blob by PyCA — do not extract it separately.

```python
# Encryption (write path)
def encrypt_file(plaintext_path, ciphertext_path, key, salt, chunk_size_mb=4):
    chunk_size = chunk_size_mb * 1024 * 1024
    sha256 = hashlib.sha256()
    aesgcm = AESGCM(key)
    with open(plaintext_path, 'rb') as fin, open(ciphertext_path, 'wb') as fout:
        fout.write(b'SAHA')                          # magic
        fout.write(b'\x01')                          # version
        fout.write(salt)                             # 32-byte PBKDF2 salt
        while True:
            chunk = fin.read(chunk_size)
            if not chunk:
                nonce = os.urandom(12)
                fout.write(nonce)
                fout.write((0).to_bytes(4, 'big'))   # terminator: blob_len=0
                break
            sha256.update(chunk)
            nonce = os.urandom(12)
            blob = aesgcm.encrypt(nonce, chunk, None)  # ciphertext+tag as one blob
            fout.write(nonce)
            fout.write(len(blob).to_bytes(4, 'big'))
            fout.write(blob)                         # write entire blob; do NOT split
    return sha256.hexdigest()

# Decryption (read path)
def decrypt_file(ciphertext_path, plaintext_path, key):
    with open(ciphertext_path, 'rb') as fin, open(plaintext_path, 'wb') as fout:
        assert fin.read(4) == b'SAHA'
        assert fin.read(1) == b'\x01'
        fin.read(32)                                 # skip salt (already used for key)
        while True:
            nonce = fin.read(12)
            blob_len = int.from_bytes(fin.read(4), 'big')
            if blob_len == 0:
                break                                # terminator
            blob = fin.read(blob_len)
            plaintext = key_aesgcm.decrypt(nonce, blob, None)  # pass full blob
            fout.write(plaintext)
```

Memory usage: ~2 × chunk_size (~8MB at 4MB chunks) per encryption thread.

### 5.3 Large File Support (Multipart)
- Threshold: `multipart_threshold_mb` (default 100MB)
- Part size: `multipart_part_size_mb` (default 8MB)
- `parts_json` schema: `[{"PartNumber": 1, "ETag": "...", "StartByte": 0, "EndByte": 8388607}]`
- Resume: recompute SHA-256 → check `file_sha256` → `ListParts` → upload missing parts
- Stale upload ID (`NoSuchUpload`): delete row, restart
- File changed during resume: abort upload, restart

### 5.4 Exclusion Patterns
- Library: `pathspec` (gitignore-compatible)
- Sources: `.saharaignore` in sync root + `sync.exclude` in config (both applied; merged)
- Single root `.saharaignore` (NOT per-directory in v1)
- Built-in non-configurable: `.DS_Store`, `Thumbs.db`, `desktop.ini`, `*.tmp`, `*.swp`, `~$*`, `.Trash-*`, `*.conflict-*`
- Files added to ignore after sync: remain in S3; `ignored: true` in manifest; excluded from future diffs
- Negation patterns (`!`): supported via pathspec

### 5.5 Retry Policy

| Error | Policy |
|-------|--------|
| Network timeout / connection reset | Retry 5×: 2,4,8,16,32s + jitter |
| S3 503 SlowDown | Retry 5× jitter, cap 60s |
| S3 412 PreconditionFailed (manifest) | Reload manifest, re-diff, retry; max 3 |
| S3 4xx (403, 404) | Fail immediately; log required action |
| Multipart interrupted | Mark pending; skip; resume next cycle |
| Disk full | Stop sync; notify |
| Auth expiry | Daemon pauses; notify; retry every 10 min |

---

## 6. Glacier Archive Operations

### 6.1 Archive Flow
1. Verify `tier=hot`
2. 180-day check: if `remote_modified_at` < 180 days → warn + require `--force`
3. `s3:CopyObject` (same key, `StorageClass=DEEP_ARCHIVE`, same metadata)
4. `s3:DeleteObject` original
5. Update `files`: `tier=cold`, `archived_at=now`; update manifest; log `sync_history`

### 6.2 Restore Flow

**Initiate**:
- If `tier=hot_temp` + not expired: print existing restore info
- `s3:RestoreObject(Days=temp_expiry_days, GlacierJobParameters.Tier=tier)`
- `--wait`: poll `s3:HeadObject` every 30 min, up to `max_poll_hours`; on timeout → log `failed` in `sync_history`, clear `restore_job_id`

**Download**:
- `s3:GetObject` to temp file → atomic rename to final path
- Update `files`: `tier=hot_temp`, `restore_expires_at` from `Restore` header expiry

### 6.3 Restore Expiry Proactive Handling
- Daemon sync cycle start: query `hot_temp` files where `restore_expires_at < now() + 24h`
- Notification: *"Glacier restore for <path> expires in <time>. Download or re-request restore."*
- After expiry: `files.tier=cold`; `sahara status` shows "local only (archive expired)"

### 6.4 Restore Tiers
| Tier | Speed | Cost/GB |
|------|-------|---------|
| Bulk | 12-48h | ~$0.0025 |
| Standard | 3-5h | ~$0.01 |
| Expedited | 1-5min | ~$0.03 |

---

## 7. Daemon

### 7.1 Process Management
- **PID file**: `~/.sahara/daemon.pid`
- **Start**: fork; write PID; run initial full sync; start file watcher
- **Stop**: SIGTERM → stop watcher → finish current sync → exit → delete PID + lock
- **Crash recovery**: stale PID + stale lock → `sahara doctor` on next `daemon start`
- **Platform startup** (`--on-login`):
  - macOS: `~/Library/LaunchAgents/com.sahara.daemon.plist`
  - Linux: `~/.config/systemd/user/sahara.service`

### 7.2 File Watching
- Library: `watchdog` (inotify/FSEvents/ReadDirectoryChangesW)
- Debounce: 5s; coalesce events per path
- On event: partial sync (affected paths only)
- Full sync: every 6 hours
- Event queue: when workers busy, events queued (deduplicated by path)
- Paused: `~/.sahara/daemon.paused` flag (NOT persisted across `daemon start`)

### 7.3 Worker Isolation (ThreadPoolExecutor)
```python
futures = {executor.submit(op.execute): op for op in operations}
failed = []
for future in concurrent.futures.as_completed(futures):
    op = futures[future]
    try:
        result = future.result()
        record_success(op, result)
    except Exception as e:
        log_error(op, e)
        record_failure(op, str(e))
        failed.append(op)
# After all futures resolved:
if failed:
    print(f"Sync completed with {len(failed)} failures.")
    sys.exit(1)
```

---

## 8. Cost Estimation

```
Sahara Usage — March 2026
Storage: Hot 45.3GB ~$1.04 | Cold 892.1GB ~$0.88 | Total ~$1.92/mo
vs Google One 1TB: $9.99 | Savings: ~$8/mo
Requests: PUT 1,203 ~$0.006 | GET 456 ~$0.002
Egress: 0.8GB ~$0.07 (⚠ $0.09/GB after 100GB free)
Monthly Total: ~$2.00
```

---

## 9. Error Handling Reference

| Scenario | Behavior |
|----------|----------|
| Network timeout | Retry 5× exponential |
| S3 503 | Retry 5× jitter |
| 412 on manifest | Reload+re-diff; max 3 retries |
| 403 | Fail; log IAM action needed |
| Bucket not found | Fail; "Run `sahara doctor`" |
| DB corrupt | Rename; `doctor --repair` |
| Disk full | Stop; notify |
| File locked (Win) | Skip; retry next cycle |
| Multipart interrupted | Mark pending; resume next |
| Auth expiry | Daemon pauses; retry every 10 min |
| Missing encryption key | Fail; "Run `sahara encryption setup`" |
| Glacier 180-day check | Abort unless `--force` |
| Restore timeout (72h) | Failed in sync_history; clear job_id |
| Stale multipart upload ID | Abort; restart |
| File changed mid-resume | Abort; restart fresh |

---

## 10. Performance Targets

| Metric | Target | Conditions |
|--------|--------|------------|
| Sync latency (event → upload start) | < 10s | Single file, 100 Mbps, SSD |
| Local scan (100k files) | < 30s | Mixed 1KB–10MB, SSD |
| Manifest download + parse (100k) | < 5s | ~20MB, 100 Mbps |
| Daemon idle RSS | < 50MB | macOS/Linux, 100k file watch |
| DB lookup by path | < 10ms | Indexed, 100k rows |
| Concurrent transfers | 4 (1–16 configurable) | ThreadPoolExecutor |
| Encryption memory | ~8MB/thread | 4MB chunk × 2 |

---

## 11. Security Summary

- AWS credentials: profile/env vars preferred; config stores profile name only
- KDF salt: `config` table in DB + OS keychain backup; NOT in config file
- Passphrase: OS keychain only; never on disk or in config
- Ciphertext: includes salt (self-contained); each chunk independently authenticated
- S3 bucket: Block Public Access ON; versioning OFF; Object Lock OFF
- IAM: minimum policy defined (Section 4.3)

---

## 12. Technology Stack

| Component | Library | Version |
|-----------|---------|---------|
| Language | Python | 3.11+ |
| AWS SDK | boto3 | latest |
| CLI | Click | 8.x |
| SQLite | sqlite3 (stdlib) | — |
| File watching | watchdog | 3.x |
| Config | tomllib (stdlib) | 3.11+ |
| Encryption | cryptography (PyCA) | 41.x+ |
| Keychain | keyring | 24.x+ |
| Notifications | plyer | 2.x |
| File locking | filelock | 3.x |
| Ignore patterns | pathspec | 0.11+ |
| Testing | pytest + moto | 7.x / 5.x |
| Retry testing | responses | 0.25+ |
| Coverage | pytest-cov | 4.x |
| Packaging | pyproject.toml + hatchling | — |

---

## 13. Testing Strategy

| Layer | Tool | Scope |
|-------|------|-------|
| Unit: sync algorithm, DB, ignore rules | pytest, in-memory SQLite | No AWS calls |
| S3 API integration | moto | Standard S3 ops |
| Retry / 503 / 412 injection | responses / pytest-httpserver | Throttling, manifest conflicts |
| Glacier restore state machine | moto + state override | Restore timing (moto is instant; override state manually) |
| Encryption round-trip | pytest | Encrypt → decrypt → verify SHA-256; chunk boundary tests |
| CLI commands | Click test runner | All commands including `--dry-run` |
| Daemon lifecycle | pytest + subprocess | Start/stop/pause/crash recovery |
| File watcher | pytest + temp dirs | Platform CI: macOS + Linux |
| Concurrent sync (multi-machine) | pytest (two sync engines, one moto bucket) | 412 retry flow |
| Integration (real AWS) | Separate suite | Requires `SAHARA_TEST_BUCKET` env var |

**Target**: ≥90% line coverage in `src/` via `pytest-cov`.

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
│       ├── cli.py           # Click CLI
│       ├── config.py        # TOML config
│       ├── sync_engine.py   # Three-way diff, manifest, sync
│       ├── s3_client.py     # boto3 wrapper
│       ├── state_db.py      # SQLite (WAL)
│       ├── file_watcher.py  # watchdog + debounce
│       ├── daemon.py        # Process management
│       ├── encryption.py    # AES-256-GCM chunked + PBKDF2
│       ├── cost_estimator.py
│       ├── ignore_rules.py  # pathspec
│       ├── notifier.py      # plyer
│       └── models.py        # Dataclasses
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
    ├── test_daemon.py
    └── test_manifest.py
```

---

## 15. Resolved Design Decisions

| # | Decision |
|---|----------|
| 1 | Remote state via manifest.json (not per-object HeadObject) |
| 2 | ListObjectsV2 used only for bootstrap; manifest for all sync cycles |
| 3 | Manifest atomicity: S3 conditional PUT with If-Match ETag; 3 retries on 412 |
| 4 | Cross-machine coordination: S3 If-Match (optimistic locking); no cross-machine file lock |
| 5 | PBKDF2 salt: global 32-byte random; stored in DB config table + OS keychain |
| 6 | Ciphertext: chunked AES-256-GCM; 4MB chunks; format: [SAHA][v][salt][chunks...][terminator] |
| 7 | Nonce: 12-byte random per chunk (os.urandom); never deterministic |
| 8 | Conflict timestamp: x-amz-meta-sahara-modified-at; 2s tolerance; simultaneous → backup |
| 9 | Manual conflict UX: halt + report; `sahara conflicts` + `sahara resolve` |
| 10 | Conflict copy naming: `<stem>.conflict-<hostname>-<YYYYMMDD-HHMMSS>.<ext>` (local only) |
| 11 | Multipart resume: SHA-256 check before resume; ListParts stale check; parts_json byte ranges |
| 12 | Rename detection: SHA-256 match; parent-dir tiebreaker; ambiguous → separate delete+upload |
| 13 | SQLite: WAL, busy_timeout=5000ms, synchronous=NORMAL |
| 14 | .saharaignore: pathspec; single root file; already-synced files NOT auto-deleted |
| 15 | Ignored files: manifest.ignored=true; excluded from diff |
| 16 | Glacier restore max poll: 72h; timeout → failed state |
| 17 | Restore expiry proactive: daemon warns 24h before expiry |
| 18 | Concurrency: ThreadPoolExecutor(4); as_completed; DB updated per-future WITHIN loop; manifest rebuilt AFTER all futures; non-zero exit on any failure |
| 19 | s3_etag: out-of-band detection only; NOT integrity check |
| 20 | Daemon: PID file; SIGTERM; initial full sync on start; crash recovery via doctor |
| 21 | Bootstrap (empty DB): all manifest entries = remote-new; local files = local-new |
| 22 | Bucket: versioning OFF, Object Lock OFF, Block Public Access ON |
