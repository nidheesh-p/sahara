"""Tests for packaged Apple Shortcuts artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from sahara.cli import main
from sahara.shortcuts import (
    copy_shortcut_artifacts,
    load_shortcut_artifact,
    load_shortcut_artifacts,
    validate_shortcut_artifact,
)


def test_packaged_shortcut_artifacts_are_valid_and_versioned() -> None:
    artifacts = load_shortcut_artifacts()

    assert {artifact.name for artifact in artifacts} == {
        "Remember in Sahara",
        "Recall from Sahara",
    }
    for artifact in artifacts:
        assert artifact.version == "2026.06.1"
        validate_shortcut_artifact(artifact.payload)


def test_remember_shortcut_contract_supports_siri_share_sheet_and_whatsapp() -> None:
    remember = load_shortcut_artifact("remember-in-sahara.json").payload

    assert remember["siri_phrase"] == "Siri, Remember in Sahara"
    assert remember["mobile_api"]["endpoint_path"] == "/v1/memories"
    assert remember["mobile_api"]["required_scope"] == "memory:capture"
    assert "siri_dictation" in remember["inputs"]
    assert "ios_share_sheet_text" in remember["inputs"]
    assert "ios_share_sheet_url" in remember["inputs"]
    assert "clipboard_fallback" in remember["inputs"]
    assert remember["privacy"]["whatsapp_mode"] == "explicit_share_or_copy_only"
    assert remember["privacy"]["scrapes_source_apps"] is False
    assert remember["tests"]["requires_idempotency_key"] is True
    assert "path" not in remember["mobile_api"]["json_body_fields"]
    assert "storage_prefix" not in remember["mobile_api"]["json_body_fields"]


def test_recall_shortcut_never_speaks_sensitive_results() -> None:
    recall = load_shortcut_artifact("recall-from-sahara.json").payload

    assert recall["mobile_api"]["endpoint_path"] == "/v1/recall"
    assert recall["mobile_api"]["required_scope"] == "memory:recall"
    assert recall["privacy"]["speaks_results"] is False
    assert recall["tests"]["does_not_speak_sensitive_results"] is True


def test_shortcut_export_writes_valid_json_files(tmp_path: Path) -> None:
    written = copy_shortcut_artifacts(tmp_path)

    assert {path.name for path in written} == {
        "remember-in-sahara.json",
        "recall-from-sahara.json",
    }
    for path in written:
        validate_shortcut_artifact(json.loads(path.read_text(encoding="utf-8")))


def test_mobile_shortcuts_cli_lists_and_exports(tmp_path: Path) -> None:
    runner = CliRunner()

    listed = runner.invoke(main, ["mobile", "shortcuts", "list"])
    exported = runner.invoke(
        main,
        ["mobile", "shortcuts", "export", str(tmp_path)],
    )

    assert listed.exit_code == 0
    assert "Remember in Sahara" in listed.output
    assert "Recall from Sahara" in listed.output
    assert exported.exit_code == 0
    assert (tmp_path / "remember-in-sahara.json").is_file()
    assert (tmp_path / "recall-from-sahara.json").is_file()
