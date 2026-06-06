# Security

---

## Basic Index-Only Mode

With `storage_mode = "none"`, Sahara scans and indexes configured content roots on the
local computer. No storage backend is constructed and no file is uploaded by Sahara.
Semantic embeddings, extracted chunks, paths, and inventory state are stored in
`~/.sahara/state.db`.

`sahara search` stays local. `sahara ask` stays local when Ollama is used; when OpenAI
is selected, the retrieved snippets needed to answer the question are sent to OpenAI.
The read-only MCP server returns indexed snippets to the connected MCP client.

Storage encryption applies only after a local-drive, MinIO, or AWS backend is
configured. It does not encrypt the local source files or local semantic index.

---

## Offload Verification

`sahara offload` does not trust an object listing or ETag alone. Before removing a local
source, Sahara downloads the stored object to temporary storage, decrypts it when
necessary, and compares its plaintext SHA-256 with the local file and last sync record.
A missing object, stale local file, unavailable passphrase, download failure, or
checksum mismatch leaves the source file untouched.

`sahara fetch` also verifies the recovered plaintext checksum. A mismatched fetch is
removed rather than exposed as a valid restored file.

---

## Encryption model

Sahara encrypts files client-side before upload. The storage backend (S3, MinIO, local drive) receives only ciphertext.

**Algorithm:** AES-256-GCM (authenticated encryption — provides both confidentiality and integrity)

**Key derivation:** PBKDF2-HMAC-SHA256 with 600,000 iterations and a random 16-byte salt. The salt is stored alongside the ciphertext.

**Nonces:** A fresh 12-byte random nonce is generated per file. The nonce is prepended to the ciphertext.

**Wire format per encrypted file:**
```
[ 16-byte salt ][ 12-byte nonce ][ GCM ciphertext + 16-byte auth tag ]
```

The auth tag is appended by Python's `cryptography` library as part of the GCM output. Any bit-flip in the ciphertext causes decryption to fail with an `InvalidTag` error — the file is never silently corrupted.

---

## Passphrase handling

The passphrase is stored in the OS system keyring after `sahara encryption setup`:

| Platform | Keyring backend |
|----------|----------------|
| macOS | Keychain |
| Linux | libsecret (GNOME Keyring or KWallet) |
| Windows | Windows Credential Manager |

The passphrase is never written to disk in plaintext, never stored in `~/.sahara/config.toml`, and never logged.

**There is no recovery path.** If you lose your passphrase, your encrypted files cannot be decrypted. Back up your passphrase securely before enabling encryption.

---

## Threat model

**Sahara protects against:**

- An attacker who gains read access to your S3 bucket, MinIO instance, or remote drive — they see only ciphertext
- An attacker who intercepts traffic between your machine and the storage backend — the data is encrypted before leaving the machine
- Accidental data corruption — GCM authentication detects any modification to the ciphertext

**Sahara does NOT protect against:**

- An attacker with access to your local machine — the plaintext files are on disk, and the passphrase is in the keyring, which an attacker with local access can read
- An attacker who steals your passphrase — all files can be decrypted with the passphrase
- Traffic analysis — the size, number, and modification timestamps of files may leak information even though content is encrypted
- Metadata — filenames and directory structure are stored in the Sahara manifest in cleartext; only file *content* is encrypted

---

## IAM minimum required policy (S3 mode)

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
      "s3:RestoreObject",
      "s3:GetObjectAttributes"
    ],
    "Resource": [
      "arn:aws:s3:::YOUR-BUCKET-NAME",
      "arn:aws:s3:::YOUR-BUCKET-NAME/*"
    ]
  }]
}
```

Do not grant `s3:*` or `*` — use the minimum above. Consider creating a dedicated IAM user for Sahara rather than using your root credentials or an admin role.

---

## Reporting a vulnerability

If you discover a security vulnerability in Sahara, please report it through GitHub's private advisory system:

1. Go to the [Sahara repository](https://github.com/nidheesh-p/sahara)
2. Click **Security** → **Advisories** → **New draft security advisory**
3. Describe the vulnerability, steps to reproduce, and potential impact

Please do not open a public issue for security vulnerabilities — this gives users time to update before a fix is publicly announced.

We aim to acknowledge reports within 48 hours and publish a fix within 14 days for critical issues.
