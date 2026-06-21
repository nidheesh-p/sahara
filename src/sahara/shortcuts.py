"""Versioned Apple Shortcuts artifacts for Sahara mobile capture."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

__all__ = [
    "ShortcutArtifact",
    "configure_shortcut_artifact",
    "copy_shortcut_artifacts",
    "copy_configured_shortcut_artifacts",
    "load_shortcut_artifact",
    "load_shortcut_artifacts",
    "validate_shortcut_artifact",
]

ARTIFACT_PACKAGE = "sahara.data.shortcuts"
ARTIFACT_NAMES = (
    "remember-in-sahara.json",
    "recall-from-sahara.json",
)
TOKEN_PLACEHOLDER = "${SAHARA_MOBILE_TOKEN}"


@dataclass(frozen=True)
class ShortcutArtifact:
    """One versioned Shortcut blueprint."""

    filename: str
    payload: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.payload["name"])

    @property
    def version(self) -> str:
        return str(self.payload["version"])

    @property
    def required_scope(self) -> str:
        return str(self.payload["mobile_api"]["required_scope"])


def load_shortcut_artifact(filename: str) -> ShortcutArtifact:
    """Load and validate one packaged Shortcut artifact."""
    if filename not in ARTIFACT_NAMES:
        raise ValueError(f"Unknown Shortcut artifact: {filename}")
    raw = resources.files(ARTIFACT_PACKAGE).joinpath(filename).read_text(
        encoding="utf-8"
    )
    payload = json.loads(raw)
    validate_shortcut_artifact(payload)
    return ShortcutArtifact(filename=filename, payload=payload)


def load_shortcut_artifacts() -> list[ShortcutArtifact]:
    """Load all packaged Shortcut artifacts."""
    return [load_shortcut_artifact(name) for name in ARTIFACT_NAMES]


def copy_shortcut_artifacts(destination: Path) -> list[Path]:
    """Copy all Shortcut artifacts into *destination* and return written paths."""
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for artifact in load_shortcut_artifacts():
        target = destination / artifact.filename
        target.write_text(
            json.dumps(artifact.payload, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append(target)
    return written


def configure_shortcut_artifact(
    artifact: ShortcutArtifact,
    *,
    endpoint: str,
    token: str,
) -> ShortcutArtifact:
    """Return a configured artifact with endpoint and token injected."""
    configured = deepcopy(artifact.payload)
    base_url = endpoint.rstrip("/")
    api = configured["mobile_api"]
    api["endpoint"] = base_url + str(api["endpoint_path"])
    api["headers"]["Authorization"] = str(api["headers"]["Authorization"]).replace(
        TOKEN_PLACEHOLDER,
        token,
    )
    configured["setup"] = {
        "base_url": base_url,
        "authorization_header": api["headers"]["Authorization"],
    }
    return ShortcutArtifact(
        filename=artifact.filename.removesuffix(".json") + ".configured.json",
        payload=configured,
    )


def copy_configured_shortcut_artifacts(
    destination: Path,
    *,
    endpoint: str,
    token: str,
) -> list[Path]:
    """Copy configured Shortcut artifacts into *destination*."""
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for artifact in load_shortcut_artifacts():
        configured = configure_shortcut_artifact(
            artifact,
            endpoint=endpoint,
            token=token,
        )
        target = destination / configured.filename
        target.write_text(
            json.dumps(configured.payload, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append(target)
    return written


def validate_shortcut_artifact(payload: dict[str, Any]) -> None:
    """Validate the repo-owned Shortcut artifact contract."""
    required_top_level = {
        "schema_version",
        "version",
        "name",
        "siri_phrase",
        "summary",
        "mobile_api",
        "inputs",
        "privacy",
        "steps",
        "tests",
    }
    missing = required_top_level - payload.keys()
    if missing:
        raise ValueError(f"Shortcut artifact missing fields: {sorted(missing)}")
    if payload["schema_version"] != 1:
        raise ValueError("Unsupported Shortcut artifact schema_version")

    api = payload["mobile_api"]
    if api["method"] != "POST":
        raise ValueError("Shortcut mobile_api.method must be POST")
    if api["endpoint_path"] not in {"/v1/memories", "/v1/recall"}:
        raise ValueError("Shortcut endpoint_path is not supported")
    if api["required_scope"] not in {"memory:capture", "memory:recall"}:
        raise ValueError("Shortcut required_scope is not supported")
    if "Authorization" not in api["headers"]:
        raise ValueError("Shortcut must send Authorization header")
    if api["headers"].get("Content-Type") != "application/json":
        raise ValueError("Shortcut must send JSON")

    privacy = payload["privacy"]
    if privacy.get("scrapes_source_apps") is not False:
        raise ValueError("Shortcut must not scrape source apps")
    if api["endpoint_path"] == "/v1/recall" and privacy.get("speaks_results") is not False:
        raise ValueError("Recall Shortcut must not speak sensitive results automatically")
    if api["endpoint_path"] == "/v1/memories":
        fields = set(api["json_body_fields"])
        required = {"text", "source_type", "idempotency_key"}
        if not required.issubset(fields):
            raise ValueError("Capture Shortcut is missing required JSON fields")
        if "path" in fields or "storage_prefix" in fields or "sync_enabled" in fields:
            raise ValueError("Shortcut cannot select paths or sync behavior")
        inputs = set(payload["inputs"])
        if "clipboard_fallback" not in inputs:
            raise ValueError("Capture Shortcut must include clipboard fallback")
