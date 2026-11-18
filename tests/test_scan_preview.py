"""Tests for the first-run folder scan preview."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from sahara import scan_preview
from sahara.scan_preview import FolderSummary, format_summary, summarize_folder
from sahara.sync.ignore_rules import IgnoreRules


def _write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_counts_supported_files_and_bytes(tmp_path):
    _write(tmp_path / "a.md", b"12345")
    _write(tmp_path / "nested" / "deep" / "b.PY", b"123")
    _write(tmp_path / "nested" / "c.pdf", b"12")

    summary = summarize_folder(tmp_path)

    assert summary == FolderSummary(
        supported_file_count=3, total_bytes=10, unsupported_count=0
    )


def test_unsupported_files_are_counted_separately(tmp_path):
    _write(tmp_path / "notes.txt", b"abc")
    _write(tmp_path / "photo.jpg", b"0" * 100)
    _write(tmp_path / "archive.unknownext", b"0" * 50)

    summary = summarize_folder(tmp_path)

    assert summary.supported_file_count == 1
    assert summary.total_bytes == 3
    assert summary.unsupported_count == 2


def test_honours_saharaignore_rules(tmp_path):
    (tmp_path / ".saharaignore").write_text("private/\n*.csv\n", encoding="utf-8")
    _write(tmp_path / "keep.md")
    _write(tmp_path / "private" / "secret.md")
    _write(tmp_path / "data.csv")

    summary = summarize_folder(tmp_path)

    assert summary.supported_file_count == 1
    assert summary.unsupported_count == 0


def test_accepts_explicit_ignore_rules(tmp_path):
    _write(tmp_path / "keep.md")
    _write(tmp_path / "drafts" / "skip.md")
    rules = IgnoreRules(tmp_path, extra_patterns=["drafts/"])

    assert summarize_folder(tmp_path, rules).supported_file_count == 1


def test_does_not_read_file_contents(tmp_path):
    _write(tmp_path / "a.md")
    with patch.object(Path, "read_bytes", side_effect=AssertionError), patch.object(
        Path, "read_text", side_effect=AssertionError
    ), patch("builtins.open", side_effect=AssertionError):
        summary = summarize_folder(tmp_path, IgnoreRules(tmp_path / "missing"))

    assert summary.supported_file_count == 1


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unsupported")
def test_does_not_follow_symlinks_out_of_folder(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    _write(root / "inside.md", b"1")
    _write(outside / "big.md", b"0" * 1000)
    _write(outside / "sub" / "more.md", b"0" * 1000)
    (root / "linked_dir").symlink_to(outside, target_is_directory=True)
    (root / "linked_file.md").symlink_to(outside / "big.md")
    (root / "alias.md").symlink_to(root / "inside.md")

    summary = summarize_folder(root)

    assert summary.supported_file_count == 2
    assert summary.total_bytes == 2


def test_stops_at_entry_cap_and_reports_lower_bound(tmp_path):
    for index in range(10):
        _write(tmp_path / f"file{index}.md")

    with patch.object(scan_preview, "MAX_ENTRIES", 4):
        summary = summarize_folder(tmp_path)

    assert summary.is_lower_bound is True
    assert summary.supported_file_count == 4


def test_under_cap_is_exact(tmp_path):
    _write(tmp_path / "a.md")

    with patch.object(scan_preview, "MAX_ENTRIES", 4):
        assert summarize_folder(tmp_path).is_lower_bound is False


def test_format_summary_wording():
    exact = FolderSummary(1234, 5 * 1024 * 1024, 0)
    capped = FolderSummary(50_000, 0, 0, is_lower_bound=True)

    assert format_summary(exact) == "Found 1,234 files (~5.0 MB) to index"
    assert format_summary(capped).startswith("Found 50,000+ files")
    assert format_summary(FolderSummary(1, 0, 0)).startswith("Found 1 file ")
