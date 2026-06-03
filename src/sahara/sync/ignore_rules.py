"""Ignore rule handling for Sahara using pathspec."""

from __future__ import annotations

from pathlib import Path

import pathspec

from sahara.config import DEFAULT_EXCLUDES

__all__ = ["IgnoreRules"]

_SAHARAIGNORE_NAME = ".saharaignore"


class IgnoreRules:
    """Manages path exclusion rules for a sync folder.

    Rules are loaded from three sources, in priority order (last wins):
    1. DEFAULT_EXCLUDES (built-in patterns)
    2. Extra patterns from SaharaConfig.exclude_patterns
    3. .saharaignore file in the sync folder (gitignore syntax)
    """

    def __init__(
        self,
        sync_folder: Path,
        extra_patterns: list[str] | None = None,
    ) -> None:
        self._sync_folder = sync_folder
        self._ignore_file = sync_folder / _SAHARAIGNORE_NAME
        self._extra_patterns: list[str] = extra_patterns or []
        self._spec: pathspec.PathSpec = self._build_spec()

    def _load_ignore_file_patterns(self) -> list[str]:
        if not self._ignore_file.exists():
            return []
        lines: list[str] = []
        for line in self._ignore_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                lines.append(stripped)
        return lines

    def _build_spec(self) -> pathspec.PathSpec:
        patterns: list[str] = (
            DEFAULT_EXCLUDES
            + self._extra_patterns
            + self._load_ignore_file_patterns()
        )
        return pathspec.PathSpec.from_lines("gitignore", patterns)

    def reload(self) -> None:
        """Reload patterns from disk (call after .saharaignore changes)."""
        self._spec = self._build_spec()

    def matches(self, relative_path: str) -> bool:
        """Return True if *relative_path* should be excluded from sync.

        The path must be relative to the sync folder (forward-slash separated).
        """
        # Normalise separators
        norm = relative_path.replace("\\", "/")
        return self._spec.match_file(norm)

    def add_pattern(self, pattern: str) -> None:
        """Append *pattern* to the .saharaignore file and reload."""
        self._ignore_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._ignore_file, "a", encoding="utf-8") as fh:
            fh.write(f"{pattern}\n")
        self.reload()

    @property
    def ignore_file_path(self) -> Path:
        return self._ignore_file

    @property
    def patterns(self) -> list[str]:
        """Return the full list of active patterns."""
        return (
            DEFAULT_EXCLUDES
            + self._extra_patterns
            + self._load_ignore_file_patterns()
        )
