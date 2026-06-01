# Sahara

**Local-first personal storage with semantic search.**

Sahara syncs your files to wherever you want — AWS S3, a self-hosted MinIO server, or locally mounted hard drives — and lets you search them with natural language.

```
sahara search "2023 tax return"
sahara ask "when does my passport expire?"
```

---

## What Sahara does

| Capability | Details |
|---|---|
| **Storage backends** | AWS S3, MinIO (self-hosted), local drives, local+Glacier |
| **Sync** | Bidirectional three-way diff (local / remote / last-known base) |
| **Encryption** | AES-256-GCM, PBKDF2-SHA256 key derivation, keyring storage |
| **Semantic search** | sqlite-vec KNN on BAAI/bge-small-en-v1.5 384-dim embeddings |
| **Ask** | LLM-powered answers from your files (OpenAI or local Ollama) |
| **Archiving** | Glacier / Deep Archive tiering with restore tracking |
| **Background sync** | Watchdog-based daemon with autostart support |
| **Multiple folders** | Register any number of folders, each synced independently |

---

## Installation

```bash
pip install sahara
```

Add semantic search and `sahara ask`:

```bash
pip install "sahara[search]"   # ~200 MB: fastembed, pypdf, python-docx, sqlite-vec
```

From source:

```bash
git clone https://github.com/nidheesh-p/sahara
cd sahara
pip install -e ".[search]"
```

---

## Quick Start

### 1. Initialise

```bash
sahara init
```

Choose your storage backend:

```
aws           — Amazon S3 with Glacier tiering (pay-per-use cloud)
minio         — Self-hosted MinIO / S3-compatible server
local         — Locally mounted hard drives (no cloud)
local+glacier — Drives as primary + S3 Glacier as cold backup
```

### 2. Sync

```bash
sahara sync       # bidirectional
sahara push       # upload only
sahara pull       # download only
```

### 3. Search your files

Index file contents, then search:

```bash
sahara index
sahara search "invoice from contractor 2024"
```

### 4. Ask questions

```bash
sahara ask "what is my passport expiry date?"
sahara ask "summarise the Q3 budget notes"
```

Uses OpenAI (`gpt-4o-mini`) when `OPENAI_API_KEY` is set, otherwise tries a local Ollama instance. Degrades gracefully to showing matching snippets if no LLM is available.

---

## Storage Backends

### AWS S3 (default)

Full feature support including Glacier archiving and restore.

```bash
export AWS_ACCESS_KEY_ID=AKIAxxxxxxxxxxxxxxxx
export AWS_SECRET_ACCESS_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export AWS_DEFAULT_REGION=us-east-1
sahara init   # choose "aws"
```

Minimum IAM policy:
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
      "arn:aws:s3:::YOUR-BUCKET",
      "arn:aws:s3:::YOUR-BUCKET/*"
    ]
  }]
}
```

### MinIO (self-hosted)

Drop-in replacement for S3. Glacier-specific features are not available.

```bash
sahara init   # choose "minio"
# prompts for endpoint URL, access key, secret key, bucket name
```

### Local drives

Files are written to one or more mounted drives (NAS, external HDD, USB). Every write goes to **all** configured drives independently — no RAID required. Drives are append-only by default: deleting a file locally does not remove it from drives.

```bash
sahara init   # choose "local"
# enter drive paths, e.g. /Volumes/Drive1/Sahara
```

### Local + Glacier

Drives as primary storage, S3 Glacier as a cold backup. Useful for archiving important files while keeping fast local access.

```bash
sahara init   # choose "local+glacier"
```

---

## Semantic Search

Sahara uses [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) (384-dimensional embeddings) with sqlite-vec for fast KNN search.

**Supported file types:** PDF, DOCX, Markdown, plain text, code files (`.py`, `.js`, `.ts`, etc.), YAML, TOML, CSV, HTML, XML.

Files are split into 1600-character chunks (320-character overlap) so specific passages — including page 30 of a long PDF — are independently retrievable.

```bash
sahara index                        # index all synced files
sahara index --force                # re-index everything
sahara index --folder ~/Documents   # index a specific folder

sahara search "quarterly report"
sahara search --snippet "tax year 2023"   # show matching text
sahara search -n 10 "passport"            # top 10 results
```

---

## Ask (LLM Q&A)

`sahara ask` retrieves relevant chunks, builds a context window, and sends it to an LLM for a grounded answer.

```bash
sahara ask "when does my car insurance expire?"
sahara ask "what was the project deadline in the notes?"
sahara ask local "summarise the meeting minutes"   # force local Ollama
```

**Provider selection:**
- `OPENAI_API_KEY` set → OpenAI (`gpt-4o-mini` by default)
- No key → local Ollama (`mistral` by default, `http://localhost:11434`)
- `--provider openai|ollama` to override
- `--model gpt-4o` or `--model llama3` to select model

---

## Commands

| Command | Description |
|---|---|
| `sahara init` | Interactive setup wizard |
| `sahara doctor [--repair]` | Diagnose config and storage connectivity |
| `sahara sync [--dry-run]` | Bidirectional sync |
| `sahara push [--dry-run]` | Upload local changes only |
| `sahara pull [--dry-run]` | Download remote changes only |
| `sahara status` | Show pending changes |
| `sahara diff` | Alias for `status` |
| `sahara ls [-l] [--tier]` | List tracked files |
| `sahara rm <path>` | Remove a file |
| `sahara mv <src> <dst>` | Rename / move a file |
| `sahara conflicts` | List unresolved conflicts |
| `sahara resolve` | Resolve a conflict |
| `sahara add <path>` | Register an additional sync folder |
| `sahara remove <path>` | Unregister a folder |
| `sahara folders` | List all registered folders |
| `sahara index [--force]` | Index file contents for search |
| `sahara search <query>` | Semantic search |
| `sahara ask <question>` | LLM-powered Q&A over your files |
| `sahara archive [paths]` | Move files to Glacier |
| `sahara restore <path>` | Initiate Glacier restore |
| `sahara restore-status` | Check restore progress |
| `sahara restore-download <path>` | Download a restored file |
| `sahara usage [--simulate]` | Storage usage and cost report |
| `sahara history` | Sync history log |
| `sahara config show/get/set` | Manage configuration |
| `sahara encryption setup` | Enable AES-256-GCM encryption |
| `sahara encryption rotate` | Rotate encryption passphrase |
| `sahara daemon start/stop/status/pause/resume/logs` | Background daemon |

---

## Configuration

Config file: `~/.sahara/config.toml`

```toml
sync_folder       = "/Users/you/Sahara"
storage_mode      = "s3"          # s3 | local | local+glacier
bucket            = "my-bucket"
region            = "us-east-1"
prefix            = ""
endpoint_url      = ""            # set for MinIO, e.g. http://192.168.1.10:9000
encryption_enabled = false
conflict_strategy = "backup"      # backup | local | remote | ask
upload_only       = false         # this machine only pushes, never pulls
max_workers       = 8
```

Manage without editing the file:

```bash
sahara config show
sahara config get conflict_strategy
sahara config set conflict_strategy local
```

---

## Encryption

Client-side AES-256-GCM encryption. The passphrase is stored in the system keyring (macOS Keychain, libsecret on Linux, Windows Credential Store) — never written to disk.

```bash
sahara encryption setup    # enable and store passphrase
sahara encryption rotate   # re-encrypt all files with a new passphrase
```

Wire format: `[16-byte salt][12-byte nonce][ciphertext + 16-byte auth tag]`

---

## Ignore Rules

Create `.saharaignore` in your sync folder (gitignore syntax):

```
*.tmp
.DS_Store
node_modules/
secrets/
```

---

## Archiving (AWS S3 only)

```bash
sahara archive --older-than 180          # archive files not modified in 6 months
sahara archive documents/old-report.pdf  # archive a specific file
sahara archive --storage-class DEEP_ARCHIVE --older-than 365

sahara restore documents/old-report.pdf  # initiate restore (takes hours for DEEP_ARCHIVE)
sahara restore-status                    # check all pending restores
sahara restore-download documents/old-report.pdf
```

---

## License

MIT
