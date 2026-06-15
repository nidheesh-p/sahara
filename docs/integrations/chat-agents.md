# Sahara Chat and Agent Integrations

Sahara exposes its local index to chat clients and agent runtimes through MCP. The
default surface is read-only. A separate create-only memory tool can be enabled for a
local stdio client; remote transports remain read-only.

## Install

Sahara requires Python 3.11 or newer. Its Python distribution is
`sahara-memory`; `sahara` on PyPI is an unrelated OpenStack project.

```bash
pipx install "sahara-memory[search,mcp]"
```

See the [installation guide](../INSTALLATION.md) for `pipx` setup, virtual
environments, and PEP 668 troubleshooting.

Index files before connecting a chat client:

```bash
sahara init --mode basic --folder ~/Documents
sahara index
sahara index-report
sahara mcp serve
```

MCP works in basic index-only mode; no storage backend or sync is required.

For remote MCP clients such as Claude mobile, serve MCP over local HTTP and expose it
through a secure HTTPS tunnel:

```bash
export SAHARA_MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
sahara mcp serve --transport http --host 127.0.0.1 --port 8765
```

Remote HTTP/SSE transports require bearer-token authentication by default. Clients must
send `Authorization: Bearer <token>`. For temporary local experiments only, use
`--allow-insecure-http`.

If authenticated startup reports that MCP SDK 1.14.0 or newer is required, upgrade the
SDK inside the pipx installation:

```bash
pipx runpip sahara-memory install --upgrade "mcp>=1.14.0"
```

For a virtual-environment installation, activate that environment and run:

```bash
python -m pip install --upgrade "mcp>=1.14.0"
```

## MCP Tools

The default MCP surface is read-only:

| Tool | Purpose |
|---|---|
| `sahara_search` | Return ranked indexed files/chunks for a query |
| `sahara_ask` | Retrieve cited sources and optionally generate an answer |
| `sahara_read_chunk` | Return one indexed chunk by id |
| `sahara_list_folders` | List primary and additional Sahara folders |
| `sahara_index_status` | Show indexed file/chunk counts and vector-index availability |
| `sahara_recall` | Search only managed captured memories with metadata filters |

The server does not expose sync mutation, file writes, shell execution, or arbitrary
filesystem reads. Search results identify intentionally offloaded files, but MCP does
not expose offload or fetch operations.

For a local stdio client, `--enable-memory-write` adds `sahara_remember`. It is
create-only and routes through `MemoryService`; it cannot choose a path, edit or delete
memories, sync files, or execute commands. Every request must attest that the user
explicitly asked to save the information, include a non-empty idempotency key, and stay
within 20,000 characters. Audit events store outcomes and a hash of the idempotency key,
not captured text.

No standalone answer provider is required. By default, `sahara_ask` returns ranked
cited snippets for the MCP client's model to use. Ollama and OpenAI are optional
providers for generating an answer inside Sahara itself.

## Claude Desktop

Claude Desktop launches Sahara locally over stdio. Install the connection without
editing JSON:

```bash
sahara mcp install-claude
```

Memory writes remain disabled unless separately enabled:

```bash
sahara mcp install-claude --enable-memory-write
```

Then fully quit and reopen Claude Desktop. Use the complete
[Claude Desktop guide](../CLAUDE_DESKTOP.md) for verification, manual fallback,
the exact tool contract, security boundaries, and troubleshooting.

## Claude Mobile / Remote MCP

Claude mobile cannot launch Sahara as a local stdio process. To use Sahara from mobile,
run Sahara's authenticated HTTP MCP transport locally, expose it through a secure tunnel,
then add the public HTTPS MCP URL as a custom connector from Claude on the web.

Remote MCP remains read-only. `--enable-memory-write` is rejected for HTTP and SSE
transports.

```bash
export SAHARA_MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
sahara mcp serve --transport http --host 127.0.0.1 --port 8765
ngrok http 8765
```

Use the tunnel's HTTPS MCP endpoint, usually:

```text
https://<your-tunnel-host>/mcp
```

Keep the Sahara server bound to `127.0.0.1` unless you have a specific network reason
not to. The tunnel becomes the public entry point. Because Claude connects from
Anthropic's cloud infrastructure, the tunnel URL must be reachable from the public
internet.

When exposing Sahara remotely, consider narrowing the tool and folder surface:

```bash
sahara mcp serve \
  --transport http \
  --auth-token "$SAHARA_MCP_AUTH_TOKEN" \
  --allow-tool sahara_search \
  --allow-tool sahara_ask \
  --allow-storage-prefix work \
  --max-snippet-chars 300
```

If a client cannot send a static bearer token, use an OAuth-capable bridge or keep the
integration local until Sahara grows first-class OAuth support.

## ChatGPT

ChatGPT connector support should remain optional until the integration can preserve
Sahara's local-first privacy expectations. If a remote bridge is used, document the
authentication scope, indexed folders, data flow, and which snippets leave the local
machine.

## Future Clients

OpenClaw integration guidance remains on the future roadmap. It should be published
after Sahara's read-only MCP tools have been validated with OpenClaw end-to-end.
