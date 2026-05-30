"""Tests for sahara.ignore_rules."""
from __future__ import annotations

from pathlib import Path

from sahara.config import DEFAULT_EXCLUDES
from sahara.ignore_rules import IgnoreRules

# ---------------------------------------------------------------------------
# Basic matching
# ---------------------------------------------------------------------------


class TestIgnoreRulesBasic:
    def test_matches_ds_store(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path)
        assert rules.matches(".DS_Store") is True

    def test_matches_tmp_extension(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path)
        assert rules.matches("somefile.tmp") is True

    def test_matches_node_modules(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path)
        assert rules.matches("node_modules/") is True

    def test_matches_nested_in_node_modules(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path)
        assert rules.matches("node_modules/inside/file.js") is True

    def test_matches_git_dir(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path)
        assert rules.matches(".git/") is True

    def test_matches_pycache(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path)
        assert rules.matches("__pycache__/") is True

    def test_does_not_match_normal_txt(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path)
        assert rules.matches("document.txt") is False

    def test_does_not_match_normal_pdf(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path)
        assert rules.matches("reports/annual.pdf") is False

    def test_does_not_match_image(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path)
        assert rules.matches("photos/vacation.jpg") is False

    def test_windows_separator_normalized(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path)
        # Backslash separators should be normalized
        assert rules.matches("node_modules\\package\\index.js") is True

    def test_swp_file_matched(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path)
        assert rules.matches("file.swp") is True

    def test_bak_file_matched(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path)
        assert rules.matches("notes.bak") is True


# ---------------------------------------------------------------------------
# Custom patterns
# ---------------------------------------------------------------------------


class TestIgnoreRulesCustomPatterns:
    def test_custom_patterns_from_config(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path, extra_patterns=["*.log", "secrets/"])
        assert rules.matches("app.log") is True
        assert rules.matches("secrets/") is True
        assert rules.matches("data.csv") is False

    def test_custom_pattern_does_not_affect_defaults(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path, extra_patterns=["*.log"])
        # Defaults still apply
        assert rules.matches(".DS_Store") is True

    def test_patterns_property_includes_defaults(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path, extra_patterns=["custom"])
        all_patterns = rules.patterns
        for pat in DEFAULT_EXCLUDES:
            assert pat in all_patterns
        assert "custom" in all_patterns


# ---------------------------------------------------------------------------
# .saharaignore file
# ---------------------------------------------------------------------------


class TestSaharaIgnoreFile:
    def test_matches_patterns_from_ignore_file(self, tmp_path: Path):
        ignore_file = tmp_path / ".saharaignore"
        ignore_file.write_text("*.secret\nbuild/\n", encoding="utf-8")
        rules = IgnoreRules(tmp_path)
        assert rules.matches("password.secret") is True
        assert rules.matches("build/") is True
        assert rules.matches("data.csv") is False

    def test_ignore_file_comments_skipped(self, tmp_path: Path):
        ignore_file = tmp_path / ".saharaignore"
        ignore_file.write_text("# This is a comment\n*.log\n", encoding="utf-8")
        rules = IgnoreRules(tmp_path)
        assert rules.matches("app.log") is True

    def test_ignore_file_blank_lines_skipped(self, tmp_path: Path):
        ignore_file = tmp_path / ".saharaignore"
        ignore_file.write_text("\n\n*.log\n\n", encoding="utf-8")
        rules = IgnoreRules(tmp_path)
        assert rules.matches("app.log") is True

    def test_no_ignore_file_still_works(self, tmp_path: Path):
        # No .saharaignore file exists
        rules = IgnoreRules(tmp_path)
        assert rules.matches("document.txt") is False

    def test_ignore_file_path_property(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path)
        assert rules.ignore_file_path == tmp_path / ".saharaignore"


# ---------------------------------------------------------------------------
# add_pattern
# ---------------------------------------------------------------------------


class TestAddPattern:
    def test_add_pattern_appends_to_file(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path)
        rules.add_pattern("*.secret")

        ignore_file = tmp_path / ".saharaignore"
        assert ignore_file.exists()
        assert "*.secret" in ignore_file.read_text()

    def test_add_pattern_takes_effect_immediately(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path)
        rules.add_pattern("*.newext")
        assert rules.matches("file.newext") is True

    def test_add_pattern_appends_to_existing_file(self, tmp_path: Path):
        ignore_file = tmp_path / ".saharaignore"
        ignore_file.write_text("*.log\n", encoding="utf-8")
        rules = IgnoreRules(tmp_path)
        rules.add_pattern("*.secret")

        content = ignore_file.read_text()
        assert "*.log" in content
        assert "*.secret" in content


# ---------------------------------------------------------------------------
# reload
# ---------------------------------------------------------------------------


class TestReload:
    def test_reload_picks_up_new_patterns(self, tmp_path: Path):
        ignore_file = tmp_path / ".saharaignore"
        ignore_file.write_text("*.log\n", encoding="utf-8")
        rules = IgnoreRules(tmp_path)
        assert rules.matches("file.newext") is False

        # Update the ignore file
        ignore_file.write_text("*.log\n*.newext\n", encoding="utf-8")
        rules.reload()
        assert rules.matches("file.newext") is True

    def test_reload_without_file_is_safe(self, tmp_path: Path):
        rules = IgnoreRules(tmp_path)
        # File does not exist, reload should not raise
        rules.reload()
        assert rules.matches("document.txt") is False
