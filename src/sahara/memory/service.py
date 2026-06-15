"""Durable Markdown capture for Sahara memory."""

from __future__ import annotations

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
from urllib.parse import urlparse

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
    "MemoryItem",
    "MemoryService",
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


@dataclass(frozen=True)
class CaptureResult:
    item: MemoryItem
    indexed: bool
    index_reason: str
    index_error: str | None = None


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
            )
            self._write_atomic(item)

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
        roots: list[ContentRoot],
        owned_prefixes: list[str],
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

        missing: list[Path] = []
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

        tags: list[str] = []
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
