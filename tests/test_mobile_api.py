"""Tests for the authenticated mobile capture API."""

from __future__ import annotations

import hashlib
import json
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from sahara.cli import main
from sahara.config import SaharaConfig, save_config
from sahara.library import IndexingService
from sahara.memory import MemoryService
from sahara.mobile_api import (
    CAPTURE_SCOPE,
    RECALL_SCOPE,
    MobileAPIError,
    MobileAPIService,
    RateLimiter,
    create_mobile_device_pairing,
    hash_device_token,
    validate_bind_host,
)
from sahara.search.search_engine import IndexFileResult
from sahara.storage.state_db import StateDB


def _config(tmp_path: Path) -> SaharaConfig:
    content = tmp_path / "content"
    content.mkdir(exist_ok=True)
    return SaharaConfig(
        sync_folder=str(content),
        storage_mode="none",
        memory_folder=str(tmp_path / "memory"),
    )


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _body(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "text": "Vendor X uses net-30 terms.",
        "source_type": "mobile",
        "tags": ["vendor"],
        "idempotency_key": "mobile-retry-1",
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


def test_bind_host_defaults_to_loopback_and_rejects_public_exposure() -> None:
    assert validate_bind_host("127.0.0.1") == "127.0.0.1"
    assert validate_bind_host("localhost") == "localhost"

    for host in ("0.0.0.0", "8.8.8.8"):
        try:
            validate_bind_host(host)
        except ValueError as exc:
            assert "loopback" in str(exc) or "public" in str(exc)
        else:
            raise AssertionError(f"{host} should be rejected")

    assert (
        validate_bind_host("192.168.1.10", allow_private_network=True)
        == "192.168.1.10"
    )


def test_pairing_stores_only_token_hash_and_revoke_blocks_auth(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    with StateDB(tmp_path / "state.db") as db:
        pairing = create_mobile_device_pairing(
            db,
            name="iPhone",
            endpoint="http://127.0.0.1:8765",
            scopes=(CAPTURE_SCOPE,),
        )
        assert pairing.token.startswith("sahara_")
        raw_rows = db.conn.execute("SELECT * FROM mobile_devices").fetchall()
        assert raw_rows[0]["token_hash"] == hash_device_token(pairing.token)
        assert pairing.token not in json.dumps(dict(raw_rows[0]))

        service = MobileAPIService(config, db)
        with patch.object(
            IndexingService,
            "index_path",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ):
            status, payload = service.handle_capture(
                _headers(pairing.token),
                _body(),
            )
        assert status == HTTPStatus.CREATED
        assert payload["status"] == "saved_and_indexed"

        assert db.revoke_mobile_device("iPhone") is True
        try:
            service.handle_capture(_headers(pairing.token), _body())
        except MobileAPIError as exc:
            assert exc.status == HTTPStatus.UNAUTHORIZED
            assert exc.code == "invalid_token"
        else:
            raise AssertionError("revoked token should fail")


def test_mobile_capture_is_idempotent_and_audited_without_content(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    with StateDB(tmp_path / "state.db") as db:
        pairing = create_mobile_device_pairing(
            db,
            name="Pixel",
            endpoint="http://127.0.0.1:8765",
        )
        service = MobileAPIService(config, db)
        with patch.object(
            IndexingService,
            "index_path",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ):
            first = service.handle_capture(_headers(pairing.token), _body())
            second = service.handle_capture(_headers(pairing.token), _body())

        items = MemoryService(config, db).list()
        audits = db.list_mobile_memory_audit()

    assert first[1]["memory_id"] == second[1]["memory_id"]
    assert second[1]["status"] == "already_saved"
    assert len(items) == 1
    assert [audit["outcome"] for audit in audits] == [
        "already_saved",
        "saved_and_indexed",
    ]
    serialized = json.dumps(audits)
    assert "Vendor X uses net-30" not in serialized
    assert hashlib.sha256(b"mobile-retry-1").hexdigest() in serialized


def test_mobile_capture_accepts_shortcuts_friendly_tags(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    with StateDB(tmp_path / "state.db") as db:
        pairing = create_mobile_device_pairing(
            db,
            name="Shortcut",
            endpoint="http://127.0.0.1:8765",
        )
        service = MobileAPIService(config, db)
        with patch.object(
            IndexingService,
            "index_path",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ):
            service.handle_capture(
                _headers(pairing.token),
                _body(tags="phone-test, siri", idempotency_key="shortcut-tags"),
            )
            service.handle_capture(
                _headers(pairing.token),
                _body(
                    text="Shortcut capture without tags.",
                    tags=None,
                    idempotency_key="shortcut-no-tags",
                ),
            )

        items = MemoryService(config, db).list()

    assert {item.idempotency_key: item.tags for item in items} == {
        "shortcut-tags": ("phone-test", "siri"),
        "shortcut-no-tags": (),
    }


def test_capture_rejects_path_selection_large_requests_and_rate_limits(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    with StateDB(tmp_path / "state.db") as db:
        pairing = create_mobile_device_pairing(
            db,
            name="iPad",
            endpoint="http://127.0.0.1:8765",
        )
        service = MobileAPIService(config, db, rate_limiter=RateLimiter(limit=1))

        try:
            service.handle_capture(
                _headers(pairing.token),
                _body(path="/tmp/private"),
            )
        except MobileAPIError as exc:
            assert exc.status == HTTPStatus.BAD_REQUEST
            assert exc.code == "unsupported_field"
        else:
            raise AssertionError("path selection should fail")

        try:
            service.handle_capture(_headers(pairing.token), _body())
        except MobileAPIError as exc:
            assert exc.status == HTTPStatus.TOO_MANY_REQUESTS
            assert exc.code == "rate_limited"
        else:
            raise AssertionError("second request should be rate limited")

        fresh = MobileAPIService(config, db)
        try:
            fresh.handle_capture(
                _headers(pairing.token),
                b"{" + (b'"x":' + b'"a"' * 90_000) + b"}",
            )
        except MobileAPIError as exc:
            assert exc.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
        else:
            raise AssertionError("oversized request should fail")


def test_recall_requires_recall_scope(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with StateDB(tmp_path / "state.db") as db:
        capture_only = create_mobile_device_pairing(
            db,
            name="Watch",
            endpoint="http://127.0.0.1:8765",
            scopes=(CAPTURE_SCOPE,),
        )
        recall_enabled = create_mobile_device_pairing(
            db,
            name="Shortcut",
            endpoint="http://127.0.0.1:8765",
            scopes=(CAPTURE_SCOPE, RECALL_SCOPE),
        )
        service = MobileAPIService(config, db)

        try:
            service.handle_recall(
                _headers(capture_only.token),
                json.dumps({"query": "vendor"}).encode("utf-8"),
            )
        except MobileAPIError as exc:
            assert exc.status == HTTPStatus.FORBIDDEN
            assert exc.code == "insufficient_scope"
        else:
            raise AssertionError("capture-only token should not recall")

        with patch.object(MemoryService, "search", return_value=[]):
            status, payload = service.handle_recall(
                _headers(recall_enabled.token),
                json.dumps({"query": "vendor"}).encode("utf-8"),
            )
        assert status == HTTPStatus.OK
        assert payload == {"results": []}


def test_mobile_pair_cli_outputs_one_time_token_and_devices_hide_hash(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    save_config(_config(tmp_path), config_path)
    runner = CliRunner()

    with patch("sahara.storage.state_db.DB_PATH", tmp_path / "state.db"):
        paired = runner.invoke(
            main,
            [
                "--config",
                str(config_path),
                "mobile",
                "pair",
                "Phone",
                "--scope",
                "memory:capture",
                "--json",
            ],
        )
        devices = runner.invoke(
            main,
            ["--config", str(config_path), "mobile", "devices"],
        )

    assert paired.exit_code == 0
    payload = json.loads(paired.output)
    assert payload["token"].startswith("sahara_")
    assert payload["type"] == "sahara-mobile-pairing"
    assert devices.exit_code == 0
    assert "Phone" in devices.output
    assert payload["token"] not in devices.output
