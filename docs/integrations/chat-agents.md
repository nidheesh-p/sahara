# Sahara Chat and Agent Integrations

Sahara can expose its local index to chat clients and agent runtimes through a
read-only MCP server. The chat client provides the conversation surface. Sahara
retrieves indexed local context, returns snippets and citations, and does not grant
write access to the filesystem.

MCP is Sahara's integration boundary. The five-tool contract and transports are
client-neutral, while configuration files, authentication support, and UI behavior are
client-specific.

## Install

Sahara requires Python 3.11 or newer. Its Python distribution is
`sahara-memory`; `sahara` on PyPI is an unrelated OpenStack project. On Windows,
replace `python3` below with `py -3.11`.

```bash
python3 -m pip install \
  "sahara-memory[search,mcp] @ git+https://github.com/nidheesh-p/sahara.git"
```

Index files before connecting a chat client:

```bash
sahara init --mode basic --folder ~/Documents
sahara index
sahara index-report
sahara mcp serve
```

MCP works in basic index-only mode; no storage backend or sync is required. For a local
stdio-capable client, the portable server definition is:

```text
command: /absolute/path/to/sahara
args: mcp serve --transport stdio
```

Clients may encode that command differently in JSON, TOML, UI settings, or extension
manifests. Use the client's own documentation for its configuration shape.

For remote MCP clients, serve MCP over local HTTP and expose it through a secure HTTPS
tunnel:

```bash
export SAHARA_MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
sahara mcp serve --transport http --host 127.0.0.1 --port 8765
```

Remote HTTP/SSE transports require bearer-token authentication by default. Clients must
send `Authorization: Bearer <token>`. For temporary local experiments only, use
`--allow-insecure-http`.

## MCP Tools

The first MCP surface is read-only:

| Tool | Purpose |
|---|---|
| `sahara_search` | Return ranked indexed files/chunks for a query |
| `sahara_ask` | Answer a question with cited Sahara sources |
| `sahara_read_chunk` | Return one indexed chunk by id |
| `sahara_list_folders` | List primary and additional Sahara folders |
| `sahara_index_status` | Show indexed file/chunk counts and vector-index availability |

The server does not expose sync mutation, file writes, shell execution, or arbitrary
filesystem reads. Search results identify intentionally offloaded files, but MCP does
not expose offload or fetch operations.

## Client Status

| Client path | Status | Transport |
|---|---|---|
| Claude Desktop | Tested and documented | Local stdio |
| Claude mobile/web custom connector | Documented; end-to-end validation pending | Authenticated streamable HTTP |
| Other local MCP clients | Protocol-compatible in principle; not yet validated | Local stdio |
| ChatGPT | Future validation and guidance | To be determined |
| OpenClaw | Future validation and guidance | To be determined |

"Protocol-compatible" means Sahara implements a standard MCP server transport. It does
not mean every client has been tested or accepts the same configuration syntax.

## Claude Desktop: First Tested Client

Claude Desktop launches Sahara locally over stdio. Install the connection without
editing JSON:

```bash
sahara mcp install-claude
```

Then fully quit and reopen Claude Desktop. Use the complete
[Claude Desktop guide](../CLAUDE_DESKTOP.md) for verification, manual fallback,
the exact tool contract, security boundaries, and troubleshooting.

## Remote MCP

Remote clients cannot launch Sahara as a local stdio process. Run Sahara's authenticated
HTTP MCP transport locally, expose it through a secure tunnel, then configure the public
HTTPS MCP URL in a client that supports remote MCP and bearer-token authentication.

Claude mobile/web custom connectors are the first documented remote path, but that
workflow still needs clean-machine end-to-end validation.

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

## Future Client Validation

### ChatGPT

ChatGPT connector support should remain optional until the integration can preserve
Sahara's local-first privacy expectations. If a remote bridge is used, document the
authentication scope, indexed folders, data flow, and which snippets leave the local
machine.

### OpenClaw

OpenClaw integration guidance remains on the future roadmap. It should be published
after Sahara's read-only MCP tools have been validated with OpenClaw end-to-end.
