"""Cheap pre-index folder scan used by first-run to preview indexing size."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sahara.search.search_engine import TextExtractor
from sahara.sync.ignore_rules import IgnoreRules

__all__ = ["MAX_ENTRIES", "FolderSummary", "format_summary", "summarize_folder"]

MAX_ENTRIES = 200_000

_DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".doc", ".epub"}
SUPPORTED_EXTENSIONS = frozenset(TextExtractor.SUPPORTED_EXTENSIONS | _DOCUMENT_EXTENSIONS)


@dataclass(frozen=True)
class FolderSummary:
    supported_file_count: int
    total_bytes: int
    unsupported_count: int
    is_lower_bound: bool = False


def _inside(root: Path, target: str) -> bool:
    try:
        return Path(os.path.realpath(target)).is_relative_to(root)
    except (OSError, ValueError):
        return False


def summarize_folder(
    path: Path,
    ignore_rules: IgnoreRules | None = None,
) -> FolderSummary:
    """Count indexable files under *path* without reading any file contents.

    Uses the same ignore semantics as ``Library._walk``. Symlinked directories
    are never descended into and symlinked files are only counted when they
    resolve inside *path*. Stops after ``MAX_ENTRIES`` entries, in which case
    the counts are a lower bound.
    """
    root = Path(os.path.realpath(path))
    ignore = ignore_rules if ignore_rules is not None else IgnoreRules(root)
    supported = 0
    total_bytes = 0
    unsupported = 0
    entries = 0

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        relative_dir = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if relative_dir == "." else relative_dir + "/"
        kept: list[str] = []
        for name in dirnames:
            entries += 1
            if entries > MAX_ENTRIES:
                return FolderSummary(supported, total_bytes, unsupported, True)
            if not ignore.matches(prefix + name + "/"):
                kept.append(name)
        dirnames[:] = kept

        for filename in filenames:
            entries += 1
            if entries > MAX_ENTRIES:
                return FolderSummary(supported, total_bytes, unsupported, True)
            relative_path = prefix + filename
            if ignore.matches(relative_path):
                continue
            file_path = os.path.join(dirpath, filename)
            if os.path.islink(file_path) and not _inside(root, file_path):
                continue
            try:
                stat = os.stat(file_path)
            except OSError:
                continue
            if not os.path.isfile(file_path):
                continue
            if os.path.splitext(filename)[1].lower() in SUPPORTED_EXTENSIONS:
                supported += 1
                total_bytes += stat.st_size
            else:
                unsupported += 1

    return FolderSummary(supported, total_bytes, unsupported, False)


def format_summary(summary: FolderSummary) -> str:
    """Return e.g. ``Found 1,234 files (~5.2 MB) to index``."""
    count = f"{summary.supported_file_count:,}"
    if summary.is_lower_bound:
        count += "+"
    noun = "file" if summary.supported_file_count == 1 and not summary.is_lower_bound else "files"
    megabytes = summary.total_bytes / (1024 * 1024)
    return f"Found {count} {noun} (~{megabytes:.1f} MB) to index"
