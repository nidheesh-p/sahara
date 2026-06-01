"""Tests for sahara.encryption."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from sahara.encryption import (
    EncryptionError,
    decrypt_file,
    derive_key,
    encrypt_file,
    generate_salt,
    get_passphrase,
    set_passphrase,
)

# ---------------------------------------------------------------------------
# generate_salt / derive_key
# ---------------------------------------------------------------------------


class TestGenerateSalt:
    def test_returns_32_bytes(self):
        salt = generate_salt()
        assert len(salt) == 32

    def test_returns_bytes(self):
        salt = generate_salt()
        assert isinstance(salt, bytes)

    def test_unique_each_call(self):
        salt1 = generate_salt()
        salt2 = generate_salt()
        assert salt1 != salt2


class TestDeriveKey:
    def test_returns_32_bytes(self):
        salt = generate_salt()
        key = derive_key("my-passphrase", salt)
        assert len(key) == 32
        assert isinstance(key, bytes)

    def test_deterministic(self):
        salt = generate_salt()
        key1 = derive_key("same-pass", salt)
        key2 = derive_key("same-pass", salt)
        assert key1 == key2

    def test_different_passwords_produce_different_keys(self):
        salt = generate_salt()
        key1 = derive_key("pass1", salt)
        key2 = derive_key("pass2", salt)
        assert key1 != key2

    def test_different_salts_produce_different_keys(self):
        key1 = derive_key("same-pass", generate_salt())
        key2 = derive_key("same-pass", generate_salt())
        assert key1 != key2

    def test_wrong_salt_length_raises(self):
        with pytest.raises(EncryptionError, match="Salt must be exactly"):
            derive_key("passphrase", b"tooshort")

    def test_empty_password(self):
        salt = generate_salt()
        key = derive_key("", salt)
        assert len(key) == 32


# ---------------------------------------------------------------------------
# encrypt_file / decrypt_file round-trip
# ---------------------------------------------------------------------------


class TestEncryptDecrypt:
    def test_roundtrip_small_file(self, tmp_path: Path, encryption_key_and_salt):
        key, salt = encryption_key_and_salt
        content = b"Hello, this is a test file content!"
        src = tmp_path / "plaintext.txt"
        src.write_bytes(content)

        enc = tmp_path / "encrypted.saha"
        dec = tmp_path / "decrypted.txt"

        sha_enc = encrypt_file(src, enc, key, salt)
        sha_dec = decrypt_file(enc, dec, key)

        assert sha_enc == sha_dec
        assert dec.read_bytes() == content

    def test_header_magic(self, tmp_path: Path, encryption_key_and_salt):
        key, salt = encryption_key_and_salt
        src = tmp_path / "f.txt"
        src.write_bytes(b"test")
        enc = tmp_path / "enc.saha"

        encrypt_file(src, enc, key, salt)
        header = enc.read_bytes()[:5]
        assert header[:4] == b"SAHA"
        assert header[4:5] == b"\x01"

    def test_salt_embedded_in_header(self, tmp_path: Path, encryption_key_and_salt):
        key, salt = encryption_key_and_salt
        src = tmp_path / "f.txt"
        src.write_bytes(b"data")
        enc = tmp_path / "enc.saha"

        encrypt_file(src, enc, key, salt)
        file_bytes = enc.read_bytes()
        embedded_salt = file_bytes[5:37]
        assert embedded_salt == salt

    def test_empty_file_roundtrip(self, tmp_path: Path, encryption_key_and_salt):
        key, salt = encryption_key_and_salt
        src = tmp_path / "empty.txt"
        src.write_bytes(b"")

        enc = tmp_path / "enc.saha"
        dec = tmp_path / "dec.txt"

        sha_enc = encrypt_file(src, enc, key, salt)
        sha_dec = decrypt_file(enc, dec, key)

        assert sha_enc == sha_dec
        assert dec.read_bytes() == b""

    def test_encrypt_returns_sha256_hex(self, tmp_path: Path, encryption_key_and_salt):
        import hashlib
        key, salt = encryption_key_and_salt
        content = b"compute sha256 of me"
        src = tmp_path / "f.txt"
        src.write_bytes(content)
        enc = tmp_path / "enc.saha"

        sha = encrypt_file(src, enc, key, salt)
        expected = hashlib.sha256(content).hexdigest()
        assert sha == expected

    def test_chunk_size_1mb(self, tmp_path: Path, encryption_key_and_salt):
        """Test with small chunk size to exercise chunking code."""
        key, salt = encryption_key_and_salt
        content = b"x" * (2 * 1024 * 1024)  # 2 MB
        src = tmp_path / "large.txt"
        src.write_bytes(content)

        enc = tmp_path / "enc.saha"
        dec = tmp_path / "dec.txt"

        sha_enc = encrypt_file(src, enc, key, salt, chunk_mb=1)
        sha_dec = decrypt_file(enc, dec, key)

        assert sha_enc == sha_dec
        assert dec.read_bytes() == content

    def test_small_file_smaller_than_chunk(self, tmp_path: Path, encryption_key_and_salt):
        """File smaller than chunk size (1 chunk used)."""
        key, salt = encryption_key_and_salt
        content = b"tiny" * 100
        src = tmp_path / "tiny.txt"
        src.write_bytes(content)

        enc = tmp_path / "enc.saha"
        dec = tmp_path / "dec.txt"

        sha_enc = encrypt_file(src, enc, key, salt, chunk_mb=10)
        sha_dec = decrypt_file(enc, dec, key)

        assert sha_enc == sha_dec
        assert dec.read_bytes() == content

    def test_large_file_10mb(self, tmp_path: Path, encryption_key_and_salt):
        """10 MB file encrypt/decrypt."""
        key, salt = encryption_key_and_salt
        content = os.urandom(10 * 1024 * 1024)
        src = tmp_path / "big.bin"
        src.write_bytes(content)

        enc = tmp_path / "enc.saha"
        dec = tmp_path / "dec.bin"

        sha_enc = encrypt_file(src, enc, key, salt, chunk_mb=4)
        sha_dec = decrypt_file(enc, dec, key)

        assert sha_enc == sha_dec
        assert dec.read_bytes() == content

    def test_wrong_key_raises_encryption_error(self, tmp_path: Path, encryption_key_and_salt):
        key, salt = encryption_key_and_salt
        src = tmp_path / "f.txt"
        src.write_bytes(b"secret data")
        enc = tmp_path / "enc.saha"
        encrypt_file(src, enc, key, salt)

        wrong_key = derive_key("wrong-passphrase", generate_salt())
        dec = tmp_path / "dec.txt"
        with pytest.raises(EncryptionError, match="GCM authentication failed"):
            decrypt_file(enc, dec, wrong_key)

    def test_corrupted_header_raises_encryption_error(self, tmp_path: Path, encryption_key_and_salt):
        key, salt = encryption_key_and_salt
        src = tmp_path / "f.txt"
        src.write_bytes(b"data")
        enc = tmp_path / "enc.saha"
        encrypt_file(src, enc, key, salt)

        # Corrupt magic bytes
        data = bytearray(enc.read_bytes())
        data[0] = 0xFF
        enc.write_bytes(bytes(data))

        dec = tmp_path / "dec.txt"
        with pytest.raises(EncryptionError, match="Invalid magic bytes"):
            decrypt_file(enc, dec, key)

    def test_truncated_file_raises_encryption_error(self, tmp_path: Path, encryption_key_and_salt):
        key, salt = encryption_key_and_salt
        # Create a truncated file (only magic + version, incomplete header)
        enc = tmp_path / "truncated.saha"
        enc.write_bytes(b"SAHA\x01")  # Only 5 bytes, header needs 37

        dec = tmp_path / "dec.txt"
        with pytest.raises(EncryptionError, match="too short"):
            decrypt_file(enc, dec, key)

    def test_unsupported_version_raises_encryption_error(self, tmp_path: Path, encryption_key_and_salt):
        key, salt = encryption_key_and_salt
        src = tmp_path / "f.txt"
        src.write_bytes(b"data")
        enc = tmp_path / "enc.saha"
        encrypt_file(src, enc, key, salt)

        # Corrupt version byte
        data = bytearray(enc.read_bytes())
        data[4] = 0x99
        enc.write_bytes(bytes(data))

        dec = tmp_path / "dec.txt"
        with pytest.raises(EncryptionError, match="Unsupported encryption version"):
            decrypt_file(enc, dec, key)

    def test_truncated_mid_chunk_raises_encryption_error(self, tmp_path: Path, encryption_key_and_salt):
        key, salt = encryption_key_and_salt
        src = tmp_path / "f.txt"
        src.write_bytes(b"some data here")
        enc = tmp_path / "enc.saha"
        encrypt_file(src, enc, key, salt)

        # Truncate after header
        data = enc.read_bytes()[:38]  # Only keep header + 1 extra byte
        enc.write_bytes(data)

        dec = tmp_path / "dec.txt"
        with pytest.raises(EncryptionError):
            decrypt_file(enc, dec, key)

    def test_encrypt_creates_parent_dirs(self, tmp_path: Path, encryption_key_and_salt):
        key, salt = encryption_key_and_salt
        src = tmp_path / "f.txt"
        src.write_bytes(b"content")

        enc = tmp_path / "nested" / "dir" / "enc.saha"
        encrypt_file(src, enc, key, salt)
        assert enc.exists()


# ---------------------------------------------------------------------------
# Keyring helpers
# ---------------------------------------------------------------------------


class TestKeyrings:
    def test_get_passphrase_calls_keyring(self):
        with patch("sahara.encryption.keyring.get_password") as mock_get:
            mock_get.return_value = "my-secret"
            result = get_passphrase()
            assert result == "my-secret"
            mock_get.assert_called_once_with("sahara", "encryption_passphrase")

    def test_get_passphrase_returns_none_when_not_set(self):
        with patch("sahara.encryption.keyring.get_password") as mock_get:
            mock_get.return_value = None
            result = get_passphrase()
            assert result is None

    def test_get_passphrase_raises_encryption_error_on_keyring_error(self):
        import keyring.errors
        with patch("sahara.encryption.keyring.get_password") as mock_get:
            mock_get.side_effect = keyring.errors.KeyringError("backend unavailable")
            with pytest.raises(EncryptionError, match="Cannot read passphrase"):
                get_passphrase()

    def test_set_passphrase_calls_keyring(self):
        with patch("sahara.encryption.keyring.set_password") as mock_set:
            set_passphrase("new-pass")
            mock_set.assert_called_once_with("sahara", "encryption_passphrase", "new-pass")

    def test_set_passphrase_raises_encryption_error_on_keyring_error(self):
        import keyring.errors
        with patch("sahara.encryption.keyring.set_password") as mock_set:
            mock_set.side_effect = keyring.errors.KeyringError("no backend")
            with pytest.raises(EncryptionError, match="Cannot write passphrase"):
                set_passphrase("pass")
