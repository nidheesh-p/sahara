"""Portable Markdown format helpers for captured knowledge."""

from __future__ import annotations

import datetime
import json
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml

MEMORY_SCHEMA_VERSION = 1
MEMORY_DOCUMENT_KIND = "sahara_memory"
MEMORY_ROOT_MARKER = "memory-root.json"
SOURCE_TYPES = {"manual", "web", "conversation", "ai-chat", "mobile"}
MAX_MEMORY_CHARS = 200_000
MAX_TITLE_CHARS = 160
MAX_TAGS = 32
MAX_TAG_CHARS = 64
MAX_SOURCE_URL_CHARS = 2_048
MAX_SOURCE_ID_CHARS = 256


def render_document(metadata: Mapping[str, Any], text: str) -> str:
    """Render validated memory metadata and body as Markdown."""
    frontmatter = yaml.safe_dump(
        dict(metadata),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    return f"---\n{frontmatter}\n---\n{text}\n"


def parse_document(raw: str) -> tuple[dict[str, Any], str]:
    """Parse and validate a Sahara memory Markdown document."""
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", raw, re.DOTALL)
    if match is None:
        raise ValueError("Memory file is missing YAML frontmatter")
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValueError("Memory frontmatter is invalid YAML") from exc
    if not isinstance(loaded, dict):
        raise ValueError("Memory frontmatter must be a mapping")

    required_strings = (
        "id",
        "created_at",
        "updated_at",
        "title",
        "source_type",
        "source_url",
        "source_id",
    )
    schema_version = loaded.get("schema_version")
    if type(schema_version) is not int or schema_version != MEMORY_SCHEMA_VERSION:
        raise ValueError("Unsupported memory schema version")
    if loaded.get("kind") != MEMORY_DOCUMENT_KIND:
        raise ValueError("Unsupported memory document kind")
    for field_name in required_strings:
        if not isinstance(loaded.get(field_name), str):
            raise ValueError(f"Memory field '{field_name}' must be text")
    tags = loaded.get("tags")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("Memory field 'tags' must be a list of text values")
    if not loaded["title"] or len(loaded["title"]) > MAX_TITLE_CHARS:
        raise ValueError("Memory title is empty or too long")
    if loaded["source_type"] not in SOURCE_TYPES:
        raise ValueError("Unsupported memory source type")
    if len(loaded["source_url"]) > MAX_SOURCE_URL_CHARS:
        raise ValueError("Memory source URL is too long")
    if loaded["source_url"]:
        parsed_url = urlparse(loaded["source_url"])
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("Memory source URL must be absolute HTTP or HTTPS")
    if len(loaded["source_id"]) > MAX_SOURCE_ID_CHARS:
        raise ValueError("Memory source ID is too long")
    if len(tags) > MAX_TAGS or any(
        not tag.strip() or len(tag) > MAX_TAG_CHARS for tag in tags
    ):
        raise ValueError("Memory tags are invalid or too numerous")
    for field_name in ("created_at", "updated_at"):
        try:
            timestamp = datetime.datetime.fromisoformat(
                loaded[field_name].replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError(f"Memory field '{field_name}' is not an ISO timestamp") from exc
        if timestamp.tzinfo is None:
            raise ValueError(f"Memory field '{field_name}' must include a timezone")
    try:
        uuid.UUID(loaded["id"])
    except ValueError as exc:
        raise ValueError("Memory id must be a UUID") from exc
    body = match.group(2)
    if body.endswith("\r\n"):
        body = body[:-2]
    elif body.endswith("\n"):
        body = body[:-1]
    if not body.strip() or len(body) > MAX_MEMORY_CHARS:
        raise ValueError("Memory body is empty or too long")
    return loaded, body


def searchable_text(raw: str) -> str:
    """Return semantic-search text without operational metadata."""
    metadata, body = parse_document(raw)
    parts = [metadata["title"]]
    if metadata["tags"]:
        parts.append("Tags: " + ", ".join(metadata["tags"]))
    if body:
        parts.append(body)
    return "\n\n".join(parts)


def is_memory_document(raw: str) -> bool:
    """Return whether text declares the Sahara memory document kind."""
    return classify_memory_document(raw) == "valid"


def classify_memory_document(
    raw: str,
) -> Literal["ordinary", "valid", "invalid"]:
    """Classify Markdown without exposing malformed claimed-memory metadata."""
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", raw, re.DOTALL)
    if match is None:
        return "ordinary"
    frontmatter = match.group(1)
    try:
        loaded = yaml.safe_load(frontmatter)
    except yaml.YAMLError:
        claims_memory = re.search(
            r"(?is)(?:^|[\s{,])['\"]?kind['\"]?\s*:\s*"
            r"(?:!!str\s*)?['\"]?sahara_memory\b",
            frontmatter,
        )
        return "invalid" if claims_memory else "ordinary"
    if not isinstance(loaded, dict):
        return "ordinary"
    if loaded.get("kind") != MEMORY_DOCUMENT_KIND:
        memory_fields = {
            "schema_version",
            "source_type",
            "source_url",
            "source_id",
            "created_at",
            "updated_at",
        }
        claimed_fields = memory_fields.intersection(loaded)
        resembles_memory = (
            isinstance(loaded.get("kind"), str)
            and loaded["kind"].casefold().startswith("sahara")
            and bool(claimed_fields)
        )
        return "invalid" if resembles_memory else "ordinary"
    try:
        parse_document(raw)
    except ValueError:
        return "invalid"
    return "valid"


def validate_memory_root_marker(root: Path, *, required: bool = False) -> bool:
    """Return whether root is managed memory, raising for an invalid marker."""
    marker_path = root / ".sahara" / MEMORY_ROOT_MARKER
    if not marker_path.exists() and not marker_path.is_symlink():
        if required:
            raise ValueError("Invalid Sahara memory root marker")
        return False
    if marker_path.is_symlink() or not marker_path.is_file():
        raise ValueError("Invalid Sahara memory root marker")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid Sahara memory root marker") from exc
    if marker != {
        "kind": MEMORY_DOCUMENT_KIND,
        "schema_version": MEMORY_SCHEMA_VERSION,
    }:
        raise ValueError("Invalid Sahara memory root marker")
    return True
