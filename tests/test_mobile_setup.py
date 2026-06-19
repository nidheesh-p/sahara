"""Tests for guided iPhone onboarding helpers."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sahara.mobile_api import DevicePairing
from sahara.mobile_setup import (
    DEFAULT_MOBILE_PORT,
    EndpointRecommendation,
    _candidate_private_ipv4s,
    _command_candidates,
    _endpoint_rank,
    _is_allowed_private,
    _serve_command,
    _socket_candidates,
    _to_ipv4,
    _url_host,
    detect_mobile_endpoint,
    resolve_mobile_endpoint,
    write_ios_setup_bundle,
)


def _pairing() -> DevicePairing:
    return DevicePairing(
        device_id="device-123",
        name="Test iPhone",
        token="sahara_test_token",
        scopes=("memory:capture", "memory:recall"),
        endpoint="http://100.64.0.10:8765",
    )


def _endpoint(*, host: str = "100.64.0.10", source: str = "vpn") -> EndpointRecommendation:
    return EndpointRecommendation(
        host=host,
        port=8765,
        endpoint=f"http://{host}:8765",
        source=source,
        requires_private_bind=True,
    )


def test_detect_mobile_endpoint_prefers_private_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sahara.mobile_setup._candidate_private_ipv4s",
        lambda: iter(
            [
                ipaddress.ip_address("192.168.1.8"),
                ipaddress.ip_address("100.99.4.2"),
            ]
        ),
    )

    result = detect_mobile_endpoint()

    assert result.host == "100.99.4.2"
    assert result.source == "vpn"
    assert result.requires_private_bind is True


def test_detect_mobile_endpoint_falls_back_to_loopback_when_no_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sahara.mobile_setup._candidate_private_ipv4s", lambda: iter(()))

    result = detect_mobile_endpoint(port=9000)

    assert result.host == "127.0.0.1"
    assert result.endpoint == "http://127.0.0.1:9000"
    assert result.requires_private_bind is False


def test_resolve_mobile_endpoint_handles_explicit_and_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auto = EndpointRecommendation(
        host="127.0.0.1",
        port=8765,
        endpoint="http://127.0.0.1:8765",
        source="loopback",
        requires_private_bind=False,
    )
    monkeypatch.setattr("sahara.mobile_setup.detect_mobile_endpoint", lambda port: auto)

    assert resolve_mobile_endpoint(None) == auto
    explicit = resolve_mobile_endpoint("http://localhost:9999")
    assert explicit.host == "localhost"
    assert explicit.port == 9999
    assert explicit.requires_private_bind is False


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        ("https://example.com", "must start with http://"),
        ("http://", "must include a host"),
        ("http://127.0.0.1/path", "base URL without a path"),
        ("http://127.0.0.1:8765?x=1", "cannot include params, query, or fragment"),
    ],
)
def test_resolve_mobile_endpoint_rejects_invalid_urls(endpoint: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_mobile_endpoint(endpoint)


def test_write_ios_setup_bundle_writes_expected_files(tmp_path: Path) -> None:
    pairing = _pairing()
    endpoint = _endpoint()

    written = write_ios_setup_bundle(tmp_path, pairing=pairing, endpoint=endpoint)

    written_names = {path.name for path in written}
    assert {
        "pairing.json",
        "pairing-uri.txt",
        "setup-summary.json",
        "README.md",
        "index.html",
    }.issubset(written_names)
    summary = json.loads((tmp_path / "setup-summary.json").read_text(encoding="utf-8"))
    assert summary["capture_url"] == "http://100.64.0.10:8765/v1/memories"
    assert summary["recall_url"] == "http://100.64.0.10:8765/v1/recall"
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "Copy Authorization value" in html
    assert "sahara mobile serve --host 100.64.0.10 --port 8765 --allow-private-network" in html
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "Treat this folder like a secret" in readme
    assert (tmp_path / "shortcuts" / "remember-in-sahara.configured.json").is_file()
    assert (tmp_path / "shortcuts" / "recall-from-sahara.configured.json").is_file()


def test_serve_command_and_endpoint_rank() -> None:
    assert _serve_command(_endpoint()) == (
        "sahara mobile serve --host 100.64.0.10 --port 8765 --allow-private-network"
    )
    loopback = EndpointRecommendation(
        host="127.0.0.1",
        port=8765,
        endpoint="http://127.0.0.1:8765",
        source="loopback",
        requires_private_bind=False,
    )
    assert _serve_command(loopback) == "sahara mobile serve --host 127.0.0.1 --port 8765"
    assert _endpoint_rank(ipaddress.ip_address("100.64.1.10")) < _endpoint_rank(
        ipaddress.ip_address("192.168.1.5")
    )


def test_candidate_private_ipv4s_filters_duplicates_and_non_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "sahara.mobile_setup._socket_candidates",
        lambda: iter(
            [
                ipaddress.ip_address("127.0.0.1"),
                ipaddress.ip_address("192.168.1.20"),
                ipaddress.ip_address("192.168.1.20"),
            ]
        ),
    )
    monkeypatch.setattr(
        "sahara.mobile_setup._command_candidates",
        lambda: iter(
            [
                ipaddress.ip_address("8.8.8.8"),
                ipaddress.ip_address("100.64.2.5"),
            ]
        ),
    )

    assert list(_candidate_private_ipv4s()) == [
        ipaddress.ip_address("192.168.1.20"),
        ipaddress.ip_address("100.64.2.5"),
    ]


def test_socket_candidates_use_getaddrinfo_and_udp_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sahara.mobile_setup.socket.gethostname", lambda: "test-host")
    monkeypatch.setattr(
        "sahara.mobile_setup.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (None, None, None, None, ("not-an-ip", 0)),
            (None, None, None, None, ("192.168.1.7", 0)),
        ],
    )

    class FakeSocket:
        def __enter__(self) -> FakeSocket:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def connect(self, address: tuple[str, int]) -> None:
            assert address == ("198.18.0.1", 80)

        def getsockname(self) -> tuple[str, int]:
            return ("100.77.0.9", 12345)

    monkeypatch.setattr("sahara.mobile_setup.socket.socket", lambda *args, **kwargs: FakeSocket())

    assert list(_socket_candidates()) == [
        ipaddress.ip_address("192.168.1.7"),
        ipaddress.ip_address("100.77.0.9"),
    ]


def test_socket_candidates_tolerate_os_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sahara.mobile_setup.socket.gethostname", lambda: "test-host")
    monkeypatch.setattr(
        "sahara.mobile_setup.socket.getaddrinfo",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")),
    )

    class BrokenSocket:
        def __enter__(self) -> BrokenSocket:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def connect(self, address: tuple[str, int]) -> None:
            raise OSError("no route")

    monkeypatch.setattr(
        "sahara.mobile_setup.socket.socket",
        lambda *args, **kwargs: BrokenSocket(),
    )

    assert list(_socket_candidates()) == []


def test_command_candidates_read_ifconfig_and_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_which(name: str) -> str | None:
        return f"/usr/bin/{name}"

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        if command == ["ifconfig"]:
            return SimpleNamespace(stdout="inet 192.168.1.11\ninet 127.0.0.1\n")
        if command == ["ip", "-4", "addr", "show"]:
            return SimpleNamespace(stdout="inet 100.64.3.4/10\n")
        raise AssertionError(command)

    monkeypatch.setattr("sahara.mobile_setup.shutil.which", fake_which)
    monkeypatch.setattr("sahara.mobile_setup.subprocess.run", fake_run)

    assert list(_command_candidates()) == [
        ipaddress.ip_address("192.168.1.11"),
        ipaddress.ip_address("127.0.0.1"),
        ipaddress.ip_address("100.64.3.4"),
    ]


def test_command_candidates_skip_failed_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sahara.mobile_setup.shutil.which", lambda name: "/usr/bin/ifconfig")

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        raise __import__("subprocess").CalledProcessError(1, command)

    monkeypatch.setattr("sahara.mobile_setup.subprocess.run", fake_run)

    assert list(_command_candidates()) == []


def test_ip_helpers_cover_edge_cases() -> None:
    assert _to_ipv4("192.168.1.2") == ipaddress.ip_address("192.168.1.2")
    assert _to_ipv4("::1") is None
    assert _to_ipv4("not-an-ip") is None
    assert _is_allowed_private(ipaddress.ip_address("192.168.1.2")) is True
    assert _is_allowed_private(ipaddress.ip_address("169.254.1.9")) is True
    assert _is_allowed_private(ipaddress.ip_address("100.64.10.5")) is True
    assert _is_allowed_private(ipaddress.ip_address("127.0.0.1")) is False
    assert _url_host("example.local") == "example.local"
    assert _url_host("127.0.0.1") == "127.0.0.1"
    assert DEFAULT_MOBILE_PORT == 8765
