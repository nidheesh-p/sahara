"""Tests for packaged Apple Shortcuts artifacts."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from sahara.cli import main
from sahara.shortcuts import (
    configure_shortcut_artifact,
    copy_configured_shortcut_artifacts,
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


def test_configured_shortcut_export_injects_endpoint_and_token(tmp_path: Path) -> None:
    written = copy_configured_shortcut_artifacts(
        tmp_path,
        endpoint="http://100.64.1.10:8765",
        token="sahara_example_token",
    )

    assert {path.name for path in written} == {
        "remember-in-sahara.configured.json",
        "recall-from-sahara.configured.json",
    }
    remember = json.loads(
        (tmp_path / "remember-in-sahara.configured.json").read_text(encoding="utf-8")
    )
    recall = json.loads(
        (tmp_path / "recall-from-sahara.configured.json").read_text(encoding="utf-8")
    )
    assert remember["mobile_api"]["endpoint"] == "http://100.64.1.10:8765/v1/memories"
    assert recall["mobile_api"]["endpoint"] == "http://100.64.1.10:8765/v1/recall"
    assert remember["mobile_api"]["headers"]["Authorization"] == (
        "Bearer sahara_example_token"
    )
    assert recall["mobile_api"]["headers"]["Authorization"] == (
        "Bearer sahara_example_token"
    )


def test_configure_shortcut_artifact_preserves_contract() -> None:
    artifact = load_shortcut_artifact("remember-in-sahara.json")

    configured = configure_shortcut_artifact(
        artifact,
        endpoint="http://100.64.1.10:8765",
        token="sahara_example_token",
    )

    validate_shortcut_artifact(configured.payload)
    assert configured.payload["setup"]["base_url"] == "http://100.64.1.10:8765"


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

def test_load_shortcut_artifact_rejects_unknown_filename() -> None:
    with pytest.raises(ValueError, match="Unknown Shortcut artifact"):
        load_shortcut_artifact("missing.json")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload.pop("tests"),
            "Shortcut artifact missing fields",
        ),
        (
            lambda payload: payload.__setitem__("schema_version", 2),
            "Unsupported Shortcut artifact schema_version",
        ),
        (
            lambda payload: payload["mobile_api"].__setitem__("method", "GET"),
            "Shortcut mobile_api.method must be POST",
        ),
        (
            lambda payload: payload["mobile_api"].__setitem__("endpoint_path", "/v1/other"),
            "Shortcut endpoint_path is not supported",
        ),
        (
            lambda payload: payload["mobile_api"].__setitem__("required_scope", "memory:delete"),
            "Shortcut required_scope is not supported",
        ),
        (
            lambda payload: payload["mobile_api"]["headers"].pop("Authorization"),
            "Shortcut must send Authorization header",
        ),
        (
            lambda payload: payload["mobile_api"]["headers"].__setitem__("Content-Type", "text/plain"),
            "Shortcut must send JSON",
        ),
        (
            lambda payload: payload["privacy"].__setitem__("scrapes_source_apps", True),
            "Shortcut must not scrape source apps",
        ),
        (
            lambda payload: payload["privacy"].__setitem__("speaks_results", True),
            "Recall Shortcut must not speak sensitive results automatically",
        ),
        (
            lambda payload: payload["mobile_api"].__setitem__(
                "json_body_fields",
                ["title"],
            ),
            "Capture Shortcut is missing required JSON fields",
        ),
        (
            lambda payload: payload["mobile_api"].__setitem__(
                "json_body_fields",
                ["text", "source_type", "idempotency_key", "path"],
            ),
            "Shortcut cannot select paths or sync behavior",
        ),
        (
            lambda payload: payload.__setitem__(
                "inputs",
                [item for item in payload["inputs"] if item != "clipboard_fallback"],
            ),
            "Capture Shortcut must include clipboard fallback",
        ),
    ],
)
def test_validate_shortcut_artifact_rejects_invalid_contract(
    mutate,
    message: str,
) -> None:
    if "Recall" in message:
        payload = copy.deepcopy(load_shortcut_artifact("recall-from-sahara.json").payload)
    else:
        payload = copy.deepcopy(load_shortcut_artifact("remember-in-sahara.json").payload)
    mutate(payload)

    with pytest.raises(ValueError, match=message):
        validate_shortcut_artifact(payload)


def test_mobile_setup_ios_cli_writes_guided_bundle(tmp_path: Path) -> None:
    from unittest.mock import patch

    from sahara.config import SaharaConfig, save_config

    config_path = tmp_path / "config.toml"
    save_config(
        SaharaConfig(sync_folder=str(tmp_path / "content"), storage_mode="none"),
        config_path,
    )
    runner = CliRunner()
    destination = tmp_path / "ios-setup"

    with patch("sahara.storage.state_db.DB_PATH", tmp_path / "state.db"):
        result = runner.invoke(
            main,
            [
                "--config",
                str(config_path),
                "mobile",
                "setup-ios",
                str(destination),
                "--name",
                "Nidheesh iPhone",
                "--endpoint",
                "http://100.64.1.10:8765",
            ],
        )

    assert result.exit_code == 0
    assert "Created iPhone setup bundle" in result.output
    assert (destination / "README.md").is_file()
    assert (destination / "index.html").is_file()
    assert (destination / "pairing.json").is_file()
    assert (destination / "pairing-uri.txt").is_file()
    assert (destination / "pairing-qr.svg").is_file()
    assert (destination / "healthcheck-qr.svg").is_file()
    assert (destination / "setup-summary.json").is_file()
    remember = json.loads(
        (destination / "shortcuts" / "remember-in-sahara.configured.json").read_text(
            encoding="utf-8"
        )
    )
    assert remember["mobile_api"]["endpoint"] == "http://100.64.1.10:8765/v1/memories"
    assert remember["mobile_api"]["headers"]["Authorization"].startswith("Bearer sahara_")
    assert "--allow-private-network" in (destination / "README.md").read_text(
        encoding="utf-8"
    )
