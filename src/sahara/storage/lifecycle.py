"""Explicit local/offloaded storage lifecycle operations."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sahara.config import SaharaConfig
from sahara.library import ContentRoot, ensure_content_roots
from sahara.storage.state_db import StateDB
from sahara.utils.hash import compute_sha256

__all__ = ["StorageLifecycle", "StoragePath"]


@dataclass(frozen=True)
class StoragePath:
    """A file resolved against a registered content root."""

    root: ContentRoot
    relative_path: str
    local_path: Path


class StorageLifecycle:
    """Safely offload and fetch indexed files through a storage backend."""

    def __init__(
        self,
        config: SaharaConfig,
        db: StateDB,
        backend: Any,
    ) -> None:
        self._config = config
        self._db = db
        self._backend = backend

    def resolve(self, value: str) -> StoragePath:
        roots = ensure_content_roots(self._config, self._db)
        candidate = Path(value).expanduser()

        if candidate.is_absolute():
            absolute = candidate.resolve()
            for root in roots:
                try:
                    relative = absolute.relative_to(root.local_path)
                except ValueError:
                    continue
                return StoragePath(root, relative.as_posix(), absolute)
            raise ValueError(f"{value} is outside Sahara's registered content roots")

        primary = next((root for root in roots if root.is_primary), None)
        if primary is None:
            raise ValueError("Sahara has no primary content root")
        return StoragePath(
            primary,
            candidate.as_posix(),
            primary.local_path / candidate,
        )

    def offload(self, value: str) -> StoragePath:
        item = self.resolve(value)
        if not item.root.sync_enabled:
            raise ValueError(
                "This content root is index-only. Enable sync and run `sahara sync` "
                "before offloading."
            )
        if not item.local_path.is_file():
            raise ValueError(f"Local file not found: {item.local_path}")
        if self._db.get_chunk_content_hash(
            item.root.storage_prefix, item.relative_path
        ) is None:
            raise ValueError(
                "The file is not searchable yet. Run `sahara index` before offloading."
            )

        record = self._db.get_file(
            item.relative_path, s3_prefix=item.root.storage_prefix
        )
        if record is None or record.is_deleted:
            raise ValueError(
                "No verified storage record exists. Run `sahara sync` before offloading."
            )

        local_hash = compute_sha256(item.local_path)
        if local_hash != record.sha256_checksum:
            raise ValueError(
                "The local file changed after its last sync. Run `sahara sync` again."
            )

        remote_hash = self._download_for_verification(item)
        if remote_hash != local_hash:
            raise ValueError(
                "Stored copy checksum does not match the local file; source was not removed."
            )

        self._db.set_storage_lifecycle(
            item.root.storage_prefix,
            item.relative_path,
            local_state="offloaded",
            remote_state="present",
            index_status="offloaded",
            reason="intentional_offload",
        )
        try:
            item.local_path.unlink()
        except OSError:
            self._db.set_storage_lifecycle(
                item.root.storage_prefix,
                item.relative_path,
                local_state="present",
                remote_state="present",
                index_status="indexed",
                reason="offload_remove_failed",
            )
            raise
        return item

    def fetch(self, value: str) -> StoragePath:
        item = self.resolve(value)
        if not item.root.sync_enabled:
            raise ValueError("This content root is not enabled for storage sync.")
        record = self._db.get_file(
            item.relative_path, s3_prefix=item.root.storage_prefix
        )
        if record is None or record.is_deleted:
            raise ValueError(f"No stored copy is tracked for: {item.relative_path}")
        if item.local_path.exists():
            raise ValueError(f"Local file already exists: {item.local_path}")

        key = self._storage_key(item)
        sha256 = self._backend.download_file(
            key,
            item.local_path,
            decrypt_fn=self._decrypt_fn() if self._config.encryption_enabled else None,
        )
        if sha256 != record.sha256_checksum:
            item.local_path.unlink(missing_ok=True)
            raise ValueError(
                "Fetched copy failed checksum verification and was removed."
            )

        self._db.set_storage_lifecycle(
            item.root.storage_prefix,
            item.relative_path,
            local_state="present",
            remote_state="present",
            index_status="indexed",
            reason="fetched",
        )
        return item

    def _download_for_verification(self, item: StoragePath) -> str:
        suffix = item.local_path.suffix or ".tmp"
        with tempfile.TemporaryDirectory(prefix="sahara-offload-") as temp_dir:
            destination = Path(temp_dir) / f"verify{suffix}"
            return self._backend.download_file(
                self._storage_key(item),
                destination,
                decrypt_fn=(
                    self._decrypt_fn()
                    if self._config.encryption_enabled
                    else None
                ),
            )

    def _storage_key(self, item: StoragePath) -> str:
        parts = []
        if self._config.prefix:
            parts.append(self._config.prefix.strip("/"))
        if item.root.storage_prefix:
            parts.append(item.root.storage_prefix.strip("/"))
        parts.append(item.relative_path)
        return "/".join(parts)

    @staticmethod
    def _decrypt_fn():
        from sahara.utils.encryption import (
            _HEADER_LEN,
            _MAGIC,
            _SALT_LEN,
            EncryptionError,
            decrypt_file,
            derive_key,
            get_passphrase,
        )

        passphrase = get_passphrase()
        if not passphrase:
            raise EncryptionError(
                "No passphrase available. Run `sahara encryption setup`."
            )

        def decrypt(src: Path, dst: Path) -> str:
            with open(src, "rb") as handle:
                header = handle.read(_HEADER_LEN)
            if header[:4] != _MAGIC:
                raise EncryptionError("Not a Sahara encrypted file.")
            salt = header[5 : 5 + _SALT_LEN]
            return decrypt_file(src, dst, derive_key(passphrase, salt))

        return decrypt
