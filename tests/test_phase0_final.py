"""Final coverage push for Phase 0 — CLI error paths and DB migration."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import numpy as np
from click.testing import CliRunner

from sahara.cli import main
from sahara.storage.state_db import StateDB


def _write_config(tmp_path: Path, *, storage_mode: str = "local") -> tuple[Path, Path]:
    drive = tmp_path / "drive"
    drive.mkdir(exist_ok=True)
    sync = tmp_path / "sync"
    sync.mkdir(exist_ok=True)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'storage_mode = "{storage_mode}"\n'
        f'sync_folder = "{sync}"\n'
        f'drive_paths = ["{drive}"]\n'
    )
    return cfg, sync


# ---------------------------------------------------------------------------
# StateDB: _migrate_v2 on old schema
# ---------------------------------------------------------------------------


class TestStateDBOldSchemaMigration:
    def _create_old_db(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE files (
                relative_path TEXT PRIMARY KEY,
                sha256_checksum TEXT NOT NULL DEFAULT '',
                size_bytes INTEGER NOT NULL DEFAULT 0,
                tier TEXT NOT NULL DEFAULT 'STANDARD',
                s3_etag TEXT NOT NULL DEFAULT '',
                last_sync_at TEXT NOT NULL DEFAULT '',
                local_modified_at TEXT NOT NULL DEFAULT '',
                remote_modified_at TEXT NOT NULL DEFAULT '',
                archived_at TEXT,
                restore_job_id TEXT,
                restore_expires_at TEXT,
                is_deleted INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                relative_path TEXT NOT NULL,
                operation TEXT NOT NULL,
                sha256 TEXT,
                size_bytes INTEGER,
                tier TEXT,
                occurred_at TEXT NOT NULL DEFAULT '',
                details TEXT
            );
            CREATE TABLE pending_multipart (
                relative_path TEXT PRIMARY KEY,
                s3_key TEXT NOT NULL DEFAULT '',
                upload_id TEXT NOT NULL DEFAULT '',
                file_sha256 TEXT NOT NULL DEFAULT '',
                size_bytes INTEGER NOT NULL DEFAULT 0,
                parts_json TEXT NOT NULL DEFAULT '[]',
                started_at TEXT NOT NULL DEFAULT ''
            );
        """)
        conn.commit()
        conn.close()

    def test_migrate_v2_upgrades_old_schema(self, tmp_path):
        """Covers lines 143-229: full _migrate_v2 path on old DB."""
        db_path = tmp_path / "old.db"
        self._create_old_db(db_path)

        db = StateDB(db_path).connect()
        try:
            cols = {r[1] for r in db.conn.execute("PRAGMA table_info(files)").fetchall()}
            assert "s3_prefix" in cols
            tables = {r[0] for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "chunks" in tables
            assert "embeddings" in tables
        finally:
            db.close()

    def test_migrate_v2_with_existing_data(self, tmp_path):
        """Old DB with data rows is migrated without data loss."""
        db_path = tmp_path / "old.db"
        self._create_old_db(db_path)

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO files VALUES ('doc.txt','abc',100,'STANDARD','','2024-01-01','2024-01-01','2024-01-01',NULL,NULL,NULL,0)"
        )
        conn.execute(
            "INSERT INTO history (relative_path, operation, occurred_at) VALUES ('doc.txt','upload','2024-01-01')"
        )
        conn.commit()
        conn.close()

        db = StateDB(db_path).connect()
        try:
            files = db.list_files(s3_prefix="")
            assert len(files) == 1
            assert files[0].relative_path == "doc.txt"
            history = db.get_history()
            assert len(history) >= 1
        finally:
            db.close()

    def test_connect_on_fresh_db_skips_v2(self, tmp_path):
        """Fresh DB: _migrate_v2 must do nothing (s3_prefix already exists)."""
        db = StateDB(tmp_path / "fresh.db").connect()
        try:
            cols = {r[1] for r in db.conn.execute("PRAGMA table_info(files)").fetchall()}
            assert "s3_prefix" in cols
        finally:
            db.close()


# ---------------------------------------------------------------------------
# CLI — index command error paths
# ---------------------------------------------------------------------------


class TestIndexCLIErrorPaths:
    def test_index_folder_not_found_aborts(self, tmp_path):
        """Covers lines 1591-1594: unknown folder in --folder flag."""
        cfg, _ = _write_config(tmp_path)
        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", tmp_path / "state.db"):
            result = runner.invoke(
                main,
                ["--config", str(cfg), "index", "--folder", "/nonexistent/path"],
            )
        assert result.exit_code != 0
        assert "not a registered" in result.output.lower()

    def test_index_error_during_indexing(self, tmp_path):
        """Covers lines 1628-1632: exception during index_file."""
        cfg, sync = _write_config(tmp_path)
        db_path = tmp_path / "state.db"

        import datetime

        from sahara.models import FileRecord

        db = StateDB(db_path).connect()
        db.upsert_file(
            FileRecord(
                relative_path="error.txt",
                sha256_checksum="abc",
                size_bytes=10,
                tier="STANDARD",
                s3_etag="abc",
                last_sync_at=datetime.datetime.now(datetime.UTC),
                local_modified_at=datetime.datetime.now(datetime.UTC),
                remote_modified_at=datetime.datetime.now(datetime.UTC),
            ),
            s3_prefix="",
        )
        db.close()
        (sync / "error.txt").write_text("some content")

        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", db_path), \
             patch(
                 "sahara.search.search_engine.SearchEngine.index_file_with_result",
                 side_effect=Exception("embedding failure"),
             ):
            result = runner.invoke(main, ["--config", str(cfg), "index"])

        assert result.exit_code == 0
        assert "Done" in result.output  # still reports completion


# ---------------------------------------------------------------------------
# CLI — search command error paths
# ---------------------------------------------------------------------------


class TestSearchCLIErrorPaths:
    def test_search_folder_not_found_aborts(self, tmp_path):
        """Covers lines 1673-1680: unknown folder in --folder flag."""
        cfg, _ = _write_config(tmp_path)
        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", tmp_path / "state.db"):
            result = runner.invoke(
                main,
                ["--config", str(cfg), "search", "--folder", "/bad/path", "query"],
            )
        assert result.exit_code != 0
        assert "not a registered" in result.output.lower()

    def test_search_no_results(self, tmp_path):
        """Covers lines 1691-1692: search returns empty results."""
        cfg, _ = _write_config(tmp_path)
        db_path = tmp_path / "state.db"

        db = StateDB(db_path).connect()
        db.upsert_embedding("", "doc.txt", "h", json.dumps([0.0] * 384), "snippet")
        db.close()

        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", db_path), \
             patch(
                 "sahara.search.search_engine.SearchEngine.search",
                 return_value=[],
             ):
            result = runner.invoke(main, ["--config", str(cfg), "search", "anything"])

        assert result.exit_code == 0
        assert "No results" in result.output


# ---------------------------------------------------------------------------
# CLI — ask command error paths
# ---------------------------------------------------------------------------


class TestAskCLIErrorPaths:
    def test_ask_folder_filter(self, tmp_path):
        """Covers lines 1768-1775: ask with valid folder filter."""
        cfg, sync = _write_config(tmp_path)
        db_path = tmp_path / "state.db"

        # Register the primary folder as a sync target is implicit;
        # just verify ask finds it when --folder points to sync dir
        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", db_path):
            result = runner.invoke(
                main,
                ["--config", str(cfg), "ask", "--folder", str(sync), "anything"],
            )
        # No index yet — should warn but not crash
        assert result.exit_code == 0

    def test_ask_folder_not_found_aborts(self, tmp_path):
        """ask with unknown --folder aborts with error."""
        cfg, _ = _write_config(tmp_path)
        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", tmp_path / "state.db"):
            result = runner.invoke(
                main,
                ["--config", str(cfg), "ask", "--folder", "/no/such/folder", "test"],
            )
        assert result.exit_code != 0
        assert "not a registered" in result.output.lower()

    def test_ask_degraded_no_results(self, tmp_path):
        """Covers lines 1796-1798: ask with degraded result (no answer, no sources)."""
        cfg, _ = _write_config(tmp_path)
        db_path = tmp_path / "state.db"

        db = StateDB(db_path).connect()
        db.upsert_embedding("", "doc.txt", "h", json.dumps([0.5] * 384), "snippet")
        db.close()

        from sahara.search.ask_engine import AskResult

        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", db_path), \
             patch(
                 "sahara.search.ask_engine.AskEngine.ask",
                 return_value=AskResult(
                     answer=None,
                     sources=[],
                     degraded=True,
                     error="Ollama not available",
                 ),
             ), \
             patch("sahara.search.search_engine.SearchEngine._embed") as m:
            m.return_value = [np.array([0.5] * 384, dtype=np.float32)]
            result = runner.invoke(main, ["--config", str(cfg), "ask", "test question"])

        assert result.exit_code == 0
        assert "Ollama not available" in result.output or "index" in result.output.lower()

    def test_ask_with_model_and_url_flags(self, tmp_path):
        """Covers --model and --ollama-url flag paths."""
        cfg, _ = _write_config(tmp_path)
        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", tmp_path / "state.db"):
            result = runner.invoke(
                main,
                [
                    "--config", str(cfg), "ask",
                    "--model", "llama3",
                    "--ollama-url", "http://localhost:11434",
                    "test question",
                ],
            )
        # No indexed files — warns to index
        assert result.exit_code == 0
