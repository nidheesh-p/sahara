# Sahara

Personal cloud storage CLI backed by AWS S3.

Sahara syncs a local folder to an S3 bucket with:
- Bidirectional sync with three-way diff (local / remote / last-known-good base)
- Client-side AES-256-GCM encryption (optional)
- Glacier / Deep Archive archiving with restore support
- Background daemon with file-watching (watchdog)
- Rename detection, conflict resolution, cost reporting

---

## Requirements

- Python 3.11+
- AWS account with an S3 bucket
- IAM user or role with: `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket`, `s3:GetBucketLocation`

---

## Installation

```bash
pip install sahara
```

For semantic search support (large download, ~200 MB):

```bash
pip install "sahara[search]"
```

Or from source:

```bash
git clone https://github.com/nidheesh-p/sahara
cd sahara
pip install -e .
```

---

## AWS Credentials

Sahara uses the standard AWS credential chain. Choose one method:

**Option 1 — Environment variables (recommended for simple setups)**
```bash
export AWS_ACCESS_KEY_ID=AKIAxxxxxxxxxxxxxxxx
export AWS_SECRET_ACCESS_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export AWS_DEFAULT_REGION=us-east-1
```

**Option 2 — AWS CLI profile**
```bash
aws configure          # creates ~/.aws/credentials
# then choose "profile" during `sahara init`
```

**Option 3 — IAM role** (EC2, ECS, Lambda — no keys needed, works automatically)

To create an IAM user with the minimum required permissions, attach this policy:
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
      "s3:ListBucket", "s3:GetBucketLocation",
      "s3:RestoreObject", "s3:GetObjectAttributes"
    ],
    "Resource": [
      "arn:aws:s3:::YOUR-BUCKET-NAME",
      "arn:aws:s3:::YOUR-BUCKET-NAME/*"
    ]
  }]
}
```

---

## Quick Start

### 1. Initialise

```bash
sahara init
```

The wizard will prompt for:
- Sync folder path (default: `~/Sahara`)
- S3 bucket name
- AWS region
- S3 key prefix (optional)
- Encryption passphrase (optional)
- Conflict resolution strategy

### 2. Run a sync

```bash
sahara sync
```

### 3. Check status

```bash
sahara status
```

### 4. Start background daemon

```bash
sahara daemon start
sahara daemon status
```

---

## Commands

| Command | Description |
|---|---|
| `sahara init` | Interactive setup wizard |
| `sahara doctor [--repair]` | Diagnose configuration and connectivity |
| `sahara sync` | Bidirectional sync |
| `sahara push` | Upload local changes only |
| `sahara pull` | Download remote changes only |
| `sahara status` | Show pending changes |
| `sahara diff` | Alias for status |
| `sahara ls [--long] [--tier]` | List tracked files |
| `sahara rm <path>` | Delete a file |
| `sahara mv <src> <dst>` | Rename / move a file |
| `sahara conflicts` | List unresolved conflicts |
| `sahara resolve` | Resolve conflicts |
| `sahara archive <paths>` | Move files to Glacier |
| `sahara restore <path>` | Initiate Glacier restore |
| `sahara restore-status` | Check restore progress |
| `sahara restore-download <path>` | Download a restored file |
| `sahara usage` | Storage usage and cost report |
| `sahara history` | Sync history log |
| `sahara config show/get/set` | Manage configuration |
| `sahara encryption setup` | Enable encryption |
| `sahara encryption rotate` | Rotate encryption key |
| `sahara daemon start/stop/status/pause/resume/logs` | Daemon control |

---

## Configuration

Config file: `~/.sahara/config.toml`

Key settings:

```toml
sync_folder = "/Users/you/Sahara"
bucket = "my-sahara-bucket"
region = "us-east-1"
prefix = ""
encryption_enabled = false
conflict_strategy = "backup"   # backup | local | remote | ask
max_workers = 8
multipart_threshold_mb = 100
```

---

## Encryption

Sahara uses AES-256-GCM with PBKDF2-HMAC-SHA256 key derivation (600,000 iterations).
The passphrase is stored in the system keyring (macOS Keychain, libsecret on Linux, Windows Credential Manager).

```bash
sahara encryption setup    # Enable and store passphrase
sahara encryption rotate   # Rotate to a new passphrase
```

---

## Ignore Rules

Place a `.saharaignore` file in your sync folder (same syntax as `.gitignore`):

```
*.tmp
node_modules/
.DS_Store
secrets/
```

---

## Archiving

```bash
# Archive files older than 180 days
sahara archive --older-than 180

# Archive specific files
sahara archive documents/old-report.pdf

# Check cost
sahara usage
```

---

## License

MIT
