"""Durable Markdown capture for Sahara memory."""

from __future__ import annotations

import builtins
import datetime
import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from filelock import FileLock

from sahara.config import SaharaConfig
from sahara.library import (
    ContentRoot,
    IndexingService,
    ensure_content_roots,
    register_content_root,
    validate_content_root_path,
)
from sahara.memory.format import (
    MAX_IDEMPOTENCY_KEY_CHARS,
    MAX_MEMORY_CHARS,
    MAX_SOURCE_ID_CHARS,
    MAX_SOURCE_URL_CHARS,
    MAX_TAG_CHARS,
    MAX_TAGS,
    MAX_TITLE_CHARS,
    MEMORY_DOCUMENT_KIND,
    MEMORY_ROOT_MARKER,
    MEMORY_SCHEMA_VERSION,
    SOURCE_TYPES,
    parse_document,
    render_document,
    validate_memory_root_marker,
)
from sahara.storage.state_db import StateDB

__all__ = [
    "CaptureRequest",
    "CaptureResult",
    "MemoryFilters",
    "MemoryItem",
    "MemoryResult",
    "MemoryService",
    "RebuildResult",
]

MEMORY_STORAGE_PREFIX = "memory"
DEFAULT_MEMORY_DIRNAME = "Sahara Memory"
@dataclass(frozen=True)
class CaptureRequest:
    text: str
    title: str | None = None
    source_type: str = "manual"
    source_url: str = ""
    source_id: str = ""
    tags: tuple[str, ...] = ()
    idempotency_key: str = ""


@dataclass(frozen=True)
class MemoryItem:
    memory_id: str
    created_at: str
    updated_at: str
    title: str
    source_type: str
    source_url: str
    source_id: str
    tags: tuple[str, ...]
    text: str
    path: Path
    relative_path: str
    idempotency_key: str = ""


@dataclass(frozen=True)
class CaptureResult:
    item: MemoryItem
    indexed: bool
    index_reason: str
    index_error: str | None = None
    deduplicated: bool = False


@dataclass(frozen=True)
class MemoryFilters:
    source_types: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    since: str | None = None
    until: str | None = None


@dataclass(frozen=True)
class MemoryResult:
    item: MemoryItem
    score: float
    snippet: str


@dataclass(frozen=True)
class RebuildResult:
    cataloged: int
    indexed: int
    pending: int
    removed: int
    failed: tuple[tuple[str, str], ...] = ()


class MemoryService:
    """Create durable memory files and index them when search is available."""

    def __init__(
        self,
        config: SaharaConfig,
        db: StateDB,
        *,
        memory_root: Path | None = None,
    ) -> None:
        self._config = config
        self._db = db
        configured = Path(config.memory_folder).expanduser() if config.memory_folder else None
        if configured is not None and not configured.is_absolute():
            configured = Path.home() / configured
        self._root = (memory_root or configured or Path.home() / DEFAULT_MEMORY_DIRNAME).resolve()
        self._storage_prefix = MEMORY_STORAGE_PREFIX

    @property
    def root(self) -> Path:
        return self._root

    def capture(self, request: CaptureRequest) -> CaptureResult:
        normalized = self._normalize_request(request)
        setup_lock = self._setup_lock_path()
        self._ensure_private_directory_tree(setup_lock.parent)
        with FileLock(str(setup_lock)):
            root, storage_prefix = self._ensure_root()
            self._ensure_catalog(root, storage_prefix)
            duplicate = self._find_duplicate(normalized, root, storage_prefix)
            if duplicate is not None:
                return CaptureResult(
                    item=duplicate,
                    indexed=(
                        self._db.get_embedding(
                            storage_prefix,
                            duplicate.relative_path,
                        )
                        is not None
                    ),
                    index_reason="duplicate",
                    deduplicated=True,
                )
            now = datetime.datetime.now(datetime.UTC)
            memory_id = str(uuid.uuid4())
            title = normalized.title or self._derive_title(normalized.text)
            slug = self._slugify(title)
            relative_path = (
                Path(f"{now.year:04d}")
                / f"{now.month:02d}"
                / f"{memory_id[:8]}-{slug}.md"
            )
            path = root / relative_path
            timestamp = now.isoformat().replace("+00:00", "Z")
            item = MemoryItem(
                memory_id=memory_id,
                created_at=timestamp,
                updated_at=timestamp,
                title=title,
                source_type=normalized.source_type,
                source_url=normalized.source_url,
                source_id=normalized.source_id,
                tags=normalized.tags,
                text=normalized.text,
                path=path,
                relative_path=relative_path.as_posix(),
                idempotency_key=normalized.idempotency_key,
            )
            self._write_atomic(item)
            try:
                self._db.upsert_memory_item(
                    self._catalog_row(item, storage_prefix)
                )
            except Exception:
                # Markdown is authoritative; `memory rebuild` can restore this cache.
                pass

        try:
            indexed = IndexingService(self._config, self._db).index_path(path)
            return CaptureResult(
                item=item,
                indexed=indexed.indexed or indexed.reason == "unchanged",
                index_reason=indexed.reason,
            )
        except Exception as exc:
            self._record_pending_index(item, storage_prefix, str(exc))
            return CaptureResult(
                item=item,
                indexed=False,
                index_reason="pending",
                index_error=str(exc),
            )

    def read(self, path: Path) -> MemoryItem:
        """Parse and validate one Sahara memory Markdown file."""
        resolved = path.expanduser().resolve()
        if not resolved.is_relative_to(self._root):
            raise ValueError("Memory path is outside the managed memory root")
        raw = resolved.read_text(encoding="utf-8")
        metadata, text = parse_document(raw)
        return MemoryItem(
            memory_id=metadata["id"],
            created_at=metadata["created_at"],
            updated_at=metadata["updated_at"],
            title=metadata["title"],
            source_type=metadata["source_type"],
            source_url=metadata["source_url"],
            source_id=metadata["source_id"],
            tags=tuple(metadata["tags"]),
            text=text,
            path=resolved,
            relative_path=resolved.relative_to(self._root).as_posix(),
            idempotency_key=metadata.get("idempotency_key", ""),
        )

    def get(self, identifier: str) -> MemoryItem:
        """Return one memory by UUID or exact title."""
        root, storage_prefix = self._ensure_root()
        self._ensure_catalog(root, storage_prefix)
        row = self._db.get_memory_item(identifier)
        if row is None:
            matches = [
                candidate
                for candidate in self._db.list_memory_items()
                if candidate["title"].casefold() == identifier.casefold()
            ]
            if not matches:
                raise ValueError(f"Memory not found: {identifier}")
            if len(matches) > 1:
                raise ValueError(
                    f"Memory title is ambiguous; use its UUID: {identifier}"
                )
            row = matches[0]
        return self._item_from_catalog(row)

    def list(
        self,
        filters: MemoryFilters | None = None,
    ) -> builtins.list[MemoryItem]:
        """List managed memories with metadata filters."""
        root, storage_prefix = self._ensure_root()
        self._ensure_catalog(root, storage_prefix)
        rows = self._filtered_rows(filters or MemoryFilters())
        return [self._item_from_catalog(row) for row in rows]

    def search(
        self,
        query: str,
        filters: MemoryFilters | None = None,
        *,
        top_k: int = 5,
    ) -> builtins.list[MemoryResult]:
        """Search only managed memories, applying metadata filters first."""
        from sahara.search.search_engine import SearchEngine

        root, storage_prefix = self._ensure_root()
        self._ensure_catalog(root, storage_prefix)
        rows = self._filtered_rows(filters or MemoryFilters())
        by_path = {row["relative_path"]: row for row in rows}
        results = SearchEngine(self._db).search(
            query,
            top_k=max(0, top_k),
            storage_prefix=storage_prefix,
            candidate_paths=set(by_path),
        )
        recalled: builtins.list[MemoryResult] = []
        for result in results:
            row = by_path.get(result["relative_path"])
            if row is None:
                continue
            recalled.append(
                MemoryResult(
                    item=(item := self._item_from_catalog(row)),
                    score=float(result["score"]),
                    snippet=self._body_snippet(
                        item,
                        result.get("snippet", ""),
                    ),
                )
            )
        return recalled

    def edit(self, identifier: str, document: str) -> CaptureResult:
        """Atomically replace one memory document while preserving its identity."""
        setup_lock = self._setup_lock_path()
        self._ensure_private_directory_tree(setup_lock.parent)
        with FileLock(str(setup_lock)):
            item = self.get(identifier)
            metadata, body = parse_document(document)
            if metadata["id"] != item.memory_id:
                raise ValueError("Memory id cannot be changed")
            if metadata["created_at"] != item.created_at:
                raise ValueError("Memory creation timestamp cannot be changed")
            updated_at = datetime.datetime.now(datetime.UTC).isoformat().replace(
                "+00:00", "Z"
            )
            updated = MemoryItem(
                memory_id=item.memory_id,
                created_at=item.created_at,
                updated_at=updated_at,
                title=metadata["title"],
                source_type=metadata["source_type"],
                source_url=metadata["source_url"],
                source_id=metadata["source_id"],
                tags=tuple(metadata["tags"]),
                text=body,
                path=item.path,
                relative_path=item.relative_path,
                idempotency_key=metadata.get("idempotency_key", ""),
            )
            self._replace_atomic(updated)
            storage_prefix = self._memory_storage_prefix()
            try:
                self._db.upsert_memory_item(
                    self._catalog_row(updated, storage_prefix)
                )
            except Exception:
                # The updated Markdown remains authoritative and rebuildable.
                pass

        try:
            indexed = IndexingService(self._config, self._db).index_path(
                updated.path,
                force=True,
            )
            return CaptureResult(
                item=updated,
                indexed=indexed.indexed or indexed.reason == "unchanged",
                index_reason=indexed.reason,
            )
        except Exception as exc:
            self._record_pending_index(updated, storage_prefix, str(exc))
            return CaptureResult(
                item=updated,
                indexed=False,
                index_reason="pending",
                index_error=str(exc),
            )

    def delete(self, identifier: str) -> MemoryItem:
        """Atomically remove one memory and all rebuildable search state."""
        setup_lock = self._setup_lock_path()
        self._ensure_private_directory_tree(setup_lock.parent)
        with FileLock(str(setup_lock)):
            item = self.get(identifier)
            storage_prefix = self._memory_storage_prefix()
            trash = self._root / ".sahara" / "trash"
            self._ensure_private_directory_tree(trash, boundary=self._root)
            delete_token = uuid.uuid4().hex
            staged = trash / f"{item.memory_id}-{delete_token}.md"
            staged_relative_path = staged.relative_to(self._root).as_posix()
            if item.path.is_symlink() or not item.path.is_file():
                raise ValueError("Memory path is not a regular managed file")
            self._db.prepare_memory_delete(
                delete_token,
                item.memory_id,
                storage_prefix,
                item.relative_path,
                staged_relative_path,
            )
            try:
                os.replace(item.path, staged)
                self._fsync_directory(item.path.parent)
                self._db.delete_memory_item_and_index(
                    item.memory_id,
                    storage_prefix,
                    item.relative_path,
                    delete_token=delete_token,
                )
            except Exception:
                if staged.exists() and not item.path.exists():
                    os.replace(staged, item.path)
                    self._fsync_directory(item.path.parent)
                self._db.finish_memory_delete(delete_token)
                raise
            try:
                staged.unlink(missing_ok=True)
                self._fsync_directory(trash)
                self._db.finish_memory_delete(delete_token)
            except Exception:
                # Search/catalog state and the committed journal are durable.
                # Recovery will finish private trash cleanup on a later run.
                pass
            return item

    def rebuild(self) -> RebuildResult:
        """Rebuild metadata and semantic state from managed Markdown files."""
        setup_lock = self._setup_lock_path()
        self._ensure_private_directory_tree(setup_lock.parent)
        with FileLock(str(setup_lock)):
            root, storage_prefix = self._ensure_root()
            self._recover_interrupted_deletes(root)
            previous_paths = {
                row["relative_path"] for row in self._db.list_memory_items()
            }
            previous_paths.update(
                row["relative_path"]
                for row in self._db.list_index_entries(
                    storage_prefix=storage_prefix,
                    limit=None,
                )
            )
            items, failures = self._scan_items(root)
            rows = [
                self._catalog_row(item, storage_prefix)
                for item in items
            ]
            self._db.replace_memory_items(rows)

        current_paths = {item.relative_path for item in items}
        removed_paths = previous_paths - current_paths
        for relative_path in removed_paths:
            self._db.delete_search_index_for_file(
                storage_prefix,
                relative_path,
            )

        indexed = 0
        pending = 0
        for item in items:
            try:
                result = IndexingService(self._config, self._db).index_path(
                    item.path,
                    force=True,
                )
                if result.indexed or result.reason == "unchanged":
                    indexed += 1
                else:
                    pending += 1
            except Exception as exc:
                pending += 1
                failures.append((item.relative_path, str(exc)))
                self._record_pending_index(item, storage_prefix, str(exc))
        return RebuildResult(
            cataloged=len(items),
            indexed=indexed,
            pending=pending,
            removed=len(removed_paths),
            failed=tuple(failures),
        )

    def _ensure_root(self) -> tuple[Path, str]:
        roots = ensure_content_roots(self._config, self._db)
        existing = next(
            (root for root in roots if root.local_path.resolve() == self._root),
            None,
        )
        if existing is None:
            validate_content_root_path(self._root, roots)
            marker = self._root_marker_path()
            if marker.exists() or marker.is_symlink():
                self._validate_root_marker()
            else:
                self._validate_adoptable_root()
            self._ensure_private_directory_tree(self._root)
            self._ensure_root_marker()
            self._storage_prefix = self._available_storage_prefix(
                roots,
                self._db.list_storage_ownership_prefixes(),
            )
            registered = register_content_root(
                self._config,
                self._db,
                self._root,
                self._storage_prefix,
                sync_enabled=False,
                allow_reserved=True,
            )
            self._storage_prefix = registered.storage_prefix
        else:
            if not validate_memory_root_marker(self._root):
                raise ValueError(
                    "Configured memory folder is already registered as an ordinary "
                    f"content root: {self._root}"
                )
            self._storage_prefix = existing.storage_prefix
            self._ensure_private_directory_tree(self._root)
        return self._root, self._storage_prefix

    def _memory_storage_prefix(self) -> str:
        root = self._db.get_content_root(str(self._root))
        if root is None or not validate_memory_root_marker(self._root):
            raise ValueError("Managed Sahara memory root is not registered")
        return root["storage_prefix"]

    def _ensure_catalog(self, root: Path, storage_prefix: str) -> None:
        self._recover_interrupted_deletes(root)
        if self._db.count_memory_items() > 0:
            return
        items, _ = self._scan_items(root)
        if items:
            self._db.replace_memory_items(
                [self._catalog_row(item, storage_prefix) for item in items]
            )

    def _recover_interrupted_deletes(self, root: Path) -> None:
        """Restore prepared deletes or finish cleanup after a committed delete."""
        for entry in self._db.list_memory_delete_journal():
            original = (root / entry["relative_path"]).resolve()
            staged = (root / entry["staged_relative_path"]).resolve()
            trash = (root / ".sahara" / "trash").resolve()
            if (
                not original.is_relative_to(root)
                or not staged.is_relative_to(trash)
                or staged.parent != trash
            ):
                raise ValueError("Memory delete journal contains an unsafe path")
            if staged.exists() and (staged.is_symlink() or not staged.is_file()):
                raise ValueError("Memory delete journal staging path is unsafe")

            if entry["state"] == "prepared":
                if staged.exists() and not original.exists():
                    self._ensure_private_directory_tree(
                        original.parent,
                        boundary=root,
                    )
                    os.replace(staged, original)
                    self._fsync_directory(original.parent)
                elif staged.exists():
                    staged.unlink()
                    self._fsync_directory(trash)
            elif staged.exists():
                staged.unlink()
                self._fsync_directory(trash)
            self._db.finish_memory_delete(entry["token"])

    def _scan_items(
        self,
        root: Path,
    ) -> tuple[
        builtins.list[MemoryItem],
        builtins.list[tuple[str, str]],
    ]:
        items: builtins.list[MemoryItem] = []
        failures: builtins.list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        if not root.exists():
            return items, failures
        for path in sorted(root.rglob("*.md")):
            try:
                relative = path.relative_to(root)
                if ".sahara" in relative.parts:
                    continue
                if path.is_symlink() or not path.is_file():
                    raise ValueError("Memory path is not a regular file")
                item = self.read(path)
                if item.memory_id in seen_ids:
                    raise ValueError(f"Duplicate memory id: {item.memory_id}")
                seen_ids.add(item.memory_id)
                items.append(item)
            except (OSError, ValueError) as exc:
                failures.append((str(path), str(exc)))
        return items, failures

    def _find_duplicate(
        self,
        request: CaptureRequest,
        root: Path,
        storage_prefix: str,
    ) -> MemoryItem | None:
        content_hash = self._content_hash(request.text)
        canonical_url = self._canonical_url(request.source_url)
        row = self._db.find_duplicate_memory(
            source_type=request.source_type,
            source_id=request.source_id,
            canonical_url=canonical_url,
            content_hash=content_hash,
            idempotency_key=request.idempotency_key,
        )
        if row is not None:
            return self._item_from_catalog(row)

        # The catalog is rebuildable and may be stale after an interrupted write.
        # Check Markdown before creating a duplicate, then repair the cache.
        items, _ = self._scan_items(root)
        for item in items:
            if (
                (
                    request.idempotency_key
                    and item.idempotency_key == request.idempotency_key
                )
                or (
                    request.source_id
                    and item.source_type == request.source_type
                    and item.source_id == request.source_id
                )
                or (
                    canonical_url
                    and self._canonical_url(item.source_url) == canonical_url
                )
                or self._content_hash(item.text) == content_hash
            ):
                self._db.upsert_memory_item(
                    self._catalog_row(item, storage_prefix)
                )
                return item
        return None

    def _filtered_rows(
        self,
        filters: MemoryFilters,
    ) -> builtins.list[dict]:
        source_types = {value.casefold() for value in filters.source_types}
        tags = {value.casefold() for value in filters.tags}
        since = self._parse_filter_timestamp(filters.since, end_of_day=False)
        until = self._parse_filter_timestamp(filters.until, end_of_day=True)
        rows: builtins.list[dict] = []
        for row in self._db.list_memory_items():
            updated = datetime.datetime.fromisoformat(
                row["updated_at"].replace("Z", "+00:00")
            )
            row_tags = {tag.casefold() for tag in row["tags"]}
            if source_types and row["source_type"].casefold() not in source_types:
                continue
            if tags and not tags.issubset(row_tags):
                continue
            if since is not None and updated < since:
                continue
            if until is not None and updated > until:
                continue
            rows.append(row)
        return rows

    @staticmethod
    def _parse_filter_timestamp(
        value: str | None,
        *,
        end_of_day: bool,
    ) -> datetime.datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Invalid memory date: {value}") from exc
        if parsed.tzinfo is None:
            if parsed.time() == datetime.time.min and end_of_day:
                parsed = parsed.replace(
                    hour=23,
                    minute=59,
                    second=59,
                    microsecond=999999,
                )
            parsed = parsed.replace(tzinfo=datetime.UTC)
        return parsed.astimezone(datetime.UTC)

    def _item_from_catalog(self, row: dict) -> MemoryItem:
        path = (self._root / row["relative_path"]).resolve()
        if not path.is_relative_to(self._root):
            raise ValueError("Memory catalog path escapes the managed root")
        item = self.read(path)
        if item.memory_id != row["memory_id"]:
            raise ValueError("Memory catalog identity does not match Markdown")
        return item

    @classmethod
    def _catalog_row(
        cls,
        item: MemoryItem,
        storage_prefix: str,
    ) -> dict:
        return {
            "memory_id": item.memory_id,
            "storage_prefix": storage_prefix,
            "relative_path": item.relative_path,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "title": item.title,
            "source_type": item.source_type,
            "source_url": item.source_url,
            "source_id": item.source_id,
            "canonical_url": cls._canonical_url(item.source_url),
            "tags": list(item.tags),
            "content_hash": cls._content_hash(item.text),
            "idempotency_key": item.idempotency_key,
        }

    @staticmethod
    def _content_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _body_snippet(item: MemoryItem, snippet: str) -> str:
        """Return the matching chunk portion that belongs to the memory body."""
        snippet = snippet.strip()
        if snippet and snippet in item.text:
            return snippet

        prefix_parts = [item.title]
        if item.tags:
            prefix_parts.append("Tags: " + ", ".join(item.tags))
        searchable = "\n\n".join([*prefix_parts, item.text])
        body_offset = len(searchable) - len(item.text)
        snippet_offset = searchable.find(snippet) if snippet else -1
        if snippet_offset >= 0:
            body_part = snippet[max(0, body_offset - snippet_offset) :].strip()
            if body_part:
                return body_part
        return item.text[:500].strip()

    @staticmethod
    def _canonical_url(value: str) -> str:
        if not value:
            return ""
        parsed = urlparse(value)
        host = (parsed.hostname or "").casefold()
        port = parsed.port
        if port and not (
            (parsed.scheme.casefold() == "http" and port == 80)
            or (parsed.scheme.casefold() == "https" and port == 443)
        ):
            host = f"{host}:{port}"
        path = parsed.path.rstrip("/")
        query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
        return urlunparse(
            (
                parsed.scheme.casefold(),
                host,
                path,
                "",
                query,
                "",
            )
        )

    def _validate_adoptable_root(self) -> None:
        """Require a new root, allowing only an empty internal setup directory."""
        if not self._root.exists():
            return
        for entry in self._root.iterdir():
            if entry.is_symlink():
                raise ValueError(
                    f"Managed directory is not a real directory: {entry}"
                )
            if (
                entry.name == ".sahara"
                and entry.is_dir()
                and not any(entry.iterdir())
            ):
                continue
            raise ValueError(
                "Configured memory folder must be empty before Sahara can manage it."
            )

    def _write_atomic(self, item: MemoryItem) -> None:
        self._ensure_private_directory_tree(item.path.parent, boundary=self._root)
        if item.path.exists() or item.path.is_symlink():
            raise FileExistsError(f"Memory path already exists: {item.path}")
        resolved_parent = item.path.parent.resolve(strict=True)
        if not resolved_parent.is_relative_to(self._root):
            raise ValueError("Memory path escapes the managed memory root")
        document = self._render_document(item)

        if os.name == "posix":
            self._write_atomic_posix(item, document)
            return

        fd, temp_name = tempfile.mkstemp(
            dir=item.path.parent,
            prefix=f".{item.path.name}.",
            suffix=".tmp",
            text=True,
        )
        temp_path = Path(temp_name)
        try:
            os.chmod(temp_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(document)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, item.path)
            self._fsync_directory(item.path.parent)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            temp_path.unlink(missing_ok=True)
            raise

    def _write_atomic_posix(self, item: MemoryItem, document: str) -> None:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(item.path.parent, flags)
        temp_name = f".{item.path.name}.{uuid.uuid4().hex}.tmp"
        file_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = -1
        try:
            try:
                os.stat(item.path.name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(f"Memory path already exists: {item.path}")

            fd = os.open(
                temp_name,
                file_flags,
                0o600,
                dir_fd=directory_fd,
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                fd = -1
                handle.write(document)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                temp_name,
                item.path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.close(directory_fd)

    def _replace_atomic(self, item: MemoryItem) -> None:
        """Atomically replace an existing managed memory document."""
        if item.path.is_symlink() or not item.path.is_file():
            raise ValueError("Memory path is not a regular managed file")
        resolved_parent = item.path.parent.resolve(strict=True)
        if not resolved_parent.is_relative_to(self._root):
            raise ValueError("Memory path escapes the managed memory root")
        document = self._render_document(item)
        if os.name != "posix":
            fd, temp_name = tempfile.mkstemp(
                dir=item.path.parent,
                prefix=f".{item.path.name}.",
                suffix=".tmp",
                text=True,
            )
            temp_path = Path(temp_name)
            try:
                os.chmod(temp_path, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(document)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, item.path)
                self._fsync_directory(item.path.parent)
            finally:
                temp_path.unlink(missing_ok=True)
            return

        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(item.path.parent, flags)
        temp_name = f".{item.path.name}.{uuid.uuid4().hex}.tmp"
        fd = -1
        try:
            metadata = os.stat(
                item.path.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Memory path is not a regular managed file")
            fd = os.open(
                temp_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                fd = -1
                handle.write(document)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                temp_name,
                item.path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.close(directory_fd)

    def _record_pending_index(
        self,
        item: MemoryItem,
        storage_prefix: str,
        reason: str,
    ) -> None:
        """Best-effort inventory record for a durable but unindexed memory."""
        try:
            stat = item.path.stat()
            self._db.upsert_index_entry(
                storage_prefix,
                item.relative_path,
                content_hash=None,
                size_bytes=stat.st_size,
                modified_ns=stat.st_mtime_ns,
                status="pending",
                reason=reason,
            )
        except Exception:
            pass

    def _root_marker_path(self) -> Path:
        return self._root / ".sahara" / MEMORY_ROOT_MARKER

    def _setup_lock_path(self) -> Path:
        root_hash = hashlib.sha256(str(self._root).encode("utf-8")).hexdigest()[:24]
        return self._db.path.parent / "locks" / f"memory-{root_hash}.lock"

    def _ensure_root_marker(self) -> None:
        marker_dir = self._root / ".sahara"
        self._ensure_private_directory_tree(marker_dir, boundary=self._root)
        marker = self._root_marker_path()
        if marker.is_symlink():
            raise ValueError("Invalid Sahara memory root marker")
        if marker.exists():
            self._validate_root_marker()
            return
        payload = json.dumps(
            {"kind": MEMORY_DOCUMENT_KIND, "schema_version": MEMORY_SCHEMA_VERSION}
        )
        temp_marker = marker_dir / f".{MEMORY_ROOT_MARKER}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if os.name == "posix":
            flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temp_marker, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_marker, marker)
            self._fsync_directory(marker_dir)
        finally:
            temp_marker.unlink(missing_ok=True)

    def _validate_root_marker(self) -> None:
        validate_memory_root_marker(self._root, required=True)

    @staticmethod
    def _available_storage_prefix(
        roots: builtins.list[ContentRoot],
        owned_prefixes: builtins.list[str],
    ) -> str:
        prefixes = {
            prefix.casefold()
            for prefix in (
                *(root.storage_prefix for root in roots),
                *owned_prefixes,
            )
        }
        suffix = 1
        while True:
            candidate = (
                MEMORY_STORAGE_PREFIX
                if suffix == 1
                else f"{MEMORY_STORAGE_PREFIX}-{suffix}"
            )
            candidate_key = candidate.casefold()
            if not any(
                existing == candidate_key
                or existing.startswith(candidate_key + "/")
                or candidate_key.startswith(existing + "/")
                for existing in prefixes
                if existing
            ):
                return candidate
            suffix += 1

    @classmethod
    def _ensure_private_directory_tree(
        cls,
        target: Path,
        *,
        boundary: Path | None = None,
    ) -> None:
        if boundary is not None and not target.is_relative_to(boundary):
            raise ValueError("Managed directory path escapes the memory root")

        missing: builtins.list[Path] = []
        current = target
        while not current.exists() and not current.is_symlink():
            missing.append(current)
            if current.parent == current:
                break
            current = current.parent

        if current.is_symlink() or not current.is_dir():
            raise ValueError(f"Managed directory is not a real directory: {current}")

        for directory in reversed(missing):
            os.mkdir(directory, mode=0o700)
            if os.name == "posix":
                os.chmod(directory, 0o700)
            cls._fsync_directory(directory.parent)

        current = target
        while boundary is not None and current != boundary.parent:
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(
                    f"Managed directory is not a real directory: {current}"
                )
            if os.name == "posix":
                os.chmod(current, 0o700)
            if current == boundary:
                break
            current = current.parent

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """Persist the rename itself on filesystems that support directory fsync."""
        if os.name != "posix":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(directory, flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _render_document(item: MemoryItem) -> str:
        metadata = {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "kind": MEMORY_DOCUMENT_KIND,
            "id": item.memory_id,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "title": item.title,
            "source_type": item.source_type,
            "source_url": item.source_url,
            "source_id": item.source_id,
            "tags": list(item.tags),
            "idempotency_key": item.idempotency_key,
        }
        return render_document(metadata, item.text)

    @staticmethod
    def _normalize_request(request: CaptureRequest) -> CaptureRequest:
        text = request.text
        if not text.strip():
            raise ValueError("Memory text cannot be empty")
        if len(text) > MAX_MEMORY_CHARS:
            raise ValueError(
                f"Memory text exceeds the {MAX_MEMORY_CHARS:,}-character limit"
            )

        source_type = request.source_type.strip().lower()
        if source_type not in SOURCE_TYPES:
            raise ValueError(
                "source_type must be one of: " + ", ".join(sorted(SOURCE_TYPES))
            )

        title = request.title.strip() if request.title else None
        if title and len(title) > MAX_TITLE_CHARS:
            raise ValueError(
                f"Memory title exceeds the {MAX_TITLE_CHARS}-character limit"
            )

        source_url = request.source_url.strip()
        if source_url:
            if len(source_url) > MAX_SOURCE_URL_CHARS:
                raise ValueError(
                    f"source_url exceeds the {MAX_SOURCE_URL_CHARS:,}-character limit"
                )
            parsed = urlparse(source_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("source_url must be an absolute HTTP or HTTPS URL")

        source_id = request.source_id.strip()
        if len(source_id) > MAX_SOURCE_ID_CHARS:
            raise ValueError(
                f"source_id exceeds the {MAX_SOURCE_ID_CHARS}-character limit"
            )

        idempotency_key = request.idempotency_key.strip()
        if len(idempotency_key) > MAX_IDEMPOTENCY_KEY_CHARS:
            raise ValueError(
                "idempotency_key exceeds the "
                f"{MAX_IDEMPOTENCY_KEY_CHARS}-character limit"
            )

        tags: builtins.list[str] = []
        seen: set[str] = set()
        if len(request.tags) > MAX_TAGS:
            raise ValueError(f"A memory can have at most {MAX_TAGS} tags")
        for raw_tag in request.tags:
            tag = raw_tag.strip()
            if not tag:
                continue
            if len(tag) > MAX_TAG_CHARS:
                raise ValueError(
                    f"Memory tags cannot exceed {MAX_TAG_CHARS} characters"
                )
            key = tag.casefold()
            if key not in seen:
                tags.append(tag)
                seen.add(key)

        return CaptureRequest(
            text=text,
            title=title,
            source_type=source_type,
            source_url=source_url,
            source_id=source_id,
            tags=tuple(tags),
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def _derive_title(text: str) -> str:
        for line in text.splitlines():
            candidate = line.strip().lstrip("#").strip()
            if candidate:
                return candidate[:MAX_TITLE_CHARS].rstrip()
        return "Untitled memory"

    @staticmethod
    def _slugify(title: str) -> str:
        ascii_title = unicodedata.normalize("NFKD", title).encode(
            "ascii", "ignore"
        ).decode("ascii")
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
        return (slug or "memory")[:64].rstrip("-")
