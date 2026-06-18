# Siri, iOS Share Sheet, And WhatsApp Capture

Sahara's first mobile capture experience uses Apple Shortcuts on top of the
private mobile capture API.

The packaged Shortcut artifacts are versioned blueprints:

- `Remember in Sahara`
- `Recall from Sahara`

Export them with:

```bash
sahara mobile shortcuts export ./sahara-shortcuts
```

Inspect available artifacts:

```bash
sahara mobile shortcuts list
```

## 1. Pair The Device

Create a capture token:

```bash
sahara mobile pair "Nidheesh iPhone" --json
```

For optional recall, include the recall scope:

```bash
sahara mobile pair "Nidheesh iPhone" \
  --scope memory:capture \
  --scope memory:recall \
  --json
```

The token is displayed once. Store it only inside the Shortcut configuration or
another trusted local secret store.

## 2. Start The Private API

On the desktop Sahara machine:

```bash
sahara mobile serve
```

For iPhone access, use a private network tunnel such as Tailscale Serve as
documented in `docs/mobile-capture-api.md`. Do not expose the API publicly.

## 3. Build "Remember in Sahara"

Use the exported `remember-in-sahara.json` blueprint while building the Shortcut.
The Shortcut should:

1. Load the paired endpoint and token.
2. Read explicit iOS share-sheet input.
3. Fall back to clipboard text when share input is empty.
4. Ask for dictated input when launched from Siri without input.
5. Ask for optional tags and a source note.
6. Generate one UUID idempotency key before posting.
7. POST JSON to `${endpoint}/v1/memories`.
8. Show the saved/already-saved/index-pending status.
9. Speak only the status, not the captured memory text.

Launch phrase:

```text
Siri, Remember in Sahara
```

## Share Sheet Behavior

The Shortcut accepts only content that iOS passes explicitly:

- selected text;
- shared URLs;
- text copied to the clipboard;
- user dictation.

For URLs, preserve the URL as `source_url` and let the user add a note or tags.
Sahara does not scrape the source app.

## WhatsApp

WhatsApp capture uses explicit user action only:

```text
Select or copy a message -> Share -> Remember in Sahara
```

If WhatsApp does not pass the selected message through the share sheet, copy the
message and run `Remember in Sahara`; the Shortcut will use its clipboard
fallback.

Sahara does not infer sender names, chat names, timestamps, or message context
unless the user includes that text manually.

## Recall Shortcut

`Recall from Sahara` is optional and requires `memory:recall` scope.

It should display results only. Do not add a `Speak Text` action for recall
results because memories may contain sensitive content.

## Revocation

List devices:

```bash
sahara mobile devices
```

Revoke a device:

```bash
sahara mobile revoke "Nidheesh iPhone"
```

After revocation, the Shortcut token stops working. Pair the device again to
generate a new token.

## Troubleshooting

If capture fails:

- confirm `sahara mobile serve` is running;
- confirm the Shortcut endpoint matches the paired endpoint;
- confirm the token was copied without spaces or line breaks;
- confirm the device can reach the private endpoint;
- run `sahara mobile audit` to see rejected or failed requests;
- reuse the same idempotency key only for retrying the same capture.
