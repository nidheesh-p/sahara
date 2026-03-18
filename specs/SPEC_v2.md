# Sahara Cloud Storage — Product Specification v2.0

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

Tables:
- `files`: `(id, relative_path, sha256_checksum, size_bytes, tier, s3_etag, last_sync_at, local_modified_at, remote_modified_at, archived_at, restore_job_id, restore_expires_at, is_deleted)`
- `sync_history`: `(id, operation, path, status, error_message, started_at, completed_at, bytes_transferred)`
- `pending_multipart`: `(id, relative_path, upload_id, s3_key, parts_json, started_at)` — for resuming interrupted uploads
- `config`: `(key, value)`

### 3.4 S3 Metadata Convention
Each file in S3 has object metadata:
- `x-amz-meta-sahara-checksum`: SHA-256 of **pre-encryption** original file content
- `x-amz-meta-sahara-original-path`: Original relative path
- `x-amz-meta-sahara-modified-at`: Local file mtime (ISO 8601 UTC)
- `x-amz-meta-sahara-tier`: `hot` or `cold`

### 3.5 Encryption

- **In transit**: HTTPS/TLS (enforced by boto3 / AWS SDK)
- **At rest**: AWS S3 Server-Side Encryption SSE-S3 enabled by default
- **Client-side** (optional): AES-256-GCM encryption before upload using user-provided passphrase
  - SHA-256 checksum is computed on **plaintext** content before encryption and stored in S3 metadata
  - This ensures incremental sync still works correctly (checksum is compared pre-encryption, not of the ciphertext)
  - Random IV per file per upload; IV stored prepended to ciphertext

### 3.6 Daemon / CLI Concurrency
- Advisory file lock: `~/.sahara/sync.lock`
- If a sync (daemon or CLI) is running, subsequent CLI `sync`/`push`/`pull` commands detect the lock and exit with a clear message: `"Sync already in progress (PID 12345). Use --wait to queue, or --force to proceed."`
- `--wait` flag causes CLI to poll until the lock is released, then runs
- Daemon acquires lock for duration of sync operation, releases immediately after

---

## 4. CLI Specification

### 4.1 `sahara init` — Detailed Flow

```
sahara init [--bucket=NAME] [--region=us-east-1] [--folder=PATH]
```

Interactive setup wizard (unless all flags provided):
1. Prompt for AWS credentials (or detect from `~/.aws/credentials` / env vars)
2. Validate AWS credentials (call `sts:GetCallerIdentity`)
3. Prompt for or accept S3 bucket name
4. Check if bucket exists; if not, offer to create it (`s3:CreateBucket`)
5. If creating: set Block Public Access, enable SSE-S3, set lifecycle rule to clean orphaned multipart uploads after 7 days
6. Prompt for local sync folder (default: `~/Sahara`)
7. Create `~/.sahara/` directory, write `~/.sahara/config.toml`
8. Create `.saharaignore` template in sync folder
9. Run `sahara doctor` preflight check (see Section 4.2)
10. Print summary and confirm

If bucket already exists and has content, offer to import existing objects into state DB.

### 4.2 `sahara doctor` — Preflight Check

Validates:
- AWS credentials valid and not expired
- Bucket exists and is accessible
- Required IAM permissions present: `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`, `s3:ListBucket`, `s3:RestoreObject`, `s3:GetObjectRestoreStatus`
- Bucket region matches configured region
- Local sync folder exists and is readable/writable
- State DB is not corrupted
- No orphaned multipart uploads in S3 (list and warn)

### 4.3 Full Command Reference

```
# Setup
sahara init [--bucket=NAME] [--region=REGION] [--folder=PATH]
sahara doctor                                Preflight check: credentials, permissions, config
sahara config set <key> <value>
sahara config get <key>
sahara config show

# Sync
sahara sync [--dry-run] [--verify]           Bidirectional sync
sahara push [path] [--dry-run] [--verify]    Upload local changes to S3
sahara pull [path] [--dry-run] [--verify]    Download remote changes to local
sahara status                                Show what would change (non-destructive)
sahara diff [path]                           Show content diff metadata (not file contents)

# File Operations
sahara ls [path] [--tier=hot|cold] [--long]  List remote files
sahara rm <path> [--force]                   Delete from remote (prompts unless --force)
sahara rm <path> --local [--force]           Delete local copy only
sahara mv <old-path> <new-path>              Rename/move file in remote (no re-upload)

# Archive / Glacier
sahara archive <path> [--older-than=DAYS]    Move to Glacier Deep Archive
sahara restore <path> [--tier=bulk|standard|expedited] [--wait]  Initiate restore
sahara restore-status <path>                 Check restore job status
sahara restore-download <path>               Download once restore is available

# Information
sahara usage [--simulate] [--month=YYYY-MM]  Storage/cost report with projections

# Daemon
sahara daemon start [--on-login]             Start background sync daemon
sahara daemon stop
sahara daemon status
sahara daemon logs [--tail=50]

# Maintenance
sahara recover [path]                        Show last known versions from sync_history (read-only audit)
```

### 4.4 Configuration Keys
```toml
[aws]
profile = ""                    # AWS profile name (alternative to access_key_id)
access_key_id = ""              # AWS Access Key ID (prefer env var or profile)
secret_access_key = ""          # AWS Secret Access Key (prefer env var or profile)
region = "us-east-1"
bucket = ""
sse = "SSE-S3"                  # SSE-S3 | SSE-KMS | none
kms_key_id = ""

[sync]
folder = "~/Sahara"
exclude = []                    # Additional glob patterns (gitignore syntax)
auto_archive_days = 0           # 0 = disabled
conflict_strategy = "backup"    # newest-wins | manual | backup
bandwidth_limit_kbps = 0        # 0 = unlimited
debounce_seconds = 5

[encryption]
client_side = false
# passphrase stored in OS keychain, not config file

[restore]
default_tier = "bulk"           # bulk | standard | expedited
temp_expiry_days = 7
notify_on_complete = true       # Desktop notification when restore done

[performance]
multipart_threshold_mb = 100
multipart_part_size_mb = 8
max_concurrent_uploads = 4
max_concurrent_downloads = 4
```

---

## 5. Sync Engine

### 5.1 Sync Algorithm
1. **Acquire lock**: `~/.sahara/sync.lock` (fail fast if `--force` not set)
2. **Scan local**: Walk sync folder, skip `.saharaignore` matches; for each file: if mtime or size changed since last sync, compute SHA-256; else reuse cached checksum from state DB
3. **Fetch remote index**: `s3:ListObjectsV2` with metadata; paginate until complete
4. **Diff**: Three-way compare (local state DB, current local, current remote)
5. **Detect renames**: Files with matching SHA-256 but different paths → treat as move
6. **Resolve conflicts**: Apply configured strategy (see 5.3)
7. **Execute**: Upload new/modified (multipart if >100MB), download remote-only, `s3:CopyObject` for moves, mark deleted
8. **Update state DB**: Record results, update `sync_history`
9. **Release lock**

### 5.2 Change Detection
- **Local**: `mtime` + `size` change → recompute SHA-256; otherwise use cached checksum
- **Conflict timestamp authority**: `x-amz-meta-sahara-modified-at` (set at upload time from local mtime) is the authoritative timestamp for conflict resolution — NOT the S3 `Last-Modified` header, NOT the local machine clock at sync time. This prevents clock-skew issues across machines.
- **Full verify mode** (`--verify`): Recomputes all SHA-256s regardless of mtime — useful for post-migration integrity checks

### 5.3 Conflict Resolution
A conflict occurs when: local file has changed (different checksum) AND remote file has changed since last sync (different ETag/metadata checksum).

| Strategy | Behavior |
|----------|----------|
| `newest-wins` | File with most recent `x-amz-meta-sahara-modified-at` wins; loser is discarded |
| `manual` | Sync halts for conflicting files; user must resolve with `sahara sync --resolve=keep-local|keep-remote|backup` |
| `backup` | Both versions kept: remote downloaded as `<filename>.conflict-TIMESTAMP.<ext>`; local version pushed as canonical (default) |

### 5.4 Rename / Move Handling
1. After diff, collect local deletes (path A gone) and local adds (path B new with same SHA-256 as A)
2. If match found: issue `s3:CopyObject` from A-key to B-key, then `s3:DeleteObject` A-key
3. No content re-upload; counts as 1 COPY + 1 DELETE request (cheap)
4. If no SHA-256 match: treated as delete + new upload

### 5.5 Large File Support
- Files > `multipart_threshold_mb` (default 100MB) use S3 multipart upload
- Part size: configurable, default 8MB
- In-progress upload IDs stored in `pending_multipart` table
- On resume: fetch existing parts from S3, continue from last uploaded part
- On `sahara init` and daemon startup: list and warn about orphaned multipart uploads > 7 days old
- S3 lifecycle rule (set by `sahara init`): auto-abort multipart uploads after 7 days

### 5.6 Exclusion Patterns
Two sources, merged (both applied):
1. `.saharaignore` file in sync root — gitignore syntax, per-directory support
2. `sync.exclude` in config — additional global patterns (gitignore syntax)

Default built-in excludes (always applied, not configurable):
```
.DS_Store
Thumbs.db
desktop.ini
*.tmp
*.swp
~$*
.Trash-*
```

---

## 6. Glacier Archive Operations

### 6.1 Archive Flow
1. Verify file exists in Hot tier (state DB + S3 check)
2. `s3:CopyObject` to same key with `StorageClass=DEEP_ARCHIVE`
3. `s3:DeleteObject` original (original is now superseded by DEEP_ARCHIVE copy)
4. Update state DB: `tier=cold`, `archived_at=now`
5. Print: "Archived <path> to Glacier Deep Archive. Retrieval will take 12-48h."

**Note**: Glacier Deep Archive has a minimum storage duration of 180 days. Deleting before 180 days still incurs the 180-day charge. Sahara warns if attempting to archive files that were uploaded less than 180 days ago.

### 6.2 Restore Flow
1. `sahara restore <path> [--tier=bulk] [--wait]`:
   - Verify file is in Cold tier
   - Call `s3:RestoreObject` with `Days=restore_expiry_days` (default 7)
   - Store `restore_job_id` in state DB
   - Print estimated completion time
   - If `--wait`: poll `s3:HeadObject` every 30 minutes until restore complete, then print notification
2. `sahara restore-status <path>`: Show restore job status from `HeadObject` response (`x-amz-restore` header)
3. `sahara restore-download <path>`:
   - Check restore is complete (error with ETA if not)
   - Download to local sync folder
   - Update state DB: `tier=hot_temp`, `restore_expires_at=date`
   - File remains in Glacier; temporary Hot copy cleaned up by Sahara after `restore_expires_at`
4. Daemon: polls all pending restores every 30 min; sends desktop notification when complete

### 6.3 Restore Expiry Handling
- State DB tracks `restore_expires_at` for all restored files
- Daemon runs daily check: warn 24h before expiry, warn on expiry day
- After expiry: state DB marks file as `cold` again, local copy (if downloaded) remains but is noted as "local only, not synced"

### 6.4 Restore Tiers
| Tier | Speed | Cost per GB |
|------|-------|-------------|
| Bulk | 12-48h | ~$0.0025 |
| Standard | 3-5h | ~$0.01 |
| Expedited | 1-5min | ~$0.03 |

Default: `bulk`. Override with `--tier=standard|expedited`.

---

## 7. Daemon / Background Sync

### 7.1 Daemon Operation
- Runs as a background process
- File system watching via `watchdog` library (inotify on Linux, FSEvents on macOS, ReadDirectoryChangesW on Windows)
- Debounces rapid changes: 5s default window before triggering sync for a path
- On file system event: sync only the affected paths (not full sync)
- Scheduled full sync every 6 hours (catches any missed events)
- Logs to `~/.sahara/daemon.log` (rotating, max 10MB, keep 3)
- PID file: `~/.sahara/daemon.pid`
- Restore polling: every 30 minutes, checks all pending restore jobs
- Sends desktop notifications via `plyer` library (cross-platform: macOS, Linux, Windows)

### 7.2 Network Awareness
- Detect network availability before sync attempt; retry with backoff if offline
- Configurable bandwidth throttling (`sync.bandwidth_limit_kbps`)
- Pause sync flag: `sahara daemon pause` / `sahara daemon resume` for manual control

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

Run 'sahara usage --simulate' for projected costs based on sync frequency.
```

### 8.2 `sahara usage --simulate` Output
Projects costs based on average daily sync volume from last 30 days.

---

## 9. Error Handling & Resilience

| Error Type | Behavior |
|------------|----------|
| Network timeout | Retry with exponential backoff: 1s, 2s, 4s (max 3 retries) |
| S3 rate limiting (503) | Retry with jitter backoff up to 60s |
| Missing permissions | Fail immediately with specific error message and required IAM action |
| Bucket not found | Fail with "Run `sahara doctor` to diagnose" message |
| State DB corrupted | Rename corrupted DB, rebuild from S3 listing (`sahara doctor --repair`) |
| Disk full (download) | Fail immediately, log, send daemon notification |
| File in use (Windows) | Skip file, log warning, retry on next sync cycle |

All errors logged with: timestamp, operation, path, error code, full stack trace → `~/.sahara/error.log` (rotating 10MB, keep 5)

---

## 10. Security Considerations

- AWS credentials: prefer AWS profile (`~/.aws/credentials`) or env vars; config file stores profile name only, not secrets
- Passphrase for client-side encryption stored in OS keychain (macOS Keychain, Linux Secret Service, Windows Credential Manager) via `keyring` library
- S3 bucket created by `sahara init` has Block Public Access enabled by default
- All API calls use HTTPS (boto3 default)
- Supports AWS IAM role-based auth (for EC2/server usage)
- Minimum required IAM policy documented in README

---

## 11. Performance Targets

| Metric | Target |
|--------|--------|
| Sync latency (daemon, file change event) | < 10s from event to upload start |
| Full scan of 100,000 files (local) | < 30s |
| S3 listing of 100,000 objects | < 60s (paginated) |
| Daemon memory usage (idle) | < 50MB RSS |
| State DB query (lookup file by path) | < 10ms |
| Concurrent upload/download streams | 4 (configurable) |

---

## 12. Technology Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.11+ |
| AWS SDK | boto3 |
| CLI Framework | Click |
| Local DB | SQLite (direct, no ORM — for simplicity) |
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
│       ├── cli.py              # Click CLI entry point + all commands
│       ├── config.py           # TOML config read/write
│       ├── sync_engine.py      # Core sync algorithm
│       ├── s3_client.py        # S3/Glacier boto3 operations
│       ├── state_db.py         # SQLite state management
│       ├── file_watcher.py     # watchdog FS event handling
│       ├── daemon.py           # Background daemon process management
│       ├── encryption.py       # AES-256-GCM client-side encryption
│       ├── cost_estimator.py   # Cost calculation + display
│       ├── ignore_rules.py     # .saharaignore / gitignore pattern matching
│       ├── notifier.py         # Desktop notifications via plyer
│       └── models.py           # Dataclasses for FileRecord, SyncOp, etc.
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

1. **Multiple sync folders (profiles)**: Not in v1. Single folder only. `sahara init` sets one folder.
2. **Glacier restore trigger**: Always manual. No automatic restore on file access.
3. **Pre-signed URL sharing**: Not in v1. Non-goal.
4. **File versioning**: Not in v1. Overwritten files are NOT recoverable. `sync_history` provides an audit log of operations only.
5. **Rename detection**: Supported via SHA-256 matching (see 5.4).
6. **Conflict timestamp authority**: `x-amz-meta-sahara-modified-at` (upload-time local mtime), not sync time or S3 last-modified.
7. **Daemon + CLI locking**: Advisory file lock; CLI blocks or fails with clear message.
8. **Orphaned multipart cleanup**: S3 lifecycle rule set on bucket creation; also listed on `sahara doctor`.
9. **Glacier 180-day minimum**: Sahara warns on archive attempt for recently uploaded files.
