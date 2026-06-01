# Sahara Local Storage: MinIO + Tailscale

## Goal

Allow Sahara to sync files to locally connected hard drives (on a home computer) instead of AWS S3, accessible from multiple devices (Mac, iPhone) over the internet via Tailscale VPN.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    Tailscale Network                      │
│                                                          │
│   [Mac Laptop]          [iPhone]                         │
│   100.x.x.10            100.x.x.20                      │
│       │  Sahara CLI         │  Owlfiles / PhotoSync      │
│       │                     │                            │
│       └──────────┬──────────┘                            │
│                  │ WireGuard (encrypted)                 │
│                  ▼                                       │
│           [Home Computer]  100.x.x.1                    │
│           MinIO :9000  (:9001 web UI)                    │
│                  │                                       │
│          macOS RAID 1 Mirror                             │
│         /Volumes/SaharaRAID                              │
│        ┌────────┴────────┐                               │
│     [Drive 1]        [Drive 2]                           │
└──────────────────────────────────────────────────────────┘
```

---

## Infrastructure Setup (one-time, no Sahara code)

### 1. Two drives → one mirrored volume

Use macOS Disk Utility → RAID → Mirror Set from the two drives.
Result: single volume `/Volumes/SaharaRAID` with automatic redundancy.
If one drive fails, the other keeps working with no data loss.

> Alternative: keep drives separate and accept no automatic redundancy
> (MinIO with only 2 drives cannot do erasure coding — needs 4+).

### 2. MinIO on home computer

```bash
brew install minio

# Set strong credentials
export MINIO_ROOT_USER=sahara-admin
export MINIO_ROOT_PASSWORD=<strong-password>

# Start pointing at mirrored drive
minio server /Volumes/SaharaRAID --console-address :9001

# Or as a persistent service
brew services start minio
# configure env vars in /opt/homebrew/etc/minio/config
```

Open `http://localhost:9001` → create bucket named `sahara`.

### 3. Tailscale on home computer

```bash
brew install --cask tailscale
```

Sign in → note the assigned `100.x.x.x` IP. All other devices use this to reach MinIO.

---

## Sahara Code Changes

### Files changed

| File | Change |
|---|---|
| `src/sahara/config.py` | Add `endpoint_url: str = ""` field |
| `src/sahara/storage/s3_client.py` | Pass `endpoint_url` + path-style addressing to boto3 |
| `src/sahara/cli.py` | Add MinIO option to `sahara init` wizard |

### `config.py` — new field

```python
# after aws_secret_access_key:
endpoint_url: str = ""   # e.g. http://100.x.x.1:9000 for MinIO; empty = AWS
```

When `endpoint_url` is set, Sahara treats itself as running in "local storage mode":
- `default_storage_class` defaults to `STANDARD` (MinIO ignores storage classes)
- Archive/restore commands are disabled with a clear message

### `s3_client.py` — endpoint + path-style

In `S3Client.__init__`:

```python
if config.endpoint_url:
    client_kwargs["endpoint_url"] = config.endpoint_url
    client_kwargs["config"] = BotoConfig(
        retries={"max_attempts": 1, "mode": "legacy"},
        max_pool_connections=config.max_workers + 4,
        s3={"addressing_style": "path"},   # required for MinIO
    )
self._s3 = session.client("s3", **client_kwargs)
```

Path-style addressing is required because MinIO does not support virtual-hosted-style
bucket URLs (`bucket.hostname`) — it uses `hostname/bucket` instead.

### `cli.py` — init wizard

New prompt in `sahara init`:

```
Storage backend:
  [1] AWS S3 (default)
  [2] MinIO / self-hosted

If MinIO:
  Endpoint URL: http://100.x.x.1:9000
  Access Key ID: <MinIO root user>
  Secret Access Key: <MinIO root password>
  Bucket name: sahara
```

When MinIO is selected, skip AWS region validation and default storage class to `STANDARD`.

---

## Other Computers

```bash
# 1. Install Tailscale → joins tailnet, gets 100.x.x.x IP
# 2. pip install sahara
# 3. sahara init → choose MinIO, enter endpoint http://100.x.x.1:9000
# 4. sahara sync / sahara daemon start
```

Tailscale auto-detects when devices are on the same LAN and uses the direct path
(full gigabit speed). Remote access goes through the WireGuard tunnel.

---

## Phone Sync

Install Tailscale on iPhone/Android, then use an S3-compatible app:

| App | Platform | Auto-backup | Notes |
|---|---|---|---|
| PhotoSync | iOS + Android | Yes (camera roll) | Best for photo backup |
| Owlfiles | iOS + Android | Yes | General file sync |
| Cyberduck (mobile) | iOS | Manual | Good for occasional transfers |

App configuration:
```
Endpoint:   http://100.x.x.1:9000
Access Key: <MinIO root user>
Secret Key: <MinIO root password>
Bucket:     sahara
Region:     us-east-1   (MinIO accepts any value)
Path style: enabled
```

Phone-uploaded files appear as `remote_new` in Sahara's next sync on other devices.

---

## Feature Compatibility

| Feature | Works with MinIO? | Notes |
|---|---|---|
| sync, push, pull | ✅ | Unchanged |
| daemon + file watcher | ✅ | Unchanged |
| AES-256-GCM encryption | ✅ | Unchanged |
| Conflict resolution | ✅ | Unchanged |
| Rename detection | ✅ | Unchanged |
| `sahara archive` (Glacier) | ❌ | No tiered storage on MinIO |
| `sahara restore` | ❌ | No tiered storage on MinIO |
| `sahara usage` (cost) | ⚠️ | Shows storage size; cost = $0 |
| Conditional PUT (manifest) | ✅ | MinIO supports `If-Match` since early 2024 |

---

## Implementation Tasks

1. Add `endpoint_url` field to `SaharaConfig`
2. Pass `endpoint_url` + `addressing_style: path` to boto3 in `S3Client.__init__`
3. Add MinIO backend option to `sahara init` wizard
4. Guard archive/restore CLI commands when `endpoint_url` is set (show clear error)
5. Default `default_storage_class` to `STANDARD` when `endpoint_url` is set
6. Update `sahara doctor` to test MinIO connectivity correctly (skip region check)
