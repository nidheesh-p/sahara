"""Tests for sahara.config."""
from __future__ import annotations

from pathlib import Path

import pytest

from sahara.config import (
    DEFAULT_EXCLUDES,
    SaharaConfig,
    _flatten_toml,
    load_config,
    save_config,
)

# ---------------------------------------------------------------------------
# SaharaConfig defaults
# ---------------------------------------------------------------------------


class TestSaharaConfigDefaults:
    def test_default_sync_folder_empty(self):
        cfg = SaharaConfig()
        assert cfg.sync_folder == ""

    def test_default_bucket_empty(self):
        cfg = SaharaConfig()
        assert cfg.bucket == ""

    def test_default_region(self):
        cfg = SaharaConfig()
        assert cfg.region == "us-east-1"

    def test_default_prefix_empty(self):
        cfg = SaharaConfig()
        assert cfg.prefix == ""

    def test_default_encryption_disabled(self):
        cfg = SaharaConfig()
        assert cfg.encryption_enabled is False

    def test_default_max_workers(self):
        cfg = SaharaConfig()
        assert cfg.max_workers == 8

    def test_default_conflict_strategy(self):
        cfg = SaharaConfig()
        assert cfg.conflict_strategy == "backup"

    def test_default_exclude_patterns_empty(self):
        cfg = SaharaConfig()
        assert cfg.exclude_patterns == []

    def test_pid_file_set_in_post_init(self):
        cfg = SaharaConfig()
        assert cfg.pid_file != ""
        assert "daemon.pid" in cfg.pid_file

    def test_custom_pid_file_preserved(self):
        cfg = SaharaConfig(pid_file="/tmp/my.pid")
        assert cfg.pid_file == "/tmp/my.pid"

    def test_default_storage_class(self):
        cfg = SaharaConfig()
        assert cfg.default_storage_class == "GLACIER_IR"

    def test_default_archive_storage_class(self):
        cfg = SaharaConfig()
        assert cfg.archive_storage_class == "DEEP_ARCHIVE"

    def test_default_restore_days(self):
        cfg = SaharaConfig()
        assert cfg.restore_days == 7

    def test_default_restore_tier(self):
        cfg = SaharaConfig()
        assert cfg.restore_tier == "Bulk"

    def test_default_answer_provider(self):
        cfg = SaharaConfig()
        assert cfg.answer_provider == "ollama"
        assert cfg.answer_model == ""


# ---------------------------------------------------------------------------
# get_sync_folder_path
# ---------------------------------------------------------------------------


class TestGetSyncFolderPath:
    def test_raises_when_empty(self):
        cfg = SaharaConfig()
        with pytest.raises(ValueError, match="sync_folder is not configured"):
            cfg.get_sync_folder_path()

    def test_returns_resolved_path(self, tmp_path: Path):
        cfg = SaharaConfig(sync_folder=str(tmp_path))
        result = cfg.get_sync_folder_path()
        assert result == tmp_path.resolve()
        assert isinstance(result, Path)

    def test_expands_home_tilde(self):
        cfg = SaharaConfig(sync_folder="~/MySyncFolder")
        result = cfg.get_sync_folder_path()
        assert not str(result).startswith("~")
        assert "MySyncFolder" in str(result)


# ---------------------------------------------------------------------------
# get_s3_key
# ---------------------------------------------------------------------------


class TestGetS3Key:
    def test_no_prefix(self):
        cfg = SaharaConfig(prefix="")
        key = cfg.get_s3_key("docs/report.pdf")
        assert key == "docs/report.pdf"

    def test_with_prefix(self):
        cfg = SaharaConfig(prefix="mybackup")
        key = cfg.get_s3_key("docs/report.pdf")
        assert key == "mybackup/docs/report.pdf"

    def test_with_prefix_trailing_slash(self):
        cfg = SaharaConfig(prefix="mybackup/")
        key = cfg.get_s3_key("file.txt")
        assert key == "mybackup/file.txt"

    def test_with_nested_prefix(self):
        cfg = SaharaConfig(prefix="user/data")
        key = cfg.get_s3_key("photo.jpg")
        assert key == "user/data/photo.jpg"


# ---------------------------------------------------------------------------
# get_all_exclude_patterns
# ---------------------------------------------------------------------------


class TestGetAllExcludePatterns:
    def test_includes_default_excludes(self):
        cfg = SaharaConfig()
        patterns = cfg.get_all_exclude_patterns()
        for pat in DEFAULT_EXCLUDES:
            assert pat in patterns

    def test_includes_custom_patterns(self):
        cfg = SaharaConfig(exclude_patterns=["*.log", "temp/"])
        patterns = cfg.get_all_exclude_patterns()
        assert "*.log" in patterns
        assert "temp/" in patterns

    def test_default_excludes_come_first(self):
        cfg = SaharaConfig(exclude_patterns=["custom_pat"])
        patterns = cfg.get_all_exclude_patterns()
        default_len = len(DEFAULT_EXCLUDES)
        # Default patterns should fill the first N slots
        assert patterns[:default_len] == DEFAULT_EXCLUDES


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_returns_defaults_when_file_missing(self, tmp_path: Path):
        cfg = load_config(tmp_path / "nonexistent.toml")
        assert isinstance(cfg, SaharaConfig)
        assert cfg.sync_folder == ""
        assert cfg.bucket == ""

    def test_load_from_valid_toml(self, tmp_path: Path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text(
            'sync_folder = "/home/user/sync"\n'
            'bucket = "my-bucket"\n'
            'region = "us-west-2"\n'
            'max_workers = 4\n',
            encoding="utf-8",
        )
        cfg = load_config(toml_path)
        assert cfg.sync_folder == "/home/user/sync"
        assert cfg.bucket == "my-bucket"
        assert cfg.region == "us-west-2"
        assert cfg.max_workers == 4

    def test_load_ignores_unknown_keys(self, tmp_path: Path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text(
            'bucket = "my-bucket"\n'
            'unknown_key = "ignored"\n',
            encoding="utf-8",
        )
        cfg = load_config(toml_path)
        assert cfg.bucket == "my-bucket"
        assert not hasattr(cfg, "unknown_key")

    def test_load_exclude_patterns_list(self, tmp_path: Path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text(
            'bucket = "b"\n'
            'exclude_patterns = ["*.log", "*.tmp"]\n',
            encoding="utf-8",
        )
        cfg = load_config(toml_path)
        assert "*.log" in cfg.exclude_patterns
        assert "*.tmp" in cfg.exclude_patterns

    def test_load_nested_toml(self, tmp_path: Path):
        # Nested TOML should be flattened
        toml_path = tmp_path / "config.toml"
        toml_path.write_text(
            '[aws]\n'
            'profile = "myprofile"\n',
            encoding="utf-8",
        )
        # aws_profile is the flattened key
        cfg = load_config(toml_path)
        assert cfg.aws_profile == "myprofile"


# ---------------------------------------------------------------------------
# save_config and round-trip
# ---------------------------------------------------------------------------


class TestSaveConfig:
    def test_creates_file(self, tmp_path: Path):
        cfg = SaharaConfig(bucket="test-bucket", region="eu-west-1")
        config_path = tmp_path / "cfg.toml"
        save_config(cfg, config_path)
        assert config_path.exists()

    def test_creates_parent_dirs(self, tmp_path: Path):
        cfg = SaharaConfig(bucket="bucket")
        config_path = tmp_path / "nested" / "dir" / "config.toml"
        save_config(cfg, config_path)
        assert config_path.exists()

    def test_round_trip(self, tmp_path: Path):
        original = SaharaConfig(
            sync_folder="/tmp/sync",
            bucket="round-trip-bucket",
            region="ap-southeast-1",
            max_workers=4,
            encryption_enabled=True,
            conflict_strategy="local",
            answer_provider="openai",
            answer_model="gpt-4o-mini",
            exclude_patterns=["*.log", "build/"],
        )
        config_path = tmp_path / "cfg.toml"
        save_config(original, config_path)
        loaded = load_config(config_path)

        assert loaded.sync_folder == original.sync_folder
        assert loaded.bucket == original.bucket
        assert loaded.region == original.region
        assert loaded.max_workers == original.max_workers
        assert loaded.encryption_enabled == original.encryption_enabled
        assert loaded.conflict_strategy == original.conflict_strategy
        assert loaded.answer_provider == "openai"
        assert loaded.answer_model == "gpt-4o-mini"
        assert "*.log" in loaded.exclude_patterns
        assert "build/" in loaded.exclude_patterns

    def test_save_bool_values(self, tmp_path: Path):
        cfg = SaharaConfig(encryption_enabled=True, notifications_enabled=False)
        config_path = tmp_path / "cfg.toml"
        save_config(cfg, config_path)
        content = config_path.read_text()
        assert "encryption_enabled = true" in content
        assert "notifications_enabled = false" in content

    def test_save_empty_list(self, tmp_path: Path):
        cfg = SaharaConfig(exclude_patterns=[])
        config_path = tmp_path / "cfg.toml"
        save_config(cfg, config_path)
        content = config_path.read_text()
        assert "exclude_patterns = []" in content


# ---------------------------------------------------------------------------
# _flatten_toml
# ---------------------------------------------------------------------------


class TestFlattenToml:
    def test_flat_dict_unchanged(self):
        data = {"key1": "val1", "key2": 42}
        result = _flatten_toml(data)
        assert result == {"key1": "val1", "key2": 42}

    def test_nested_dict_flattened(self):
        data = {"aws": {"profile": "default", "region": "us-east-1"}}
        result = _flatten_toml(data)
        assert result == {"aws_profile": "default", "aws_region": "us-east-1"}

    def test_deeply_nested(self):
        data = {"a": {"b": {"c": "deep_value"}}}
        result = _flatten_toml(data)
        assert result == {"a_b_c": "deep_value"}

    def test_mixed_flat_and_nested(self):
        data = {"top": "val", "nested": {"key": "nested_val"}}
        result = _flatten_toml(data)
        assert result["top"] == "val"
        assert result["nested_key"] == "nested_val"

    def test_list_values_preserved(self):
        data = {"patterns": ["*.log", "*.tmp"]}
        result = _flatten_toml(data)
        assert result["patterns"] == ["*.log", "*.tmp"]
