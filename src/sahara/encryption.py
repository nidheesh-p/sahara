"""AES-256-GCM chunked streaming encryption for Sahara.

File format
-----------
Header  : b'SAHA' (4 bytes) + b'\\x01' (1 byte version) + salt (32 bytes)
          = 37 bytes total

Per chunk (repeated until EOF):
    nonce    : 12 random bytes
    blob_len : 4 bytes, uint32 big-endian  (length of the encrypted blob)
    blob     : PyCA AESGCM.encrypt() output — ciphertext + 16-byte GCM tag
               as one inseparable unit

Terminator chunk:
    nonce    : 12 bytes (zeros or random — ignored by decoder)
    blob_len : 0x00000000  (4 bytes)

Plaintext SHA-256 is computed BEFORE encryption and returned by both
encrypt_file() and decrypt_file() for integrity verification.
"""

from __future__ import annotations

import hashlib
import os
import struct
from pathlib import Path
from typing import Callable, Optional

import keyring
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "EncryptionError",
    "derive_key",
    "generate_salt",
    "encrypt_file",
    "decrypt_file",
    "get_passphrase",
    "set_passphrase",
]

_MAGIC = b"SAHA"
_VERSION = b"\x01"
_SALT_LEN = 32
_NONCE_LEN = 12
_BLOB_LEN_SIZE = 4  # uint32 BE
_HEADER_LEN = len(_MAGIC) + len(_VERSION) + _SALT_LEN  # 37

_PBKDF2_ITERATIONS = 600_000
_KEY_LEN = 32  # 256 bits

_KEYRING_SERVICE = "sahara"
_KEYRING_USERNAME = "encryption_passphrase"

_DEFAULT_CHUNK_MB = 4


class EncryptionError(Exception):
    """Raised for encryption/decryption failures."""


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


def derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a 256-bit AES key from *passphrase* using PBKDF2-HMAC-SHA256.

    Uses 600 000 iterations as recommended by OWASP (2023).
    """
    if len(salt) != _SALT_LEN:
        raise EncryptionError(
            f"Salt must be exactly {_SALT_LEN} bytes, got {len(salt)}."
        )
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_LEN,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def generate_salt() -> bytes:
    """Return 32 cryptographically random bytes."""
    return os.urandom(_SALT_LEN)


# ---------------------------------------------------------------------------
# Passphrase keyring helpers
# ---------------------------------------------------------------------------


def get_passphrase() -> Optional[str]:
    """Retrieve the stored passphrase from the system keyring."""
    try:
        return keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
    except keyring.errors.KeyringError as exc:
        raise EncryptionError(f"Cannot read passphrase from keyring: {exc}") from exc


def set_passphrase(passphrase: str) -> None:
    """Store *passphrase* in the system keyring."""
    try:
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, passphrase)
    except keyring.errors.KeyringError as exc:
        raise EncryptionError(f"Cannot write passphrase to keyring: {exc}") from exc


def delete_passphrase() -> None:
    """Remove the passphrase from the keyring."""
    try:
        keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
    except keyring.errors.PasswordDeleteError:
        pass  # Already absent
    except keyring.errors.KeyringError as exc:
        raise EncryptionError(f"Cannot delete passphrase from keyring: {exc}") from exc


# ---------------------------------------------------------------------------
# Core encrypt / decrypt
# ---------------------------------------------------------------------------


def encrypt_file(
    src_path: Path,
    dst_path: Path,
    key: bytes,
    salt: bytes,
    chunk_mb: int = _DEFAULT_CHUNK_MB,
) -> str:
    """Encrypt *src_path* → *dst_path* using AES-256-GCM chunked streaming.

    Returns the hex-encoded SHA-256 of the plaintext (computed before encryption).
    The *salt* is embedded in the file header so the caller must persist it
    alongside the derived key or store it in the file (as we do here).
    """
    chunk_size = chunk_mb * 1024 * 1024
    aesgcm = AESGCM(key)
    sha256 = hashlib.sha256()

    dst_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with (
            open(src_path, "rb") as src,
            open(dst_path, "wb") as dst,
        ):
            # Write header
            dst.write(_MAGIC + _VERSION + salt)

            while True:
                plaintext = src.read(chunk_size)
                if not plaintext:
                    break

                sha256.update(plaintext)

                nonce = os.urandom(_NONCE_LEN)
                # blob = ciphertext + 16-byte GCM tag as one unit
                blob = aesgcm.encrypt(nonce, plaintext, None)
                blob_len = struct.pack(">I", len(blob))

                dst.write(nonce + blob_len + blob)

            # Terminator chunk
            term_nonce = os.urandom(_NONCE_LEN)
            dst.write(term_nonce + struct.pack(">I", 0))

    except OSError as exc:
        raise EncryptionError(f"Encryption I/O error: {exc}") from exc

    return sha256.hexdigest()


def decrypt_file(
    src_path: Path,
    dst_path: Path,
    key: bytes,
) -> str:
    """Decrypt a Sahara-encrypted file *src_path* → *dst_path*.

    Returns the hex-encoded SHA-256 of the recovered plaintext.
    Raises EncryptionError on any integrity failure.
    """
    aesgcm_cache: dict[bytes, AESGCM] = {}
    sha256 = hashlib.sha256()

    dst_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with (
            open(src_path, "rb") as src,
            open(dst_path, "wb") as dst,
        ):
            # Read and validate header
            header = src.read(_HEADER_LEN)
            if len(header) < _HEADER_LEN:
                raise EncryptionError("File is too short to be a valid Sahara archive.")
            magic = header[:4]
            version = header[4:5]
            # salt stored in header for reference but key is passed in
            # salt = header[5:37]  # noqa: F841

            if magic != _MAGIC:
                raise EncryptionError(
                    f"Invalid magic bytes {magic!r}; not a Sahara encrypted file."
                )
            if version != _VERSION:
                raise EncryptionError(
                    f"Unsupported encryption version {version!r}."
                )

            # Lazily construct AESGCM (key is fixed for whole file)
            aesgcm = AESGCM(key)

            while True:
                nonce = src.read(_NONCE_LEN)
                if len(nonce) < _NONCE_LEN:
                    raise EncryptionError("Unexpected EOF while reading chunk nonce.")

                blob_len_bytes = src.read(_BLOB_LEN_SIZE)
                if len(blob_len_bytes) < _BLOB_LEN_SIZE:
                    raise EncryptionError("Unexpected EOF while reading chunk length.")

                blob_len = struct.unpack(">I", blob_len_bytes)[0]

                if blob_len == 0:
                    # Terminator chunk
                    break

                blob = src.read(blob_len)
                if len(blob) < blob_len:
                    raise EncryptionError(
                        f"Unexpected EOF: expected {blob_len} bytes, got {len(blob)}."
                    )

                try:
                    plaintext = aesgcm.decrypt(nonce, blob, None)
                except Exception as exc:
                    raise EncryptionError(
                        "GCM authentication failed — data is corrupt or key is wrong."
                    ) from exc

                sha256.update(plaintext)
                dst.write(plaintext)

    except OSError as exc:
        raise EncryptionError(f"Decryption I/O error: {exc}") from exc

    return sha256.hexdigest()


# ---------------------------------------------------------------------------
# Convenience wrappers that resolve the passphrase from keyring
# ---------------------------------------------------------------------------


def encrypt_file_with_passphrase(
    src_path: Path,
    dst_path: Path,
    chunk_mb: int = _DEFAULT_CHUNK_MB,
) -> tuple[str, bytes]:
    """Encrypt using the passphrase stored in keyring.

    Returns (plaintext_sha256_hex, salt).
    """
    passphrase = get_passphrase()
    if not passphrase:
        raise EncryptionError(
            "No encryption passphrase found. Run `sahara encryption setup` first."
        )
    salt = generate_salt()
    key = derive_key(passphrase, salt)
    sha256 = encrypt_file(src_path, dst_path, key, salt, chunk_mb)
    return sha256, salt


def decrypt_file_with_passphrase(
    src_path: Path,
    dst_path: Path,
) -> str:
    """Decrypt using the passphrase stored in keyring.

    The salt is read from the file header; key is re-derived on the fly.
    Returns plaintext_sha256_hex.
    """
    passphrase = get_passphrase()
    if not passphrase:
        raise EncryptionError(
            "No encryption passphrase found. Run `sahara encryption setup` first."
        )

    # Read salt from header without disturbing the main file handle
    with open(src_path, "rb") as fh:
        header = fh.read(_HEADER_LEN)

    if len(header) < _HEADER_LEN:
        raise EncryptionError("File is too short to be a valid Sahara archive.")
    if header[:4] != _MAGIC:
        raise EncryptionError("Not a Sahara encrypted file.")

    salt = header[5:37]
    key = derive_key(passphrase, salt)
    return decrypt_file(src_path, dst_path, key)
