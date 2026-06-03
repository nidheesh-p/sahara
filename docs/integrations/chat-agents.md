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
sahara mcp serve
```

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

Add Sahara as a local MCP server in Claude Desktop's MCP configuration. Use the full
path to the `sahara` executable if Claude Desktop cannot find it through your shell
environment.

```json
{
  "mcpServers": {
    "sahara": {
      "command": "sahara",
      "args": ["mcp", "serve"]
    }
  }
}
```

With a custom config path:

```json
{
  "mcpServers": {
    "sahara": {
      "command": "sahara",
      "args": ["--config", "/Users/you/.sahara/config.toml", "mcp", "serve"]
    }
  }
}
```

## OpenClaw

Treat OpenClaw as the agent runtime and Sahara as the retrieval layer. Prefer calling
Sahara's read-only MCP tools over giving an agent broad filesystem access.

Example flow:

```text
User asks OpenClaw:
  Find the document where I discussed the kitchen renovation budget.

OpenClaw calls:
  sahara_search("kitchen renovation budget", top_k=8)

Sahara returns:
  ranked local snippets, paths, scores, and citations
```

## ChatGPT

ChatGPT connector support should remain optional until the integration can preserve
Sahara's local-first privacy expectations. If a remote bridge is used, document the
authentication scope, indexed folders, data flow, and which snippets leave the local
machine.
