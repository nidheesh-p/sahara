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

Sahara turns extended personal storage into searchable memory. It syncs and protects
your files, extracts their text, chunks long documents, embeds those chunks into a local
vector database, and lets you search or ask questions with citations. The goal is to
give you the same kind of instant knowledge retrieval companies get from enterprise AI
connectors, but for your own machine and under your control.

Sahara is not trying to be a general autonomous agent. Tools like Claude Desktop,
OpenClaw, or future ChatGPT connectors can become chat front ends. Sahara's job is to
be the trusted local index: retrieve the right local context, cite where it came from,
and avoid broad filesystem access unless you explicitly opt in.

---

## What it does today

- **Sync** a local folder to S3, MinIO, or a locally mounted drive — bidirectional, with three-way diff conflict resolution
- **Encrypt** files client-side with AES-256-GCM before they leave your machine
- **Index** your documents — PDF, DOCX, Markdown, code, plain text — into a local vector database
- **Search** by meaning: `sahara search "tax return 2024"` finds the right file even if none of those words appear in the filename
- **Ask** natural language questions: `sahara ask "what is my passport expiry date?"` extracts the answer and cites the source
- **Expose read-only MCP tools** for chat clients and agent runtimes with `sahara mcp serve`

All search and answer generation runs locally. Your files never leave your machine for indexing purposes.

## What is coming next

- Chat/agent integration docs for Claude Desktop, OpenClaw, and future ChatGPT connector paths
- Hybrid retrieval: BM25 keyword + vector search with cross-encoder reranking
- Entity extraction: dates, names, amounts, document types
- OCR support via a plugin (opt-in, not default)
- Plugin marketplace for parsers, embedders, and rerankers

See [ROADMAP.md](ROADMAP.md) for the full plan.

---

## Installation

```bash
# Minimal — sync only, no search
pip install sahara

# With semantic search (downloads ~200 MB embedding model on first use)
pip install "sahara[search]"

# With MCP server support for chat/agent clients
pip install "sahara[search,mcp]"

# With OCR support
pip install "sahara[search,ocr]"

# Everything
pip install "sahara[all]"
```

### Developer setup

```bash
git clone https://github.com/nidheesh-p/sahara
cd sahara
pip install -e ".[search,dev]"
```

---

## Quick start

Three commands to your first semantic search:

```bash
pip install "sahara[search]"
sahara init          # 2-minute interactive wizard — choose local, MinIO, or S3
sahara index && sahara search "my tax return 2024"
```

---

## Storage backends

Sahara supports four storage modes, selected during `sahara init`:

| Mode | Use case |
|------|----------|
| `local` | Second drive, NAS, or network share — no cloud account needed |
| `local+glacier` | Local drives as primary + S3 Glacier as cold backup |
| `minio` | Self-hosted S3-compatible object storage |
| `s3` (aws) | AWS S3 with optional Glacier archiving |

### Local drive

Files are written to one or more mounted drives independently — no RAID required. Drives are append-only by default (deleting a file locally does not remove it from drives).

```bash
sahara init   # choose 'local', point at /Volumes/MyDrive/Backup
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

sahara init   # choose 'aws'
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
sahara index                    # index all synced files
sahara index --force            # re-index everything (ignores content hash)
sahara index --folder ~/Docs    # index a specific registered folder
```

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
| `sahara index-report` | Show indexed/unindexed counts and sample gaps |
| `sahara search <query>` | Semantic search |
| `sahara ask <question>` | LLM-powered Q&A over your files |
| `sahara mcp serve` | Serve read-only search/ask tools over MCP stdio |
| `sahara mcp serve --transport http --auth-token <token>` | Serve authenticated MCP over local HTTP for a secure tunnel / remote connector |
| `sahara archive [paths]` | Move files to Glacier (AWS only) |
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
sync_folder        = "/Users/you/Sahara"
storage_mode       = "s3"         # s3 | local | local+glacier
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

Create `.saharaignore` in your sync folder (gitignore syntax):

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
- [STATUS.md](STATUS.md) — Current project status, completed work, and remaining tasks
- [ROADMAP.md](ROADMAP.md) — What is built, what is next, explicit non-goals
- [SECURITY.md](SECURITY.md) — Encryption wire format, threat model, vulnerability reporting
- [CHANGELOG.md](CHANGELOG.md) — Release history
- [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) — Pre-release verification and publish checklist
- [docs/integrations/chat-agents.md](docs/integrations/chat-agents.md) — MCP, Claude Desktop, and agent integration notes

---

## License

MIT
