# Sahara Chat and Agent Integrations

Sahara can expose its local index to chat clients and agent runtimes through a
read-only MCP server. The chat client provides the conversation surface. Sahara
retrieves indexed local context, returns snippets and citations, and does not grant
write access to the filesystem.

## Install

```bash
pip install "sahara[search,mcp]"
```

Index files before connecting a chat client:

```bash
sahara init
sahara index
sahara index-report
sahara mcp serve
```

For remote MCP clients such as Claude mobile, serve MCP over local HTTP and expose it
through a secure HTTPS tunnel:

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
filesystem reads.

## Claude Desktop

Claude Desktop launches Sahara locally over stdio. Use the complete
[Claude Desktop guide](../CLAUDE_DESKTOP.md) for platform config locations,
copy-pasteable JSON, verification, the exact tool contract, security boundaries, and
troubleshooting.

## Claude Mobile / Remote MCP

Claude mobile cannot launch Sahara as a local stdio process. To use Sahara from mobile,
run Sahara's authenticated HTTP MCP transport locally, expose it through a secure tunnel,
then add the public HTTPS MCP URL as a custom connector from Claude on the web.

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
