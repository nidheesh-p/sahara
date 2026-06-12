"""Tests for .saharaignore creation when adding content roots (issue #39)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from sahara.cli import _ensure_saharaignore, main
from sahara.storage.state_db import StateDB

TEMPLATE_MARKER = "# .saharaignore — Sahara ignore rules"


class TestEnsureSaharaignoreHelper:
    def test_creates_file_from_packaged_template(self, tmp_path: Path) -> None:
        created = _ensure_saharaignore(tmp_path)

        assert created is True
        content = (tmp_path / ".saharaignore").read_text(encoding="utf-8")
        assert TEMPLATE_MARKER in content
        assert ".git/" in content

    def test_preserves_existing_file(self, tmp_path: Path) -> None:
        ignore_path = tmp_path / ".saharaignore"
        ignore_path.write_text("# my custom rules\ncustom/\n", encoding="utf-8")

        created = _ensure_saharaignore(tmp_path)

        assert created is False
        assert ignore_path.read_text(encoding="utf-8") == "# my custom rules\ncustom/\n"

    def test_falls_back_to_minimal_template_when_packaged_resource_missing(
        self, tmp_path: Path
    ) -> None:
        with patch("sahara.cli.resources.files", side_effect=ModuleNotFoundError):
            created = _ensure_saharaignore(tmp_path)

        assert created is True
        content = (tmp_path / ".saharaignore").read_text(encoding="utf-8")
        assert content == "# Sahara ignore rules (gitignore syntax)\n"

    def test_concurrent_creation_does_not_raise(self, tmp_path: Path) -> None:
        """A .saharaignore created between the exists() check and the write
        (e.g. by a concurrent invocation) must not crash the caller."""
        ignore_path = tmp_path / ".saharaignore"

        real_open = open

        def racing_open(path, mode="r", *args, **kwargs):
            if path == ignore_path and "x" in mode:
                ignore_path.write_text("raced/\n", encoding="utf-8")
            return real_open(path, mode, *args, **kwargs)

        with patch("sahara.cli.open", side_effect=racing_open):
            created = _ensure_saharaignore(tmp_path)

        assert created is False
        assert ignore_path.read_text(encoding="utf-8") == "raced/\n"

    def test_not_a_directory_is_a_noop(self, tmp_path: Path) -> None:
        not_a_dir = tmp_path / "afile"
        not_a_dir.write_text("hello", encoding="utf-8")

        created = _ensure_saharaignore(not_a_dir)

        assert created is False
        assert not (tmp_path / ".saharaignore").exists()


class TestFolderAddCreatesIgnoreFile:
    def test_folder_add_creates_saharaignore(self, tmp_path: Path) -> None:
        primary = tmp_path / "primary"
        additional = tmp_path / "additional"
        primary.mkdir()
        additional.mkdir()
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f'sync_folder = "{primary}"\nstorage_mode = "none"\n', encoding="utf-8"
        )
        db_path = tmp_path / "state.db"

        with patch("sahara.storage.state_db.DB_PATH", db_path):
            result = CliRunner().invoke(
                main,
                ["--config", str(config_path), "folder", "add", str(additional)],
            )

        assert result.exit_code == 0
        assert "Created .saharaignore from template." in result.output
        content = (additional / ".saharaignore").read_text(encoding="utf-8")
        assert TEMPLATE_MARKER in content

    def test_folder_add_preserves_existing_saharaignore(self, tmp_path: Path) -> None:
        primary = tmp_path / "primary"
        additional = tmp_path / "additional"
        primary.mkdir()
        additional.mkdir()
        (additional / ".saharaignore").write_text("custom/\n", encoding="utf-8")

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f'sync_folder = "{primary}"\nstorage_mode = "none"\n', encoding="utf-8"
        )
        db_path = tmp_path / "state.db"

        with patch("sahara.storage.state_db.DB_PATH", db_path):
            result = CliRunner().invoke(
                main,
                ["--config", str(config_path), "folder", "add", str(additional)],
            )

        assert result.exit_code == 0
        assert "Created .saharaignore" not in result.output
        assert (additional / ".saharaignore").read_text(encoding="utf-8") == "custom/\n"

    def test_re_adding_registered_folder_does_not_overwrite(self, tmp_path: Path) -> None:
        primary = tmp_path / "primary"
        additional = tmp_path / "additional"
        primary.mkdir()
        additional.mkdir()
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f'sync_folder = "{primary}"\nstorage_mode = "none"\n', encoding="utf-8"
        )
        db_path = tmp_path / "state.db"

        with patch("sahara.storage.state_db.DB_PATH", db_path):
            first = CliRunner().invoke(
                main,
                ["--config", str(config_path), "folder", "add", str(additional)],
            )
            assert first.exit_code == 0

            ignore_path = additional / ".saharaignore"
            ignore_path.write_text("custom-after-add/\n", encoding="utf-8")

            with StateDB(db_path) as db:
                db.remove_content_root(str(additional.resolve()))

            second = CliRunner().invoke(
                main,
                ["--config", str(config_path), "folder", "add", str(additional)],
            )

        assert second.exit_code == 0
        assert "Created .saharaignore" not in second.output
        assert ignore_path.read_text(encoding="utf-8") == "custom-after-add/\n"
