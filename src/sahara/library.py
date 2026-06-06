"""Local content-root registration and indexing services."""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass
from pathlib import Path

from sahara.config import SaharaConfig
from sahara.search.search_engine import SearchEngine
from sahara.storage.state_db import StateDB
from sahara.sync.ignore_rules import IgnoreRules

__all__ = [
    "ContentRoot",
    "IndexRunResult",
    "IndexingService",
    "ensure_content_roots",
]


@dataclass(frozen=True)
class ContentRoot:
    """A local folder Sahara indexes and may optionally sync."""

    local_path: Path
    storage_prefix: str
    is_primary: bool
    sync_enabled: bool


@dataclass
class IndexRunResult:
    """Aggregate result from scanning and indexing content roots."""

    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    missing: int = 0
    unsupported: int = 0
    no_text: int = 0
    unchanged: int = 0


def ensure_content_roots(config: SaharaConfig, db: StateDB) -> list[ContentRoot]:
    """Migrate configured folders into the canonical content-root registry."""
    if config.sync_folder:
        existing_primary = db.get_content_root(config.sync_folder)
        db.upsert_content_root(
            config.sync_folder,
            "",
            is_primary=True,
            sync_enabled=(
                config.has_storage_backend if existing_primary is None else False
            ),
        )

    for target in db.list_sync_targets():
        db.upsert_content_root(
            target["local_path"],
            target["s3_prefix"],
            sync_enabled=True,
        )

    return [
        ContentRoot(
            local_path=Path(row["local_path"]),
            storage_prefix=row["storage_prefix"],
            is_primary=row["is_primary"],
            sync_enabled=row["sync_enabled"],
        )
        for row in db.list_content_roots()
    ]


class IndexingService:
    """Walk content roots, maintain inventory, and update semantic search."""

    def __init__(self, config: SaharaConfig, db: StateDB) -> None:
        self._config = config
        self._db = db
        self._search = SearchEngine(db)

    def roots(self) -> list[ContentRoot]:
        return ensure_content_roots(self._config, self._db)

    def index(
        self,
        *,
        root_path: Path | None = None,
        force: bool = False,
    ) -> IndexRunResult:
        roots = self.roots()
        if root_path is not None:
            resolved = root_path.expanduser().resolve()
            roots = [root for root in roots if root.local_path == resolved]
            if not roots:
                raise ValueError(f"{root_path} is not a registered content root")

        result = IndexRunResult()
        for root in roots:
            self._index_root(root, result, force=force)
        return result

    def _index_root(
        self,
        root: ContentRoot,
        result: IndexRunResult,
        *,
        force: bool,
    ) -> None:
        if not root.local_path.is_dir():
            result.failed += 1
            return

        ignore = IgnoreRules(
            root.local_path,
            extra_patterns=self._config.exclude_patterns,
        )
        seen: set[str] = set()

        for file_path, relative_path in self._walk(root.local_path, ignore):
            seen.add(relative_path)
            try:
                stat = file_path.stat()
                indexed = self._search.index_file_with_result(
                    file_path,
                    root.storage_prefix,
                    relative_path,
                    force=force,
                )
                content_hash = self._db.get_chunk_content_hash(
                    root.storage_prefix, relative_path
                )
                status = "indexed" if indexed.indexed or indexed.reason == "unchanged" else indexed.reason
                indexed_at = (
                    datetime.datetime.now(datetime.UTC).isoformat()
                    if status == "indexed"
                    else None
                )
                self._db.upsert_index_entry(
                    root.storage_prefix,
                    relative_path,
                    content_hash=content_hash,
                    size_bytes=stat.st_size,
                    modified_ns=stat.st_mtime_ns,
                    status=status,
                    reason=indexed.reason,
                    indexed_at=indexed_at,
                )
                residency = self._db.get_storage_residency(
                    root.storage_prefix, relative_path
                )
                if residency and residency["local_state"] != "present":
                    self._db.set_storage_residency(
                        root.storage_prefix,
                        relative_path,
                        local_state="present",
                        remote_state=residency["remote_state"],
                    )
                if indexed.indexed:
                    result.indexed += 1
                else:
                    result.skipped += 1
                    if hasattr(result, indexed.reason):
                        setattr(result, indexed.reason, getattr(result, indexed.reason) + 1)
            except Exception as exc:
                try:
                    stat = file_path.stat()
                    size_bytes = stat.st_size
                    modified_ns = stat.st_mtime_ns
                except OSError:
                    size_bytes = 0
                    modified_ns = 0
                self._db.upsert_index_entry(
                    root.storage_prefix,
                    relative_path,
                    content_hash=None,
                    size_bytes=size_bytes,
                    modified_ns=modified_ns,
                    status="failed",
                    reason=str(exc),
                )
                result.failed += 1

        missing = self._db.mark_unseen_index_entries_missing(
            root.storage_prefix, seen
        )
        for relative_path in missing:
            residency = self._db.get_storage_residency(
                root.storage_prefix, relative_path
            )
            self._db.set_storage_residency(
                root.storage_prefix,
                relative_path,
                local_state="missing",
                remote_state=(
                    residency["remote_state"] if residency else "unknown"
                ),
            )
            self._db.delete_search_index_for_file(
                root.storage_prefix, relative_path
            )
        result.missing += len(missing)

    @staticmethod
    def _walk(root: Path, ignore: IgnoreRules):
        for dirpath, dirnames, filenames in os.walk(root):
            current = Path(dirpath)
            relative_dir = current.relative_to(root).as_posix()
            dirnames[:] = [
                name
                for name in dirnames
                if not ignore.matches(
                    (relative_dir + "/" + name + "/").lstrip("/")
                )
            ]
            for filename in filenames:
                file_path = current / filename
                relative_path = file_path.relative_to(root).as_posix()
                if ignore.matches(relative_path) or not file_path.is_file():
                    continue
                yield file_path, relative_path
