# Connect Sahara to Claude Desktop

Sahara exposes a read-only Model Context Protocol (MCP) server for Claude Desktop.
Claude launches Sahara as a local subprocess over **stdio**. HTTP is not needed for
Claude Desktop on the same computer.

## Prerequisites

Install Sahara with search and MCP support, initialize it, and build the local index:

```bash
pip install "sahara[search,mcp]"
sahara init
sahara index
sahara index-report
```

Find the absolute path to the Sahara executable:

```bash
# macOS
command -v sahara
```

```powershell
# Windows PowerShell
(Get-Command sahara).Source
```

The server command Claude Desktop will run is:

```bash
sahara mcp serve --transport stdio
```

`stdio` is the default transport, but keeping it explicit in the Claude configuration
makes the intended local connection clear. The process may appear silent when run in a
terminal because it is waiting for MCP messages on standard input.

## Claude Desktop Configuration

Open Claude Desktop, then use **Settings > Developer > Edit Config**, or edit the file
directly:

| Platform | Configuration file |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | Claude Desktop is not officially available, so there is no supported Linux config location |

Use the absolute executable path returned above. A macOS example:

```json
{
  "mcpServers": {
    "sahara": {
      "command": "/Users/you/.local/bin/sahara",
      "args": ["mcp", "serve", "--transport", "stdio"]
    }
  }
}
```

A Windows example:

```json
{
  "mcpServers": {
    "sahara": {
      "command": "C:\\Users\\you\\AppData\\Local\\Programs\\Python\\Python312\\Scripts\\sahara.exe",
      "args": ["mcp", "serve", "--transport", "stdio"]
    }
  }
}
```

For a non-default Sahara config, add the global `--config` option before `mcp`:

```json
{
  "mcpServers": {
    "sahara": {
      "command": "/absolute/path/to/sahara",
      "args": [
        "--config",
        "/absolute/path/to/config.toml",
        "mcp",
        "serve",
        "--transport",
        "stdio"
      ]
    }
  }
}
```

Save the JSON, fully quit Claude Desktop, and reopen it. Closing only the window is not
enough after an MCP configuration change.

## Verify the Connection

1. Start a new Claude Desktop conversation.
2. Click the **Add files, connectors, and more** plus icon in the chat input.
3. Open or hover over **Connectors**.
4. Confirm that **sahara** appears and exposes five tools:
   `sahara_search`, `sahara_ask`, `sahara_read_chunk`, `sahara_list_folders`, and
   `sahara_index_status`.
5. Ask:

   ```text
   Use Sahara to find the document about <a topic in your indexed files>.
   Include the source path and supporting snippet.
   ```

Claude may ask for permission before calling a tool. The answer should cite a path from
Sahara's indexed corpus.

## MCP Tool Contract

Sahara exposes exactly five tools. There is no separate `list_documents` tool;
`sahara_search` returns matching documents and `sahara_list_folders` reports configured
index scopes.

### `sahara_search`

Search the local semantic index.

Inputs:

| Name | Type | Required | Meaning |
|---|---|---|---|
| `query` | string | yes | Natural-language search query |
| `top_k` | integer | no | Number of results; defaults to 5 and is clamped to 1–20 |
| `storage_prefix` | string or null | no | Restrict search to one configured Sahara prefix |

Output: a list of objects containing `storage_prefix`, `relative_path`, `score`, and
`snippet`.

### `sahara_ask`

Answer a question from retrieved indexed snippets and return cited sources.

Inputs:

| Name | Type | Required | Meaning |
|---|---|---|---|
| `question` | string | yes | Question to answer from indexed content |
| `top_k` | integer | no | Retrieval count; defaults to 5 and is clamped to 1–20 |
| `storage_prefix` | string or null | no | Restrict retrieval to one configured Sahara prefix |
| `provider` | string or null | no | `ollama` for local generation or `openai` for OpenAI |

Output: an object containing `answer`, `sources`, `degraded`, `model_used`,
`provider_used`, and `error`. Each source contains `storage_prefix`, `relative_path`,
`score`, and `snippet`.

Search and indexing stay local. `sahara_ask` uses local Ollama by default when no
OpenAI key is available. If OpenAI is selected or available through `OPENAI_API_KEY`,
the retrieved snippets used as context are sent to OpenAI.

### `sahara_read_chunk`

Read one chunk that already exists in Sahara's index.

Input:

| Name | Type | Required | Meaning |
|---|---|---|---|
| `chunk_id` | integer | yes | Existing Sahara chunk identifier |

Output: `null` when not found, otherwise an object containing `id`, `storage_prefix`,
`relative_path`, `chunk_index`, `content_hash`, `chunk_text`, and `indexed_at`.

### `sahara_list_folders`

List the primary and additional folders configured in Sahara.

Inputs: none.

Output: a list containing `local_path`, `storage_prefix`, `role`, and, for additional
folders, `added_at`.

### `sahara_index_status`

Report whether the local semantic index is ready.

Inputs: none.

Output: an object containing `indexed_files`, `indexed_chunks`, `latest_indexed_at`,
`vector_index_available`, and `embedding_model`.

## Security Boundary

The MCP surface is read-only and scoped to Sahara's configured and indexed corpus.

- It cannot write, rename, delete, sync, archive, restore, or execute shell commands.
- It cannot accept an arbitrary filesystem path and read that file.
- `sahara_read_chunk` can only return text already stored in Sahara's index.
- `sahara_search` and `sahara_ask` can only retrieve indexed content.
- `sahara_list_folders` reveals configured local folder paths but does not read
  arbitrary files from those paths.
- Snippets and chunks are limited to 500 characters by default.

For tighter scope, add repeated `--allow-tool` and `--allow-storage-prefix` arguments,
or reduce `--max-snippet-chars` in the Claude configuration.

## Troubleshooting

### Sahara does not appear in Claude Desktop

- Validate the JSON syntax and fully quit and reopen Claude Desktop.
- Confirm MCP support is installed: `pip install "sahara[search,mcp]"`.
- Run `/absolute/path/to/sahara mcp serve --help` in a terminal.
- Check Claude MCP logs:
  - macOS: `~/Library/Logs/Claude`
  - Windows: `%APPDATA%\Claude\logs`

### Claude reports `ENOENT`, `spawn`, or command-not-found errors

Claude Desktop receives a limited shell environment. Use the absolute path from
`command -v sahara` or `(Get-Command sahara).Source`; do not rely on `PATH`, shell
aliases, or relative paths. Windows JSON paths require escaped backslashes.

### Permission or configuration errors

- Confirm the user running Claude can read `~/.sahara/config.toml` and
  `~/.sahara/state.db`.
- Confirm the configured sync folders still exist.
- On macOS, review **System Settings > Privacy & Security** if access is blocked.
- If encryption is enabled, ensure the same user can access the operating-system
  keyring entry.

### The daemon is not running or results are empty/stale

The Sahara daemon is **not required** for Claude Desktop or MCP. The MCP server reads
the existing local index directly. Check and refresh the index:

```bash
sahara index-report
sahara index
```

Run `sahara daemon start` only when you want background sync and file watching. If a
recent file is missing, index it after it has been synchronized or added locally.

## Cold-Start Launch Test

Before wider promotion, test this guide on a clean macOS or Windows account/machine:

1. Start a timer before installation.
2. Install Sahara and its search/MCP extras.
3. Run `sahara init`, add a small known document, and run `sahara index`.
4. Configure Claude Desktop using this guide.
5. Ask one question whose answer is in that document.
6. Confirm Claude invokes Sahara and returns the cited source path and snippet.
7. Stop the timer and record the OS, install method, elapsed time, friction, and result
   with the release verification notes.

Target: a new user reaches a cited Claude Desktop answer in under five minutes.

## References

- [Connect to local MCP servers](https://modelcontextprotocol.io/docs/tutorials/use-local-mcp-server)
- [Debugging MCP and Claude Desktop](https://modelcontextprotocol.io/docs/tools/debugging)
- [Anthropic: local MCP servers on Claude Desktop](https://support.anthropic.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop)
