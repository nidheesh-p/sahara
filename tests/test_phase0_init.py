"""Phase 0 coverage: init wizard, index/search/ask remaining paths."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
from click.testing import CliRunner

from sahara.cli import main
from sahara.models import FileRecord
from sahara.storage.state_db import StateDB


def _write_config(tmp_path: Path) -> tuple[Path, Path]:
    drive = tmp_path / "drive"
    drive.mkdir(exist_ok=True)
    sync = tmp_path / "sync"
    sync.mkdir(exist_ok=True)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'storage_mode = "local"\n'
        f'sync_folder = "{sync}"\n'
        f'drive_paths = ["{drive}"]\n'
    )
    return cfg, sync


def _insert_file(db_path: Path, relative_path: str, sync_dir: Path) -> None:
    db = StateDB(db_path).connect()
    db.upsert_file(
        FileRecord(
            relative_path=relative_path,
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


# ---------------------------------------------------------------------------
# Init wizard — local mode
# ---------------------------------------------------------------------------


class TestInitWizardLocal:
    def test_init_local_mode_basic(self, tmp_path):
        """Covers lines 167-226, 298-344: local mode init wizard."""
        drive = tmp_path / "drive"
        drive.mkdir()
        sync = tmp_path / "sync"
        cfg_out = tmp_path / "config.toml"

        # Input sequence: sync_folder, backend=local, drive_path, empty (done),
        # no encryption, conflict=backup, no upload-only
        init_input = "\n".join([
            str(sync),           # sync folder
            "local",             # backend choice
            str(drive),          # drive path 1
            "",                  # done (empty = finish drive paths)
            "n",                 # no encryption
            "backup",            # conflict strategy
            "n",                 # no upload-only
            "",                  # any trailing newline
        ])

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--config", str(cfg_out), "init"],
            input=init_input,
        )
        # Drive is accessible since we created it
        assert result.exit_code == 0
        assert cfg_out.exists()
        content = cfg_out.read_text()
        assert "local" in content

    def test_init_local_mode_multiple_drives(self, tmp_path):
        """Covers lines 204-218: multiple drive path entries."""
        drive1 = tmp_path / "drive1"
        drive1.mkdir()
        drive2 = tmp_path / "drive2"
        drive2.mkdir()
        sync = tmp_path / "sync"
        cfg_out = tmp_path / "config.toml"

        init_input = "\n".join([
            str(sync),    # sync folder
            "local",      # backend
            str(drive1),  # drive 1
            str(drive2),  # drive 2
            "",           # done
            "n",          # no encryption
            "backup",     # conflict strategy
            "n",          # no upload-only
        ])

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--config", str(cfg_out), "init"],
            input=init_input,
        )
        assert result.exit_code == 0
        content = cfg_out.read_text()
        assert str(drive1) in content
        assert str(drive2) in content

    def test_init_local_mode_empty_drive_then_retry(self, tmp_path):
        """Covers lines 212-215: empty drive path with retry (at least one required)."""
        drive = tmp_path / "drive"
        drive.mkdir()
        sync = tmp_path / "sync"
        cfg_out = tmp_path / "config.toml"

        init_input = "\n".join([
            str(sync),    # sync folder
            "local",      # backend
            "",           # empty first — triggers "at least one required" warning
            str(drive),   # then provide one
            "",           # done
            "n",          # no encryption
            "backup",
            "n",
        ])

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--config", str(cfg_out), "init"],
            input=init_input,
        )
        assert result.exit_code == 0

    def test_init_encryption_enabled(self, tmp_path):
        """Covers lines 301-307: encryption passphrase entry."""
        drive = tmp_path / "drive"
        drive.mkdir()
        sync = tmp_path / "sync"
        cfg_out = tmp_path / "config.toml"

        init_input = "\n".join([
            str(sync),
            "local",
            str(drive),
            "",
            "y",                   # enable encryption
            "mysecretpassphrase",  # passphrase
            "mysecretpassphrase",  # confirm
            "backup",
            "n",
        ])

        runner = CliRunner()
        with patch("sahara.utils.encryption.set_passphrase"):
            result = runner.invoke(
                main,
                ["--config", str(cfg_out), "init"],
                input=init_input,
            )
        assert result.exit_code == 0

    def test_init_upload_only_enabled(self, tmp_path):
        """Covers lines 321-322: upload-only mode enabled."""
        drive = tmp_path / "drive"
        drive.mkdir()
        sync = tmp_path / "sync"
        cfg_out = tmp_path / "config.toml"

        init_input = "\n".join([
            str(sync),
            "local",
            str(drive),
            "",
            "n",
            "backup",
            "y",    # upload-only
        ])

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--config", str(cfg_out), "init"],
            input=init_input,
        )
        assert result.exit_code == 0
        content = cfg_out.read_text()
        assert "upload_only" in content


# ---------------------------------------------------------------------------
# Index: unchanged file skips (lines 1628-1629)
# ---------------------------------------------------------------------------


class TestIndexUnchangedFile:
    def test_index_unchanged_file_shows_dash(self, tmp_path):
        """Covers lines 1628-1629: file hasn't changed → shows '–' (skipped)."""
        cfg, sync = _write_config(tmp_path)
        db_path = tmp_path / "state.db"

        (sync / "note.txt").write_text("Stable content that won't change")
        _insert_file(db_path, "note.txt", sync)

        # Pre-index so content hash is stored
        db = StateDB(db_path).connect()
        import json
        db.upsert_embedding("", "note.txt", "somehash", json.dumps([0.1] * 384), "snippet")
        db.close()

        runner = CliRunner()
        from sahara.search.search_engine import IndexFileResult

        with patch("sahara.storage.state_db.DB_PATH", db_path), \
             patch(
                 "sahara.search.search_engine.SearchEngine.index_file_with_result",
                 return_value=IndexFileResult(indexed=False, reason="unchanged"),
             ):
            result = runner.invoke(main, ["--config", str(cfg), "index"])

        assert result.exit_code == 0
        assert "Done" in result.output
        assert "[unchanged]" in result.output


# ---------------------------------------------------------------------------
# Search: valid --folder filter (lines 1676, 1680)
# ---------------------------------------------------------------------------


class TestSearchWithFolderFilter:
    def test_search_with_valid_folder_filter(self, tmp_path):
        """Covers lines 1676, 1680: search with --folder matching sync dir."""
        cfg, sync = _write_config(tmp_path)
        db_path = tmp_path / "state.db"

        db = StateDB(db_path).connect()
        db.upsert_embedding("", "doc.txt", "h", json.dumps([0.5] * 384), "snippet")
        db.close()

        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", db_path), \
             patch("sahara.search.search_engine.SearchEngine._embed") as mock_embed:
            mock_embed.return_value = [np.array([0.5] * 384, dtype=np.float32)]
            result = runner.invoke(
                main,
                ["--config", str(cfg), "search", "--folder", str(sync), "test query"],
            )
        # Should not abort — sync dir is a valid folder
        assert "not a registered" not in result.output


# ---------------------------------------------------------------------------
# Ask: valid --folder filter (line 1771)
# ---------------------------------------------------------------------------


class TestAskWithFolderFilter:
    def test_ask_with_valid_folder_filter(self, tmp_path):
        """Covers line 1771: ask with --folder matching sync dir."""
        cfg, sync = _write_config(tmp_path)
        db_path = tmp_path / "state.db"

        db = StateDB(db_path).connect()
        db.upsert_embedding("", "note.txt", "h", json.dumps([0.5] * 384), "snippet text")
        db.close()

        fake_resp = json.dumps({"response": "The answer is 42."}).encode()

        class FakeResp:
            def read(self):
                return fake_resp
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", db_path), \
             patch("sahara.search.search_engine.SearchEngine._embed") as mock_embed, \
             patch("urllib.request.urlopen", return_value=FakeResp()):
            mock_embed.return_value = [np.array([0.5] * 384, dtype=np.float32)]
            result = runner.invoke(
                main,
                ["--config", str(cfg), "ask", "--folder", str(sync), "what is the answer?"],
            )
        assert "not a registered" not in result.output
        assert result.exit_code == 0
