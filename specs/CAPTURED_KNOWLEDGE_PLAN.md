# Sahara Captured Knowledge Plan

## Goal

Extend Sahara from searching existing files to preserving knowledge the user encounters
outside the filesystem: observations, conversations, web research, AI chats, shared
mobile content, and voice notes.

The primary interaction should be:

```bash
sahara remember "Vendor X uses net-30 terms"
sahara recall "Which vendors use net-30?"
```

Captured knowledge remains local-first. Storage sync, remote access, mobile capture,
and answer generation are optional extensions.

## Product Principles

- A captured memory is a portable Markdown file, not an opaque database record.
- Capture must work in basic index-only mode without AWS, another drive, or an LLM.
- A successful file write is a successful capture. Embedding failure leaves the memory
  durable and marks indexing as pending.
- The managed memory folder is a normal Sahara content root with optional sync.
- Memory roots must not overlap another content root as an ancestor or descendant.
- Existing `chunks` and `vec_chunks` remain the retrieval implementation.
- The local SQLite database may cache metadata, but Markdown is the source of truth.
- Write-capable integrations are opt-in, narrowly scoped, auditable, and revocable.
- Every capture adapter calls the same `MemoryService`; adapters do not implement their
  own storage or indexing behavior.

## Storage Model

The default managed root is:

```text
~/Sahara Memory/
└── 2026/
    └── 06/
        └── 550e8400-vendor-payment-terms.md
```

It is marked as a Sahara-managed root and normally registered with reserved storage
prefix `memory` and `sync_enabled = false`. If an upgraded library already uses that
previously unrestricted prefix, Sahara preserves it and allocates `memory-2` (or the
next available suffix) for the managed root. Users may explicitly enable optional
storage sync:

```bash
sahara folder sync ~/Sahara\ Memory --enable
```

Local source files remain plaintext. When storage sync and Sahara encryption are both
enabled, stored copies are encrypted through the existing sync pipeline.

### File Format

```markdown
---
schema_version: 1
kind: "sahara_memory"
id: "550e8400-e29b-41d4-a716-446655440000"
created_at: "2026-06-13T18:30:00Z"
updated_at: "2026-06-13T18:30:00Z"
title: "Vendor payment terms"
source_type: "conversation"
source_url: ""
source_id: ""
idempotency_key: ""
tags:
  - "vendor"
  - "finance"
---
Vendor X uses net-30 terms. Priya is the billing contact.
```

UUID4 provides stable identity without an additional runtime dependency. Frontmatter
is parsed with safe YAML loading and strict field validation. Unknown fields are
preserved where practical so later schema versions remain forward-compatible.

## Core Service

All capture surfaces use:

```python
MemoryService.capture(CaptureRequest) -> CaptureResult
MemoryService.search(query, filters) -> list[MemoryResult]
MemoryService.get(memory_id) -> MemoryItem
MemoryService.list(filters) -> list[MemoryItem]
MemoryService.edit(memory_id, document) -> CaptureResult
MemoryService.delete(memory_id) -> None
MemoryService.rebuild() -> RebuildResult
```

The capture path:

1. Validate content, metadata, URL, tags, and size.
2. Ensure the managed memory root exists and is registered without root overlap.
3. Generate the UUID, timestamps, title, slug, and relative path.
4. Atomically write Markdown inside the managed root.
5. Update the rebuildable metadata cache.
6. Index only the new path.
7. Return `saved_and_indexed` or `saved_index_pending`.

Concurrent writers use a lock scoped to the memory root. Paths are generated internally;
callers cannot choose arbitrary filesystem destinations.

## Retrieval

Sahara already stores chunk text in `chunks` and vectors in `vec_chunks`. This feature
does not replace or migrate that schema.

Add `IndexingService.index_path()` so capture and external edits can update one file
without scanning every content root. Memory Markdown frontmatter is parsed before
indexing; searchable text may include the title and tags, followed by the body, but
must exclude operational identifiers and source URLs.

Normal `sahara search` includes memories. `sahara recall` discovers the marked managed
root, scopes retrieval to its allocated prefix, and supports metadata filtering without
allowing unrelated vector candidates to consume the result window.

## Local CLI

Initial capture:

```bash
sahara remember "A useful fact" --title "Optional title" --tag research
cat transcript.txt | sahara remember --source conversation
```

Recall and management:

```bash
sahara recall "What did I learn about partial indexes?"
sahara memory list --tag postgres
sahara memory show UUID
sahara memory edit UUID
sahara memory delete UUID
sahara memory rebuild
```

Input precedence for `remember` is an explicit argument, then piped standard input.
Clipboard and editor integrations are added after the core capture path is stable.

## MCP

Read-only recall may be exposed as `sahara_recall`.

Write capture is a separate `sahara_remember` tool with these constraints:

- excluded from the default MCP tool set;
- enabled explicitly during local client installation or server startup;
- local stdio only in its first release;
- invoked only after an explicit user request to save information;
- restricted to create-only memory operations;
- request-size limit, idempotency key, and audit event;
- no arbitrary paths, editing, deletion, sync, or shell access.

Remote HTTP MCP remains read-only until write authorization has a separate security
design and test matrix.

The initial MCP implementation uses a 20,000-character capture limit, requires a
non-empty idempotency key and `explicit_user_request=true`, and stores metadata-only
audit events with the idempotency key hashed.

## Background Ingestion

The current daemon couples filesystem events to storage sync. Before inbox ingestion,
introduce an always-local index watcher that works without a storage backend.

The watcher will:

- monitor every registered content root;
- incrementally index created or changed supported files;
- remove search data for true deletions;
- normalize files placed in the memory inbox through `MemoryService`;
- keep optional storage synchronization as a separate worker.

## Mobile Capture

Mobile devices call a minimal capture API, not MCP:

```http
POST /v1/memories
Authorization: Bearer <device-token>
Content-Type: application/json

{
  "text": "Vendor X uses net-30 terms",
  "source_type": "conversation",
  "source_url": "",
  "tags": ["vendor"],
  "idempotency_key": "device-generated-uuid"
}
```

The service binds to loopback by default. Recommended remote access is a private
device network such as Tailscale Serve. Public exposure is not part of the initial
mobile release.

Device tokens are individually named and revocable, stored as hashes, and restricted
to `memory:capture` or optional `memory:recall`. Requests have content-size limits,
rate limits, idempotency, and an audit trail.

### Siri And Apple Shortcuts

An installable `Remember in Sahara` Shortcut provides the first iPhone experience:

1. Receive text or a URL from the iOS share sheet, or ask the user for dictated input.
2. Optionally ask for tags or a note about the source.
3. Generate an idempotency value.
4. POST JSON to the private Sahara capture endpoint.
5. Show and optionally speak a short success or pending-index result.

It can be launched by saying its name to Siri:

```text
"Siri, Remember in Sahara"
```

A separate `Recall from Sahara` Shortcut may send a query and display results. Recall
content should not be spoken automatically because memories may be sensitive.

The Shortcut is versioned as an integration artifact and documented step-by-step.
Device pairing should eventually generate its endpoint and token through a QR code.

### WhatsApp

WhatsApp is treated as a source application:

```text
Select or copy a message -> Share -> Remember in Sahara
```

When direct sharing is unavailable, the Shortcut captures clipboard content. Sahara
does not scrape WhatsApp or infer sender/conversation metadata it did not receive.

A WhatsApp Business bot is deferred. It would require a public webhook, business
number, Meta configuration, policy maintenance, and additional privacy exposure.
Unofficial WhatsApp Web automation is out of scope.

### Companion App

A lightweight mobile companion is considered after the Shortcut and API validate the
workflow. See `specs/COMPANION_APP_SPIKE.md` for the framework evaluation and
recommended rollout order. It should provide:

- iOS share extension and Android share target;
- quick text and voice capture;
- offline encrypted outbox with retry;
- QR-code device pairing;
- capture status and lightweight recall;
- attachment upload;
- Siri App Intent and Android App Action.

The companion does not contain the embedding model, semantic index, storage sync
engine, or complete archive. The desktop Sahara instance remains authoritative.

## Security Requirements

- Reject empty and oversized captures.
- Canonicalize and validate all generated paths beneath the memory root.
- Parse YAML with safe loading and validate expected scalar/list types.
- Use atomic writes and restrictive file permissions where supported.
- Store only token hashes; compare secrets in constant time.
- Use separate read and write scopes.
- Do not fetch source URLs during capture.
- Treat captured web and chat text as untrusted content during answer generation.
- Preserve the user's original text separately from any later generated summary.
- Record source type as user-supplied provenance, not verified truth.
- Require confirmation and consent for recorded or transcribed conversations.

## Modular Issue And PR Sequence

### 1. Core durable memory capture ([#57](https://github.com/nidheesh-p/sahara/issues/57))

Deliver:

- managed non-overlapping memory content root;
- versioned Markdown model and safe parser;
- atomic `MemoryService.capture`;
- single-path indexing;
- `sahara remember` with argument and stdin input;
- saved-but-index-pending behavior;
- focused tests and architecture documentation.

Depends on: nothing.

### 2. Memory catalog, recall, and lifecycle ([#58](https://github.com/nidheesh-p/sahara/issues/58))

Deliver:

- rebuildable `memory_items` metadata cache;
- `sahara recall`;
- list, show, edit, delete, and rebuild commands;
- source, tag, and date filters;
- deduplication by source ID, canonical URL, content hash, or idempotency key.

Depends on: issue 1.

### 3. Opt-in local MCP memory tools ([#59](https://github.com/nidheesh-p/sahara/issues/59))

Deliver:

- default read-only `sahara_recall`;
- opt-in stdio-only `sahara_remember`;
- Claude Desktop installer flag;
- create-only authorization, size limits, idempotency, and audit events;
- updated MCP security documentation.

Depends on: issues 1 and 2.

### 4. Always-local incremental index watcher and inbox ([#60](https://github.com/nidheesh-p/sahara/issues/60))

Deliver:

- index watcher separated from optional sync;
- incremental create/change/delete handling;
- managed memory inbox;
- external edit re-indexing;
- clipboard and editor capture helpers.

Depends on: issues 1 and 2.

### 5. Authenticated mobile capture API and device pairing ([#61](https://github.com/nidheesh-p/sahara/issues/61))

Deliver:

- loopback-only capture and optional recall endpoints;
- named scoped device tokens stored as hashes;
- idempotency, limits, rate limiting, and audit logging;
- QR/device pairing contract;
- private-network deployment guide.

Depends on: issues 1 and 2.

### 6. Siri, iOS share sheet, and WhatsApp sharing ([#62](https://github.com/nidheesh-p/sahara/issues/62))

Deliver:

- versioned `Remember in Sahara` Shortcut;
- Siri dictation and iOS share-sheet input;
- clipboard fallback for WhatsApp;
- optional display-only recall Shortcut;
- setup, revocation, privacy, and troubleshooting documentation.

Depends on: issue 5.

### 7. Lightweight cross-platform companion application ([#63](https://github.com/nidheesh-p/sahara/issues/63))

Deliver:

- product/technical spike followed by a separately approved implementation;
- iOS share extension and Android `ACTION_SEND` target;
- encrypted offline outbox and retry;
- QR pairing, status, and attachment capture;
- Siri App Intent and Android App Action.

Depends on: issues 5 and 6 plus validated user demand.

The current recommendation from the spike is to keep this issue as the decision
record and begin implementation with the iOS-first app in
[#74](https://github.com/nidheesh-p/sahara/issues/74) once demand is validated.

## First Release Acceptance

- Capture works in basic mode with no storage or answer provider.
- A saved memory survives embedding/model failure.
- Successful captures are searchable immediately without a full index scan.
- The memory root cannot overlap another content root.
- Storage sync is disabled until explicitly enabled.
- Existing file search, chunk storage, and sqlite-vec behavior remain compatible.
- Write-capable MCP and mobile integrations are absent by default.
- Markdown files alone are sufficient to rebuild memory metadata and retrieval state.
