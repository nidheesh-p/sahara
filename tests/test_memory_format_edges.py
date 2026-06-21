"""Extra edge-case coverage for memory format helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from sahara.memory.format import (
    MEMORY_DOCUMENT_KIND,
    MEMORY_ROOT_MARKER,
    MEMORY_SCHEMA_VERSION,
    classify_memory_document,
    parse_document,
    validate_memory_root_marker,
)


def _valid_document(*, body: str = "Visible body") -> str:
    return (
        "---\n"
        "schema_version: 1\n"
        "kind: sahara_memory\n"
        "id: 550e8400-e29b-41d4-a716-446655440000\n"
        'created_at: "2026-06-13T18:30:00Z"\n'
        'updated_at: "2026-06-13T18:30:00Z"\n'
        "title: Example\n"
        "source_type: manual\n"
        "source_url: https://example.com\n"
        "source_id: source-1\n"
        "tags:\n"
        "  - note\n"
        "---\n"
        f"{body}\n"
    )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("No frontmatter here", "missing YAML frontmatter"),
        ("---\n[\n---\nBody\n", "invalid YAML"),
        ("---\n- just\n- a\n- list\n---\nBody\n", "must be a mapping"),
        (_valid_document(body=""), "body is empty or too long"),
        (
            _valid_document().replace("id: 550e8400-e29b-41d4-a716-446655440000", "id: nope"),
            "Memory id must be a UUID",
        ),
        (
            _valid_document().replace("source_url: https://example.com", "source_url: ftp://example.com"),
            "absolute HTTP or HTTPS",
        ),
        (
            _valid_document().replace("tags:\n  - note\n", "tags:\n  - 42\n"),
            "list of text values",
        ),
    ],
)
def test_parse_document_rejects_invalid_shapes(raw: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_document(raw)


def test_classify_memory_document_handles_yaml_edge_cases() -> None:
    assert classify_memory_document("---\n[\n---\nBody\n") == "ordinary"
    assert (
        classify_memory_document(
            "---\nkind: sahara_memory: broken\nschema_version: 1\n---\nBody\n"
        )
        == "invalid"
    )
    assert classify_memory_document("---\n- item\n---\nBody\n") == "ordinary"


def test_validate_memory_root_marker_handles_missing_invalid_and_valid_markers(
    tmp_path: Path,
) -> None:
    assert validate_memory_root_marker(tmp_path) is False
    with pytest.raises(ValueError, match="Invalid Sahara memory root marker"):
        validate_memory_root_marker(tmp_path, required=True)

    marker_dir = tmp_path / ".sahara"
    marker_dir.mkdir()
    marker_path = marker_dir / MEMORY_ROOT_MARKER

    marker_path.write_text("{not json}", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid Sahara memory root marker"):
        validate_memory_root_marker(tmp_path)

    marker_path.write_text(
        '{"kind": "ordinary", "schema_version": 1}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Invalid Sahara memory root marker"):
        validate_memory_root_marker(tmp_path)

    marker_path.write_text(
        (
            '{"kind": "'
            + MEMORY_DOCUMENT_KIND
            + '", "schema_version": '
            + str(MEMORY_SCHEMA_VERSION)
            + "}"
        ),
        encoding="utf-8",
    )
    assert validate_memory_root_marker(tmp_path) is True
