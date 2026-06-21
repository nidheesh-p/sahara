# Getting Started

Sahara starts as a local semantic index. Storage is optional and can be selected during
initial setup. Local-drive and AWS modes index the source folder on the computer; Sahara
does not index the storage copy as a second source.

Sahara requires Python 3.11 or newer. The PyPI distribution is `sahara-memory`;
the distribution named `sahara` is an unrelated OpenStack project.

## Basic: Local Indexing

Use this when you only need semantic search and MCP access. The quickest path is
the guided `sahara setup`, which configures basic indexing, builds the first
index, and connects Claude Desktop when it is detected:

```bash
pipx install "sahara-memory[search,mcp]"
sahara setup --folder ~/Documents
sahara search "known phrase" --snippet
```

`sahara setup` is idempotent and safe to re-run; it preserves existing
configuration, content roots, and indexes. Add `--yes` for a non-interactive run,
and `--no-index` or `--no-mcp` to skip those steps. Prefer the individual commands?
Run them directly instead:

```bash
sahara init --mode basic --folder ~/Documents
sahara index
sahara search "known phrase" --snippet
```

Use the [installation guide](INSTALLATION.md) to install `pipx` or use a virtual
environment. A direct install into Homebrew Python may be blocked by PEP 668; do not
work around it with `--break-system-packages`.

The first `sahara index` run downloads the local embedding model (roughly
70 MB). A Hugging Face warning about unauthenticated requests is informational:
no account or token is required. `HF_TOKEN` is optional for higher download
rate limits.

To fetch and verify the model ahead of time — for example before going offline —
run `sahara models prepare`. It needs no configuration and can be run before
`sahara init`.

This command is non-interactive. It requires no bucket, credentials, drive, storage
validation, or standalone answer provider.

Semantic search and retrieval-only questions are now ready:

```bash
sahara ask --snippet "what does this folder say about the project deadline?"
```

This returns ranked source passages and makes no answer-generation network request. It
is also enough for an MCP client to use Sahara as retrieval context.

To generate standalone answers locally, install and launch
[Ollama](https://ollama.com/download), then download the optional default model:

```bash
ollama pull mistral
ollama run mistral "Reply with only: Ollama is ready"
sahara config set answer_provider ollama
sahara ask "what does this folder say about the project deadline?"
```

The optional Ollama model download is approximately 4.4 GB. OpenAI is an explicit
alternative; setting `OPENAI_API_KEY` does not change the saved provider. Follow
[Answer provider setup](ANSWER_PROVIDERS.md) for platform-specific Ollama installation,
OpenAI setup, and troubleshooting.

To use only OpenAI and skip Ollama, set `OPENAI_API_KEY`, then run:

```bash
sahara config set answer_provider openai
sahara ask "what does this folder say about the project deadline?"
```

Connect the indexed library to Claude Desktop:

```bash
sahara mcp install-claude
```

Fully quit and reopen Claude Desktop, then confirm Sahara appears in **Connectors**.

Add more folders as index-only content roots:

```bash
sahara folder add ~/Projects
sahara folder list
sahara index
```

## Local Drive: Index and Sync

Use a mounted external drive, NAS path, or network share:

```bash
sahara init \
  --mode local \
  --folder ~/Sahara \
  --storage-drive /Volumes/Archive/Sahara
sahara sync
sahara index
```

Local-drive copies use append-only deletion behavior by default. Deleting a source file
does not automatically delete its drive copy.

## AWS: Index and Sync

Configure AWS credentials through environment variables or an AWS profile, then run:

```bash
sahara init \
  --mode aws \
  --folder ~/Sahara \
  --bucket my-sahara-bucket \
  --region us-east-1
sahara sync
sahara index
```

## Content Roots and Sync

Every content root is indexed. New roots added with `sahara folder add` start in
index-only mode:

```bash
sahara folder add ~/PrivateNotes
```

Storage-backed configurations can explicitly enable or disable sync per root:

```bash
sahara folder sync ~/PrivateNotes --enable
sahara folder sync ~/PrivateNotes --disable
```

The legacy `sahara add <path>` command retains its original behavior and registers an
additional sync folder.

## Add Storage Later

Upgrade an existing basic library without rebuilding its semantic index:

```bash
sahara storage configure local --drive /Volumes/Archive/Sahara
sahara folder sync ~/Documents --enable
sahara sync
```

Or attach AWS:

```bash
sahara storage configure aws \
  --bucket my-sahara-bucket \
  --region us-east-1
sahara folder sync ~/Documents --enable
sahara sync
```

Storage validation must succeed before Sahara saves the new backend configuration.

## Free and Restore Local Space

Offload is explicit. Sahara first verifies the stored copy by downloading it,
decrypting it when necessary, and comparing the recovered plaintext SHA-256. Only then
does it remove the local source:

```bash
sahara sync
sahara index
sahara offload notes/old-project.md
```

The file remains searchable and appears as `offloaded` in search, listings, storage
status, and MCP results. Restore it with:

```bash
sahara fetch notes/old-project.md
```

Ordinary filesystem deletion is not treated as offload. Sahara removes stale search
data for a genuinely missing file, while an intentionally offloaded file keeps its
chunks and embeddings.
