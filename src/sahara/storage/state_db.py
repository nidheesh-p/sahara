"""SQLite state database for Sahara."""

from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sahara.models import FileRecord, StorageTier

__all__ = ["StateDB", "DB_PATH"]

DB_PATH = Path.home() / ".sahara" / "state.db"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    s3_prefix           TEXT    NOT NULL DEFAULT '',
    relative_path       TEXT    NOT NULL,
    sha256_checksum     TEXT    NOT NULL,
    size_bytes          INTEGER NOT NULL DEFAULT 0,
    tier                TEXT    NOT NULL DEFAULT 'STANDARD',
    s3_etag             TEXT    NOT NULL DEFAULT '',
    last_sync_at        TEXT    NOT NULL,
    local_modified_at   TEXT    NOT NULL,
    remote_modified_at  TEXT    NOT NULL,
    archived_at         TEXT,
    restore_job_id      TEXT,
    restore_expires_at  TEXT,
    is_deleted          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (s3_prefix, relative_path)
);

CREATE INDEX IF NOT EXISTS idx_files_tier       ON files (s3_prefix, tier);
CREATE INDEX IF NOT EXISTS idx_files_is_deleted ON files (s3_prefix, is_deleted);
CREATE INDEX IF NOT EXISTS idx_files_sha256     ON files (sha256_checksum);

CREATE TABLE IF NOT EXISTS history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    s3_prefix       TEXT    NOT NULL DEFAULT '',
    relative_path   TEXT    NOT NULL,
    operation       TEXT    NOT NULL,
    sha256          TEXT,
    size_bytes      INTEGER,
    tier            TEXT,
    occurred_at     TEXT    NOT NULL,
    details         TEXT
);

CREATE INDEX IF NOT EXISTS idx_history_path       ON history (s3_prefix, relative_path);
CREATE INDEX IF NOT EXISTS idx_history_occurred   ON history (occurred_at);

CREATE TABLE IF NOT EXISTS pending_multipart (
    s3_prefix       TEXT    NOT NULL DEFAULT '',
    relative_path   TEXT    NOT NULL,
    s3_key          TEXT    NOT NULL,
    upload_id       TEXT    NOT NULL,
    file_sha256     TEXT    NOT NULL,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    parts_json      TEXT    NOT NULL DEFAULT '[]',
    started_at      TEXT    NOT NULL,
    PRIMARY KEY (s3_prefix, relative_path)
);

CREATE TABLE IF NOT EXISTS sync_targets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    local_path  TEXT    NOT NULL UNIQUE,
    s3_prefix   TEXT    NOT NULL UNIQUE,
    added_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS config_kv (
    key     TEXT    PRIMARY KEY,
    value   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS embeddings (
    s3_prefix       TEXT    NOT NULL DEFAULT '',
    relative_path   TEXT    NOT NULL,
    content_hash    TEXT    NOT NULL,
    embedding_json  TEXT    NOT NULL,
    snippet         TEXT    NOT NULL DEFAULT '',
    indexed_at      TEXT    NOT NULL,
    PRIMARY KEY (s3_prefix, relative_path)
);

CREATE INDEX IF NOT EXISTS idx_embeddings_prefix ON embeddings (s3_prefix);

CREATE TABLE IF NOT EXISTS chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    storage_prefix  TEXT    NOT NULL DEFAULT '',
    relative_path   TEXT    NOT NULL,
    chunk_index     INTEGER NOT NULL,
    content_hash    TEXT    NOT NULL,
    chunk_text      TEXT    NOT NULL,
    indexed_at      TEXT    NOT NULL,
    UNIQUE(storage_prefix, relative_path, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_prefix ON chunks (storage_prefix);
CREATE INDEX IF NOT EXISTS idx_chunks_path   ON chunks (storage_prefix, relative_path);
"""


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Apply required SQLite PRAGMAs to every new connection."""
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")


def _row_to_file_record(row: sqlite3.Row) -> FileRecord:
    def _dt(val: str | None) -> datetime.datetime | None:
        if val is None:
            return None
        return datetime.datetime.fromisoformat(val)

    return FileRecord(
        relative_path=row["relative_path"],
        sha256_checksum=row["sha256_checksum"],
        size_bytes=row["size_bytes"],
        tier=row["tier"],
        s3_etag=row["s3_etag"],
        last_sync_at=datetime.datetime.fromisoformat(row["last_sync_at"]),
        local_modified_at=datetime.datetime.fromisoformat(row["local_modified_at"]),
        remote_modified_at=datetime.datetime.fromisoformat(row["remote_modified_at"]),
        archived_at=_dt(row["archived_at"]),
        restore_job_id=row["restore_job_id"],
        restore_expires_at=_dt(row["restore_expires_at"]),
        is_deleted=bool(row["is_deleted"]),
    )


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Add s3_prefix namespacing (idempotent — runs once on old databases)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
    if not cols:
        return  # empty DB — SCHEMA_SQL will create fresh tables with s3_prefix
    if "s3_prefix" in cols:
        return  # already migrated

    # files: rename → recreate with composite PK → copy → drop
    conn.executescript("""
        ALTER TABLE files RENAME TO files_old;

        CREATE TABLE files (
            s3_prefix           TEXT    NOT NULL DEFAULT '',
            relative_path       TEXT    NOT NULL,
            sha256_checksum     TEXT    NOT NULL,
            size_bytes          INTEGER NOT NULL DEFAULT 0,
            tier                TEXT    NOT NULL DEFAULT 'STANDARD',
            s3_etag             TEXT    NOT NULL DEFAULT '',
            last_sync_at        TEXT    NOT NULL,
            local_modified_at   TEXT    NOT NULL,
            remote_modified_at  TEXT    NOT NULL,
            archived_at         TEXT,
            restore_job_id      TEXT,
            restore_expires_at  TEXT,
            is_deleted          INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (s3_prefix, relative_path)
        );

        INSERT INTO files
            (s3_prefix, relative_path, sha256_checksum, size_bytes, tier, s3_etag,
             last_sync_at, local_modified_at, remote_modified_at, archived_at,
             restore_job_id, restore_expires_at, is_deleted)
        SELECT
            '', relative_path, sha256_checksum, size_bytes, tier, s3_etag,
            last_sync_at, local_modified_at, remote_modified_at, archived_at,
            restore_job_id, restore_expires_at, is_deleted
        FROM files_old;

        DROP TABLE files_old;

        CREATE INDEX IF NOT EXISTS idx_files_tier
            ON files (s3_prefix, tier);
        CREATE INDEX IF NOT EXISTS idx_files_is_deleted
            ON files (s3_prefix, is_deleted);
        CREATE INDEX IF NOT EXISTS idx_files_sha256
            ON files (sha256_checksum);
    """)

    # history: ADD COLUMN is sufficient (no PK change needed)
    conn.execute(
        "ALTER TABLE history ADD COLUMN s3_prefix TEXT NOT NULL DEFAULT ''"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_path "
        "ON history (s3_prefix, relative_path)"
    )

    # pending_multipart: rename → recreate → copy → drop
    conn.executescript("""
        ALTER TABLE pending_multipart RENAME TO pending_multipart_old;

        CREATE TABLE pending_multipart (
            s3_prefix       TEXT    NOT NULL DEFAULT '',
            relative_path   TEXT    NOT NULL,
            s3_key          TEXT    NOT NULL,
            upload_id       TEXT    NOT NULL,
            file_sha256     TEXT    NOT NULL,
            size_bytes      INTEGER NOT NULL DEFAULT 0,
            parts_json      TEXT    NOT NULL DEFAULT '[]',
            started_at      TEXT    NOT NULL,
            PRIMARY KEY (s3_prefix, relative_path)
        );

        INSERT INTO pending_multipart
            (s3_prefix, relative_path, s3_key, upload_id, file_sha256,
             size_bytes, parts_json, started_at)
        SELECT
            '', relative_path, s3_key, upload_id, file_sha256,
            size_bytes, parts_json, started_at
        FROM pending_multipart_old;

        DROP TABLE pending_multipart_old;
    """)

    # sync_targets table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_targets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            local_path  TEXT    NOT NULL UNIQUE,
            s3_prefix   TEXT    NOT NULL UNIQUE,
            added_at    TEXT    NOT NULL
        )
    """)

    conn.commit()


def _try_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension if available. Returns True on success."""
    try:
        import sqlite_vec  # type: ignore[import]
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Create vec_chunks virtual table for ANN search (idempotent)."""
    # Check if chunks table exists (created by _SCHEMA_SQL in v3+)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "chunks" not in tables:
        return  # schema not yet applied (shouldn't happen)

    # vec_chunks is a virtual table — check via sqlite_master
    virtual_tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' OR type='shadow'"
    ).fetchall()}
    if "vec_chunks" in virtual_tables:
        return  # already exists

    has_vec = _try_load_sqlite_vec(conn)
    if not has_vec:
        return  # sqlite-vec not installed; degrade gracefully

    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks
            USING vec0(embedding float[384])
    """)
    conn.commit()


class StateDB:
    """Manages the local SQLite state database for Sahara.

    Supports use as a context manager:

        with StateDB(db_path) as db:
            record = db.get_file("docs/report.pdf")
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._path = db_path or DB_PATH
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> StateDB:
        """Open the database connection and initialise the schema."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _try_load_sqlite_vec(conn)
        _apply_pragmas(conn)
        # Run v2 migration BEFORE SCHEMA_SQL so that old DBs (without s3_prefix)
        # have their tables upgraded before the index CREATE statements run.
        _migrate_v2(conn)
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
        _migrate_v3(conn)
        self._conn = conn
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> StateDB:
        return self.connect()

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("StateDB is not connected. Call connect() first.")
        return self._conn

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager that commits on success, rolls back on exception."""
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------------
    # files table
    # ------------------------------------------------------------------

    def upsert_file(self, record: FileRecord, s3_prefix: str = "") -> None:
        """Insert or replace a FileRecord in the files table."""
        sql = """
        INSERT INTO files (
            s3_prefix, relative_path, sha256_checksum, size_bytes, tier, s3_etag,
            last_sync_at, local_modified_at, remote_modified_at,
            archived_at, restore_job_id, restore_expires_at, is_deleted
        ) VALUES (
            :s3_prefix, :relative_path, :sha256_checksum, :size_bytes, :tier, :s3_etag,
            :last_sync_at, :local_modified_at, :remote_modified_at,
            :archived_at, :restore_job_id, :restore_expires_at, :is_deleted
        )
        ON CONFLICT(s3_prefix, relative_path) DO UPDATE SET
            sha256_checksum    = excluded.sha256_checksum,
            size_bytes         = excluded.size_bytes,
            tier               = excluded.tier,
            s3_etag            = excluded.s3_etag,
            last_sync_at       = excluded.last_sync_at,
            local_modified_at  = excluded.local_modified_at,
            remote_modified_at = excluded.remote_modified_at,
            archived_at        = excluded.archived_at,
            restore_job_id     = excluded.restore_job_id,
            restore_expires_at = excluded.restore_expires_at,
            is_deleted         = excluded.is_deleted
        """

        def _iso(dt: datetime.datetime | None) -> str | None:
            return dt.isoformat() if dt is not None else None

        params = {
            "s3_prefix": s3_prefix,
            "relative_path": record.relative_path,
            "sha256_checksum": record.sha256_checksum,
            "size_bytes": record.size_bytes,
            "tier": record.tier,
            "s3_etag": record.s3_etag,
            "last_sync_at": record.last_sync_at.isoformat(),
            "local_modified_at": record.local_modified_at.isoformat(),
            "remote_modified_at": record.remote_modified_at.isoformat(),
            "archived_at": _iso(record.archived_at),
            "restore_job_id": record.restore_job_id,
            "restore_expires_at": _iso(record.restore_expires_at),
            "is_deleted": int(record.is_deleted),
        }
        with self.transaction():
            self.conn.execute(sql, params)

    def get_file(self, relative_path: str, s3_prefix: str = "") -> FileRecord | None:
        """Retrieve a single FileRecord by relative path."""
        row = self.conn.execute(
            "SELECT * FROM files WHERE s3_prefix = ? AND relative_path = ?",
            (s3_prefix, relative_path),
        ).fetchone()
        if row is None:
            return None
        return _row_to_file_record(row)

    def delete_file(self, relative_path: str, s3_prefix: str = "") -> None:
        """Hard-delete a row from the files table."""
        with self.transaction():
            self.conn.execute(
                "DELETE FROM files WHERE s3_prefix = ? AND relative_path = ?",
                (s3_prefix, relative_path),
            )

    def mark_deleted(self, relative_path: str, s3_prefix: str = "") -> None:
        """Soft-delete: set is_deleted = 1."""
        with self.transaction():
            self.conn.execute(
                "UPDATE files SET is_deleted = 1 "
                "WHERE s3_prefix = ? AND relative_path = ?",
                (s3_prefix, relative_path),
            )

    def list_files(
        self, include_deleted: bool = False, s3_prefix: str = ""
    ) -> list[FileRecord]:
        """Return all tracked files for the given s3_prefix."""
        if include_deleted:
            rows = self.conn.execute(
                "SELECT * FROM files WHERE s3_prefix = ?", (s3_prefix,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM files WHERE s3_prefix = ? AND is_deleted = 0",
                (s3_prefix,),
            ).fetchall()
        return [_row_to_file_record(r) for r in rows]

    def list_files_by_tier(
        self, tier: StorageTier, s3_prefix: str = ""
    ) -> list[FileRecord]:
        """Return files of a specific storage tier."""
        rows = self.conn.execute(
            "SELECT * FROM files WHERE s3_prefix = ? AND tier = ? AND is_deleted = 0",
            (s3_prefix, tier),
        ).fetchall()
        return [_row_to_file_record(r) for r in rows]

    def list_files_by_sha256(
        self, sha256: str, s3_prefix: str = ""
    ) -> list[FileRecord]:
        """Find all files with a given SHA-256 checksum."""
        rows = self.conn.execute(
            "SELECT * FROM files "
            "WHERE s3_prefix = ? AND sha256_checksum = ? AND is_deleted = 0",
            (s3_prefix, sha256),
        ).fetchall()
        return [_row_to_file_record(r) for r in rows]

    def get_total_size_by_tier(self, s3_prefix: str | None = None) -> dict[str, int]:
        """Return {tier: total_bytes} for all non-deleted files.

        If s3_prefix is provided, scopes to that prefix only.
        If s3_prefix is None, aggregates across all prefixes.
        """
        if s3_prefix is not None:
            rows = self.conn.execute(
                "SELECT tier, SUM(size_bytes) FROM files "
                "WHERE s3_prefix = ? AND is_deleted = 0 GROUP BY tier",
                (s3_prefix,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT tier, SUM(size_bytes) FROM files "
                "WHERE is_deleted = 0 GROUP BY tier"
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    # ------------------------------------------------------------------
    # history table
    # ------------------------------------------------------------------

    def add_history(
        self,
        relative_path: str,
        operation: str,
        sha256: str | None = None,
        size_bytes: int | None = None,
        tier: str | None = None,
        details: str | None = None,
        s3_prefix: str = "",
    ) -> None:
        """Append an entry to the history log."""
        sql = """
        INSERT INTO history
            (s3_prefix, relative_path, operation, sha256, size_bytes, tier, occurred_at, details)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self.transaction():
            self.conn.execute(
                sql,
                (
                    s3_prefix,
                    relative_path,
                    operation,
                    sha256,
                    size_bytes,
                    tier,
                    datetime.datetime.now(datetime.UTC).isoformat(),
                    details,
                ),
            )

    def get_history(
        self,
        relative_path: str | None = None,
        limit: int = 100,
        s3_prefix: str | None = None,
    ) -> list[dict]:
        """Return history entries, optionally filtered by path and prefix."""
        conditions: list[str] = []
        params: list = []

        if s3_prefix is not None:
            conditions.append("s3_prefix = ?")
            params.append(s3_prefix)
        if relative_path:
            conditions.append("relative_path = ?")
            params.append(relative_path)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT * FROM history {where} ORDER BY occurred_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # pending_multipart table
    # ------------------------------------------------------------------

    def upsert_pending_multipart(
        self,
        relative_path: str,
        s3_key: str,
        upload_id: str,
        file_sha256: str,
        size_bytes: int,
        parts_json: str = "[]",
        s3_prefix: str = "",
    ) -> None:
        sql = """
        INSERT INTO pending_multipart
            (s3_prefix, relative_path, s3_key, upload_id, file_sha256, size_bytes, parts_json, started_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(s3_prefix, relative_path) DO UPDATE SET
            s3_key      = excluded.s3_key,
            upload_id   = excluded.upload_id,
            file_sha256 = excluded.file_sha256,
            size_bytes  = excluded.size_bytes,
            parts_json  = excluded.parts_json
        """
        with self.transaction():
            self.conn.execute(
                sql,
                (
                    s3_prefix,
                    relative_path,
                    s3_key,
                    upload_id,
                    file_sha256,
                    size_bytes,
                    parts_json,
                    datetime.datetime.now(datetime.UTC).isoformat(),
                ),
            )

    def get_pending_multipart(
        self, relative_path: str, s3_prefix: str = ""
    ) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM pending_multipart WHERE s3_prefix = ? AND relative_path = ?",
            (s3_prefix, relative_path),
        ).fetchone()
        return dict(row) if row else None

    def get_pending_multiparts(self, s3_prefix: str = "") -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM pending_multipart WHERE s3_prefix = ?", (s3_prefix,)
        ).fetchall()
        return [dict(r) for r in rows]

    def update_pending_multipart_parts(
        self, relative_path: str, parts_json: str, s3_prefix: str = ""
    ) -> None:
        with self.transaction():
            self.conn.execute(
                "UPDATE pending_multipart SET parts_json = ? "
                "WHERE s3_prefix = ? AND relative_path = ?",
                (parts_json, s3_prefix, relative_path),
            )

    def delete_pending_multipart(
        self, relative_path: str, s3_prefix: str = ""
    ) -> None:
        with self.transaction():
            self.conn.execute(
                "DELETE FROM pending_multipart WHERE s3_prefix = ? AND relative_path = ?",
                (s3_prefix, relative_path),
            )

    # ------------------------------------------------------------------
    # config_kv table
    # ------------------------------------------------------------------

    def get_config_value(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM config_kv WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_config_value(self, key: str, value: str) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT INTO config_kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def delete_config_value(self, key: str) -> None:
        with self.transaction():
            self.conn.execute("DELETE FROM config_kv WHERE key = ?", (key,))

    # ------------------------------------------------------------------
    # Restore-tracking helpers
    # ------------------------------------------------------------------

    def list_pending_restores(self, s3_prefix: str = "") -> list[FileRecord]:
        """Return all files with an active restore_job_id."""
        rows = self.conn.execute(
            "SELECT * FROM files "
            "WHERE s3_prefix = ? AND restore_job_id IS NOT NULL AND is_deleted = 0",
            (s3_prefix,),
        ).fetchall()
        return [_row_to_file_record(r) for r in rows]

    def list_expiring_restores(
        self, within_hours: int = 48, s3_prefix: str = ""
    ) -> list[FileRecord]:
        """Return HOT_TEMP files whose restore window expires soon."""
        cutoff = (
            datetime.datetime.now(datetime.UTC)
            + datetime.timedelta(hours=within_hours)
        ).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM files "
            "WHERE s3_prefix = ? AND tier = 'HOT_TEMP' "
            "AND restore_expires_at <= ? AND is_deleted = 0",
            (s3_prefix, cutoff),
        ).fetchall()
        return [_row_to_file_record(r) for r in rows]

    # ------------------------------------------------------------------
    # sync_targets table
    # ------------------------------------------------------------------

    def add_sync_target(self, local_path: str, s3_prefix: str) -> None:
        """Register an additional folder for sync."""
        with self.transaction():
            self.conn.execute(
                "INSERT INTO sync_targets (local_path, s3_prefix, added_at) "
                "VALUES (?, ?, ?) ON CONFLICT(local_path) DO NOTHING",
                (
                    local_path,
                    s3_prefix,
                    datetime.datetime.now(datetime.UTC).isoformat(),
                ),
            )

    def remove_sync_target(self, local_path: str) -> None:
        """Unregister an additional sync folder."""
        with self.transaction():
            self.conn.execute(
                "DELETE FROM sync_targets WHERE local_path = ?", (local_path,)
            )

    def list_sync_targets(self) -> list[dict]:
        """Return all registered additional sync targets (excludes primary folder)."""
        rows = self.conn.execute(
            "SELECT id, local_path, s3_prefix, added_at "
            "FROM sync_targets ORDER BY added_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_sync_target_by_prefix(self, s3_prefix: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM sync_targets WHERE s3_prefix = ?", (s3_prefix,)
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # embeddings table (for semantic search)
    # ------------------------------------------------------------------

    def upsert_embedding(
        self,
        s3_prefix: str,
        relative_path: str,
        content_hash: str,
        embedding_json: str,
        snippet: str = "",
    ) -> None:
        """Insert or update an embedding record."""
        sql = """
        INSERT INTO embeddings
            (s3_prefix, relative_path, content_hash, embedding_json, snippet, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(s3_prefix, relative_path) DO UPDATE SET
            content_hash   = excluded.content_hash,
            embedding_json = excluded.embedding_json,
            snippet        = excluded.snippet,
            indexed_at     = excluded.indexed_at
        """
        with self.transaction():
            self.conn.execute(
                sql,
                (
                    s3_prefix,
                    relative_path,
                    content_hash,
                    embedding_json,
                    snippet,
                    datetime.datetime.now(datetime.UTC).isoformat(),
                ),
            )

    def get_embedding(self, s3_prefix: str, relative_path: str) -> dict | None:
        """Return the embedding record for a file, or None."""
        row = self.conn.execute(
            "SELECT * FROM embeddings WHERE s3_prefix = ? AND relative_path = ?",
            (s3_prefix, relative_path),
        ).fetchone()
        return dict(row) if row else None

    def list_embeddings(self, s3_prefix: str | None = None) -> list[dict]:
        """Return all embedding records, optionally filtered by s3_prefix."""
        if s3_prefix is not None:
            rows = self.conn.execute(
                "SELECT * FROM embeddings WHERE s3_prefix = ?", (s3_prefix,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM embeddings").fetchall()
        return [dict(r) for r in rows]

    def count_embeddings(self, s3_prefix: str | None = None) -> int:
        """Return the count of indexed files, optionally scoped to s3_prefix."""
        if s3_prefix is not None:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM embeddings WHERE s3_prefix = ?", (s3_prefix,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()
        return row[0] if row else 0

    def count_tracked_files(self, s3_prefix: str | None = None) -> int:
        """Return the count of tracked non-deleted files."""
        if s3_prefix is not None:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM files WHERE s3_prefix = ? AND is_deleted = 0",
                (s3_prefix,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM files WHERE is_deleted = 0"
            ).fetchone()
        return row[0] if row else 0

    def list_unindexed_files(
        self, limit: int = 25, s3_prefix: str | None = None
    ) -> list[dict]:
        """Return tracked non-deleted files that do not have an embedding row."""
        conditions = ["f.is_deleted = 0", "e.relative_path IS NULL"]
        params: list[object] = []
        if s3_prefix is not None:
            conditions.append("f.s3_prefix = ?")
            params.append(s3_prefix)
        params.append(limit)
        rows = self.conn.execute(
            "SELECT f.s3_prefix, f.relative_path, f.size_bytes "
            "FROM files f "
            "LEFT JOIN embeddings e "
            "ON e.s3_prefix = f.s3_prefix AND e.relative_path = f.relative_path "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY f.relative_path LIMIT ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def count_unindexed_by_extension(self, s3_prefix: str | None = None) -> dict[str, int]:
        """Return unindexed tracked files grouped by lowercase file extension."""
        conditions = ["f.is_deleted = 0", "e.relative_path IS NULL"]
        params: list[object] = []
        if s3_prefix is not None:
            conditions.append("f.s3_prefix = ?")
            params.append(s3_prefix)
        rows = self.conn.execute(
            "SELECT f.relative_path "
            "FROM files f "
            "LEFT JOIN embeddings e "
            "ON e.s3_prefix = f.s3_prefix AND e.relative_path = f.relative_path "
            f"WHERE {' AND '.join(conditions)}",
            params,
        ).fetchall()

        counts: dict[str, int] = {}
        for row in rows:
            suffix = Path(row["relative_path"]).suffix.lower() or "(none)"
            counts[suffix] = counts.get(suffix, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True))

    # ------------------------------------------------------------------
    # chunks table (chunked semantic search)
    # ------------------------------------------------------------------

    def upsert_chunk(
        self,
        storage_prefix: str,
        relative_path: str,
        chunk_index: int,
        content_hash: str,
        chunk_text: str,
    ) -> int:
        """Insert or replace a chunk row. Returns the row id."""
        sql = """
        INSERT INTO chunks
            (storage_prefix, relative_path, chunk_index, content_hash, chunk_text, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(storage_prefix, relative_path, chunk_index) DO UPDATE SET
            content_hash = excluded.content_hash,
            chunk_text   = excluded.chunk_text,
            indexed_at   = excluded.indexed_at
        RETURNING id
        """
        with self.transaction():
            row = self.conn.execute(
                sql,
                (
                    storage_prefix,
                    relative_path,
                    chunk_index,
                    content_hash,
                    chunk_text,
                    datetime.datetime.now(datetime.UTC).isoformat(),
                ),
            ).fetchone()
        return row[0]

    def delete_chunks_for_file(self, storage_prefix: str, relative_path: str) -> list[int]:
        """Delete all chunks for a file. Returns the deleted chunk ids."""
        rows = self.conn.execute(
            "SELECT id FROM chunks WHERE storage_prefix = ? AND relative_path = ?",
            (storage_prefix, relative_path),
        ).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            with self.transaction():
                self.conn.execute(
                    "DELETE FROM chunks WHERE storage_prefix = ? AND relative_path = ?",
                    (storage_prefix, relative_path),
                )
        return ids

    def get_chunk_content_hash(
        self, storage_prefix: str, relative_path: str
    ) -> str | None:
        """Return the content_hash of the first chunk for a file, or None."""
        row = self.conn.execute(
            "SELECT content_hash FROM chunks "
            "WHERE storage_prefix = ? AND relative_path = ? AND chunk_index = 0",
            (storage_prefix, relative_path),
        ).fetchone()
        return row[0] if row else None

    def get_chunk(self, chunk_id: int) -> dict | None:
        """Return one indexed chunk by id, or None."""
        row = self.conn.execute(
            "SELECT id, storage_prefix, relative_path, chunk_index, content_hash, "
            "chunk_text, indexed_at FROM chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
        return dict(row) if row else None

    def upsert_vec_chunk(self, chunk_id: int, embedding: bytes) -> None:
        """Insert or replace a vec0 row. embedding must be float32 bytes."""
        self.conn.execute(
            "INSERT OR REPLACE INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
            (chunk_id, embedding),
        )
        self.conn.commit()

    def delete_vec_chunks(self, chunk_ids: list[int]) -> None:
        """Delete vec0 rows by rowid."""
        if not chunk_ids:
            return
        placeholders = ",".join("?" * len(chunk_ids))
        with self.transaction():
            self.conn.execute(
                f"DELETE FROM vec_chunks WHERE rowid IN ({placeholders})", chunk_ids
            )

    def vec_knn_search(
        self,
        query_embedding: bytes,
        k: int,
        storage_prefix: str | None = None,
    ) -> list[dict]:
        """Run KNN search via sqlite-vec. Returns rows with chunk metadata + distance."""
        sql = """
            SELECT c.id, c.storage_prefix, c.relative_path, c.chunk_index,
                   c.chunk_text, c.content_hash, v.distance
            FROM vec_chunks v
            JOIN chunks c ON c.id = v.rowid
            WHERE v.embedding MATCH ?
              AND k = ?
            ORDER BY v.distance
        """
        rows = self.conn.execute(sql, (query_embedding, k)).fetchall()
        results = [dict(r) for r in rows]
        if storage_prefix is not None:
            results = [r for r in results if r["storage_prefix"] == storage_prefix]
        return results

    def has_vec_table(self) -> bool:
        """Return True if the vec_chunks virtual table exists."""
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'vec_chunks'"
        ).fetchone()
        return row is not None

    def count_chunks(self, storage_prefix: str | None = None) -> int:
        """Return the total number of indexed chunks."""
        if storage_prefix is not None:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE storage_prefix = ?",
                (storage_prefix,),
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
        return row[0] if row else 0

    def latest_chunk_indexed_at(self, storage_prefix: str | None = None) -> str | None:
        """Return the latest chunk indexed_at timestamp, optionally scoped by prefix."""
        if storage_prefix is not None:
            row = self.conn.execute(
                "SELECT MAX(indexed_at) FROM chunks WHERE storage_prefix = ?",
                (storage_prefix,),
            ).fetchone()
        else:
            row = self.conn.execute("SELECT MAX(indexed_at) FROM chunks").fetchone()
        return row[0] if row and row[0] else None
