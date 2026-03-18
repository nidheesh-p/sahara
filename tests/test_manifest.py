"""Tests for ManifestEntry serialization (sahara.models)."""
from __future__ import annotations

import json

import pytest

from sahara.models import ManifestEntry


# ---------------------------------------------------------------------------
# ManifestEntry to_dict / from_dict
# ---------------------------------------------------------------------------


class TestManifestEntryRoundTrip:
    def _entry(self, **kwargs) -> ManifestEntry:
        defaults = dict(
            sha256="deadbeef" * 8,
            size=4096,
            tier="STANDARD",
            modified_at="2024-01-15T12:00:00+00:00",
            etag="etag-abc",
            ignored=False,
        )
        defaults.update(kwargs)
        return ManifestEntry(**defaults)

    def test_to_dict_all_fields(self):
        entry = self._entry()
        d = entry.to_dict()
        assert d["sha256"] == entry.sha256
        assert d["size"] == entry.size
        assert d["tier"] == entry.tier
        assert d["modified_at"] == entry.modified_at
        assert d["etag"] == entry.etag
        assert d["ignored"] == entry.ignored

    def test_from_dict_all_fields(self):
        data = {
            "sha256": "abc123",
            "size": 512,
            "tier": "GLACIER",
            "modified_at": "2024-06-01T00:00:00",
            "etag": "etag-123",
            "ignored": True,
        }
        entry = ManifestEntry.from_dict(data)
        assert entry.sha256 == "abc123"
        assert entry.size == 512
        assert entry.tier == "GLACIER"
        assert entry.modified_at == "2024-06-01T00:00:00"
        assert entry.etag == "etag-123"
        assert entry.ignored is True

    def test_from_dict_round_trip(self):
        original = self._entry(tier="DEEP_ARCHIVE", ignored=True)
        d = original.to_dict()
        restored = ManifestEntry.from_dict(d)
        assert restored.sha256 == original.sha256
        assert restored.size == original.size
        assert restored.tier == original.tier
        assert restored.modified_at == original.modified_at
        assert restored.etag == original.etag
        assert restored.ignored == original.ignored

    def test_from_dict_missing_tier_defaults_standard(self):
        data = {
            "sha256": "sha",
            "size": 0,
            "modified_at": "2024-01-01T00:00:00",
            "etag": "e",
        }
        entry = ManifestEntry.from_dict(data)
        assert entry.tier == "STANDARD"

    def test_from_dict_missing_ignored_defaults_false(self):
        data = {
            "sha256": "sha",
            "size": 0,
            "tier": "STANDARD",
            "modified_at": "2024-01-01T00:00:00",
            "etag": "e",
        }
        entry = ManifestEntry.from_dict(data)
        assert entry.ignored is False


# ---------------------------------------------------------------------------
# Full manifest dict serialization
# ---------------------------------------------------------------------------


class TestManifestDictSerialization:
    def test_manifest_with_multiple_files(self):
        files = {
            "docs/report.pdf": ManifestEntry(
                sha256="aaa",
                size=1024,
                tier="STANDARD",
                modified_at="2024-01-01T00:00:00",
                etag="etag1",
            ),
            "photos/vacation.jpg": ManifestEntry(
                sha256="bbb",
                size=5 * 1024 * 1024,
                tier="STANDARD",
                modified_at="2024-02-15T12:00:00",
                etag="etag2",
            ),
            "archive/old.zip": ManifestEntry(
                sha256="ccc",
                size=100 * 1024 * 1024,
                tier="GLACIER",
                modified_at="2023-12-01T00:00:00",
                etag="etag3",
            ),
        }

        manifest_dict = {path: entry.to_dict() for path, entry in files.items()}

        # Serialize to JSON
        json_str = json.dumps(manifest_dict)
        assert isinstance(json_str, str)

        # Deserialize and compare
        loaded = json.loads(json_str)
        assert len(loaded) == 3
        assert "docs/report.pdf" in loaded
        assert "photos/vacation.jpg" in loaded
        assert "archive/old.zip" in loaded

        restored_entry = ManifestEntry.from_dict(loaded["archive/old.zip"])
        assert restored_entry.tier == "GLACIER"
        assert restored_entry.sha256 == "ccc"

    def test_manifest_empty_dict(self):
        manifest_dict = {}
        json_str = json.dumps(manifest_dict)
        loaded = json.loads(json_str)
        assert loaded == {}

    def test_manifest_entry_json_serializable(self):
        entry = ManifestEntry(
            sha256="sha",
            size=0,
            tier="DEEP_ARCHIVE",
            modified_at="2024-01-01T00:00:00",
            etag="etag",
        )
        # Should be JSON-serializable
        d = entry.to_dict()
        serialized = json.dumps(d)
        assert isinstance(serialized, str)


# ---------------------------------------------------------------------------
# Gzip detection placeholder
# ---------------------------------------------------------------------------


class TestGzipDetection:
    """
    Document behavior: ManifestEntry does not currently detect gzip encoding.
    The 'ignored' field can be used to mark files that should not be synced.
    This is a behavioral note rather than a functional test.
    """

    def test_ignored_flag_marks_file_as_excluded(self):
        """An entry with ignored=True should be identifiable as excluded."""
        entry = ManifestEntry(
            sha256="sha",
            size=100,
            tier="STANDARD",
            modified_at="2024-01-01T00:00:00",
            etag="etag",
            ignored=True,
        )
        assert entry.ignored is True

    def test_ignored_flag_default_is_false(self):
        """By default, entries are not ignored."""
        entry = ManifestEntry(
            sha256="sha",
            size=100,
            tier="STANDARD",
            modified_at="2024-01-01T00:00:00",
            etag="etag",
        )
        assert entry.ignored is False

    def test_ignored_preserved_through_serialization(self):
        entry = ManifestEntry(
            sha256="sha",
            size=100,
            tier="STANDARD",
            modified_at="2024-01-01T00:00:00",
            etag="etag",
            ignored=True,
        )
        restored = ManifestEntry.from_dict(entry.to_dict())
        assert restored.ignored is True
