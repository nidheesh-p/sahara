# Sahara Cloud Storage — Product Specification v1.0

## 1. Overview

**Sahara** is a personal, self-hosted cloud storage system built on AWS S3 that provides a Dropbox-like experience without recurring subscription costs. Users pay only for what they store and transfer, directly to AWS.

### 1.1 Problem Statement
Consumer cloud storage services (Google Drive, iCloud, Dropbox) charge monthly subscriptions regardless of usage. AWS S3 pricing is usage-based, making it significantly cheaper for users who store large amounts of data infrequently accessed, especially when cold-tier archival storage is used.

### 1.2 Goals
- Provide seamless, bidirectional file sync between a local folder and AWS S3
- Support two storage tiers: Hot (S3 Standard) and Cold (Glacier Deep Archive)
- Offer a fast, intuitive CLI for all operations
- Track file changes efficiently using checksums (no unnecessary re-uploads)
- Support personal use — single user, multiple machines

### 1.3 Non-Goals
- Multi-user/team collaboration (v1)
- Web UI (v1)
- Mobile apps (v1)
- Real-time collaboration / locking
- Full POSIX filesystem semantics

---

## 2. User Stories

1. **US-01**: As a user, I want to sync a local folder to S3 so my files are backed up in the cloud.
2. **US-02**: As a user, I want to download synced files from any machine to restore my data.
3. **US-03**: As a user, I want to archive old files to Glacier Deep Archive to save money on cold data.
4. **US-04**: As a user, I want to restore archived files from Glacier when I need them.
5. **US-05**: As a user, I want to see a list of all my files with their storage tier, size, and last modified date.
6. **US-06**: As a user, I want automatic conflict detection when syncing from multiple machines.
7. **US-07**: As a user, I want to configure which folders/files to exclude from sync (like .gitignore).
8. **US-08**: As a user, I want to see bandwidth and storage usage cost estimates.
9. **US-09**: As a user, I want incremental sync — only changed files are uploaded/downloaded.
10. **US-10**: As a user, I want encryption at rest and in transit.

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
│                       │  - S3Client                  │   │
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

### 3.3 Local State Database (SQLite)
Location: `~/.sahara/state.db`

Tables:
- `files`: Tracks all synced files (path, etag/checksum, size, tier, last_sync, last_modified)
- `sync_history`: Log of all sync operations
- `config`: Key-value configuration store

### 3.4 S3 Metadata Convention
Each file in S3 has object metadata:
- `x-amz-meta-sahara-checksum`: SHA-256 of original file
- `x-amz-meta-sahara-original-path`: Original relative path
- `x-amz-meta-sahara-modified-at`: Last modified timestamp
- `x-amz-meta-sahara-tier`: `hot` or `cold`

### 3.5 Encryption
- **In transit**: HTTPS/TLS (enforced by AWS SDK)
- **At rest**: AWS S3 Server-Side Encryption (SSE-S3 or SSE-KMS, user configurable)
- **Client-side** (optional): AES-256 encryption before upload using user-provided passphrase

---

## 4. CLI Specification

### 4.1 Commands

```
sahara init                          Initialize Sahara in current directory
sahara config set <key> <value>      Set configuration value
sahara config get <key>              Get configuration value
sahara config show                   Show all configuration

sahara sync                          Bidirectional sync (push + pull)
sahara push [path]                   Upload local changes to S3
sahara pull [path]                   Download remote changes to local
sahara status                        Show sync status (what would change)

sahara ls [path]                     List files in remote storage
sahara ls --tier=cold                List only archived files
sahara ls --tier=hot                 List only hot storage files

sahara archive <path>                Move file(s) from hot to Glacier
sahara archive --older-than=90d      Archive files not accessed in 90 days
sahara restore <path>                Request restore from Glacier (initiates job)
sahara restore-status <path>         Check restore job status
sahara restore-download <path>       Download restored file once available

sahara diff                          Show differences between local and remote

sahara rm <path>                     Delete file from remote storage
sahara rm --local                    Delete local copy only

sahara usage                         Show storage usage and cost estimates

sahara daemon start                  Start background sync daemon
sahara daemon stop                   Stop background sync daemon
sahara daemon status                 Check daemon status
```

### 4.2 Configuration Keys
```
aws.access_key_id         AWS Access Key ID
aws.secret_access_key     AWS Secret Access Key
aws.region                AWS Region (default: us-east-1)
aws.bucket                S3 Bucket Name
aws.sse                   Server-side encryption (SSE-S3, SSE-KMS, none)
aws.kms_key_id            KMS Key ID (if SSE-KMS)

sync.folder               Local folder to sync (absolute path)
sync.exclude              Comma-separated glob patterns to exclude
sync.auto_archive_days    Days before auto-archiving (0=disabled)
sync.conflict_strategy    Strategy for conflicts (newest-wins, manual, backup)
sync.bandwidth_limit      Max upload bandwidth in KB/s (0=unlimited)

encryption.client_side    Enable client-side encryption (true/false)
encryption.passphrase     Passphrase for client-side encryption (stored in keychain)
```

---

## 5. Sync Engine

### 5.1 Sync Algorithm
1. **Scan local**: Walk sync folder, compute SHA-256 for changed files
2. **Fetch remote index**: List S3 objects with metadata
3. **Diff**: Compare local state DB with S3 state
4. **Resolve conflicts**: Apply conflict strategy
5. **Execute**: Upload new/modified, download remote-only, mark deleted
6. **Update state DB**: Record sync results

### 5.2 Change Detection
- Local: Modified timestamp + file size change triggers SHA-256 recompute
- Remote: ETag comparison (MD5 for single-part, composite for multipart)
- Full checksum verify mode available via `--verify` flag

### 5.3 Conflict Resolution
A conflict occurs when both local and remote versions have changed since last sync.

| Strategy | Behavior |
|----------|----------|
| `newest-wins` | The file with the most recent mtime wins (default) |
| `manual` | Pause sync, report conflict, require user resolution |
| `backup` | Keep both: rename local copy with `.conflict-TIMESTAMP` suffix |

### 5.4 Large File Support
- Files > 100MB use S3 multipart upload (8MB parts)
- Resume interrupted uploads using multipart upload IDs stored in state DB
- Configurable part size

### 5.5 Exclusion Patterns
Uses `.gitignore`-style patterns in `.saharaignore` file or via config:
- `*.tmp`, `*.DS_Store`, `node_modules/`, `.git/` (default excludes)
- User-configurable via `sync.exclude` config or `.saharaignore` file

---

## 6. Glacier Archive Operations

### 6.1 Archive Flow
1. Verify file exists in S3 Hot tier
2. Copy object to same bucket with `DEEP_ARCHIVE` storage class
3. Delete original Hot tier object
4. Update local state DB: tier = `cold`, archived_at = now

### 6.2 Restore Flow
1. `sahara restore <path>`: Initiate S3 Glacier restore (Bulk tier = cheapest)
2. Returns job ID, estimated completion time (12-48h)
3. `sahara restore-status <path>`: Poll restore status
4. `sahara restore-download <path>`: Download once available (creates temporary Hot copy)
5. Temporary Hot copy expires in 7 days (configurable)
6. After download, file remains in Glacier; Hot copy is cleaned up

### 6.3 Restore Tiers
| Tier | Speed | Cost |
|------|-------|------|
| Bulk | 12-48h | Cheapest |
| Standard | 3-5h | Moderate |
| Expedited | 1-5min | Expensive |

Default: Bulk. Override with `--restore-tier=standard|expedited`.

---

## 7. Daemon / Background Sync

### 7.1 Daemon Operation
- Runs as a background process (uses watchdog/inotify for file system events)
- Debounces rapid file changes (5s default window)
- Syncs on file system events (create, modify, delete, move)
- Also runs scheduled full sync every 6 hours
- Logs to `~/.sahara/daemon.log`
- PID file: `~/.sahara/daemon.pid`

### 7.2 Network Awareness
- Pause sync on metered/slow connections (optional)
- Configurable bandwidth throttling

---

## 8. Cost Estimation

### 8.1 Usage Command Output
```
Storage:
  Hot (S3 Standard):      45.3 GB   ~$1.04/month
  Cold (Glacier Deep):   892.1 GB   ~$0.88/month
  Total:                 937.4 GB   ~$1.92/month

Requests (this month):
  PUT/COPY:              1,203       ~$0.006
  GET:                     456       ~$0.002
  Glacier retrievals:       12       ~$0.001

Data Transfer:
  Upload:                 2.3 GB    Free
  Download:               0.8 GB    ~$0.07

Estimated Monthly Total: ~$2.00
```

---

## 9. Error Handling & Resilience

- Automatic retry with exponential backoff for transient AWS errors
- Maximum 3 retries per operation
- Partial sync state saved — resume on next run
- Graceful handling of: network interruption, rate limiting, permissions errors
- All errors logged with full context to `~/.sahara/error.log`

---

## 10. Security Considerations

- AWS credentials never stored in plain text (use AWS profile or env vars)
- Optional keychain integration for passphrase storage
- All API calls use HTTPS
- S3 bucket policy enforced: Block all public access
- Support AWS IAM role (for EC2/ECS usage)

---

## 11. Technology Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.11+ |
| AWS SDK | boto3 |
| CLI Framework | Click |
| Local DB | SQLite (via SQLAlchemy) |
| File Watching | watchdog |
| Config | TOML (tomllib / tomli) |
| Testing | pytest + moto (AWS mock) |
| Packaging | pip / pyproject.toml |

---

## 12. File Structure

```
sahara/
├── pyproject.toml
├── README.md
├── src/
│   └── sahara/
│       ├── __init__.py
│       ├── cli.py              # Click CLI entry point
│       ├── config.py           # Configuration management
│       ├── sync_engine.py      # Core sync logic
│       ├── s3_client.py        # S3/Glacier operations
│       ├── state_db.py         # SQLite state management
│       ├── file_watcher.py     # FS event watching (daemon)
│       ├── daemon.py           # Background daemon
│       ├── encryption.py       # Client-side encryption
│       ├── cost_estimator.py   # Cost calculation
│       └── models.py           # Data models
└── tests/
    ├── conftest.py
    ├── test_sync_engine.py
    ├── test_s3_client.py
    ├── test_state_db.py
    ├── test_cli.py
    ├── test_config.py
    ├── test_encryption.py
    └── test_cost_estimator.py
```

---

## 13. Open Questions
1. Should we support multiple sync folders (profiles)?
2. Should Glacier restore be automatic when a file is accessed, or always manual?
3. Should we support sharing files via pre-signed URLs?
4. Should we support versioning (S3 versioning + local version history)?
