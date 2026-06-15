"""Tests for Sahara's index-first, optional-storage product model."""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from sahara.cli import main
from sahara.config import SaharaConfig, load_config
from sahara.library import IndexingService, ensure_content_roots
from sahara.models import FileRecord, ManifestEntry
from sahara.search.search_engine import IndexFileResult
from sahara.storage.lifecycle import StorageLifecycle
from sahara.storage.local_drive_client import LocalDriveClient
from sahara.storage.state_db import StateDB
from sahara.sync.ignore_rules import IgnoreRules
from sahara.sync.sync_engine import SyncEngine
from sahara.utils.hash import compute_sha256


def test_fresh_config_defaults_to_index_only() -> None:
    config = SaharaConfig()

    assert config.storage_mode == "none"
    assert config.is_index_only_mode is True
    assert config.has_storage_backend is False


def test_primary_content_root_follows_configured_folder_change(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    with StateDB(tmp_path / "state.db") as db:
        ensure_content_roots(
            SaharaConfig(sync_folder=str(first), storage_mode="none"),
            db,
        )
        roots = ensure_content_roots(
            SaharaConfig(sync_folder=str(second), storage_mode="none"),
            db,
        )

    primary = next(root for root in roots if root.is_primary)
    assert primary.local_path == second.resolve()
    assert primary.storage_prefix == ""


def test_primary_content_root_change_rolls_back_index_on_failure(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    with StateDB(tmp_path / "state.db") as db:
        ensure_content_roots(
            SaharaConfig(sync_folder=str(first), storage_mode="none"),
            db,
        )
        db.upsert_index_entry(
            "",
            "note.md",
            content_hash="abc",
            size_bytes=3,
            modified_ns=1,
            status="indexed",
        )
        db.conn.execute(
            """
            CREATE TRIGGER fail_primary_replacement
            BEFORE DELETE ON content_roots
            WHEN OLD.is_primary = 1
            BEGIN
                SELECT RAISE(ABORT, 'simulated primary replacement failure');
            END
            """
        )
        db.conn.commit()

        with pytest.raises(
            sqlite3.IntegrityError,
            match="simulated primary replacement failure",
        ):
            ensure_content_roots(
                SaharaConfig(sync_folder=str(second), storage_mode="none"),
                db,
            )

        primary = next(root for root in db.list_content_roots() if root["is_primary"])
        assert Path(primary["local_path"]) == first.resolve()
        entries = db.list_index_entries(storage_prefix="")
        assert [entry["relative_path"] for entry in entries] == ["note.md"]


def test_primary_content_root_change_is_blocked_with_storage(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    drive = tmp_path / "drive"
    first.mkdir()
    second.mkdir()
    drive.mkdir()

    with StateDB(tmp_path / "state.db") as db:
        ensure_content_roots(
            SaharaConfig(sync_folder=str(first), storage_mode="none"),
            db,
        )
        with pytest.raises(ValueError, match="explicit migration"):
            ensure_content_roots(
                SaharaConfig(
                    sync_folder=str(second),
                    storage_mode="local",
                    drive_paths=[str(drive)],
                ),
                db,
            )

        primary = next(root for root in db.list_content_roots() if root["is_primary"])
        assert Path(primary["local_path"]) == first.resolve()


def test_existing_additional_root_can_be_promoted_to_primary(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    with StateDB(tmp_path / "state.db") as db:
        ensure_content_roots(
            SaharaConfig(sync_folder=str(first), storage_mode="none"),
            db,
        )
        db.upsert_content_root(str(second), "second", sync_enabled=False)
        roots = ensure_content_roots(
            SaharaConfig(sync_folder=str(second), storage_mode="none"),
            db,
        )

    assert len(roots) == 1
    assert roots[0].local_path == second.resolve()
    assert roots[0].storage_prefix == ""
    assert roots[0].is_primary is True


def test_additional_root_with_storage_ownership_cannot_be_promoted(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    now = datetime.datetime.now(datetime.UTC)

    with StateDB(tmp_path / "state.db") as db:
        ensure_content_roots(
            SaharaConfig(sync_folder=str(first), storage_mode="none"),
            db,
        )
        db.upsert_content_root(str(second), "archive", sync_enabled=False)
        db.upsert_file(
            FileRecord(
                relative_path="retained.txt",
                sha256_checksum="abc",
                size_bytes=3,
                tier="STANDARD",
                s3_etag="etag",
                last_sync_at=now,
                local_modified_at=now,
                remote_modified_at=now,
            ),
            s3_prefix="archive",
        )

        with pytest.raises(ValueError, match="explicit storage migration"):
            ensure_content_roots(
                SaharaConfig(sync_folder=str(second), storage_mode="none"),
                db,
            )

        roots = db.list_content_roots()
        primary = next(root for root in roots if root["is_primary"])
        additional = next(root for root in roots if not root["is_primary"])
        assert Path(primary["local_path"]) == first.resolve()
        assert additional["storage_prefix"] == "archive"


def test_primary_replacement_rejects_overlap_with_additional_root(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    additional = tmp_path / "additional"
    nested = additional / "nested"
    first.mkdir()
    nested.mkdir(parents=True)

    with StateDB(tmp_path / "state.db") as db:
        ensure_content_roots(
            SaharaConfig(sync_folder=str(first), storage_mode="none"),
            db,
        )
        db.upsert_content_root(str(additional), "additional", sync_enabled=False)
        with pytest.raises(ValueError, match="overlaps registered root"):
            ensure_content_roots(
                SaharaConfig(sync_folder=str(nested), storage_mode="none"),
                db,
            )


def test_legacy_config_without_storage_mode_remains_s3(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'sync_folder = "/tmp/sahara"\n'
        'bucket = "existing-bucket"\n',
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.storage_mode == "s3"
    assert config.has_storage_backend is True


def test_basic_init_is_non_interactive(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    content = tmp_path / "documents"

    result = CliRunner().invoke(
        main,
        [
            "--config",
            str(config_path),
            "init",
            "--mode",
            "basic",
            "--folder",
            str(content),
        ],
    )

    assert result.exit_code == 0
    assert "basic mode" in result.output
    assert content.is_dir()
    config = load_config(config_path)
    assert config.storage_mode == "none"
    assert config.get_sync_folder_path() == content.resolve()


def test_basic_mode_rejects_sync_with_upgrade_message(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    content = tmp_path / "documents"
    content.mkdir()
    config_path.write_text(
        f'sync_folder = "{content}"\n'
        'storage_mode = "none"\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main, ["--config", str(config_path), "sync"]
    )

    assert result.exit_code != 0
    assert "No storage backend is configured" in result.output


def test_content_roots_migrate_primary_and_sync_targets(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    additional = tmp_path / "additional"
    primary.mkdir()
    additional.mkdir()
    config = SaharaConfig(
        sync_folder=str(primary),
        storage_mode="local",
        drive_paths=[str(tmp_path / "drive")],
    )

    with StateDB(tmp_path / "state.db") as db:
        db.add_sync_target(str(additional), "additional")

        roots = ensure_content_roots(config, db)

        assert [(root.storage_prefix, root.sync_enabled) for root in roots] == [
            ("", True),
            ("additional", True),
        ]
        assert roots[0].is_primary is True


def test_index_inventory_backfill_is_marked_complete(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"

    with StateDB(db_path) as db:
        assert db.get_config_value("schema_v4_backfilled") == "1"

    with StateDB(db_path) as db:
        assert db.get_config_value("schema_v4_backfilled") == "1"


def test_indexing_scans_content_root_without_sync_records(tmp_path: Path) -> None:
    content = tmp_path / "documents"
    content.mkdir()
    (content / "note.txt").write_text("A known phrase", encoding="utf-8")
    config = SaharaConfig(sync_folder=str(content), storage_mode="none")

    with StateDB(tmp_path / "state.db") as db:
        service = IndexingService(config, db)
        with patch.object(
            service._search,
            "index_file_with_result",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ):
            result = service.index()

        assert result.indexed == 1
        entries = db.list_index_entries()
        assert len(entries) == 1
        assert entries[0]["relative_path"] == "note.txt"
        assert entries[0]["status"] == "indexed"
        assert db.count_tracked_files() == 0


def test_indexing_removes_search_data_for_deleted_file(tmp_path: Path) -> None:
    content = tmp_path / "documents"
    content.mkdir()
    config = SaharaConfig(sync_folder=str(content), storage_mode="none")

    with StateDB(tmp_path / "state.db") as db:
        ensure_content_roots(config, db)
        db.upsert_embedding("", "gone.txt", "hash", "[0.1]", "gone")
        db.upsert_chunk("", "gone.txt", 0, "hash", "gone")
        db.upsert_index_entry(
            "",
            "gone.txt",
            content_hash="hash",
            size_bytes=4,
            modified_ns=1,
            status="indexed",
        )

        result = IndexingService(config, db).index()

        assert result.missing == 1
        assert db.count_embeddings() == 0
        assert db.count_chunks() == 0
        assert db.list_index_entries()[0]["status"] == "missing"


def test_basic_index_command_does_not_require_storage_records(
    tmp_path: Path,
) -> None:
    content = tmp_path / "documents"
    content.mkdir()
    (content / "note.txt").write_text("A known phrase", encoding="utf-8")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'sync_folder = "{content}"\n'
        'storage_mode = "none"\n',
        encoding="utf-8",
    )
    db_path = tmp_path / "state.db"

    with (
        patch("sahara.storage.state_db.DB_PATH", db_path),
        patch(
            "sahara.search.search_engine.SearchEngine.index_file_with_result",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ),
    ):
        result = CliRunner().invoke(
            main, ["--config", str(config_path), "index"]
        )

    assert result.exit_code == 0
    assert "1 indexed" in result.output
    assert "First use may download" in result.output
    assert "authentication is optional" in result.output


def test_basic_mode_can_add_an_index_only_folder(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    additional = tmp_path / "additional"
    primary.mkdir()
    additional.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'sync_folder = "{primary}"\n'
        'storage_mode = "none"\n',
        encoding="utf-8",
    )
    db_path = tmp_path / "state.db"

    with patch("sahara.storage.state_db.DB_PATH", db_path):
        result = CliRunner().invoke(
            main,
            [
                "--config",
                str(config_path),
                "folder",
                "add",
                str(additional),
            ],
        )

    assert result.exit_code == 0
    assert "index only" in result.output
    with StateDB(db_path) as db:
        root = db.get_content_root(str(additional))
        assert root is not None
        assert root["sync_enabled"] is False


def test_enabling_folder_sync_requires_storage(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'sync_folder = "{primary}"\n'
        'storage_mode = "none"\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        [
            "--config",
            str(config_path),
            "folder",
            "sync",
            str(primary),
            "--enable",
        ],
    )

    assert result.exit_code != 0
    assert "No storage backend is configured" in result.output


def test_basic_library_can_add_local_storage_without_rebuilding_index(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    drive = tmp_path / "drive"
    primary.mkdir()
    drive.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'sync_folder = "{primary}"\n'
        'storage_mode = "none"\n',
        encoding="utf-8",
    )

    with patch(
        "sahara.storage.local_drive_client.LocalDriveClient.validate_bucket_access"
    ):
        result = CliRunner().invoke(
            main,
            [
                "--config",
                str(config_path),
                "storage",
                "configure",
                "local",
                "--drive",
                str(drive),
            ],
        )

    assert result.exit_code == 0
    config = load_config(config_path)
    assert config.storage_mode == "local"
    assert config.drive_paths == [str(drive.resolve())]
    assert "remain index-only" in result.output


def test_storage_configuration_is_not_saved_when_validation_fails(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'sync_folder = "{primary}"\n'
        'storage_mode = "none"\n',
        encoding="utf-8",
    )

    with patch(
        "sahara.storage.local_drive_client.LocalDriveClient.validate_bucket_access",
        side_effect=RuntimeError("drive unavailable"),
    ):
        result = CliRunner().invoke(
            main,
            [
                "--config",
                str(config_path),
                "storage",
                "configure",
                "local",
                "--drive",
                str(tmp_path / "missing-drive"),
            ],
        )

    assert result.exit_code != 0
    assert load_config(config_path).storage_mode == "none"


def _prepare_offload(
    tmp_path: Path,
) -> tuple[SaharaConfig, StateDB, Path, str]:
    primary = tmp_path / "primary"
    drive = tmp_path / "drive"
    primary.mkdir()
    drive.mkdir()
    local_file = primary / "notes.txt"
    local_file.write_text("searchable offload content", encoding="utf-8")
    sha256 = compute_sha256(local_file)
    config = SaharaConfig(
        sync_folder=str(primary),
        storage_mode="local",
        drive_paths=[str(drive)],
    )
    db = StateDB(tmp_path / "state.db").connect()
    ensure_content_roots(config, db)
    LocalDriveClient(config).upload_file(local_file, "notes.txt")
    now = datetime.datetime.now(datetime.UTC)
    db.upsert_file(
        FileRecord(
            relative_path="notes.txt",
            sha256_checksum=sha256,
            size_bytes=local_file.stat().st_size,
            tier="STANDARD",
            s3_etag=sha256,
            last_sync_at=now,
            local_modified_at=now,
            remote_modified_at=now,
        )
    )
    db.upsert_chunk("", "notes.txt", 0, sha256, "searchable offload content")
    db.upsert_embedding("", "notes.txt", sha256, "[0.1]", "searchable")
    db.upsert_index_entry(
        "",
        "notes.txt",
        content_hash=sha256,
        size_bytes=local_file.stat().st_size,
        modified_ns=local_file.stat().st_mtime_ns,
        status="indexed",
    )
    return config, db, local_file, sha256


def test_offload_and_fetch_preserve_search_index(tmp_path: Path) -> None:
    config, db, local_file, sha256 = _prepare_offload(tmp_path)
    try:
        lifecycle = StorageLifecycle(config, db, LocalDriveClient(config))

        lifecycle.offload("notes.txt")

        assert not local_file.exists()
        assert db.count_embeddings() == 1
        assert db.count_chunks() == 1
        assert db.get_storage_residency("", "notes.txt")["local_state"] == "offloaded"
        assert db.list_index_entries()[0]["status"] == "offloaded"

        lifecycle.fetch("notes.txt")

        assert local_file.exists()
        assert compute_sha256(local_file) == sha256
        assert db.get_storage_residency("", "notes.txt")["local_state"] == "present"
        assert db.list_index_entries()[0]["status"] == "indexed"
    finally:
        db.close()


def test_offload_keeps_source_when_remote_checksum_fails(tmp_path: Path) -> None:
    config, db, local_file, _ = _prepare_offload(tmp_path)
    backend = MagicMock()
    backend.download_file.return_value = "wrong-checksum"
    try:
        lifecycle = StorageLifecycle(config, db, backend)

        try:
            lifecycle.offload("notes.txt")
        except ValueError as exc:
            assert "checksum" in str(exc)
        else:
            raise AssertionError("offload should reject a checksum mismatch")

        assert local_file.exists()
        assert db.get_storage_residency("", "notes.txt") is None
    finally:
        db.close()


def test_sync_does_not_delete_intentionally_offloaded_file(
    tmp_path: Path,
) -> None:
    config, db, local_file, sha256 = _prepare_offload(tmp_path)
    try:
        db.set_storage_residency(
            "",
            "notes.txt",
            local_state="offloaded",
            remote_state="present",
        )
        local_file.unlink()
        engine = SyncEngine(
            config,
            db,
            MagicMock(),
            IgnoreRules(config.get_sync_folder_path()),
        )
        record = db.get_file("notes.txt")
        assert record is not None
        manifest = {
            "notes.txt": ManifestEntry(
                sha256=sha256,
                size=record.size_bytes,
                modified_at=record.remote_modified_at.isoformat(),
                tier="STANDARD",
                etag=sha256,
            )
        }

        diff = engine._three_way_diff({}, manifest, {"notes.txt": record})

        assert diff.local_deleted == []
    finally:
        db.close()
