"""Shared file-hashing utilities."""

from __future__ import annotations

import hashlib
from pathlib import Path

__all__ = ["compute_sha256"]


def compute_sha256(path: Path) -> str:
    """Return hex SHA-256 digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
