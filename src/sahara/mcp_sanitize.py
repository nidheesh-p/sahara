"""Make indexed text inert before it is returned to an MCP client.

Indexed files are untrusted. These helpers strip characters that can hide or
reorder text from a reader (terminal escapes, control, zero-width and bidi
characters) and bound the length of what is returned. They do not, and cannot,
stop a client model from following instructions written in plain text.
"""

from __future__ import annotations

import re

__all__ = ["sanitize_snippet"]

# CSI (ESC [ ... final), OSC (ESC ] ... BEL or ST) and two-byte ESC sequences.
_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[@-Z\\-_]"
)
# C0 except \t and \n, DEL, C1, zero-width, bidi embedding/override/isolate, BOM.
_INVISIBLE_RE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f"
    "​-‏‪-‮⁦-⁩﻿]"
)
# A newline followed by three or more blank (or whitespace-only) lines.
_BLANK_RUN_RE = re.compile(r"\n(?:[ \t]*\n){3,}")


def sanitize_snippet(text: str, max_chars: int) -> str:
    """Return ``text`` without hidden characters, truncated to ``max_chars``.

    Pure and idempotent: ``sanitize_snippet(sanitize_snippet(t, n), n)`` equals
    ``sanitize_snippet(t, n)``.
    """
    if max_chars <= 0 or not text:
        return ""
    cleaned = _ANSI_RE.sub("", text)
    cleaned = _INVISIBLE_RE.sub("", cleaned)
    cleaned = _BLANK_RUN_RE.sub("\n\n", cleaned)
    return cleaned[:max_chars]
