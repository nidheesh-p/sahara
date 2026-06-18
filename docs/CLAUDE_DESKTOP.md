# Connect Sahara to Claude Desktop

Sahara exposes read-only Model Context Protocol (MCP) retrieval tools for Claude
Desktop by default. Claude launches Sahara as a local subprocess over **stdio**. HTTP
is not needed for Claude Desktop on the same computer.

## Prerequisites

Use Python 3.11 or newer. Install the `sahara-memory` distribution with search and
MCP support through `pipx`, initialize it, and build the local index:

```bash
pipx install "sahara-memory[search,mcp]"
sahara init --mode basic --folder ~/Documents
sahara index
sahara index-report
```

See [Installation](INSTALLATION.md) for `pipx` setup and the virtual-environment
alternative.

## Install in Claude Desktop

```bash
sahara mcp install-claude
```

This default installation includes memory recall but cannot save or modify anything.
To add create-only memory capture:

```bash
sahara mcp install-claude --enable-memory-write
```

Only enable it when you want Claude to save information after you explicitly ask.

The installer:

- detects the supported macOS or Windows Claude Desktop config location
- detects the absolute path to the current Sahara executable
- preserves existing Claude preferences and other MCP servers
- adds or updates only `mcpServers.sahara`
- creates `claude_desktop_config.json.sahara-backup` before changing an existing file
- keeps a non-default Sahara config when invoked as
  `sahara --config /path/to/config.toml mcp install-claude`

Fully quit and reopen Claude Desktop. Closing only the window is not enough after an
MCP configuration change.

### Path overrides

Automatic detection covers standard macOS installs, standard Windows installs, and
known Windows MSIX package locations. For an unusual installation, provide explicit
paths:

```bash
sahara mcp install-claude \
  --claude-config "/custom/path/claude_desktop_config.json" \
  --executable "/absolute/path/to/sahara"
```

## Manual Configuration

Use this fallback when automatic installation is not appropriate. Find the executable
with `command -v sahara` on macOS or `(Get-Command sahara).Source` in Windows
PowerShell.

Standard config locations:

| Platform | Configuration file |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Windows MSIX | Under `%LOCALAPPDATA%\Packages\<Claude package>\LocalCache\Roaming\Claude\claude_desktop_config.json` |
| Linux | Claude Desktop is not officially available, so there is no supported Linux config location |

Merge this entry into the existing JSON rather than replacing the whole file:

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

The server command is `sahara mcp serve --transport stdio`. The process may appear
silent when run directly in a terminal because it is waiting for MCP messages on
standard input.

## Verify the Connection

1. Start a new Claude Desktop conversation.
2. Click the **Add files, connectors, and more** plus icon in the chat input.
3. Open or hover over **Connectors**.
4. Confirm that **sahara** appears and exposes six read-only tools:
   `sahara_search`, `sahara_ask`, `sahara_read_chunk`, `sahara_list_folders`, and
   `sahara_index_status`, plus `sahara_recall`.
5. Ask:

   ```text
   Use Sahara to find the document about <a topic in your indexed files>.
   Include the source path and supporting snippet.
   ```

Claude may ask for permission before calling a tool. The answer should cite a path from
Sahara's indexed corpus.

## MCP Tool Contract

Sahara exposes six read-only tools by default. There is no separate `list_documents` tool;
`sahara_search` returns matching documents and `sahara_list_folders` reports configured
index scopes. An explicitly enabled local installation exposes a seventh,
create-only tool: `sahara_remember`.

### `sahara_search`

Search the local semantic index.

Inputs:

| Name | Type | Required | Meaning |
|---|---|---|---|
| `query` | string | yes | Natural-language search query |
| `top_k` | integer | no | Number of results; defaults to 5 and is clamped to 1–20 |
| `storage_prefix` | string or null | no | Restrict search to one configured Sahara prefix |

Output: a list of objects containing `storage_prefix`, `relative_path`, `score`, and
`snippet`, plus `local_state` (`present` or `offloaded`).

### `sahara_ask`

Answer a question from retrieved indexed snippets and return cited sources.

Inputs:

| Name | Type | Required | Meaning |
|---|---|---|---|
| `question` | string | yes | Question to answer from indexed content |
| `top_k` | integer | no | Retrieval count; defaults to 5 and is clamped to 1–20 |
| `storage_prefix` | string or null | no | Restrict retrieval to one configured Sahara prefix |
| `provider` | string or null | no | Override with `none`, `ollama`, or `openai`; null uses the saved `answer_provider` |

Output: an object containing `answer`, `sources`, `degraded`, `model_used`,
`provider_used`, and `error`. Each source contains `storage_prefix`, `relative_path`,
`score`, and `snippet`.

Search and indexing stay local. `sahara_ask` is retrieval-only by default and returns
cited snippets for Claude to reason over, so Ollama and OpenAI are not required.
Users who intentionally choose OpenAI can save it with:

```bash
sahara config set answer_provider openai
```

When selected, the retrieved snippets used as context are sent to OpenAI. The key must
be available to the MCP process and is never stored in Sahara's configuration.

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

Output: a list containing `local_path`, `storage_prefix`, `role`, and `sync_enabled`.
Index-only folders are included.

### `sahara_index_status`

Report whether the local semantic index is ready.

Inputs: none.

Output: an object containing `indexed_files`, `indexed_chunks`, `latest_indexed_at`,
`vector_index_available`, and `embedding_model`.

### `sahara_recall`

Search only the managed Sahara memory root.

Inputs include `query`, optional `top_k`, optional lists of `source_types` and `tags`,
and optional `since`/`until` timestamps. Results contain the memory UUID, title,
relative path, score, body-only snippet, source metadata, tags, and update timestamp.

### `sahara_remember` (Opt-In)

Create one durable Markdown memory. This tool exists only when Claude Desktop was
installed with `--enable-memory-write`.

Required inputs:

| Name | Type | Meaning |
|---|---|---|
| `text` | string | Knowledge to save, limited to 20,000 characters |
| `idempotency_key` | string | Stable retry key chosen by the client |
| `explicit_user_request` | boolean | Must be true only after the user explicitly asks to save |

Optional inputs are `title`, `source_type`, `source_url`, `source_id`, and `tags`.
Responses report `saved_and_indexed`, `saved_index_pending`, or `already_saved`.

## Security Boundary

The default MCP surface is read-only and scoped to Sahara's configured and indexed
corpus.

- Default tools cannot write, rename, delete, sync, archive, restore, or execute shell
  commands.
- It cannot accept an arbitrary filesystem path and read that file.
- `sahara_read_chunk` can only return text already stored in Sahara's index.
- `sahara_search` and `sahara_ask` can only retrieve indexed content.
- `sahara_list_folders` reveals configured local folder paths but does not read
  arbitrary files from those paths.
- Snippets and chunks are limited to 500 characters by default.
- `sahara_remember` is local-stdio-only, create-only, explicitly enabled, size-limited,
  and idempotent. It cannot edit or delete memories.
- Capture audit events contain outcome metadata and a hashed idempotency key, never the
  captured text.
- HTTP and SSE transports reject `--enable-memory-write`, even when authenticated.

For tighter scope, add repeated `--allow-tool` and `--allow-storage-prefix` arguments,
or reduce `--max-snippet-chars` in the Claude configuration.

## Troubleshooting

### Sahara does not appear in Claude Desktop

- Re-run `sahara mcp install-claude` and fully quit and reopen Claude Desktop.
- If the installer reports invalid JSON, repair the existing Claude config first; it
  deliberately leaves malformed files unchanged.
- On unusual Windows installations, use **Settings > Developer > Edit Config** to
  identify the active path, then pass it with `--claude-config`.
- Confirm MCP support is installed with `pipx list`. If the installation lacks the MCP
  extra, uninstall Sahara and reinstall it with
  `pipx install "sahara-memory[search,mcp]"`.
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
4. Run `sahara mcp install-claude` and restart Claude Desktop.
5. Ask one question whose answer is in that document.
6. Confirm Claude invokes Sahara and returns the cited source path and snippet.
7. Stop the timer and record the OS, install method, elapsed time, friction, and result
   with the release verification notes.

Target: a new user reaches a cited Claude Desktop answer in under five minutes.

## References

- [Connect to local MCP servers](https://modelcontextprotocol.io/docs/tutorials/use-local-mcp-server)
- [Debugging MCP and Claude Desktop](https://modelcontextprotocol.io/docs/tools/debugging)
- [Anthropic: local MCP servers on Claude Desktop](https://support.anthropic.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop)
