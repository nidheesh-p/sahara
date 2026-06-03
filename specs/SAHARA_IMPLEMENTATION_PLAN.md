# Sahara — Implementation Plan
## From Local Testing → Open Source Release → Plugin Ecosystem

**Author:** Implementation Review  
**Date:** May 2026  
**Codebase baseline:** v6 spec + search layer + multi-backend support

---

## Executive Summary

Sahara already has a strong technical foundation: a production-grade sync engine, three
storage backends (AWS S3, MinIO, local drives), AES-256-GCM encryption, conflict
resolution, a daemon, and a basic semantic search layer. What it lacks is the packaging,
documentation, and architectural seams that make it trustworthy for local use, attractive
to OSS adopters, and safe for external contributors to extend.

This plan is organized into three sequential phases. Each phase has a clear exit
criterion — the work in that phase must be complete and verified before the next begins.

---

## Phase 0 — Local Testing Ready
**Timeline: 3–5 days**  
**Exit criterion:** You can install Sahara on a clean machine from the repo, run all
three storage modes (local drives, MinIO, AWS), run `sahara index && sahara search`,
and the test suite passes with ≥90% coverage.

---

### 0.1 Fix the Install Experience

The current `pyproject.toml` describes Sahara as "Personal cloud storage CLI backed by
AWS S3" with keywords `["s3", "cloud", "storage", "sync", "backup"]`. This is already
inaccurate — Sahara now supports local drives, MinIO, and semantic search. Update it
before anything else, because this is the first thing a new user sees.

**File: `pyproject.toml`**

```toml
[project]
name = "sahara"
version = "0.2.0"
description = "Local-first intelligent storage with semantic search"
keywords = ["storage", "sync", "semantic-search", "local-first", "privacy", "s3", "backup"]

[project.optional-dependencies]
search = [
    "fastembed>=0.3.0",
    "pypdf>=4.0.0",
    "python-docx>=1.1.0",
    "sqlite-vec>=0.1.0",    # replaces in-memory cosine scan (see 0.3)
]
ocr = [
    "pytesseract>=0.3.10",
    "Pillow>=10.0.0",
]
dev = [
    "pytest>=8.0.0",
    "pytest-cov>=5.0.0",
    "moto[s3]>=5.0.0",
    "ruff>=0.4.0",
    "mypy>=1.10.0",
]
all = ["sahara[search,ocr]"]
```

Install paths to document clearly in README:

```bash
# Minimal (sync only, no search)
pip install sahara

# With semantic search
pip install sahara[search]

# With OCR support
pip install sahara[search,ocr]

# Everything
pip install sahara[all]

# Developer setup
git clone https://github.com/nidheesh-p/sahara
cd sahara
pip install -e ".[search,dev]"
```

---

### 0.2 Fix the `_require_config` Guard

**Current bug:** `_require_config` checks for both `config.bucket` and
`config.sync_folder`. In local drive mode there is no bucket — this causes a false
abort. Fix before testing local mode.

**File: `src/sahara/cli.py`**

```python
def _require_config(config: SaharaConfig) -> None:
    if not config.sync_folder:
        _abort("Sahara is not initialised. Run `sahara init` to set up.")
    # Local drive mode has no bucket — only S3/MinIO modes require one
    if config.storage_mode == "s3" and not config.bucket:
        _abort("No S3 bucket configured. Run `sahara init` to set up.")
```

---

### 0.3 Replace In-Memory Cosine Scan with sqlite-vec

**Current problem:** `SearchEngine.search()` loads all embedding rows from SQLite into
Python, converts each to a numpy array, and does a brute-force cosine scan. At 10k
files this is ~2–3 seconds. At 100k files it is unusable. More critically, the current
code embeds each file as a single vector truncated at 8,000 characters — a 50-page PDF
gets one embedding of its first ~1,500 words, so anything on page 3 onwards is
invisible to search.

**What to change:**

Replace the embeddings table with `sqlite-vec` virtual tables and implement chunked
indexing. This is the most important technical change in Phase 0 — getting it right now
prevents the search layer from needing a full rewrite when contributors arrive.

**New schema (add to `StateDB` migration):**

```sql
-- One row per chunk, not one row per file
CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks
    USING vec0(embedding float[384]);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY,
    s3_prefix   TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content_hash TEXT NOT NULL,   -- hash of the full file text, not the chunk
    chunk_text  TEXT NOT NULL,
    indexed_at  TEXT NOT NULL,
    UNIQUE(s3_prefix, relative_path, chunk_index)
);
```

**New chunking strategy in `SearchEngine.index_file()`:**

```python
CHUNK_SIZE    = 400   # tokens ≈ 1600 chars
CHUNK_OVERLAP = 80    # tokens ≈ 320 chars overlap between adjacent chunks
```

Chunk the extracted text with overlap, embed each chunk independently, insert rows
into both `chunks` and `vec_chunks`. The `vec_chunks` rowid matches the `chunks.id`.

**New `SearchEngine.search()` using sqlite-vec KNN:**

```python
def search(self, query: str, top_k: int = 5, s3_prefix=None) -> list[dict]:
    query_vec = self._embed([query])[0]
    rows = self._db.conn.execute("""
        SELECT c.relative_path, c.s3_prefix, c.chunk_text,
               v.distance
        FROM vec_chunks v
        JOIN chunks c ON c.id = v.rowid
        WHERE v.embedding MATCH ?
          AND k = ?
        ORDER BY v.distance
    """, (query_vec.tobytes(), top_k * 3)).fetchall()
    # Deduplicate to top file per result, keep best chunk score
    ...
```

This brings search from O(n) Python loop to O(log n) ANN query, and makes long
documents retrievable by any part of their content.

---

### 0.4 Implement `sahara ask`

The OSS proposal's headline examples — `sahara ask "what is my passport expiry date?"` —
require a command that does not yet exist. `sahara search` returns ranked files;
`sahara ask` should extract an answer from those files and cite the source.

**Architecture:** Keep this local-first. The answer generation layer reads the top-k
chunks from the search results and sends them as context to a local LLM via ollama's
REST API. Ollama is optional — if it is not running, `ask` degrades gracefully to
showing the top search results with their snippets (same as `search --snippet`).

**File: `src/sahara/search/ask_engine.py`** (new file)

```python
class AskEngine:
    """Wraps SearchEngine + optional local LLM to answer natural language questions."""

    OLLAMA_URL = "http://localhost:11434/api/generate"
    DEFAULT_MODEL = "mistral"  # user can override in config

    def ask(self, question: str, top_k: int = 5) -> AskResult:
        # 1. Semantic search for relevant chunks
        chunks = self.search_engine.search(question, top_k=top_k)
        if not chunks:
            return AskResult(answer=None, sources=[], degraded=True)

        # 2. Try ollama; degrade to snippet display if unavailable
        context = self._build_context(chunks)
        answer = self._call_ollama(question, context)
        return AskResult(answer=answer, sources=chunks, degraded=(answer is None))
```

**Config additions:**

```toml
[ask]
model = "mistral"          # ollama model name
ollama_url = "http://localhost:11434"
max_context_chunks = 5
```

**CLI command:**

```bash
sahara ask "what is my passport expiry date?"
sahara ask "find the invoice from Amazon last month" --top 10
sahara ask "what did I decide about the kitchen renovation?" --snippet
```

Output format (always show sources, never hallucinate without them):

```
Answer: Your passport expires on Aug 14, 2032.

Source: Documents/Personal/passport_scan.pdf  (score: 94%)
  "…passport valid until 14 AUG 2032. Issued by Government of India…"

Note: Answer generated by local model mistral via ollama.
      Run `ollama serve` if model is not available.
```

---

### 0.5 Local Testing Checklist

Run through all of these before calling Phase 0 done:

```bash
# 1. Clean install
python -m venv .venv && source .venv/bin/activate
pip install -e ".[search,dev]"

# 2. Local drive mode
sahara init    # choose 'local', point at a test directory
sahara sync
sahara status
sahara index
sahara search "test query"
sahara ask "what files do I have about invoices"

# 3. MinIO mode (requires docker)
docker run -p 9000:9000 -p 9001:9001 \
  -e MINIO_ROOT_USER=admin -e MINIO_ROOT_PASSWORD=password \
  minio/minio server /data --console-address :9001
sahara init    # choose 'minio', endpoint http://localhost:9000

# 4. Test suite
pytest --cov=src/sahara --cov-report=term-missing
# Must pass ≥90% line coverage

# 5. Type check
mypy src/
```

---

## Phase 1 — Open Source Ready
**Timeline: 1 week**  
**Exit criterion:** A developer who has never seen Sahara can clone the repo, read the
docs, set it up locally, run the tests, and submit a meaningful PR — all without
asking you a single question.

---

### 1.1 README Rewrite

The current README describes Sahara as an S3 sync tool. Rewrite it to lead with the
semantic search vision while being honest about current capabilities.

**Structure:**

```markdown
# Sahara

> A local-first personal storage system with semantic search.
> Find your files by meaning, not by filename.

## What it does today
## What it will do (roadmap)
## Installation
## Quick start (3 commands to first search)
## Storage backends
## Search & ask
## Configuration reference
## Contributing
## License
```

The "Quick start" section is the most important part of the README for adoption. It must
be three commands or fewer to a working first experience:

```bash
pip install sahara[search]
sahara init          # 2-minute wizard
sahara index && sahara search "my tax return 2024"
```

---

### 1.2 ARCHITECTURE.md

This is the single most important document for contributors. It must explain:

- The three storage backends and the `StorageBackend` Protocol — where to add new ones
- The sync pipeline (scan → three-way diff → conflict resolution → execute)
- The search pipeline (extract → chunk → embed → index → retrieve → answer)
- The daemon and file watcher lifecycle
- The SQLite schema (all tables: `files`, `config`, `sync_history`, `sync_targets`,
  `chunks`, `vec_chunks`)
- How the manifest works and why (the HeadObject cost problem)
- What is intentionally NOT abstracted (the TOML config, the SQLite state)
- Known limitations (current chunking strategy, no reranker yet)

Include the system diagram from the spec. Include a "where to start" section that maps
contribution areas to specific files.

---

### 1.3 CONTRIBUTING.md

```markdown
## Development setup
## Running tests (pytest + moto — no real AWS needed)
## Test conventions (fixtures, mocking strategy)
## Submitting a PR (branch naming, commit style, CI requirements)
## Adding a storage backend
## Adding a file parser
## Adding an embedding model
## Code style (ruff, mypy strict)
## Release process
```

The most important section is "Adding a storage backend" — make it a concrete walkthrough
of implementing the `StorageBackend` Protocol with a toy in-memory backend as the
example.

---

### 1.4 ROADMAP.md

Be honest about current state. Structure it around the three phases from the proposal,
but reflect Phase 0 work as already done.

```markdown
## Now (v0.2 — current)
- Sync: S3, MinIO, local drives, local+glacier
- Encryption: AES-256-GCM client-side
- Search: chunked semantic search via sqlite-vec
- Ask: local LLM answer generation via ollama (optional)

## Next (v0.3)
- Hybrid retrieval: BM25 + vector with cross-encoder reranking
- Entity extraction: dates, names, amounts, document types
- OCR: tesseract integration (opt-in plugin)
- Rucksack backend (B2/R2/Wasabi)

## Future (v0.4+)
- Image search: CLIP embeddings + EXIF metadata
- Audio/video: Whisper transcription + scene indexing
- Plugin marketplace

## Non-goals (forever)
- Cloud SaaS
- Multi-user / shared storage
- AI agent framework
```

---

### 1.5 SECURITY.md

Include:
- Encryption model (AES-256-GCM, PBKDF2, per-chunk nonces)
- Threat model (what Sahara protects against, what it does not)
- Passphrase handling (keyring, no recovery path)
- How to report a vulnerability (GitHub private advisory)
- IAM minimum required policy

---

### 1.6 GitHub Actions CI

**File: `.github/workflows/ci.yml`**

```yaml
name: CI
on: [push, pull_request]

jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest]
        python: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "${{ matrix.python }}" }
      - run: pip install -e ".[search,dev]"
      - run: pytest --cov=src/sahara --cov-fail-under=90
      - run: ruff check src/ tests/
      - run: mypy src/

  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install build
      - run: python -m build   # verifies the package builds cleanly
```

---

### 1.7 GitHub Issue and PR Templates

**`.github/ISSUE_TEMPLATE/bug_report.md`** — include: OS, Python version, storage
backend, install method, reproduction steps, expected vs actual output.

**`.github/ISSUE_TEMPLATE/feature_request.md`** — include: use case, which phase of
the roadmap it relates to, willingness to implement.

**`.github/PULL_REQUEST_TEMPLATE.md`** — include: what it changes, tests added, which
issue it closes, storage backends tested against.

---

### 1.8 CHANGELOG.md

Start with v0.1.0 (the initial sync-only release) and v0.2.0 (the semantic search
release with chunked indexing and `sahara ask`). Follow Keep a Changelog format. This
matters for OSS credibility — projects without changelogs look abandoned.

---

## Future — Plugin Ecosystem
**Timeline: 2–3 weeks after MCP/chat-agent integration**  
**Exit criterion:** An external developer can write and publish a Sahara plugin that
adds a new file parser, and another developer can install and use it with
`pip install sahara-plugin-pdf-advanced && sahara plugins enable pdf-advanced`.

---

### F.1 Plugin Architecture Design

Sahara has four natural plugin extension points. Each maps to an interface that
contributors can implement without touching core code.

```
Plugin Type       Interface            Example implementations
─────────────────────────────────────────────────────────────
StorageBackend    StorageBackend       B2Client, R2Client, WebDAVClient
FileParser        FileParser           PDFParser, ImageParser, AudioParser
Embedder          Embedder             FastEmbedEmbedder, OllamaEmbedder
Reranker          Reranker             FlashRankReranker, CrossEncoderReranker
```

Use Python's `importlib.metadata` entry-points system. This is the standard pattern
used by pytest plugins, Flask extensions, and Pydantic validators — contributors will
already know it.

---

### F.2 Define the Plugin Interfaces

**File: `src/sahara/plugins/interfaces.py`** (new file)

```python
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class FileParser(Protocol):
    """Extracts plain text from a file for indexing."""

    #: List of file extensions this parser handles, e.g. [".pdf", ".PDF"]
    supported_extensions: list[str]

    def extract(self, file_path: Path) -> Optional[str]:
        """Return extracted text, or None if extraction failed."""
        ...

    def can_handle(self, file_path: Path) -> bool:
        """Return True if this parser can process the given file."""
        ...


@runtime_checkable
class Embedder(Protocol):
    """Generates embeddings for text chunks."""

    #: Dimensionality of the embedding vectors this model produces
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text."""
        ...

    def model_name(self) -> str:
        """Return a stable identifier for this model (used in DB to detect model changes)."""
        ...


@runtime_checkable
class Reranker(Protocol):
    """Re-scores search results given the original query."""

    def rerank(
        self,
        query: str,
        results: list[dict],   # list of {relative_path, chunk_text, score, ...}
        top_k: int = 5,
    ) -> list[dict]:
        """Return results re-sorted by relevance to query."""
        ...
```

---

### F.3 Plugin Registry and Discovery

**File: `src/sahara/plugins/registry.py`** (new file)

```python
from importlib.metadata import entry_points
from sahara.plugins.interfaces import FileParser, Embedder, Reranker

_PARSERS:   dict[str, FileParser]  = {}
_EMBEDDERS: dict[str, Embedder]    = {}
_RERANKERS: dict[str, Reranker]    = {}


def load_plugins() -> None:
    """Discover and load all installed Sahara plugins via entry-points."""
    for ep in entry_points(group="sahara.parsers"):
        cls = ep.load()
        instance = cls()
        _PARSERS[ep.name] = instance

    for ep in entry_points(group="sahara.embedders"):
        cls = ep.load()
        instance = cls()
        _EMBEDDERS[ep.name] = instance

    for ep in entry_points(group="sahara.rerankers"):
        cls = ep.load()
        instance = cls()
        _RERANKERS[ep.name] = instance


def get_parser_for(file_path) -> FileParser | None:
    """Return the first registered parser that can handle the file."""
    for parser in _PARSERS.values():
        if parser.can_handle(file_path):
            return parser
    return None


def get_embedder(name: str | None = None) -> Embedder | None:
    if name:
        return _EMBEDDERS.get(name)
    return next(iter(_EMBEDDERS.values()), None)


def get_reranker(name: str | None = None) -> Reranker | None:
    if name:
        return _RERANKERS.get(name)
    return next(iter(_RERANKERS.values()), None)
```

---

### F.4 Register Built-in Implementations as Plugins

Move the existing `TextExtractor` and `fastembed` embedding code out of
`search_engine.py` and into the plugin system, registered via `pyproject.toml`
entry-points. This means the built-in parsers and embedder are plugins too — they just
happen to ship with the package. This is critical: it proves the plugin interface
works before any external plugin is written.

**`pyproject.toml` additions:**

```toml
[project.entry-points."sahara.parsers"]
text    = "sahara.parsers.text_parser:TextParser"
pdf     = "sahara.parsers.pdf_parser:PDFParser"
docx    = "sahara.parsers.docx_parser:DocxParser"

[project.entry-points."sahara.embedders"]
bge-small = "sahara.embedders.fastembed_embedder:FastEmbedEmbedder"

[project.entry-points."sahara.rerankers"]
# none built-in yet; FlashRank will be the first contributed plugin
```

---

### F.5 `sahara plugins` CLI Commands

```bash
sahara plugins list              # show all discovered plugins with type and source
sahara plugins list --type parsers
sahara plugins enable flashrank   # sets config: reranker = "flashrank"
sahara plugins disable flashrank
```

Example output of `sahara plugins list`:

```
Parsers
  text     sahara.parsers.text_parser      built-in   .txt .md .rst .py ...
  pdf      sahara.parsers.pdf_parser       built-in   .pdf
  docx     sahara.parsers.docx_parser      built-in   .docx .doc
  ocr      sahara-plugin-ocr               installed  .jpg .jpeg .png .tiff

Embedders
  bge-small  sahara.embedders.fastembed_embedder  built-in  384-dim  ✓ active

Rerankers
  (none installed)
  Tip: pip install sahara-plugin-flashrank
```

---

### F.6 Plugin Author Guide

Write `PLUGIN_SYSTEM.md`. This is the document that turns Sahara from a project you
contribute to into a project you build on. It must include:

- The four plugin types and their interfaces
- How to package a plugin (`pyproject.toml` entry-point declaration)
- A complete minimal example: a parser plugin that handles `.epub` files
- Naming conventions (`sahara-plugin-<name>`)
- How to test a plugin against Sahara's test fixtures
- Plugin versioning and compatibility

**Minimal example plugin `pyproject.toml`:**

```toml
[project]
name = "sahara-plugin-epub"
version = "0.1.0"
dependencies = ["ebooklib>=0.18", "sahara>=0.2.0"]

[project.entry-points."sahara.parsers"]
epub = "sahara_plugin_epub:EpubParser"
```

When this package is `pip install`-ed, `sahara plugins list` will show it
automatically — no config file changes, no restart of the daemon required.

---

### F.7 Hybrid Retrieval (Bonus — do after plugin system is stable)

Once the plugin system is in place, add BM25 keyword search as a second retrieval
path that runs in parallel with the vector search and whose results are merged before
reranking. This directly enables stronger precision for structured queries like invoice numbers,
names, and dates.

The implementation:
- `sqlite-fts5` virtual table for BM25 (already available in Python's stdlib `sqlite3`)
- No new dependency, no new optional install
- Run in `SearchEngine.search()` in parallel with the vec query
- Reciprocal Rank Fusion (RRF) to merge scores from both paths
- If a reranker plugin is active, run the merged list through it before returning

---

## Phase 2 — MCP / Chat and Agent Integrations
**Timeline: 1–2 weeks after Phase 1**  
**Exit criterion:** A user can connect a chat/agent client such as Claude Desktop or
OpenClaw to Sahara and ask questions about local files through Sahara's local index,
without granting the agent broad filesystem write access.

---

### 2.1 Why This Phase Exists

Sahara's core problem is the same one enterprise ChatGPT deployments solve with
company knowledge connectors: retrieve relevant private context and give it to an LLM
at question time. The important distinction is that Sahara does this for a user's local
machine, with a local SQLite/sqlite-vec index, rather than relying on cloud document
stores such as Google Drive, SharePoint, or GitHub.

OpenClaw, Claude Desktop, ChatGPT connectors, and similar systems are useful chat or
agent front ends. They are not, by themselves, a replacement for Sahara's local
retrieval layer. Without a durable local index, an agent usually falls back to ad hoc
filesystem reads, shell searches, or manual file inspection. That works for small
folders, but not for a whole computer with long PDFs, source trees, notes, and archived
documents.

The intended architecture is:

```text
OpenClaw / Claude Desktop / other MCP client
        │
        │ calls read-only tools
        ▼
Sahara MCP server
        │
        ▼
SearchEngine + AskEngine
        │
        ▼
SQLite chunks + sqlite-vec embeddings
```

Sahara remains the retrieval engine. Chat clients become optional front ends.

---

### 2.2 Local MCP Server

Add a local MCP server package or command, for example:

```bash
sahara mcp serve
```

The first version should expose read-only tools only:

```
Tool                         Purpose
────────────────────────────────────────────────────────────────────
sahara_search(query, top_k)  Return ranked local files/chunks
sahara_ask(question, top_k)  Return answer + cited sources
sahara_read_chunk(id)        Return one indexed chunk by ID
sahara_list_folders()        Show indexed/synced folders
sahara_index_status()        Show index size, last indexed time, model
```

Do not expose file writes, shell execution, or sync mutation in the first MCP release.
The safest initial promise is: "A chat client can ask about your files, but cannot
modify them through Sahara."

---

### 2.3 OpenClaw Integration

OpenClaw is best treated as a personal agent runtime: it provides the chat surface,
tool routing, automation loop, and optional access to OpenAI, Claude, or local models.
Sahara should integrate with it by exposing a narrow MCP tool server.

Recommended OpenClaw flow:

```text
User asks OpenClaw:
  "Find the document where I discussed the kitchen renovation budget."

OpenClaw calls:
  sahara_search("kitchen renovation budget", top_k=8)

Sahara returns:
  ranked chunks, paths, scores, and snippets

OpenClaw answers:
  a conversational answer with Sahara citations
```

Security posture:

- Prefer read-only Sahara tools over broad OpenClaw filesystem access.
- Let Sahara enforce indexed-folder boundaries.
- Return citations and snippets, not arbitrary full-file dumps by default.
- Add explicit opt-in for any future write/action tools.

---

### 2.4 Claude Desktop Integration

Claude Desktop supports local MCP servers and is likely the easiest first chat client
for Sahara's local-first use case.

Deliverables:

- `docs/integrations/claude-desktop.md`
- Example Claude Desktop MCP config
- Smoke test using a temporary indexed folder
- Troubleshooting notes for Python environment paths and permissions

The goal is to let a user run Sahara locally, add one MCP server entry to Claude
Desktop, and ask questions about indexed local files with citations.

---

### 2.5 ChatGPT Integration

ChatGPT's built-in connector model is strongest for cloud document stores and
enterprise-managed data sources. Sahara should not depend on ChatGPT for local-first
retrieval. However, support may be possible through future local or remote MCP-style
connectors.

Approach:

- Document ChatGPT as an optional client path only when the integration can preserve
  Sahara's local-first privacy expectations.
- Do not require users to expose their whole local filesystem to a remote service.
- If a remote bridge is needed, make authentication, scope, and data-flow warnings
  explicit.

This keeps Sahara useful even if the preferred chat front end changes.

---

### 2.6 Integration Documentation

Add an integration guide:

**File: `docs/integrations/chat-agents.md`**

It should explain:

- Sahara as the local retrieval/index layer
- OpenClaw as an agent/runtime front end
- Claude Desktop as the easiest local MCP client
- ChatGPT as a possible future or remote connector client
- Threat model: read-only tools first, least privilege, indexed-folder boundaries
- Example queries and expected cited output

---

## Dependency and Compatibility Matrix

| Feature              | Extra install      | Requires system dep?       |
|----------------------|--------------------|----------------------------|
| Sync (all backends)  | none               | no                         |
| Semantic search      | `[search]`         | no                         |
| Answer generation    | `[search]` + ollama| ollama running locally      |
| Local MCP server     | `[mcp]` or `[all]` | no                         |
| OCR                  | `[search,ocr]`     | tesseract binary            |
| Reranking            | plugin             | no                         |
| Image search         | future             | CLIP model via plugin       |
| Video transcription  | future             | Whisper via plugin          |

---

## What NOT to Build Yet

These are tempting but will slow down the OSS launch if included:

- **A web UI or desktop GUI.** The CLI is the product for now. A web UI requires a
  server process, auth, and significant frontend work. Let the plugin system prove out
  first.

- **Automatic OCR on sync.** OCR is slow (10–60 seconds per page for complex PDFs) and
  requires a system binary. It belongs as a plugin that users opt into, not a default
  behavior triggered on every file change.

- **Photo/video indexing.** CLIP and Whisper are large models that require GPU or
  significant CPU time. Committing to these in the roadmap is fine; building them before
  the plugin system is stable is premature.

- **A general-purpose autonomous agent framework.** Sahara can expose read-only search
  tools to agents such as OpenClaw, but it should not become the agent runtime itself.
  Tool routing, automation loops, browser control, and cross-app actions belong in the
  client/agent layer.

- **Multi-user support.** The manifest + SQLite architecture is single-user by design.
  Multi-user would require a server process, access control, and a fundamentally
  different consistency model. Non-goal.

- **Cloud hosted version.** Explicitly out of scope per the proposal's non-goals.
  Don't let the OSS momentum create pressure toward this.

---

## Summary Timeline

| Phase       | Duration    | Key deliverables                                              |
|-------------|-------------|---------------------------------------------------------------|
| Phase 0     | 3–5 days    | Fixed install, chunked indexing, sqlite-vec, `sahara ask`     |
| Phase 1     | 1 week      | README, ARCHITECTURE.md, CONTRIBUTING.md, CI, templates       |
| Phase 2     | 1–2 weeks   | Local MCP server, Claude Desktop/OpenClaw integration docs    |
| Future      | 2–3 weeks   | Plugin interfaces, registry, built-ins as plugins, PLUGIN_SYSTEM.md |
| Post-launch | Ongoing     | Hybrid retrieval, OCR plugin, reranker plugin, community PRs  |

Total to OSS launch with the chat/agent integration layer: **3–5 weeks of focused work.**
Total including plugin support: **5–8 weeks of focused work.**

The single most important task is Phase 0.3 (chunked indexing + sqlite-vec). Everything
else is documentation, packaging, and interface design — necessary but straightforward.
The search architecture is the one thing that is painful to change after contributors
have built on it.
