# Answer Provider Setup

Semantic indexing and `sahara search` do not require an LLM. `sahara ask` first
retrieves relevant passages from the local index, then uses an answer provider to
summarize those passages with citations.

New Sahara installations use local Ollama by default. OpenAI is an optional,
deliberately selected alternative that can also be saved as the user's default.

## Local Ollama Setup

### 1. Install and start Ollama

macOS 14 Sonoma or newer:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Windows 10 or newer, from PowerShell:

```powershell
irm https://ollama.com/install.ps1 | iex
```

Graphical installers are also available from the official
[Ollama download page](https://ollama.com/download).

Linux:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

The Linux installer normally creates and starts a system service. If it is not
running, start `ollama serve` in a separate terminal.

Restart the terminal if `ollama` is not found immediately after installation.

### 2. Download Sahara's default model

```bash
ollama pull mistral
```

The current `mistral:latest` download is approximately 4.4 GB and happens only
once for that model version.

### 3. Verify Ollama

```bash
ollama --version
ollama list
ollama run mistral "Reply with only: Ollama is ready"
```

To check the local API directly:

```bash
curl http://localhost:11434/api/tags
```

### 4. Ask Sahara

After running `sahara index`:

```bash
sahara ask "what is the project deadline?"
```

Sahara connects to `http://localhost:11434` and uses `mistral`. Override either
setting when needed:

```bash
sahara ask --model MODEL_NAME "what is the project deadline?"
OLLAMA_URL=http://localhost:11434 sahara ask "what is the project deadline?"
```

The model must already exist in Ollama. Download another model with
`ollama pull MODEL_NAME`.

## Optional OpenAI Setup

OpenAI API usage is separate from a ChatGPT subscription. OpenAI currently enrolls
new API accounts in prepaid billing with a minimum $5 credit purchase. Review
[OpenAI API billing](https://help.openai.com/en/articles/8264778-what-is-prepaid-billing)
for the current terms before enabling it.

Create an [OpenAI API key](https://platform.openai.com/api-keys), then expose it
only in the shell or secret manager that starts Sahara. Ollama does not need to
be installed for this path:

```bash
# macOS or Linux
export OPENAI_API_KEY="your-api-key"

# Make OpenAI the persistent answer provider
sahara config set answer_provider openai
sahara ask "what is the project deadline?"
```

Windows PowerShell:

```powershell
$env:OPENAI_API_KEY = "your-api-key"
sahara config set answer_provider openai
sahara ask "what is the project deadline?"
```

The preference is stored in `~/.sahara/config.toml`; the API key is not. The CLI
and MCP server both use the saved provider. Explicit `--provider` flags override
it for one command.

The default OpenAI model is `gpt-4o-mini`. Save another model or select one for
a single command:

```bash
sahara config set answer_model gpt-4o
sahara ask --provider openai --model gpt-4o "what is the project deadline?"
```

Merely setting `OPENAI_API_KEY` does not change Sahara's default. A normal
`sahara ask` still uses the saved provider, which is Ollama on a new installation.
When OpenAI is selected, the retrieved snippets needed to answer the question are
sent to OpenAI.

For MCP clients, `OPENAI_API_KEY` must be available to the process that launches
`sahara mcp serve`. Terminal-launched clients inherit an exported key. Desktop
clients may use a limited environment; follow the client's secret/environment
configuration instead of putting the key in Sahara's TOML file.

Never put an API key in Sahara configuration, committed files, screenshots, or
bug reports.

## Troubleshooting

### `ollama: command not found`

Restart the terminal. If the command is still unavailable, reopen the Ollama
application on macOS or Windows, or rerun the Linux installer.

### `Ollama unavailable` or connection refused

Launch the Ollama application. On Linux, inspect the service:

```bash
systemctl status ollama
```

If no service is configured, run `ollama serve` in another terminal. Confirm
that `curl http://localhost:11434/api/tags` succeeds.

### Model not found

```bash
ollama pull mistral
ollama list
```

If using `--model`, its value must match a model shown by `ollama list`.

### Ollama uses a different host or port

```bash
OLLAMA_URL=http://127.0.0.1:11434 sahara ask "your question"
```

The `--ollama-url` option provides the same override for one command.

### OpenAI is selected but no answer is generated

Confirm that `OPENAI_API_KEY` is available in the same shell that runs Sahara,
that API billing is active, and that `answer_provider` is `openai` or the command
includes `--provider openai`. Check the saved choice with:

```bash
sahara config get answer_provider
```

Sahara falls back to ranked search results when either provider is unavailable.
