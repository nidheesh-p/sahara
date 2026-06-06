# Sahara Roadmap

---

## Now (v0.2 — current)

- **Sync:** S3, MinIO, local drives, local+glacier dual-write
- **Encryption:** AES-256-GCM client-side, PBKDF2-HMAC-SHA256 key derivation, keyring storage
- **Conflict resolution:** three-way diff with backup / local / remote / ask strategies
- **Daemon:** background sync with file-watching via watchdog
- **Search:** chunked semantic search via sqlite-vec (BAAI/bge-small-en-v1.5, 384-dim)
- **Ask:** natural language question answering via local Ollama or OpenAI (optional; degrades gracefully)
- **Parsers:** PDF (pypdf), DOCX (python-docx), plain text, Markdown, code files
- **MCP:** read-only local stdio and authenticated HTTP transports for Claude Desktop, remote clients, and agent runtimes
- **MCP security:** bearer-token auth, tool/folder allowlists, snippet limits, and non-loopback binding warnings
- **Integration guides:** Claude Desktop and Claude mobile via secure tunnel
- **Basic indexing:** fresh setups can index and search local folders without storage
- **Content roots:** indexed folders are tracked separately from sync state
- **Index inventory:** indexed, unsupported, failed, and missing files are tracked locally
- **Extended storage:** checksum-verified offload/fetch retains semantic search metadata
- **Migration:** legacy S3, MinIO, local-drive, and local+glacier configs retain behavior

---

## Next: Three-Step Product Model

- **Release hardening:** clean-install timing for basic, local-drive, and AWS paths
- **Live validation:** exercise local-drive and temporary AWS storage end to end
- **Daemon refinement:** separate the always-local index watcher from the optional sync worker
- **Prerelease feedback:** publish the three-step model for external migration testing

See [specs/THREE_STEP_PRODUCT_MODEL_PLAN.md](specs/THREE_STEP_PRODUCT_MODEL_PLAN.md)
for the implementation sequence and compatibility plan.

---

## Later (v0.3+)

- **Hybrid retrieval:** BM25 keyword search (sqlite-fts5, no new dependency) + vector search merged via Reciprocal Rank Fusion
- **Cross-encoder reranking:** optional future plugin — the top merged results are re-scored by a cross-encoder model for much better precision on ambiguous queries
- **Entity extraction:** structured extraction of dates, names, amounts, and document types from indexed content — enables queries like `sahara ask "invoices over $500 in March"`
- **Rucksack backend:** Backblaze B2, Cloudflare R2, Wasabi via a thin wrapper (no new SDK dependency)
- **OAuth for remote MCP:** support clients that cannot provide a static bearer token
- **ChatGPT connector path:** document only when authentication and local-first privacy expectations can be preserved

---

## Future (v0.4+)

- **Plugin ecosystem:** parser, embedder, and reranker extension interfaces plus `sahara plugins list`
- **OCR plugin:** opt-in tesseract integration for image-heavy PDFs and scanned documents (`pip install sahara-plugin-ocr`)
- **Image search:** CLIP embeddings for photos, EXIF metadata indexing — find images by content description
- **Audio / video:** Whisper transcription + scene indexing for MP3, MP4, MOV
- **Plugin marketplace:** `sahara plugins install`, curated list of community parsers and embedders
- **Incremental re-indexing:** track which files need re-embedding without scanning all chunks
- **OpenClaw integration guidance:** validate Sahara's read-only MCP tools with OpenClaw and document the supported setup

---

## Non-goals (forever)

- **Cloud SaaS.** Sahara is local-first. There will be no hosted version.
- **Multi-user / shared storage.** The manifest + SQLite architecture is single-user by design. A multi-user system would require a server process, access control, and a different consistency model.
- **AI agent framework.** Sahara may expose read-only MCP tools to agents such as OpenClaw, but it does not autonomously take actions on your behalf.
- **Web UI or desktop GUI.** The CLI is the product. A web UI requires a server process, auth, and significant frontend work. This is firmly post-v0.4 territory, if ever.

---

## Contributing to the roadmap

If you want to work on a roadmap item, open an issue first to discuss the approach.
Roadmap items represent the intended direction but not necessarily a reserved claim.
