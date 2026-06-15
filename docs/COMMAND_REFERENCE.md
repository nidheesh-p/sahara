# Sahara Command Reference

This page lists every Sahara CLI command, grouped by purpose. Run
`sahara COMMAND --help` or `sahara GROUP COMMAND --help` for complete option
descriptions and current defaults.

## Global Options

These options appear before the command:

| Option | Purpose |
|---|---|
| `sahara --version` | Print the installed Sahara version |
| `sahara --config PATH COMMAND ...` | Use a configuration file other than `~/.sahara/config.toml` |
| `sahara --help` | Show top-level help and available commands |

## Setup and Diagnostics

| Command | Purpose |
|---|---|
| `sahara init [--mode MODE] [--folder PATH]` | Configure a new library; omit options for the interactive wizard |
| `sahara init --mode basic --folder PATH` | Create an index-only library with no storage backend |
| `sahara init --mode local --folder PATH --storage-drive PATH` | Create a library backed by a mounted drive, NAS, or network share |
| `sahara init --mode aws --folder PATH --bucket NAME [--region REGION]` | Create a library backed by AWS S3 |
| `sahara init` | Interactively configure basic, local, AWS, MinIO, or local-plus-Glacier mode |
| `sahara doctor [--repair]` | Check configuration, folders, index state, and storage connectivity |

`--mode` accepts `basic`, `local`, `aws`, `minio`, or `local+glacier`.
Non-interactive MinIO setup is not currently supported.

## Indexing and Search

These commands work in basic index-only mode and do not require optional storage.

| Command | Purpose |
|---|---|
| `sahara folder add PATH [--name NAME]` | Add a folder to the semantic index; it starts with storage sync disabled |
| `sahara folder list` | List indexed folders and their sync state |
| `sahara folder remove PATH [--force]` | Remove a non-primary folder and its local search index |
| `sahara folders` | Alias-style top-level listing of all content roots |
| `sahara index [--folder PATH] [--force]` | Index all registered folders, one folder, or force unchanged files to re-index |
| `sahara index-report [--top N] [--sample N]` | Show indexed, unsupported, failed, missing, and unindexed content |
| `sahara remember [TEXT] [OPTIONS]` | Save captured knowledge as Markdown and index it |
| `sahara search QUERY [-n N] [-f PATH] [--snippet]` | Search indexed content by meaning |
| `sahara ask QUESTION [-n N] [-f PATH] [--snippet]` | Retrieve passages and optionally generate an answer with a configured provider |

`sahara remember` accepts text as an argument or through standard input. Its options
include `--title`, `--source manual|web|conversation|ai-chat|mobile`, `--url`,
`--source-id`, and repeatable `--tag`. The first capture creates `~/Sahara Memory`
unless `memory_folder` is configured. The folder starts with storage sync disabled,
and the Markdown file remains saved if semantic indexing is unavailable.

Answer-provider overrides:

| Option | Purpose |
|---|---|
| `--provider none\|ollama\|openai` | Override the saved provider for one question |
| `--model NAME` | Override the selected provider's model |
| `--ollama-url URL` | Override the Ollama server URL |
| `--snippet` | Show supporting source snippets even when an answer is generated |

## MCP

| Command | Purpose |
|---|---|
| `sahara mcp install-claude` | Merge Sahara into the detected Claude Desktop configuration |
| `sahara mcp install-claude --claude-config PATH --executable PATH` | Override automatic config or executable detection |
| `sahara mcp serve` | Run the read-only MCP server over stdio |
| `sahara mcp serve --transport http --auth-token TOKEN` | Run authenticated streamable HTTP MCP on `127.0.0.1:8765` |

Useful `mcp serve` options:

| Option | Purpose |
|---|---|
| `--transport stdio\|http\|streamable-http\|sse` | Choose the MCP transport |
| `--host HOST` / `--port PORT` | Set the HTTP or SSE bind address |
| `--auth-token TOKEN` | Require a bearer token; also available as `SAHARA_MCP_AUTH_TOKEN` |
| `--allow-tool TOOL` | Expose only one named tool; repeat to allow several |
| `--allow-storage-prefix PREFIX` | Restrict MCP to one indexed folder scope; repeatable |
| `--max-snippet-chars N` | Limit text returned per snippet or chunk |
| `--allow-insecure-http` | Disable bearer-token enforcement for temporary local experiments |

Remote transports require authentication unless `--allow-insecure-http` is explicitly
used. Binding beyond loopback prints a security warning.

## Optional Storage

### Configure Storage

| Command | Purpose |
|---|---|
| `sahara storage configure local --drive PATH` | Attach a mounted drive, NAS, or network share to an existing library |
| `sahara storage configure aws --bucket NAME [--region REGION]` | Attach AWS S3 to an existing library |
| `sahara storage status` | Show the active backend, sync-root count, and offloaded-file count |
| `sahara storage disable [--force]` | Disable sync without deleting already stored data |
| `sahara folder sync PATH --enable` | Enable storage sync for an indexed folder |
| `sahara folder sync PATH --disable` | Keep indexing a folder but stop syncing it |

Storage configuration is validated before it is saved. Adding storage does not
automatically enable sync for existing content roots.

### Synchronize

| Command | Purpose |
|---|---|
| `sahara sync [--dry-run] [--verify] [--wait] [-f PATH]` | Run bidirectional synchronization |
| `sahara push [--dry-run] [--verify] [-f PATH]` | Upload local changes without pulling |
| `sahara pull [--dry-run] [--wait] [-f PATH]` | Download remote changes without pushing |
| `sahara status` | Show pending changes without applying them |
| `sahara diff` | Alias for `sahara status` |

`--folder PATH` limits an operation to one registered sync root. `--wait` is retained
for restore-aware workflows.

### Offload and Restore Local Space

| Command | Purpose |
|---|---|
| `sahara offload PATH` | Verify the stored plaintext checksum, retain search metadata, and remove the local source |
| `sahara fetch PATH` | Restore an intentionally offloaded file and verify its checksum |

Ordinary filesystem deletion is not treated as offload.

## Tracked Files and History

These commands operate on storage-tracked files.

| Command | Purpose |
|---|---|
| `sahara ls [PREFIX] [-l] [--tier TIER] [--all]` | List tracked files, optionally with metadata or across all registered folders |
| `sahara rm PATH [--force] [--local]` | Delete a tracked file; `--local` keeps the S3 copy |
| `sahara mv SRC DST` | Move a file in the primary folder and S3 |
| `sahara history [PATH] [--limit N]` | Show synchronization history globally or for one path |

`sahara rm` and `sahara mv` are currently S3-oriented commands. Use ordinary
filesystem tools plus `sahara sync` for local-drive workflows.

## Conflicts

| Command | Purpose |
|---|---|
| `sahara conflicts` | List unresolved synchronization conflicts |
| `sahara resolve [PATH] [--keep local\|remote\|backup]` | Resolve one conflict or run resolution using the selected strategy |

The default resolution strategy is `backup`.

## AWS Archival and Restore

These commands require AWS S3 storage classes; they are not available for local-drive
or MinIO backends.

| Command | Purpose |
|---|---|
| `sahara archive [PATHS] [--older-than DAYS] [--storage-class CLASS] [--dry-run] [--force] [--folder PATH]` | Move selected, old, or all tracked files into another S3 storage class |
| `sahara restore PATH [--days N] [--tier TIER]` | Request temporary access to a Glacier object |
| `sahara restore-status [PATH]` | Check one restore or all pending restores |
| `sahara restore-download PATH` | Download an object after its Glacier restore is ready |
| `sahara usage` | Report current S3 storage usage and estimated cost |
| `sahara usage --simulate [COST OPTIONS]` | Estimate cost from supplied storage and request volumes |

Archive storage classes are `DEEP_ARCHIVE`, `GLACIER_IR`, and `STANDARD`. Restore
tiers are `Expedited`, `Standard`, and `Bulk`.

## Encryption

Encryption applies to optional storage uploads, not the local semantic index.

| Command | Purpose |
|---|---|
| `sahara encryption setup` | Enable AES-256-GCM storage encryption and save the passphrase in the OS keyring |
| `sahara encryption rotate` | Re-encrypt existing S3 objects with a new passphrase |

Rotation currently operates on S3-backed files. Review [SECURITY.md](../SECURITY.md)
before relying on encrypted storage.

## Background Daemon

| Command | Purpose |
|---|---|
| `sahara daemon start [--autostart]` | Start background watching and synchronization |
| `sahara daemon stop` | Stop the daemon |
| `sahara daemon status` | Show running and paused state plus PID and log paths |
| `sahara daemon pause` | Pause synchronization without stopping the process |
| `sahara daemon resume` | Resume a paused daemon |
| `sahara daemon logs [-n LINES] [--follow]` | Show or follow daemon logs |

The daemon is not required for manual indexing, search, or MCP.

## Configuration

| Command | Purpose |
|---|---|
| `sahara config show` | Print every loaded configuration value |
| `sahara config get KEY` | Print one configuration value |
| `sahara config set KEY VALUE` | Save one configuration value |

The default file is `~/.sahara/config.toml`. Use the global
`sahara --config PATH ...` option for another library or configuration.

API keys and bearer tokens should remain in environment variables or a secret manager,
not in Sahara's TOML file.

## Legacy Folder Commands

These commands predate the index-first `sahara folder ...` interface. They remain
available for compatibility with existing storage-backed workflows.

| Command | Purpose |
|---|---|
| `sahara add PATH [--as NAME] [--dest PREFIX]` | Register an additional folder with storage sync enabled |
| `sahara remove PATH [--force]` | Unregister a legacy sync target without deleting remote data |
| `sahara folders` | List current content roots and their index/sync state |

For new index-only folders, prefer `sahara folder add PATH`.
