# Sahara Demo Assets

All demo documents and screenshots in this directory are fictional.

## README visual

`../images/sahara-memory-demo.svg` presents three common personal-memory flows:

- timeline reconstruction from an itinerary
- forgotten vendor lookup from an invoice
- honest retrieval when the requested detail is absent

## Social image

`../images/sahara-mcp-social.png` is a wide social-post image showing cited
retrieval inside a generic MCP chat client. It contains no real people or data.

## Terminal recording

Install [VHS](https://github.com/charmbracelet/vhs), install Sahara from the
repository with its search and MCP extras, then run:

```bash
vhs docs/demo/sahara-demo.tape
```

The tape copies `fixtures/` into an isolated temporary home, builds a real Sahara
index, runs two semantic searches, demonstrates `sahara ask` falling back to
retrieved sources when the configured local LLM is unavailable, and runs the
Claude Desktop installer.

The first recording may take longer than the scripted sleep while the embedding
model downloads. Run `sahara index` once before recording or increase that sleep
on a slower connection.
