"""Tests for durable captured knowledge."""

from __future__ import annotations

import os
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from click.testing import CliRunner

from sahara.cli import main
from sahara.config import SaharaConfig, load_config, save_config
from sahara.library import (
    ContentRoot,
    IndexingService,
    ensure_content_roots,
    register_content_root,
    unregister_content_root,
    validate_content_root_path,
    validate_storage_prefix,
)
from sahara.memory import CaptureRequest, MemoryService
from sahara.memory.format import (
    classify_memory_document,
    parse_document,
    render_document,
    searchable_text,
)
from sahara.models import FileRecord
from sahara.search.search_engine import IndexFileResult, SearchEngine
from sahara.storage.state_db import StateDB


def _config(tmp_path: Path, *, memory_folder: Path | None = None) -> SaharaConfig:
    primary = tmp_path / "primary"
    primary.mkdir(exist_ok=True)
    return SaharaConfig(
        sync_folder=str(primary),
        storage_mode="none",
        memory_folder=str(memory_folder or tmp_path / "memory"),
    )


def test_capture_writes_portable_markdown_and_registers_index_only_root(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    db_path = tmp_path / "state.db"

    with StateDB(db_path) as db:
        service = MemoryService(config, db)
        with patch.object(
            IndexingService,
            "index_path",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ):
            result = service.capture(
                CaptureRequest(
                    text="Vendor X uses net-30 terms.",
                    title="Vendor payment terms",
                    source_type="conversation",
                    source_url="https://example.com/conversation",
                    tags=("vendor", "finance"),
                )
            )

        assert result.indexed is True
        assert result.item.path.is_file()
        assert result.item.relative_path.startswith("20")
        assert result.item.relative_path.endswith("-vendor-payment-terms.md")
        assert result.item.path.read_text(encoding="utf-8").startswith("---\n")

        parsed = service.read(result.item.path)
        assert parsed.memory_id == result.item.memory_id
        assert parsed.text == "Vendor X uses net-30 terms."
        assert parsed.tags == ("vendor", "finance")

        root = db.get_content_root(str(service.root))
        assert root is not None
        assert root["storage_prefix"] == "memory"
        assert root["sync_enabled"] is False

    if os.name != "nt":
        assert result.item.path.stat().st_mode & 0o777 == 0o600
        assert service.root.stat().st_mode & 0o777 == 0o700
        assert result.item.path.parent.stat().st_mode & 0o777 == 0o700


def test_capture_preserves_markdown_whitespace(tmp_path: Path) -> None:
    config = _config(tmp_path)
    body = "    SELECT *\n    FROM users  \n"

    with StateDB(tmp_path / "state.db") as db:
        service = MemoryService(config, db)
        with patch.object(
            IndexingService,
            "index_path",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ):
            result = service.capture(CaptureRequest(text=body))

        assert service.read(result.item.path).text == body


def test_memory_format_round_trip_preserves_trailing_whitespace() -> None:
    metadata = {
        "schema_version": 1,
        "kind": "sahara_memory",
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "created_at": "2026-06-13T18:30:00Z",
        "updated_at": "2026-06-13T18:30:00Z",
        "title": "Whitespace",
        "source_type": "manual",
        "source_url": "",
        "source_id": "",
        "tags": [],
    }
    body = "  leading\nline with hard break  \n\n"

    _, parsed = parse_document(render_document(metadata, body))

    assert parsed == body


def test_content_root_validation_rejects_case_alias_of_same_directory(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "Notes"
    alias = tmp_path / "notes"
    existing.mkdir()
    root = ContentRoot(existing, "notes", False, False)

    def case_insensitive_samefile(first: Path, second: Path) -> bool:
        return str(first).casefold() == str(second).casefold()

    with patch.object(
        Path,
        "samefile",
        autospec=True,
        side_effect=case_insensitive_samefile,
    ):
        with pytest.raises(ValueError, match="already registered"):
            validate_content_root_path(alias, [root])


def test_capture_remains_saved_when_indexing_fails(tmp_path: Path) -> None:
    config = _config(tmp_path)

    with StateDB(tmp_path / "state.db") as db:
        service = MemoryService(config, db)
        with patch.object(
            IndexingService,
            "index_path",
            side_effect=RuntimeError("embedding model unavailable"),
        ):
            result = service.capture(CaptureRequest(text="Keep this even if indexing fails"))

        assert result.indexed is False
        assert result.index_reason == "pending"
        assert result.index_error == "embedding model unavailable"
        assert result.item.path.is_file()
        assert service.read(result.item.path).text == "Keep this even if indexing fails"
        entries = db.list_index_entries(storage_prefix="memory")
        assert len(entries) == 1
        assert entries[0]["status"] == "pending"
        assert entries[0]["reason"] == "embedding model unavailable"


def test_memory_search_text_excludes_operational_metadata(tmp_path: Path) -> None:
    config = _config(tmp_path)

    with StateDB(tmp_path / "state.db") as db:
        service = MemoryService(config, db)
        with patch.object(
            IndexingService,
            "index_path",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ):
            result = service.capture(
                CaptureRequest(
                    text="The body remains searchable.",
                    title="Searchable title",
                    source_url="https://secret.example/path",
                    source_id="private-thread-42",
                    tags=("useful",),
                )
            )

        indexed_text = searchable_text(result.item.path.read_text(encoding="utf-8"))

    assert indexed_text == (
        "Searchable title\n\nTags: useful\n\nThe body remains searchable."
    )
    assert result.item.memory_id not in indexed_text
    assert "secret.example" not in indexed_text
    assert "private-thread-42" not in indexed_text


def test_search_engine_indexes_only_searchable_memory_text(tmp_path: Path) -> None:
    config = _config(tmp_path)

    with StateDB(tmp_path / "state.db") as db:
        service = MemoryService(config, db)
        with patch.object(
            IndexingService,
            "index_path",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ):
            result = service.capture(
                CaptureRequest(
                    text="Body knowledge",
                    title="Useful title",
                    source_url="https://private.example/source",
                    tags=("research",),
                )
            )

        engine = SearchEngine(db)
        with patch.object(
            engine,
            "_embed",
            return_value=[np.zeros(384)],
        ):
            indexed = engine.index_file_with_result(
                result.item.path,
                "memory",
                result.item.relative_path,
            )

        assert indexed.indexed is True
        embedding = db.list_embeddings(s3_prefix="memory")[0]
        assert embedding["snippet"] == (
            "Useful title\n\nTags: research\n\nBody knowledge"
        )
        assert result.item.memory_id not in embedding["snippet"]
        assert "private.example" not in embedding["snippet"]


def test_search_engine_does_not_treat_prefix_as_memory_format(tmp_path: Path) -> None:
    note = tmp_path / "ordinary.md"
    note.write_text("Ordinary Markdown without frontmatter", encoding="utf-8")

    with StateDB(tmp_path / "state.db") as db:
        engine = SearchEngine(db)
        with patch.object(engine, "_embed", return_value=[np.zeros(384)]):
            indexed = engine.index_file_with_result(note, "memory", "ordinary.md")

        assert indexed.indexed is True
        assert db.list_embeddings(s3_prefix="memory")[0]["snippet"] == (
            "Ordinary Markdown without frontmatter"
        )


@pytest.mark.parametrize(
    ("document", "expected_snippet"),
    [
        (
            "---\r\nkind: \"sahara_memory\"\r\nschema_version: 1\r\n"
            "id: \"550e8400-e29b-41d4-a716-446655440000\"\r\n"
            "created_at: \"2026-06-13T18:30:00Z\"\r\n"
            "updated_at: \"2026-06-13T18:30:00Z\"\r\n"
            "title: \"Private\"\r\nsource_type: manual\r\n"
            "source_url: \"https://private.example\"\r\nsource_id: \"secret\"\r\n"
            "tags: []\r\n---\r\nVisible body\r\n",
            "Private\n\nVisible body",
        ),
        (
            "An ordinary body mentioning\nkind: sahara_memory\nwithout frontmatter.",
            "An ordinary body mentioning\nkind: sahara_memory\nwithout frontmatter.",
        ),
    ],
)
def test_memory_detection_handles_yaml_variants_without_metadata_leaks(
    tmp_path: Path,
    document: str,
    expected_snippet: str,
) -> None:
    note = tmp_path / "note.md"
    note.write_text(document, encoding="utf-8")

    with StateDB(tmp_path / "state.db") as db:
        engine = SearchEngine(db)
        with patch.object(engine, "_embed", return_value=[np.zeros(384)]):
            indexed = engine.index_file_with_result(note, "notes", "note.md")

        assert indexed.indexed is True
        snippet = db.list_embeddings(s3_prefix="notes")[0]["snippet"]

    assert snippet == expected_snippet
    assert "private.example" not in snippet
    assert "secret" not in snippet


@pytest.mark.parametrize(
    "document",
    [
        "---\nkind: sahara_memory\ninvalid: [\n---\nMalformed body\n",
        "---\nkind: sahara_memory\nschema_version: 1\n"
        "id: 550e8400-e29b-41d4-a716-446655440000\n"
        "created_at: 2026-06-13T18:30:00Z\n"
        "updated_at: 2026-06-13T18:30:00Z\n"
        "title: Private\nsource_type: manual\n"
        "source_url: file:///private\nsource_id: secret\ntags: []\n"
        "---\nVisible body\n",
    ],
)
def test_search_engine_fails_closed_for_invalid_claimed_memory(
    tmp_path: Path,
    document: str,
) -> None:
    note = tmp_path / "note.md"
    note.write_text(document, encoding="utf-8")

    with StateDB(tmp_path / "state.db") as db:
        db.upsert_embedding(
            "notes",
            "note.md",
            "old",
            "[]",
            "old private metadata",
        )
        db.upsert_chunk(
            "notes",
            "note.md",
            0,
            "old",
            "old private metadata",
        )
        engine = SearchEngine(db)
        indexed = engine.index_file_with_result(note, "notes", "note.md")

        assert indexed == IndexFileResult(
            indexed=False,
            reason="invalid_memory",
        )
        assert db.list_embeddings(s3_prefix="notes") == []
        assert db.count_chunks(storage_prefix="notes") == 0


def test_malformed_flow_memory_declaration_fails_closed(
    tmp_path: Path,
) -> None:
    note = tmp_path / "note.md"
    note.write_text(
        "---\n{kind: sahara_memory, source_url: https://private.example, "
        "source_id: secret, invalid: [}\n---\nVisible body\n",
        encoding="utf-8",
    )

    with StateDB(tmp_path / "state.db") as db:
        indexed = SearchEngine(db).index_file_with_result(
            note,
            "notes",
            "note.md",
        )

        assert indexed.reason == "invalid_memory"
        assert db.list_embeddings(s3_prefix="notes") == []


def test_tagged_malformed_memory_declaration_fails_closed(
    tmp_path: Path,
) -> None:
    note = tmp_path / "note.md"
    note.write_text(
        "---\nkind: !!str sahara_memory\nsource_url: https://private.example\n"
        "source_id: secret\ninvalid: [\n---\nVisible body\n",
        encoding="utf-8",
    )

    with StateDB(tmp_path / "state.db") as db:
        indexed = SearchEngine(db).index_file_with_result(
            note,
            "notes",
            "note.md",
        )

        assert indexed.reason == "invalid_memory"
        assert db.list_embeddings(s3_prefix="notes") == []


@pytest.mark.parametrize(
    "frontmatter",
    [
        "schema_version: 1\ntitle: Missing kind",
        "kind: sahara-memroy\nschema_version: 1\ntitle: Typo",
        "kind: 42\nschema_version: 1\ntitle: Wrong type",
    ],
)
def test_managed_memory_provenance_fails_closed_without_valid_kind(
    tmp_path: Path,
    frontmatter: str,
) -> None:
    note = tmp_path / "note.md"
    note.write_text(
        f"---\n{frontmatter}\nsource_url: https://private.example\n"
        "source_id: secret\n---\nVisible body\n",
        encoding="utf-8",
    )

    with StateDB(tmp_path / "state.db") as db:
        engine = SearchEngine(db)
        indexed = engine.index_file_with_result(
            note,
            "memory",
            "note.md",
            managed_memory=True,
        )

        assert indexed.reason == "invalid_memory"
        assert db.list_embeddings(s3_prefix="memory") == []


def test_indexing_service_marks_edited_managed_memory_invalid(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    with StateDB(tmp_path / "state.db") as db:
        service = MemoryService(config, db)
        with patch.object(
            IndexingService,
            "index_path",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ):
            result = service.capture(CaptureRequest(text="Private memory"))

        result.item.path.write_text(
            "---\nkind: sahara-memroy\nsource_url: https://private.example\n"
            "source_id: secret\n---\nVisible body\n",
            encoding="utf-8",
        )
        indexed = IndexingService(config, db).index_path(
            result.item.path,
            force=True,
        )

        assert indexed.reason == "invalid_memory"
        entry = db.list_index_entries(storage_prefix="memory")[0]
        assert entry["status"] == "invalid_memory"


def test_invalid_memory_marker_rejects_indexing_without_deleting_state(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    memory_root = Path(config.memory_folder)
    memory_root.mkdir()
    marker_dir = memory_root / ".sahara"
    marker_dir.mkdir()
    (marker_dir / "memory-root.json").write_text(
        '{"kind": "wrong"}',
        encoding="utf-8",
    )
    note = memory_root / "ordinary.md"
    note.write_text("Ordinary note", encoding="utf-8")

    with StateDB(tmp_path / "state.db") as db:
        db.upsert_content_root(str(memory_root), "memory", sync_enabled=False)
        db.upsert_embedding("memory", "ordinary.md", "old", "[]", "Ordinary note")

        with pytest.raises(ValueError, match="Invalid Sahara memory root marker"):
            IndexingService(config, db).index_path(note, force=True)

        assert db.list_embeddings(s3_prefix="memory")[0]["snippet"] == (
            "Ordinary note"
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", True, "schema version"),
        ("created_at", "not-a-date", "ISO timestamp"),
        ("updated_at", "2026-06-13T18:30:00", "timezone"),
        ("source_url", "file:///tmp/private", "absolute HTTP or HTTPS"),
        ("source_id", "x" * 257, "source ID is too long"),
        ("title", "x" * 161, "title is empty or too long"),
        ("tags", ["x" * 65], "tags are invalid"),
    ],
)
def test_memory_parser_rejects_invalid_external_metadata(
    field: str,
    value: object,
    message: str,
) -> None:
    metadata = {
        "schema_version": 1,
        "kind": "sahara_memory",
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "created_at": "2026-06-13T18:30:00Z",
        "updated_at": "2026-06-13T18:30:00Z",
        "title": "Valid title",
        "source_type": "manual",
        "source_url": "",
        "source_id": "",
        "tags": [],
    }
    metadata[field] = value

    with pytest.raises(ValueError, match=message):
        parse_document(render_document(metadata, "Valid body"))


def test_capture_uses_fallback_prefix_when_memory_prefix_already_exists(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    legacy = tmp_path / "legacy"
    legacy.mkdir()

    with StateDB(tmp_path / "state.db") as db:
        db.upsert_content_root(str(legacy), "memory", sync_enabled=False)
        service = MemoryService(config, db)
        with patch.object(
            IndexingService,
            "index_path",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ):
            service.capture(CaptureRequest(text="New managed memory"))

        root = db.get_content_root(str(service.root))
        assert root is not None
        assert root["storage_prefix"] == "memory-2"
        assert db.get_content_root(str(legacy))["storage_prefix"] == "memory"


@pytest.mark.parametrize("_attempt", range(5))
def test_concurrent_first_captures_share_root_initialization_lock(
    tmp_path: Path,
    _attempt: int,
) -> None:
    config = _config(tmp_path)
    db_path = tmp_path / "state.db"

    def capture(text: str) -> str:
        with StateDB(db_path) as db:
            return MemoryService(config, db).capture(
                CaptureRequest(text=text)
            ).item.memory_id

    with (
        patch.object(
            IndexingService,
            "index_path",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        ids = list(executor.map(capture, ("First", "Second")))

    assert len(set(ids)) == 2
    assert len(list(Path(config.memory_folder).rglob("*.md"))) == 2


def test_capture_rejects_symlinked_date_directory(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("symlink creation requires platform-specific privileges on Windows")
    config = _config(tmp_path)
    memory_root = Path(config.memory_folder)
    memory_root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    current_year = str(datetime.now(UTC).year)
    (memory_root / current_year).symlink_to(outside, target_is_directory=True)

    with StateDB(tmp_path / "state.db") as db:
        service = MemoryService(config, db)
        with pytest.raises(ValueError, match="not a real directory"):
            service.capture(CaptureRequest(text="Must stay inside"))

    assert not list(outside.rglob("*.md"))


def test_atomic_write_refuses_to_overwrite_existing_memory(tmp_path: Path) -> None:
    config = _config(tmp_path)

    with StateDB(tmp_path / "state.db") as db:
        service = MemoryService(config, db)
        with (
            patch("sahara.memory.service.uuid.uuid4") as new_uuid,
            patch.object(
                IndexingService,
                "index_path",
                return_value=IndexFileResult(indexed=True, reason="indexed"),
            ),
        ):
            new_uuid.return_value = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")
            service.capture(CaptureRequest(text="First value", title="Same title"))
            with pytest.raises(FileExistsError, match="already exists"):
                service.capture(CaptureRequest(text="Second value", title="Same title"))

        note = next(service.root.rglob("*.md"))
        assert service.read(note).text == "First value"


def test_failed_marker_install_leaves_root_recoverable(tmp_path: Path) -> None:
    config = _config(tmp_path)
    db_path = tmp_path / "state.db"

    with StateDB(db_path) as db:
        service = MemoryService(config, db)
        with patch(
            "sahara.memory.service.os.replace",
            side_effect=OSError("simulated marker install failure"),
        ):
            with pytest.raises(OSError, match="marker install failure"):
                service.capture(CaptureRequest(text="First attempt"))

        marker = service.root / ".sahara" / "memory-root.json"
        assert not marker.exists()
        assert not list(marker.parent.glob("*.tmp"))

        with patch.object(
            IndexingService,
            "index_path",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ):
            recovered = service.capture(CaptureRequest(text="Second attempt"))

    assert recovered.item.path.is_file()


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is POSIX-specific")
def test_atomic_write_fsyncs_file_and_parent_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)

    with StateDB(tmp_path / "state.db") as db:
        service = MemoryService(config, db)
        real_fsync = os.fsync
        synced_fds: list[int] = []

        def recording_fsync(fd: int) -> None:
            synced_fds.append(fd)
            real_fsync(fd)

        with (
            patch("sahara.memory.service.os.fsync", side_effect=recording_fsync),
            patch.object(
                IndexingService,
                "index_path",
                return_value=IndexFileResult(indexed=True, reason="indexed"),
            ),
        ):
            service.capture(CaptureRequest(text="Durable rename"))

    assert len(synced_fds) >= 2


def test_folder_add_rejects_memory_prefix_and_root_overlap(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config_path = tmp_path / "config.toml"
    save_config(config, config_path)
    memory_root = Path(config.memory_folder)
    parent = tmp_path
    other = tmp_path / "other"
    other.mkdir()
    db_path = tmp_path / "state.db"
    runner = CliRunner()

    with StateDB(db_path) as db:
        db.upsert_content_root(str(memory_root), "memory", sync_enabled=False)

    with patch("sahara.storage.state_db.DB_PATH", db_path):
        reserved = runner.invoke(
            main,
            [
                "--config",
                str(config_path),
                "folder",
                "add",
                str(other),
                "--name",
                "memory",
            ],
        )
        overlap = runner.invoke(
            main,
            ["--config", str(config_path), "folder", "add", str(parent)],
        )

    assert reserved.exit_code != 0
    assert "reserved by Sahara" in reserved.output
    assert overlap.exit_code != 0
    assert "overlaps registered root" in overlap.output


def test_folder_add_rejects_reserved_memory_descendant(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config_path = tmp_path / "config.toml"
    save_config(config, config_path)
    other = tmp_path / "other"
    other.mkdir()

    with patch("sahara.storage.state_db.DB_PATH", tmp_path / "state.db"):
        result = CliRunner().invoke(
            main,
            [
                "--config",
                str(config_path),
                "folder",
                "add",
                str(other),
                "--name",
                "memory/archive",
            ],
        )

    assert result.exit_code != 0
    assert "reserved by Sahara" in result.output


@pytest.mark.parametrize("prefix", [".sahara", "docs/.Sahara/archive"])
def test_storage_prefix_rejects_control_namespace(prefix: str) -> None:
    with pytest.raises(ValueError, match="control namespace"):
        validate_storage_prefix(prefix, [])


@pytest.mark.parametrize(
    "prefix",
    ["CON", "NUL.txt", "team:notes", "team.", "team ", "bad?name"],
)
def test_storage_prefix_rejects_nonportable_values(prefix: str) -> None:
    with pytest.raises(ValueError, match="portable"):
        validate_storage_prefix(prefix, [])


def test_storage_prefix_uniqueness_is_case_insensitive(tmp_path: Path) -> None:
    existing = ContentRoot(tmp_path / "one", "Team/Notes", False, False)

    with pytest.raises(ValueError, match="already registered"):
        validate_storage_prefix("team/notes", [existing])
    with pytest.raises(ValueError, match="overlaps"):
        validate_storage_prefix("TEAM", [existing])


@pytest.mark.parametrize(
    ("candidate", "existing_prefix"),
    [("a-b", "a/b"), ("a/b", "a-b")],
)
def test_storage_prefix_rejects_legacy_manifest_key_alias(
    tmp_path: Path,
    candidate: str,
    existing_prefix: str,
) -> None:
    existing = ContentRoot(
        tmp_path / "existing",
        existing_prefix,
        False,
        True,
    )

    with pytest.raises(ValueError, match="legacy manifest key"):
        validate_storage_prefix(candidate, [existing])


def test_storage_prefix_with_retained_file_ownership_cannot_be_reused(
    tmp_path: Path,
) -> None:
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    now = datetime.now(UTC)

    with StateDB(tmp_path / "state.db") as db:
        db.upsert_content_root(str(old_root), "archive", sync_enabled=True)
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
        unregister_content_root(db, old_root, "archive")

        with pytest.raises(ValueError, match="retained storage state"):
            register_content_root(
                _config(tmp_path),
                db,
                new_root,
                "archive",
            )


def test_capture_avoids_case_alias_of_memory_prefix(tmp_path: Path) -> None:
    config = _config(tmp_path)
    legacy = tmp_path / "legacy"
    legacy.mkdir()

    with StateDB(tmp_path / "state.db") as db:
        db.upsert_content_root(str(legacy), "Memory", sync_enabled=False)
        service = MemoryService(config, db)
        with patch.object(
            IndexingService,
            "index_path",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ):
            service.capture(CaptureRequest(text="Managed separately"))

        root = db.get_content_root(str(service.root))
        assert root is not None
        assert root["storage_prefix"] == "memory-2"


def test_capture_avoids_retained_memory_storage_ownership(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    now = datetime.now(UTC)

    with StateDB(tmp_path / "state.db") as db:
        db.upsert_file(
            FileRecord(
                relative_path="retained.md",
                sha256_checksum="abc",
                size_bytes=3,
                tier="STANDARD",
                s3_etag="etag",
                last_sync_at=now,
                local_modified_at=now,
                remote_modified_at=now,
            ),
            s3_prefix="memory",
        )
        service = MemoryService(config, db)
        with patch.object(
            IndexingService,
            "index_path",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ):
            service.capture(CaptureRequest(text="Managed separately"))

        root = db.get_content_root(str(service.root))
        assert root is not None
        assert root["storage_prefix"] == "memory-2"


def test_capture_rejects_nonempty_unmanaged_memory_folder(
    tmp_path: Path,
) -> None:
    memory_root = tmp_path / "existing-notes"
    memory_root.mkdir()
    ordinary = memory_root / "ordinary.md"
    ordinary.write_text("Keep my note unchanged", encoding="utf-8")
    original_mode = memory_root.stat().st_mode & 0o777
    config = _config(tmp_path, memory_folder=memory_root)

    with StateDB(tmp_path / "state.db") as db:
        with pytest.raises(ValueError, match="must be empty"):
            MemoryService(config, db).capture(CaptureRequest(text="New memory"))

        assert db.get_content_root(str(memory_root)) is None
        assert ordinary.read_text(encoding="utf-8") == "Keep my note unchanged"
        assert not (memory_root / ".sahara").exists()
        assert memory_root.stat().st_mode & 0o777 == original_mode


def test_capture_readopts_valid_marker_after_registration_failure(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    service_path = Path(config.memory_folder)

    with StateDB(tmp_path / "state.db") as db:
        service = MemoryService(config, db)
        with (
            patch(
                "sahara.memory.service.register_content_root",
                side_effect=RuntimeError("registration failed"),
            ),
            pytest.raises(RuntimeError, match="registration failed"),
        ):
            service.capture(CaptureRequest(text="First attempt"))

        assert (service_path / ".sahara" / "memory-root.json").is_file()
        assert db.get_content_root(str(service_path)) is None

        with patch.object(
            IndexingService,
            "index_path",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ):
            result = MemoryService(config, db).capture(
                CaptureRequest(text="Second attempt")
            )

        assert result.item.path.is_file()
        assert db.get_content_root(str(service_path)) is not None


def test_managed_memory_root_cannot_be_unregistered(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    with StateDB(tmp_path / "state.db") as db:
        service = MemoryService(config, db)
        with patch.object(
            IndexingService,
            "index_path",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ):
            service.capture(CaptureRequest(text="Keep registered"))
        root = db.get_content_root(str(service.root))
        assert root is not None

        with pytest.raises(ValueError, match="memory folder cannot be removed"):
            unregister_content_root(
                db,
                service.root,
                root["storage_prefix"],
            )

        assert db.get_content_root(str(service.root)) is not None


@pytest.mark.parametrize(
    ("frontmatter", "expected"),
    [
        (
            "source_url: https://private.example\nsource_id: secret",
            "ordinary",
        ),
        (
            "kind: sahara-memroy\nsource_url: https://private.example",
            "invalid",
        ),
        (
            "created_at: 2026-06-13T18:30:00Z\n"
            "updated_at: 2026-06-13T19:30:00Z",
            "ordinary",
        ),
    ],
)
def test_memory_detection_requires_explicit_sahara_provenance(
    tmp_path: Path,
    frontmatter: str,
    expected: str,
) -> None:
    note = tmp_path / "note.md"
    document = f"---\n{frontmatter}\n---\nVisible body\n"
    note.write_text(document, encoding="utf-8")

    with StateDB(tmp_path / "state.db") as db:
        engine = SearchEngine(db)
        with patch.object(engine, "_embed", return_value=[np.zeros(384)]):
            result = engine.index_file_with_result(
                note,
                "notes",
                "note.md",
            )

        assert classify_memory_document(document) == expected
        assert result.reason == (
            "invalid_memory" if expected == "invalid" else "indexed"
        )


def test_capture_avoids_legacy_memory_descendant_prefix(tmp_path: Path) -> None:
    config = _config(tmp_path)
    legacy = tmp_path / "legacy"
    legacy.mkdir()

    with StateDB(tmp_path / "state.db") as db:
        db.upsert_content_root(str(legacy), "memory/archive", sync_enabled=False)
        service = MemoryService(config, db)
        with patch.object(
            IndexingService,
            "index_path",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ):
            service.capture(CaptureRequest(text="Managed separately"))

        root = db.get_content_root(str(service.root))
        assert root is not None
        assert root["storage_prefix"] == "memory-2"


@pytest.mark.parametrize(
    "prefix",
    [
        "../escape",
        "/absolute",
        r"..\escape",
        "C:/escape",
        "foo//bar",
        "foo/.",
        "foo/",
    ],
)
def test_folder_add_rejects_unsafe_storage_prefix(
    tmp_path: Path,
    prefix: str,
) -> None:
    config = _config(tmp_path)
    config_path = tmp_path / "config.toml"
    save_config(config, config_path)
    other = tmp_path / "other"
    other.mkdir()
    db_path = tmp_path / "state.db"

    with patch("sahara.storage.state_db.DB_PATH", db_path):
        result = CliRunner().invoke(
            main,
            [
                "--config",
                str(config_path),
                "folder",
                "add",
                str(other),
                "--name",
                prefix,
            ],
        )

    assert result.exit_code != 0
    assert "safe relative path" in result.output or "backslashes" in result.output


def test_folder_add_rejects_overlapping_storage_namespace(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config_path = tmp_path / "config.toml"
    save_config(config, config_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    db_path = tmp_path / "state.db"

    with patch("sahara.storage.state_db.DB_PATH", db_path):
        runner = CliRunner()
        added = runner.invoke(
            main,
            [
                "--config",
                str(config_path),
                "folder",
                "add",
                str(first),
                "--name",
                "archive",
            ],
        )
        overlap = runner.invoke(
            main,
            [
                "--config",
                str(config_path),
                "folder",
                "add",
                str(second),
                "--name",
                "archive/2026",
            ],
        )

    assert added.exit_code == 0
    assert overlap.exit_code != 0
    assert "overlaps registered prefix" in overlap.output


def test_concurrent_content_root_registration_is_atomic(tmp_path: Path) -> None:
    config = _config(tmp_path)
    db_path = tmp_path / "state.db"
    first = tmp_path / "first"
    second = first / "second"
    first.mkdir()
    second.mkdir()

    def register(path: Path, prefix: str) -> str:
        with StateDB(db_path) as db:
            return register_content_root(
                config,
                db,
                path,
                prefix,
            ).storage_prefix

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(register, first, "first"),
            executor.submit(register, second, "second"),
        ]
        outcomes: list[str] = []
        errors: list[Exception] = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except Exception as exc:
                errors.append(exc)

    assert len(outcomes) == 1
    assert len(errors) == 1
    assert "overlaps registered root" in str(errors[0])


def test_content_root_removal_cannot_be_recreated_by_migration(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    extra = tmp_path / "extra"
    extra.mkdir()
    db_path = tmp_path / "state.db"

    with StateDB(db_path) as db:
        ensure_content_roots(config, db)
        db.add_sync_target(str(extra), "extra")
        db.upsert_content_root(str(extra), "extra", sync_enabled=True)

    def remove() -> None:
        with StateDB(db_path) as db:
            unregister_content_root(db, extra, "extra")

    def migrate() -> None:
        with StateDB(db_path) as db:
            ensure_content_roots(config, db)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(remove), executor.submit(migrate)]
        for future in futures:
            future.result()

    with StateDB(db_path) as db:
        assert db.get_content_root(str(extra)) is None
        assert db.list_sync_targets() == []


def test_content_root_removal_rolls_back_all_local_state_on_failure(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    extra = tmp_path / "extra"
    extra.mkdir()

    with StateDB(tmp_path / "state.db") as db:
        ensure_content_roots(config, db)
        db.add_sync_target(str(extra), "extra")
        db.upsert_content_root(str(extra), "extra", sync_enabled=True)
        db.upsert_index_entry(
            "extra",
            "note.md",
            content_hash="abc",
            size_bytes=3,
            modified_ns=1,
            status="indexed",
        )
        db.conn.execute(
            """
            CREATE TRIGGER fail_content_root_removal
            BEFORE DELETE ON content_roots
            WHEN OLD.storage_prefix = 'extra'
            BEGIN
                SELECT RAISE(ABORT, 'simulated removal failure');
            END
            """
        )
        db.conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="simulated removal failure"):
            unregister_content_root(db, extra, "extra")

        assert db.get_content_root(str(extra)) is not None
        assert db.list_sync_targets() != []
        entries = db.list_index_entries(storage_prefix="extra")
        assert [entry["relative_path"] for entry in entries] == ["note.md"]


def test_content_root_removal_batches_large_vector_deletes(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    extra = tmp_path / "extra"
    extra.mkdir()

    with StateDB(tmp_path / "state.db") as db:
        ensure_content_roots(config, db)
        db.upsert_content_root(str(extra), "extra", sync_enabled=False)
        if not db.has_vec_table():
            db.conn.execute(
                "CREATE TABLE vec_chunks (rowid INTEGER PRIMARY KEY, embedding BLOB)"
            )
        now = datetime.now(UTC).isoformat()
        db.conn.executemany(
            "INSERT INTO chunks "
            "(storage_prefix, relative_path, chunk_index, content_hash, "
            "chunk_text, indexed_at) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("extra", "large.md", index, str(index), "text", now)
                for index in range(1_201)
            ],
        )
        chunk_ids = [
            row["id"]
            for row in db.conn.execute(
                "SELECT id FROM chunks WHERE storage_prefix = 'extra'"
            )
        ]
        embedding = np.zeros(384, dtype=np.float32).tobytes()
        db.conn.executemany(
            "INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
            [(chunk_id, embedding) for chunk_id in chunk_ids],
        )
        db.conn.commit()
        statements: list[str] = []
        db.conn.set_trace_callback(statements.append)

        unregister_content_root(db, extra, "extra")

        db.conn.set_trace_callback(None)
        vector_deletes = [
            statement
            for statement in statements
            if statement.startswith("DELETE FROM vec_chunks")
        ]
        assert len(vector_deletes) == 3
        assert db.count_chunks(storage_prefix="extra") == 0


def test_legacy_add_rejects_memory_prefix_and_root_overlap(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    memory_root = tmp_path / "memories"
    memory_root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    config = SaharaConfig(
        sync_folder=str(primary),
        bucket="test-bucket",
        memory_folder=str(memory_root),
    )
    config_path = tmp_path / "config.toml"
    save_config(config, config_path)
    db_path = tmp_path / "state.db"
    runner = CliRunner()

    with StateDB(db_path) as db:
        db.upsert_content_root(str(memory_root), "memory", sync_enabled=False)

    with patch("sahara.storage.state_db.DB_PATH", db_path):
        reserved = runner.invoke(
            main,
            ["--config", str(config_path), "add", str(other), "--as", "memory"],
        )
        overlap = runner.invoke(
            main,
            ["--config", str(config_path), "add", str(tmp_path)],
        )

    assert reserved.exit_code != 0
    assert "reserved by Sahara" in reserved.output
    assert overlap.exit_code != 0
    assert "overlaps registered root" in overlap.output


def test_capture_rejects_memory_root_nested_inside_existing_root(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    config = SaharaConfig(
        sync_folder=str(primary),
        storage_mode="none",
        memory_folder=str(primary / "memory"),
    )

    with StateDB(tmp_path / "state.db") as db:
        with pytest.raises(ValueError, match="overlaps registered root"):
            MemoryService(config, db).capture(CaptureRequest(text="Do not duplicate me"))

        assert not (primary / "memory").exists()


@pytest.mark.parametrize(
    ("capture_request", "message"),
    [
        (CaptureRequest(text=""), "cannot be empty"),
        (
            CaptureRequest(text="valid", source_url="file:///tmp/private"),
            "absolute HTTP or HTTPS",
        ),
        (
            CaptureRequest(text="valid", source_type="unverified"),
            "source_type must be",
        ),
    ],
)
def test_capture_validates_untrusted_metadata(
    tmp_path: Path,
    capture_request: CaptureRequest,
    message: str,
) -> None:
    config = _config(tmp_path)

    with StateDB(tmp_path / "state.db") as db:
        with pytest.raises(ValueError, match=message):
            MemoryService(config, db).capture(capture_request)


def test_index_path_updates_only_one_registered_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    memory_root = Path(config.memory_folder)
    memory_root.mkdir()
    note = memory_root / "note.md"
    note.write_text("A known fact", encoding="utf-8")

    with StateDB(tmp_path / "state.db") as db:
        db.upsert_content_root(str(memory_root), "memory", sync_enabled=False)
        service = IndexingService(config, db)
        with patch.object(
            service._search,
            "index_file_with_result",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ) as index_file:
            result = service.index_path(note)

        assert result.indexed is True
        index_file.assert_called_once_with(
            note.resolve(),
            "memory",
            "note.md",
            force=False,
            managed_memory=False,
        )
        entries = db.list_index_entries(storage_prefix="memory")
        assert len(entries) == 1
        assert entries[0]["relative_path"] == "note.md"
        assert entries[0]["status"] == "indexed"


def test_index_path_rejects_file_outside_content_roots(tmp_path: Path) -> None:
    config = _config(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("not registered", encoding="utf-8")

    with StateDB(tmp_path / "state.db") as db:
        with pytest.raises(ValueError, match="exactly one content root"):
            IndexingService(config, db).index_path(outside)


def test_remember_cli_accepts_argument_and_stdin(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config_path = tmp_path / "config.toml"
    save_config(config, config_path)
    db_path = tmp_path / "state.db"
    runner = CliRunner()

    with (
        patch("sahara.storage.state_db.DB_PATH", db_path),
        patch.object(
            IndexingService,
            "index_path",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ),
    ):
        argument_result = runner.invoke(
            main,
            [
                "--config",
                str(config_path),
                "remember",
                "A fact from a conversation",
                "--source",
                "conversation",
                "--tag",
                "work",
            ],
        )
        stdin_result = runner.invoke(
            main,
            ["--config", str(config_path), "remember"],
            input="A fact from standard input\n",
        )

    assert argument_result.exit_code == 0
    assert "Saved memory" in argument_result.output
    assert "Indexed for semantic retrieval" in argument_result.output
    assert stdin_result.exit_code == 0
    assert "Saved memory" in stdin_result.output
    memories = list(Path(config.memory_folder).rglob("*.md"))
    assert len(memories) == 2
    with StateDB(db_path) as db:
        stdin_memory = next(
            MemoryService(config, db).read(path)
            for path in memories
            if "standard-input" in path.name
        )
    assert stdin_memory.text == "A fact from standard input\n"


def test_remember_cli_rejects_oversized_stdin_before_capture(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config_path = tmp_path / "config.toml"
    save_config(config, config_path)

    result = CliRunner().invoke(
        main,
        ["--config", str(config_path), "remember"],
        input="x" * 200_001,
    )

    assert result.exit_code != 0
    assert "exceeds the 200,000-character limit" in result.output


def test_memory_folder_round_trips_through_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    memory_folder = tmp_path / "portable memories"
    save_config(
        SaharaConfig(
            sync_folder=str(tmp_path / "primary"),
            storage_mode="none",
            memory_folder=str(memory_folder),
        ),
        config_path,
    )

    assert load_config(config_path).memory_folder == str(memory_folder)


def test_relative_memory_folder_is_stable_across_working_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()
    config = _config(tmp_path)
    config.memory_folder = "memories"

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with StateDB(tmp_path / "state.db") as db:
        monkeypatch.chdir(first_cwd)
        first = MemoryService(config, db).root
        monkeypatch.chdir(second_cwd)
        second = MemoryService(config, db).root

    assert first == second == home / "memories"


def test_config_rejects_changing_initialized_memory_folder(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config_path = tmp_path / "config.toml"
    save_config(config, config_path)
    db_path = tmp_path / "state.db"

    with StateDB(db_path) as db:
        with patch.object(
            IndexingService,
            "index_path",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ):
            MemoryService(config, db).capture(
                CaptureRequest(text="Initialized memory")
            )

    with patch("sahara.storage.state_db.DB_PATH", db_path):
        result = CliRunner().invoke(
            main,
            [
                "--config",
                str(config_path),
                "config",
                "set",
                "memory_folder",
                str(tmp_path / "different-memory"),
            ],
        )

    assert result.exit_code != 0
    assert "cannot be changed" in result.output
    assert load_config(config_path).memory_folder == config.memory_folder
