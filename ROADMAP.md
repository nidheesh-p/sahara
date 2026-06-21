# Sahara Roadmap

---

## Now (v0.3 — current)

- **Guided onboarding:** `sahara setup` and `sahara models prepare` for basic local search
- **Captured knowledge:** durable Markdown memories via CLI, MCP, mobile API, and inbox
- **Local index watcher:** always-on incremental indexing separated from optional sync
- **Sync:** S3, MinIO, local drives, local+glacier dual-write
- **Encryption:** AES-256-GCM client-side, PBKDF2-HMAC-SHA256 key derivation, keyring storage
- **Conflict resolution:** three-way diff with backup / local / remote / ask strategies
- **Daemon:** background index watcher plus optional storage sync with file-watching
- **Search:** chunked semantic search via sqlite-vec (BAAI/bge-small-en-v1.5, 384-dim)
- **Ask:** retrieval-only by default, with optional answer generation via Ollama or OpenAI
- **Parsers:** PDF (pypdf), DOCX (python-docx), EPUB (stdlib), plain text, Markdown, code files
- **MCP:** read-only search/ask/recall tools; opt-in stdio-only memory capture
- **Mobile capture:** authenticated loopback API, device pairing, iOS Shortcuts artifacts
- **Claude Desktop installer:** `sahara mcp install-claude` safely merges the local stdio server
- **Basic indexing:** fresh setups can index and search local folders without storage
- **Content roots:** indexed folders tracked separately from sync state
- **Extended storage:** checksum-verified offload/fetch retains semantic search metadata

---

## Next: Release Hardening and Simple Installation

- **Release hardening:** clean-install timing for basic, local-drive, and AWS paths
- **Live validation:** exercise local-drive and temporary AWS storage end to end
- **Prerelease feedback:** publish the three-step model for external migration testing
- **Standalone runtime:** bundle Sahara without requiring a system Python installation
- **Native installers:** macOS Apple Silicon and Windows x64 first
- **Package managers:** Homebrew and WinGet after native artifacts are stable

See [specs/THREE_STEP_PRODUCT_MODEL_PLAN.md](specs/THREE_STEP_PRODUCT_MODEL_PLAN.md) and
[specs/SIMPLE_INSTALLER_PLAN.md](specs/SIMPLE_INSTALLER_PLAN.md). Progress is tracked in
[#54](https://github.com/nidheesh-p/sahara/issues/54).

---

## Later (v0.4+)

- **Hybrid retrieval:** BM25 keyword search (sqlite-fts5, no new dependency) + vector search merged via Reciprocal Rank Fusion
- **Cross-encoder reranking:** optional future plugin — the top merged results are re-scored by a cross-encoder model for much better precision on ambiguous queries
- **Entity extraction:** structured extraction of dates, names, amounts, and document types from indexed content — enables queries like `sahara ask "invoices over $500 in March"`
- **Rucksack backend:** Backblaze B2, Cloudflare R2, Wasabi via a thin wrapper (no new SDK dependency)
- **OAuth for remote MCP:** support clients that cannot provide a static bearer token
- **QR mobile pairing:** terminal QR display for Shortcut and companion onboarding
- **Lightweight companion app:** iOS share extension and Android share target after Shortcuts validate demand ([#63](https://github.com/nidheesh-p/sahara/issues/63))
- **ChatGPT connector path:** document only when authentication and local-first privacy expectations can be preserved

---

## Future (v0.5+)

- **Plugin ecosystem:** parser, embedder, and reranker extension interfaces plus `sahara plugins list`
- **Claude Desktop extension:** package Sahara as an MCPB desktop extension alongside the CLI installer
- **OCR plugin:** opt-in tesseract integration for image-heavy PDFs and scanned documents (`pip install sahara-plugin-ocr`)
- **Image search:** CLIP embeddings for photos, EXIF metadata indexing — find images by content description
- **Audio / video:** Whisper transcription + scene indexing for MP3, MP4, MOV
- **Plugin marketplace:** `sahara plugins install`, curated list of community parsers and embedders
- **OpenClaw integration guidance:** validate Sahara's read-only MCP tools with OpenClaw and document the supported setup

---

## Non-goals (forever)

- **Cloud SaaS.** Sahara is local-first. There will be no hosted version.
- **Multi-user / shared storage.** The manifest + SQLite architecture is single-user by design. A multi-user system would require a server process, access control, and a different consistency model.
- **AI agent framework.** Sahara may expose read-only MCP tools to agents such as OpenClaw, but it does not autonomously take actions on your behalf.
- **Web UI or desktop GUI.** The CLI is the product. A web UI requires a server process, auth, and significant frontend work. This is firmly post-v0.5 territory, if ever.

---

## Contributing to the roadmap

If you want to work on a roadmap item, open an issue first to discuss the approach.
Roadmap items represent the intended direction but not necessarily a reserved claim.
