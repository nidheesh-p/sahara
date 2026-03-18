"""Extended encryption tests covering delete_passphrase and passphrase wrappers."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import keyring.errors

from sahara.encryption import (
    EncryptionError,
    derive_key,
    generate_salt,
    encrypt_file,
    decrypt_file,
    get_passphrase,
    set_passphrase,
    delete_passphrase,
    encrypt_file_with_passphrase,
    decrypt_file_with_passphrase,
)


# ---------------------------------------------------------------------------
# delete_passphrase
# ---------------------------------------------------------------------------


class TestDeletePassphrase:
    def test_delete_passphrase_calls_keyring(self):
        with patch("keyring.delete_password") as mock_delete:
            delete_passphrase()
            mock_delete.assert_called_once_with("sahara", "encryption_passphrase")

    def test_delete_passphrase_swallows_password_delete_error(self):
        with patch("keyring.delete_password",
                   side_effect=keyring.errors.PasswordDeleteError("not found")):
            # Should not raise
            delete_passphrase()

    def test_delete_passphrase_raises_encryption_error_on_keyring_error(self):
        with patch("keyring.delete_password",
                   side_effect=keyring.errors.KeyringError("backend error")):
            with pytest.raises(EncryptionError, match="Cannot delete passphrase"):
                delete_passphrase()


# ---------------------------------------------------------------------------
# get_passphrase / set_passphrase error paths
# ---------------------------------------------------------------------------


class TestPassphraseKeyringErrors:
    def test_get_passphrase_raises_on_keyring_error(self):
        with patch("keyring.get_password",
                   side_effect=keyring.errors.KeyringError("read error")):
            with pytest.raises(EncryptionError, match="Cannot read passphrase"):
                get_passphrase()

    def test_set_passphrase_raises_on_keyring_error(self):
        with patch("keyring.set_password",
                   side_effect=keyring.errors.KeyringError("write error")):
            with pytest.raises(EncryptionError, match="Cannot write passphrase"):
                set_passphrase("mypassword")


# ---------------------------------------------------------------------------
# encrypt_file_with_passphrase
# ---------------------------------------------------------------------------


class TestEncryptFileWithPassphrase:
    def test_encrypt_with_passphrase_no_passphrase_raises(self, tmp_path: Path):
        src = tmp_path / "plain.txt"
        src.write_bytes(b"hello world")
        dst = tmp_path / "encrypted.saha"

        with patch("keyring.get_password", return_value=None):
            with pytest.raises(EncryptionError, match="No encryption passphrase"):
                encrypt_file_with_passphrase(src, dst)

    def test_encrypt_with_passphrase_success(self, tmp_path: Path):
        src = tmp_path / "plain.txt"
        src.write_bytes(b"hello world")
        dst = tmp_path / "encrypted.saha"

        with patch("keyring.get_password", return_value="test-passphrase"):
            sha256, salt = encrypt_file_with_passphrase(src, dst)
            assert isinstance(sha256, str)
            assert len(sha256) == 64  # SHA-256 hex
            assert isinstance(salt, bytes)
            assert len(salt) == 32
            assert dst.exists()

    def test_encrypt_then_decrypt_with_passphrase(self, tmp_path: Path):
        src = tmp_path / "plain.txt"
        content = b"test content for round-trip"
        src.write_bytes(content)
        enc = tmp_path / "encrypted.saha"
        dec = tmp_path / "decrypted.txt"

        with patch("keyring.get_password", return_value="round-trip-pass"):
            sha_enc, salt = encrypt_file_with_passphrase(src, enc)

        with patch("keyring.get_password", return_value="round-trip-pass"):
            sha_dec = decrypt_file_with_passphrase(enc, dec)

        assert sha_enc == sha_dec
        assert dec.read_bytes() == content


# ---------------------------------------------------------------------------
# decrypt_file_with_passphrase
# ---------------------------------------------------------------------------


class TestDecryptFileWithPassphrase:
    def test_decrypt_with_passphrase_no_passphrase_raises(self, tmp_path: Path):
        # Create a valid encrypted file first
        src = tmp_path / "plain.txt"
        src.write_bytes(b"hello")
        enc = tmp_path / "enc.saha"

        with patch("keyring.get_password", return_value="mypass"):
            encrypt_file_with_passphrase(src, enc)

        dst = tmp_path / "decrypted.txt"
        with patch("keyring.get_password", return_value=None):
            with pytest.raises(EncryptionError, match="No encryption passphrase"):
                decrypt_file_with_passphrase(enc, dst)

    def test_decrypt_with_passphrase_too_short_file(self, tmp_path: Path):
        short_file = tmp_path / "short.saha"
        short_file.write_bytes(b"\x00" * 10)  # Too short to be valid
        dst = tmp_path / "output.txt"

        with patch("keyring.get_password", return_value="pass"):
            with pytest.raises(EncryptionError, match="too short"):
                decrypt_file_with_passphrase(short_file, dst)

    def test_decrypt_with_passphrase_wrong_magic(self, tmp_path: Path):
        bad_file = tmp_path / "bad.saha"
        bad_file.write_bytes(b"BADM" + b"\x01" + b"\x00" * 32)
        dst = tmp_path / "output.txt"

        with patch("keyring.get_password", return_value="pass"):
            with pytest.raises(EncryptionError, match="Not a Sahara encrypted file"):
                decrypt_file_with_passphrase(bad_file, dst)


# ---------------------------------------------------------------------------
# decrypt_file error paths
# ---------------------------------------------------------------------------


class TestDecryptFileErrors:
    def test_decrypt_truncated_nonce(self, tmp_path: Path):
        """File with valid header but truncated nonce in first chunk."""
        salt = generate_salt()
        key = derive_key("test-pass", salt)

        truncated = tmp_path / "truncated.saha"
        # Write header then only partial nonce (less than 12 bytes)
        with open(truncated, "wb") as f:
            f.write(b"SAHA" + b"\x01" + salt)
            f.write(b"\x00" * 5)  # Partial nonce (only 5 bytes instead of 12)

        dst = tmp_path / "output.txt"
        with pytest.raises(EncryptionError, match="Unexpected EOF while reading chunk nonce"):
            decrypt_file(truncated, dst, key)

    def test_decrypt_truncated_blob_length(self, tmp_path: Path):
        """File with valid header and nonce but truncated blob length."""
        salt = generate_salt()
        key = derive_key("test-pass", salt)

        truncated = tmp_path / "truncated.saha"
        with open(truncated, "wb") as f:
            f.write(b"SAHA" + b"\x01" + salt)
            f.write(b"\x00" * 12)  # Full nonce
            f.write(b"\x00" * 2)   # Partial blob length (only 2 bytes instead of 4)

        dst = tmp_path / "output.txt"
        with pytest.raises(EncryptionError, match="Unexpected EOF while reading chunk length"):
            decrypt_file(truncated, dst, key)

    def test_decrypt_truncated_blob_data(self, tmp_path: Path):
        """File with valid header, nonce, length, but truncated blob data."""
        salt = generate_salt()
        key = derive_key("test-pass", salt)
        import struct

        truncated = tmp_path / "truncated.saha"
        with open(truncated, "wb") as f:
            f.write(b"SAHA" + b"\x01" + salt)
            f.write(b"\x00" * 12)  # Full nonce
            f.write(struct.pack(">I", 100))  # Says 100 bytes
            f.write(b"\x00" * 50)  # Only 50 bytes (truncated)

        dst = tmp_path / "output.txt"
        with pytest.raises(EncryptionError, match="Unexpected EOF"):
            decrypt_file(truncated, dst, key)

    def test_decrypt_gcm_authentication_failure(self, tmp_path: Path):
        """File with correct structure but corrupted ciphertext (wrong key)."""
        salt = generate_salt()
        key_correct = derive_key("correct-pass", salt)
        key_wrong = derive_key("wrong-pass", salt)

        src = tmp_path / "plain.txt"
        src.write_bytes(b"secret")
        enc = tmp_path / "enc.saha"
        encrypt_file(src, enc, key_correct, salt)

        dst = tmp_path / "output.txt"
        with pytest.raises(EncryptionError, match="GCM authentication failed"):
            decrypt_file(enc, dst, key_wrong)

    def test_decrypt_wrong_version(self, tmp_path: Path):
        """File with valid magic but wrong version byte."""
        salt = generate_salt()
        key = derive_key("test-pass", salt)

        bad_ver = tmp_path / "bad_ver.saha"
        with open(bad_ver, "wb") as f:
            f.write(b"SAHA" + b"\x02" + salt)  # Version 2 is unsupported

        dst = tmp_path / "output.txt"
        with pytest.raises(EncryptionError, match="Unsupported encryption version"):
            decrypt_file(bad_ver, dst, key)

    def test_encrypt_file_io_error(self, tmp_path: Path):
        """Test that IO errors are wrapped in EncryptionError."""
        salt = generate_salt()
        key = derive_key("test-pass", salt)

        # Non-existent source
        nonexistent = tmp_path / "doesnotexist.txt"
        dst = tmp_path / "output.saha"

        with pytest.raises(EncryptionError, match="Encryption I/O error"):
            encrypt_file(nonexistent, dst, key, salt)

    def test_decrypt_file_io_error(self, tmp_path: Path):
        """Test that IO errors are wrapped in EncryptionError."""
        key = b"\x00" * 32
        nonexistent = tmp_path / "doesnotexist.saha"
        dst = tmp_path / "output.txt"

        with pytest.raises(EncryptionError, match="Decryption I/O error"):
            decrypt_file(nonexistent, dst, key)
