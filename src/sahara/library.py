"""Local content-root registration and indexing services."""

from __future__ import annotations

import datetime
import os
import tempfile
import unicodedata
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from filelock import FileLock

from sahara.config import SaharaConfig
from sahara.search.search_engine import IndexFileResult, SearchEngine
from sahara.storage.state_db import StateDB
from sahara.sync.ignore_rules import IgnoreRules

__all__ = [
    "ContentRoot",
    "IndexRunResult",
    "IndexingService",
    "RESERVED_STORAGE_PREFIXES",
    "ensure_content_roots",
    "normalize_storage_prefix",
    "register_content_root",
    "unregister_content_root",
    "validate_content_root_path",
    "validate_storage_prefix",
]

RESERVED_STORAGE_PREFIXES = frozenset({"memory"})
CONTROL_STORAGE_SEGMENT = ".sahara"
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_WINDOWS_FORBIDDEN_CHARS = frozenset('<>:"|?*')


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


def _rows_to_content_roots(rows: list[dict]) -> list[ContentRoot]:
    return [
        ContentRoot(
            local_path=Path(row["local_path"]),
            storage_prefix=row["storage_prefix"],
            is_primary=row["is_primary"],
            sync_enabled=row["sync_enabled"],
        )
        for row in rows
    ]


def _same_physical_path(first: Path, second: Path) -> bool:
    """Return whether two existing path spellings identify the same object."""
    if first == second:
        return True
    try:
        return first.samefile(second)
    except OSError:
        return False


def _is_physical_ancestor(parent: Path, child: Path) -> bool:
    """Return whether parent identifies child or one of its physical parents."""
    return any(
        _same_physical_path(parent, candidate)
        for candidate in (child, *child.parents)
    )


def _ensure_content_roots_unlocked(
    config: SaharaConfig,
    db: StateDB,
) -> list[ContentRoot]:
    if config.sync_folder:
        resolved_primary = Path(config.sync_folder).expanduser().resolve()
        roots = _rows_to_content_roots(db.list_content_roots())
        current_primary = next((root for root in roots if root.is_primary), None)
        matching_root = next(
            (
                root
                for root in roots
                if _same_physical_path(root.local_path.resolve(), resolved_primary)
            ),
            None,
        )
        if matching_root is not None:
            resolved_primary = matching_root.local_path.resolve()
        validation_roots = [
            root
            for root in roots
            if root is not current_primary
        ]
        validate_content_root_path(
            resolved_primary,
            validation_roots,
            allow_same=True,
        )
        existing_primary = db.get_content_root(str(resolved_primary))
        replacing_primary = (
            current_primary is not None
            and not _same_physical_path(
                current_primary.local_path.resolve(),
                resolved_primary,
            )
        )
        if replacing_primary:
            if config.has_storage_backend or (
                "" in db.list_storage_ownership_prefixes()
            ):
                raise ValueError(
                    "Changing the primary folder after storage ownership exists "
                    "requires an explicit migration."
                )
        promoted = (
            matching_root
            if matching_root is not None and not matching_root.is_primary
            else None
        )
        if (
            promoted is not None
            and promoted.storage_prefix in db.list_storage_ownership_prefixes()
        ):
            raise ValueError(
                "Promoting this folder requires an explicit storage migration."
            )
        clear_prefixes: list[str] = []
        if replacing_primary:
            clear_prefixes.append("")
        if promoted is not None:
            clear_prefixes.append(promoted.storage_prefix)
        db.replace_primary_content_root(
            str(resolved_primary),
            sync_enabled=(
                config.has_storage_backend if existing_primary is None else False
            ),
            clear_index_prefixes=tuple(clear_prefixes),
            remove_sync_target=promoted is not None,
        )

    for target in db.list_sync_targets():
        if (
            config.sync_folder
            and Path(target["local_path"]).expanduser().resolve()
            == Path(config.sync_folder).expanduser().resolve()
        ):
            continue
        db.upsert_content_root(
            target["local_path"],
            target["s3_prefix"],
            sync_enabled=True,
        )

    return _rows_to_content_roots(db.list_content_roots())


def _content_root_lock_path(db: StateDB) -> Path:
    db_path = db.path
    if isinstance(db_path, Path):
        return db_path.with_name(f"{db_path.name}.roots.lock")
    return Path(tempfile.gettempdir()) / f"sahara-roots-{id(db)}.lock"


def ensure_content_roots(config: SaharaConfig, db: StateDB) -> list[ContentRoot]:
    """Migrate configured folders into the canonical content-root registry."""
    with FileLock(str(_content_root_lock_path(db))):
        return _ensure_content_roots_unlocked(config, db)


def validate_content_root_path(
    candidate: Path,
    roots: list[ContentRoot],
    *,
    allow_same: bool = False,
) -> None:
    """Reject content roots that overlap an existing root."""
    resolved = candidate.expanduser().resolve()
    for root in roots:
        existing = root.local_path.expanduser().resolve()
        if _same_physical_path(resolved, existing):
            if allow_same:
                continue
            raise ValueError(f"Folder already registered: {resolved}")
        if (
            resolved.is_relative_to(existing)
            or existing.is_relative_to(resolved)
            or _is_physical_ancestor(existing, resolved)
            or _is_physical_ancestor(resolved, existing)
        ):
            raise ValueError(
                f"Content root '{resolved}' overlaps registered root '{existing}'."
            )


def normalize_storage_prefix(storage_prefix: str) -> str:
    """Return one safe canonical spelling for a storage prefix."""
    raw = storage_prefix
    if raw != raw.strip():
        raise ValueError("Storage prefix must be portable across filesystems.")
    if (
        not raw
        or raw.startswith(("/", "\\"))
        or raw.endswith(("/", "\\"))
        or "\\" in raw
        or "//" in raw
    ):
        raise ValueError("Storage prefix must be a safe relative path.")
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or any(part in {"", ".", ".."} for part in raw.split("/"))
    ):
        raise ValueError("Storage prefix must be a safe relative path.")
    if any(
        part.casefold() == CONTROL_STORAGE_SEGMENT
        for part in posix.parts
    ):
        raise ValueError("Storage prefix cannot use Sahara's control namespace.")
    for part in posix.parts:
        normalized = unicodedata.normalize("NFKC", part)
        device_name = normalized.split(".", 1)[0].upper()
        if (
            normalized in {".", ".."}
            or normalized.casefold() == CONTROL_STORAGE_SEGMENT
            or normalized.endswith((" ", "."))
            or "/" in normalized
            or "\\" in normalized
            or any(character in _WINDOWS_FORBIDDEN_CHARS for character in normalized)
            or any(ord(character) < 32 for character in normalized)
            or device_name in _WINDOWS_RESERVED_NAMES
        ):
            raise ValueError("Storage prefix must be portable across filesystems.")
    return posix.as_posix()


def validate_storage_prefix(
    storage_prefix: str,
    roots: list[ContentRoot],
    *,
    allow_reserved: bool = False,
    owned_prefixes: list[str] | None = None,
) -> str:
    """Return a canonical prefix after policy and uniqueness validation."""
    normalized = normalize_storage_prefix(storage_prefix)
    normalized_key = unicodedata.normalize("NFKC", normalized).casefold()
    if not allow_reserved:
        for reserved in RESERVED_STORAGE_PREFIXES:
            reserved_key = reserved.casefold()
            if normalized_key == reserved_key or normalized_key.startswith(
                reserved_key + "/"
            ):
                raise ValueError(
                    f"Storage prefix '{normalized}' is reserved by Sahara."
                )
    existing_prefixes = [
        *(root.storage_prefix for root in roots),
        *(owned_prefixes or []),
    ]
    for existing in existing_prefixes:
        existing_key = unicodedata.normalize("NFKC", existing).casefold()
        if existing_key == normalized_key:
            raise ValueError(
                f"Storage prefix '{normalized}' is already registered or owns "
                "retained storage state."
            )
        if existing and (
            normalized_key.startswith(existing_key + "/")
            or existing_key.startswith(normalized_key + "/")
        ):
            raise ValueError(
                f"Storage prefix '{normalized}' overlaps registered prefix "
                f"'{existing}'."
            )
        candidate_current = urllib.parse.quote(normalized, safe="").casefold()
        candidate_legacy = normalized.replace("/", "-").casefold()
        existing_current = urllib.parse.quote(existing, safe="").casefold()
        existing_legacy = existing.replace("/", "-").casefold()
        if (
            candidate_current == existing_legacy
            or candidate_legacy == existing_current
        ):
            raise ValueError(
                f"Storage prefix '{normalized}' aliases a legacy manifest key "
                f"owned by '{existing}'."
            )
    return normalized


def register_content_root(
    config: SaharaConfig,
    db: StateDB,
    local_path: Path,
    storage_prefix: str,
    *,
    sync_enabled: bool = False,
    allow_reserved: bool = False,
) -> ContentRoot:
    """Atomically validate and register one non-primary content root."""
    resolved = local_path.expanduser().resolve()
    with FileLock(str(_content_root_lock_path(db))):
        roots = _ensure_content_roots_unlocked(config, db)
        validate_content_root_path(resolved, roots)
        normalized = validate_storage_prefix(
            storage_prefix,
            roots,
            allow_reserved=allow_reserved,
            owned_prefixes=db.list_storage_ownership_prefixes(),
        )
        db.upsert_content_root(
            str(resolved),
            normalized,
            sync_enabled=sync_enabled,
        )
    return ContentRoot(
        local_path=resolved,
        storage_prefix=normalized,
        is_primary=False,
        sync_enabled=sync_enabled,
    )


def unregister_content_root(
    db: StateDB,
    local_path: Path,
    storage_prefix: str,
) -> None:
    """Remove one non-primary root and all local searchable state atomically."""
    from sahara.memory.format import validate_memory_root_marker

    resolved = local_path.expanduser().resolve()
    with FileLock(str(_content_root_lock_path(db))):
        root = db.get_content_root(str(resolved))
        if root is None:
            raise ValueError(f"Folder not registered: {resolved}")
        if root["is_primary"]:
            raise ValueError("The primary folder cannot be removed.")
        if root["storage_prefix"] != storage_prefix:
            raise ValueError("Content root storage prefix changed during removal.")
        if validate_memory_root_marker(resolved):
            raise ValueError(
                "The managed Sahara memory folder cannot be removed."
            )
        db.unregister_content_root(str(resolved), storage_prefix)


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

    def index_path(self, file_path: Path, *, force: bool = False) -> IndexFileResult:
        """Index one file in a registered content root and update its inventory."""
        resolved = file_path.expanduser().resolve()
        if not resolved.is_file():
            raise ValueError(f"{file_path} is not a file")

        matches = [
            root
            for root in self.roots()
            if resolved.is_relative_to(root.local_path.expanduser().resolve())
        ]
        if len(matches) != 1:
            raise ValueError(f"{file_path} is not inside exactly one content root")

        root = matches[0]
        relative_path = resolved.relative_to(root.local_path.resolve()).as_posix()
        ignore = IgnoreRules(
            root.local_path,
            extra_patterns=self._config.exclude_patterns,
        )
        if ignore.matches(relative_path):
            raise ValueError(f"{file_path} is excluded by Sahara ignore rules")
        return self._index_file(root, resolved, relative_path, force=force)

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
                indexed = self._index_file(
                    root, file_path, relative_path, force=force
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

    def _index_file(
        self,
        root: ContentRoot,
        file_path: Path,
        relative_path: str,
        *,
        force: bool,
    ) -> IndexFileResult:
        stat = file_path.stat()
        indexed = self._search.index_file_with_result(
            file_path,
            root.storage_prefix,
            relative_path,
            force=force,
            managed_memory=self._is_managed_memory_root(root),
        )
        content_hash = self._db.get_chunk_content_hash(
            root.storage_prefix, relative_path
        )
        status = (
            "indexed"
            if indexed.indexed or indexed.reason == "unchanged"
            else indexed.reason
        )
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
        return indexed

    def _is_managed_memory_root(self, root: ContentRoot) -> bool:
        from sahara.memory.format import validate_memory_root_marker

        return validate_memory_root_marker(root.local_path)

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
