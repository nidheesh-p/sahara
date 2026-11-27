"""First-run setup for the Claude Desktop extension (MCPB) flow.

Claude Desktop passes the folders the user picked in the extension settings as
arguments to ``sahara mcp serve --extension``. Nothing here needs a terminal:
the config is created, folders are registered and the index is built in the
background while the MCP server already answers.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sahara.config import DEFAULT_CONFIG_PATH, SaharaConfig, load_config, save_config
from sahara.library import (
    IndexingService,
    ensure_content_roots,
    register_content_root,
    validate_content_root_path,
    validate_storage_prefix,
)
from sahara.storage.state_db import StateDB

__all__ = ["BackgroundIndexer", "bootstrap_extension", "clean_folder_args"]


def clean_folder_args(raw: Iterable[str]) -> list[Path]:
    """Drop empty values Claude Desktop passes for unset optional settings."""
    return [Path(item).expanduser() for item in raw if item and item.strip()]


class BackgroundIndexer:
    """Build or refresh the index on a worker thread and report progress."""

    def __init__(self, config: SaharaConfig, db_path: Path | None = None) -> None:
        self._config = config
        self._db_path = db_path
        self._lock = threading.Lock()
        self._state = "idle"
        self._detail: dict[str, Any] = {}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._state = "indexing"
            self._thread = threading.Thread(
                target=self._run, name="sahara-extension-index", daemon=True
            )
            self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"state": self._state, **self._detail}

    def _run(self) -> None:
        try:
            db = StateDB(self._db_path).connect()
            try:
                result = IndexingService(self._config, db).index(changed_only=True)
            finally:
                db.close()
        except Exception as exc:
            with self._lock:
                self._state = "failed"
                self._detail = {"error": f"{type(exc).__name__}: {exc}"}
            return
        with self._lock:
            self._state = "ready"
            self._detail = {
                "indexed": result.indexed,
                "skipped": result.skipped,
                "failed": result.failed,
            }


def bootstrap_extension(
    folders: list[Path],
    *,
    config_path: Path | None = None,
    db_path: Path | None = None,
) -> tuple[SaharaConfig, list[str]]:
    """Create or extend the config from the folders picked in extension settings.

    Returns the config and human-readable notes about folders that were skipped.
    Folders removed from the settings are not unregistered, so a changed
    selection never discards an existing index.
    """
    path = config_path or DEFAULT_CONFIG_PATH
    notes: list[str] = []
    usable: list[Path] = []
    for folder in folders:
        resolved = folder.resolve()
        if resolved.is_dir():
            usable.append(resolved)
        else:
            notes.append(f"Skipped {resolved}: not an existing folder.")

    config = load_config(path)
    if not config.sync_folder:
        if not usable:
            raise RuntimeError(
                "No folder selected. Open the Sahara extension settings in "
                "Claude Desktop and choose at least one folder to index."
            )
        config = SaharaConfig(sync_folder=str(usable[0]), storage_mode="none")
        save_config(config, path)

    db = StateDB(db_path).connect()
    try:
        roots = ensure_content_roots(config, db)
        for folder in usable:
            if any(root.local_path == folder for root in roots):
                continue
            try:
                validate_content_root_path(folder, roots)
                prefix = validate_storage_prefix(
                    folder.name,
                    roots,
                    owned_prefixes=db.list_storage_ownership_prefixes(),
                )
                register_content_root(config, db, folder, prefix)
            except ValueError as exc:
                notes.append(f"Skipped {folder}: {exc}")
                continue
            roots = ensure_content_roots(config, db)
    finally:
        db.close()
    return config, notes
