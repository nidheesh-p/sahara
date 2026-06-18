# Mobile Capture API

Sahara mobile capture is a private API for trusted devices that need to save
knowledge into the desktop Sahara instance.

The API is local-first:

- it binds to `127.0.0.1` by default;
- every request requires a paired device bearer token;
- device tokens are stored only as hashes;
- capture requests are routed through `MemoryService`;
- requests cannot choose filesystem paths or storage sync behavior.

## Pair A Device

Create a one-time pairing payload:

```bash
sahara mobile pair "Nidheesh iPhone" --json
```

The JSON contains:

- `endpoint`: the URL the mobile device should call;
- `token`: a one-time bearer token shown only at pairing time;
- `scopes`: `memory:capture` and, only when requested, `memory:recall`;
- `device_id`: the revocable device identity.

For recall-capable clients:

```bash
sahara mobile pair "Shortcut" --scope memory:capture --scope memory:recall --json
```

Revoke a device at any time:

```bash
sahara mobile revoke "Nidheesh iPhone"
```

## Run The API

Loopback-only default:

```bash
sahara mobile serve
```

This starts `http://127.0.0.1:8765`.

To use a trusted private network address, pass the explicit private-network
option. Sahara refuses wildcard and public bind addresses.

```bash
sahara mobile serve --host 100.100.100.10 --allow-private-network
```

## Tailscale Serve

The recommended remote access pattern is a private device network such as
Tailscale. Keep Sahara bound to loopback, then expose that loopback service only
inside the tailnet:

```bash
sahara mobile serve
tailscale serve --bg 8765
```

Use the Tailscale HTTPS URL as the pairing `endpoint`:

```bash
sahara mobile pair "Nidheesh iPhone" \
  --endpoint "https://desktop-name.tailnet-name.ts.net" \
  --json
```

Do not expose the mobile API directly to the public internet.

## Capture Request

```http
POST /v1/memories
Authorization: Bearer <device-token>
Content-Type: application/json

{
  "text": "Vendor X uses net-30 terms",
  "source_type": "mobile",
  "source_url": "",
  "tags": ["vendor"],
  "idempotency_key": "device-generated-uuid"
}
```

Allowed fields are `text`, `title`, `source_type`, `source_url`, `source_id`,
`tags`, and `idempotency_key`.

The API rejects fields such as `path`, `relative_path`, `storage_prefix`, and
`sync_enabled`.

## Optional Recall

Recall requires a token with `memory:recall`.

```http
POST /v1/recall
Authorization: Bearer <device-token>
Content-Type: application/json

{
  "query": "vendor payment terms",
  "top_k": 5
}
```

## Audit

Show recent metadata-only events:

```bash
sahara mobile audit
```

Audit entries include timing, device identity, scope, outcome, source type, and
an idempotency-key hash. Captured memory text is not stored in the audit table.
