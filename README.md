# Sahara

> Sahara: extended storage, searchable memory and instant retrieval.
> Find the right file by meaning, even when you forget its name.

---

## The problem

Most people now have years of important context scattered across PDFs, notes, code,
invoices, screenshots, folders, drives, and cloud buckets. Traditional file search only
works when you remember the exact filename or keyword. General chat assistants can
answer questions, but they usually cannot see your local files, cannot maintain a
durable index of your computer, and often require sending private documents to a cloud
service.

Sahara turns your files into searchable memory. Local semantic indexing is the basic
mode and requires no cloud account or extra drive. Optional local-drive and S3 storage
can then sync and protect selected indexed folders. Sahara extracts text, chunks long
documents, embeds those chunks into a local vector database, and lets you search or ask
questions with citations.

Sahara is not trying to be a general autonomous agent. Claude Desktop can act as a
local chat front end today; OpenClaw and ChatGPT connectors are possible future client
paths. Sahara's job is to be the trusted local index: retrieve the right local context,
cite where it came from, and avoid broad filesystem access unless you explicitly opt in.

---

## What it does today

- **Index locally without storage setup** using basic mode
- **Add multiple content folders** and keep them index-only unless sync is explicitly enabled
- **Optionally sync** selected folders to S3, MinIO, or a locally mounted drive
- **Offload and fetch** verified stored files while keeping them discoverable in search
- **Encrypt** files client-side with AES-256-GCM before they leave your machine
- **Index** your documents — PDF, DOCX, Markdown, code, plain text — into a local vector database
- **Search** by meaning: `sahara search "tax return 2024"` finds the right file even if none of those words appear in the filename
- **Ask** natural language questions: `sahara ask "what is my passport expiry date?"` extracts the answer and cites the source
- **Expose read-only MCP tools** for chat clients and agent runtimes with `sahara mcp serve`

Indexing and semantic search run locally. Answer generation uses local Ollama by
default, but retrieved snippets are sent to OpenAI when that provider is selected.

## What is coming next

- Configure storage after starting in basic mode without rebuilding the index
- Explicitly offload and fetch files while preserving searchable metadata
- Validate Claude mobile access through authenticated remote MCP
- Future OpenClaw and ChatGPT connector guidance
- Hybrid retrieval: BM25 keyword + vector search with cross-encoder reranking
- Entity extraction: dates, names, amounts, document types
- OCR support via a plugin (opt-in, not default)
- Plugin marketplace for parsers, embedders, and rerankers

See [ROADMAP.md](ROADMAP.md) for the full plan.

---

## Installation

Sahara requires **Python 3.11 or newer**. Check with `python3 --version` before
installing; Python 3.9 and 3.10 are not supported. On Windows, use
`py -3.11` anywhere these examples use `python3`.

The Python distribution is named **`sahara-memory`**. It still installs the
`sahara` command and `sahara` Python package. Do not run `pip install sahara`:
that name belongs to the unrelated OpenStack data-processing project.

Until the first `sahara-memory` release is published to PyPI, install Sahara
directly from GitHub:

```bash
python3 -m pip install \
  "sahara-memory[search,mcp] @ git+https://github.com/nidheesh-p/sahara.git"
```

After the PyPI release, these shorter commands will be available:

```bash
# Local semantic search
python3 -m pip install "sahara-memory[search]"

# Semantic search plus MCP support for Claude Desktop
python3 -m pip install "sahara-memory[search,mcp]"

# Everything, including optional storage, MCP, and OCR dependencies
python3 -m pip install "sahara-memory[all]"
```

### Developer setup

```bash
git clone https://github.com/nidheesh-p/sahara
cd sahara
python3 -m pip install -e ".[search,dev]"
```

---

## Quick start

### Flow A: CLI search

```bash
python3 -m pip install \
  "sahara-memory[search,mcp] @ git+https://github.com/nidheesh-p/sahara.git"
sahara init --mode basic --folder ~/Documents
sahara index
sahara search "my tax return 2024" --snippet
```

No bucket, drive, credentials, or additional prompts are required. Add another local
folder at any time:

```bash
sahara folder add ~/Projects
sahara index
```

Attach storage later without rebuilding the index:

```bash
sahara storage configure local --drive /Volumes/Archive/Sahara
sahara folder sync ~/Documents --enable
sahara sync
```

After syncing and indexing a file, free its local disk space while retaining search:

```bash
sahara offload Documents/archive/report.pdf
sahara search "quarterly forecast"   # result is marked [offloaded]
sahara fetch Documents/archive/report.pdf
```

### Flow B: Ask from Claude Desktop

After connecting Claude Desktop, ask the same question:

```text
Use Sahara to find my tax return from 2024.
Include the source path and supporting snippet.
```

Claude calls Sahara's read-only MCP tools and returns citations from the same local
index used by the CLI.

---

## Connecting to Claude Desktop in 60 seconds

Prerequisite: Sahara is installed, initialized, and indexed.

1. Find the executable path with `command -v sahara` on macOS or
   `(Get-Command sahara).Source` in Windows PowerShell.
2. In Claude Desktop, open **Settings > Developer > Edit Config**.
3. Add this entry, replacing the command with the absolute path:

```json
{
  "mcpServers": {
    "sahara": {
      "command": "/absolute/path/to/sahara",
      "args": ["mcp", "serve", "--transport", "stdio"]
    }
  }
}
```

4. Fully quit and reopen Claude Desktop.
5. Click the plus icon in the chat input, open **Connectors**, and confirm **sahara**
   lists its tools.

Claude Desktop runs Sahara locally over stdio. Do not use HTTP or ngrok for this
same-computer setup. See [docs/CLAUDE_DESKTOP.md](docs/CLAUDE_DESKTOP.md) for platform
config locations, the complete tool contract, security boundaries, and troubleshooting.

---

## Storage backends

Storage is optional. Sahara supports basic indexing plus four storage-backed modes:

| Mode | Use case |
|------|----------|
| `basic` | Local indexing and semantic search with no storage destination |
| `local` | Second drive, NAS, or network share — no cloud account needed |
| `local+glacier` | Local drives as primary + S3 Glacier as cold backup |
| `minio` | Self-hosted S3-compatible object storage |
| `s3` (aws) | AWS S3 with optional Glacier archiving |

### Local drive

Files are written to one or more mounted drives independently — no RAID required. Drives are append-only by default (deleting a file locally does not remove it from drives).

```bash
sahara init --mode local --folder ~/Sahara \
  --storage-drive /Volumes/MyDrive/Backup
sahara sync
```

### MinIO (self-hosted)

```bash
docker run -p 9000:9000 -p 9001:9001 \
  -e MINIO_ROOT_USER=admin -e MINIO_ROOT_PASSWORD=password \
  minio/minio server /data --console-address :9001

sahara init   # choose 'minio', endpoint http://localhost:9000
sahara sync
```

### AWS S3

```bash
export AWS_ACCESS_KEY_ID=AKIAxxxxxxxx
export AWS_SECRET_ACCESS_KEY=xxxxxxxxxxxx
export AWS_DEFAULT_REGION=us-east-1

sahara init --mode aws --folder ~/Sahara \
  --bucket my-sahara-bucket --region us-east-1
sahara sync
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
      "arn:aws:s3:::YOUR-BUCKET-NAME",
      "arn:aws:s3:::YOUR-BUCKET-NAME/*"
    ]
  }]
}
```

---

## Search & ask

### Index your files

```bash
sahara index                    # scan and index all registered content roots
sahara index --force            # re-index everything (ignores content hash)
sahara index --folder ~/Docs    # index a specific registered folder
```

The first indexing run downloads the local embedding model (roughly 200 MB).
Hugging Face may print an unauthenticated-request warning during this download;
it is informational and no account or token is required. Setting `HF_TOKEN` is
optional and only improves download rate limits.

**Supported file types:** PDF, DOCX, Markdown, plain text, code (`.py`, `.js`, `.ts`, etc.), YAML, TOML, CSV, HTML, XML.

Files are split into 1600-character chunks with 320-character overlap so specific passages — including page 30 of a long PDF — are independently retrievable.

### Search by meaning

```bash
sahara search "tax return 2024"
sahara search "kitchen renovation quote" --top 10
sahara search "passport expiry" --snippet
```

### Ask a question

```bash
sahara ask "what is my passport expiry date?"
sahara ask "find the invoice from Amazon last month" --top 10
```

```
Answer: Your passport expires on Aug 14, 2032.

Source: Documents/Personal/passport_scan.pdf  (score: 94%)
  "…passport valid until 14 AUG 2032. Issued by Government of India…"

Note: Answer generated by local model mistral via Ollama.
```

**Provider selection:**
- `OPENAI_API_KEY` set → OpenAI (`gpt-4o-mini` by default)
- No key → local Ollama (`mistral` by default at `http://localhost:11434`)
- `sahara ask local "..."` or `--provider ollama` to force Ollama
- `--provider openai` or `--model gpt-4o` to override

---

## Command reference

### Core: searchable memory

Start here. These commands create and search a local semantic index; they do not
require an external drive, cloud account, or storage backend.

| Command | Description |
|---|---|
| `sahara init --mode basic --folder <path>` | Create a local searchable library |
| `sahara folder add/list/remove` | Manage indexed content roots |
| `sahara index [--force]` | Index file contents for semantic search |
| `sahara index-report` | Show indexed/unindexed counts and sample gaps |
| `sahara search <query>` | Find relevant files and passages by meaning |
| `sahara ask <question>` | Answer a question over indexed files with citations |
| `sahara mcp serve` | Expose read-only search and Q&A tools over MCP stdio |

<details>
<summary><strong>Optional storage and sync commands</strong></summary>

Use these commands only when you want to sync, protect, or offload indexed files to
another drive, MinIO, or AWS S3.

| Command | Description |
|---|---|
| `sahara init` | Interactive basic/local/AWS setup wizard |
| `sahara storage configure local/aws` | Attach storage to an existing basic library |
| `sahara storage status/disable` | Inspect or disable storage without deleting stored data |
| `sahara folder sync <path> --enable/--disable` | Select whether an indexed content root syncs |
| `sahara sync [--dry-run]` | Bidirectional sync |
| `sahara push [--dry-run]` | Upload local changes only |
| `sahara pull [--dry-run]` | Download remote changes only |
| `sahara status` | Show pending sync changes |
| `sahara diff` | Alias for `status` |
| `sahara ls [-l] [--tier]` | List tracked files |
| `sahara rm <path>` | Remove a tracked file |
| `sahara mv <src> <dst>` | Rename or move a tracked file |
| `sahara conflicts` | List unresolved sync conflicts |
| `sahara resolve` | Resolve a sync conflict |
| `sahara add <path>` | Register an additional sync folder |
| `sahara remove <path>` | Unregister a sync folder |
| `sahara folders` | List registered sync folders |
| `sahara offload <path>` | Verify storage, retain search data, and remove the local file |
| `sahara fetch <path>` | Restore an offloaded file with checksum verification |
| `sahara archive [paths]` | Move files to Glacier (AWS only) |
| `sahara restore <path>` | Initiate a Glacier restore |
| `sahara restore-status` | Check Glacier restore progress |
| `sahara restore-download <path>` | Download a restored file |
| `sahara usage [--simulate]` | Show storage usage and estimated cost |
| `sahara history` | Show sync history |
| `sahara encryption setup` | Enable AES-256-GCM encryption |
| `sahara encryption rotate` | Rotate the encryption passphrase |

</details>

<details>
<summary><strong>Configuration, diagnostics, and background operation</strong></summary>

| Command | Description |
|---|---|
| `sahara doctor [--repair]` | Diagnose configuration and storage connectivity |
| `sahara config show/get/set` | Manage configuration |
| `sahara daemon start/stop/status/pause/resume/logs` | Manage the background daemon |
| `sahara mcp serve --transport http --auth-token <token>` | Serve authenticated MCP over local HTTP for a secure tunnel or remote connector |

</details>

---

## Configuration

Config file: `~/.sahara/config.toml`

```toml
sync_folder        = "/Users/you/Sahara"
storage_mode       = "none"       # none | s3 | local | local+glacier
bucket             = "my-bucket"
region             = "us-east-1"
prefix             = ""
endpoint_url       = ""           # set for MinIO, e.g. http://192.168.1.10:9000
encryption_enabled = false
conflict_strategy  = "backup"     # backup | local | remote | ask
upload_only        = false        # this machine only pushes, never pulls
max_workers        = 8
```

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

See [SECURITY.md](SECURITY.md) for the wire format and threat model.

---

## Ignore rules

Create `.saharaignore` in any indexed content root (gitignore syntax):

```
*.tmp
.DS_Store
node_modules/
secrets/
```

---

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — System design, storage protocols, search pipeline, SQLite schema
- [CONTRIBUTING.md](CONTRIBUTING.md) — Dev setup, test conventions, how to add a storage backend
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — Community expectations and private incident reporting
- [ROADMAP.md](ROADMAP.md) — What is built, what is next, explicit non-goals
- [SECURITY.md](SECURITY.md) — Encryption wire format, threat model, vulnerability reporting
- [CHANGELOG.md](CHANGELOG.md) — Release history
- [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) — Pre-release verification and publish checklist
- [specs/THREE_STEP_PRODUCT_MODEL_PLAN.md](specs/THREE_STEP_PRODUCT_MODEL_PLAN.md) — Current indexing and optional-storage implementation plan
- [docs/CLAUDE_DESKTOP.md](docs/CLAUDE_DESKTOP.md) — Claude Desktop setup, MCP tool contract, security, and troubleshooting
- [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) — Basic, local-drive, and AWS setup paths
- [docs/integrations/chat-agents.md](docs/integrations/chat-agents.md) — MCP and Claude integration notes

---

## License

MIT, copyright Nidheesh Puthalath.
