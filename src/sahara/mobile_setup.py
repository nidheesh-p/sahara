"""Helpers for guided iPhone onboarding."""

from __future__ import annotations

import html
import io
import ipaddress
import json
import re
import shlex
import shutil
import socket
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import qrcode
from qrcode.constants import ERROR_CORRECT_M
from qrcode.image.svg import SvgPathImage

from sahara.mobile_api import DevicePairing, pairing_uri, validate_bind_host
from sahara.shortcuts import copy_configured_shortcut_artifacts

DEFAULT_MOBILE_PORT = 8765

__all__ = [
    "DEFAULT_MOBILE_PORT",
    "EndpointRecommendation",
    "detect_mobile_endpoint",
    "resolve_mobile_endpoint",
    "write_ios_setup_bundle",
]


@dataclass(frozen=True)
class EndpointRecommendation:
    host: str
    port: int
    endpoint: str
    source: str
    requires_private_bind: bool


def resolve_mobile_endpoint(
    endpoint: str | None,
    *,
    port: int = DEFAULT_MOBILE_PORT,
) -> EndpointRecommendation:
    """Resolve an explicit or auto-detected mobile endpoint."""
    if endpoint:
        return _parse_explicit_endpoint(endpoint)
    return detect_mobile_endpoint(port=port)


def detect_mobile_endpoint(*, port: int = DEFAULT_MOBILE_PORT) -> EndpointRecommendation:
    """Detect the best private endpoint for mobile onboarding."""
    candidates = list(_candidate_private_ipv4s())
    if not candidates:
        return EndpointRecommendation(
            host="127.0.0.1",
            port=port,
            endpoint=f"http://127.0.0.1:{port}",
            source="loopback",
            requires_private_bind=False,
        )

    ranked = sorted(candidates, key=_endpoint_rank)
    chosen = ranked[0]
    source = "vpn" if _is_cgnat(chosen) else "lan"
    return EndpointRecommendation(
        host=str(chosen),
        port=port,
        endpoint=f"http://{chosen}:{port}",
        source=source,
        requires_private_bind=True,
    )


def write_ios_setup_bundle(
    destination: Path,
    *,
    pairing: DevicePairing,
    endpoint: EndpointRecommendation,
) -> list[Path]:
    """Write a guided iPhone onboarding bundle."""
    destination.mkdir(parents=True, exist_ok=True)
    shortcuts_dir = destination / "shortcuts"
    written = copy_configured_shortcut_artifacts(
        shortcuts_dir,
        endpoint=pairing.endpoint,
        token=pairing.token,
    )

    payload_path = destination / "pairing.json"
    payload_path.write_text(
        json.dumps(pairing.payload(), indent=2) + "\n",
        encoding="utf-8",
    )
    written.append(payload_path)

    uri_path = destination / "pairing-uri.txt"
    uri_path.write_text(pairing_uri(pairing.payload()) + "\n", encoding="utf-8")
    written.append(uri_path)

    summary_path = destination / "setup-summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "device_name": pairing.name,
                "device_id": pairing.device_id,
                "endpoint": endpoint.endpoint,
                "endpoint_source": endpoint.source,
                "capture_url": pairing.endpoint.rstrip("/") + "/v1/memories",
                "recall_url": pairing.endpoint.rstrip("/") + "/v1/recall",
                "healthcheck_url": pairing.endpoint.rstrip("/") + "/healthz",
                "pairing_uri": pairing_uri(pairing.payload()),
                "requires_private_bind": endpoint.requires_private_bind,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    written.append(summary_path)

    readme_path = destination / "README.md"
    readme_path.write_text(_render_setup_readme(pairing, endpoint), encoding="utf-8")
    written.append(readme_path)

    pairing_qr_path = destination / "pairing-qr.svg"
    pairing_qr_path.write_text(
        _build_qr_svg(pairing_uri(pairing.payload())),
        encoding="utf-8",
    )
    written.append(pairing_qr_path)

    health_qr_path = destination / "healthcheck-qr.svg"
    health_qr_path.write_text(
        _build_qr_svg(pairing.endpoint.rstrip("/") + "/healthz"),
        encoding="utf-8",
    )
    written.append(health_qr_path)

    html_path = destination / "index.html"
    html_path.write_text(_render_setup_html(pairing, endpoint), encoding="utf-8")
    written.append(html_path)
    return written


def _parse_explicit_endpoint(endpoint: str) -> EndpointRecommendation:
    parsed = urlparse(endpoint.strip())
    if parsed.scheme != "http":
        raise ValueError("Mobile setup endpoint must start with http://")
    if not parsed.hostname:
        raise ValueError("Mobile setup endpoint must include a host")
    if parsed.params or parsed.query or parsed.fragment:
        raise ValueError("Mobile setup endpoint cannot include params, query, or fragment")
    if parsed.path not in ("", "/"):
        raise ValueError("Mobile setup endpoint must be a base URL without a path")

    host = parsed.hostname
    if host == "localhost":
        normalized = validate_bind_host(host)
        requires_private_bind = False
    else:
        try:
            normalized = validate_bind_host(host, allow_private_network=True)
            requires_private_bind = not ipaddress.ip_address(normalized).is_loopback
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
    port = parsed.port or DEFAULT_MOBILE_PORT
    return EndpointRecommendation(
        host=normalized,
        port=port,
        endpoint=f"http://{_url_host(normalized)}:{port}",
        source="explicit",
        requires_private_bind=requires_private_bind,
    )


def _render_setup_readme(pairing: DevicePairing, endpoint: EndpointRecommendation) -> str:
    serve_command = _serve_command(endpoint)
    base_url = pairing.endpoint.rstrip("/")
    capture_payload = _capture_test_payload()
    recall_payload = _recall_test_payload()
    return (
        "# Sahara iPhone Setup\n\n"
        "This folder contains a preconfigured iPhone onboarding bundle for Sahara.\n\n"
        "## Start the mobile API\n\n"
        "```bash\n"
        f"{serve_command}\n"
        "```\n\n"
        "Use this health check from the phone after the server starts:\n\n"
        f"`{base_url}/healthz`\n\n"
        "## Files in this bundle\n\n"
        "- `shortcuts/remember-in-sahara.configured.json`: prefilled capture blueprint.\n"
        "- `shortcuts/recall-from-sahara.configured.json`: prefilled recall blueprint.\n"
        "- `pairing.json`: the one-time device token payload.\n"
        "- `pairing-uri.txt`: compact pairing URI for future QR/import tooling.\n"
        "- `pairing-qr.svg`: QR for the pairing URI.\n"
        "- `healthcheck-qr.svg`: QR for the network health test.\n"
        "- `index.html`: browser-friendly setup page with copy buttons and step-by-step testing.\n\n"
        "## Important values\n\n"
        f"- Device name: `{pairing.name}`\n"
        f"- Endpoint: `{base_url}`\n"
        f"- Capture URL: `{base_url}/v1/memories`\n"
        f"- Recall URL: `{base_url}/v1/recall`\n"
        f"- Endpoint source: `{endpoint.source}`\n\n"
        "## Suggested manual smoke tests\n\n"
        "Capture body:\n\n"
        "```json\n"
        f"{capture_payload}\n"
        "```\n\n"
        "Recall body:\n\n"
        "```json\n"
        f"{recall_payload}\n"
        "```\n\n"
        "Treat this folder like a secret because it includes the bearer token.\n"
    )


def _render_setup_html(pairing: DevicePairing, endpoint: EndpointRecommendation) -> str:
    base_url = pairing.endpoint.rstrip("/")
    capture_url = base_url + "/v1/memories"
    recall_url = base_url + "/v1/recall"
    health_url = base_url + "/healthz"
    pairing_value = pairing_uri(pairing.payload())
    auth_header = f"Bearer {pairing.token}"
    serve_command = _serve_command(endpoint)
    capture_payload = html.escape(_capture_test_payload())
    recall_payload = html.escape(_recall_test_payload())
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sahara iPhone Setup</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f3ec;
      --panel: #fffdf8;
      --ink: #1f2933;
      --muted: #5b6470;
      --accent: #0f766e;
      --line: #d8d2c8;
      --code: #f1ede5;
      --card: #fbf6ee;
    }}
    body {{
      margin: 0;
      font-family: "SF Pro Text", "Helvetica Neue", sans-serif;
      background: radial-gradient(circle at top, #fffdf8 0%, var(--bg) 65%);
      color: var(--ink);
    }}
    main {{
      max-width: 860px;
      margin: 0 auto;
      padding: 32px 20px 56px;
    }}
    p {{
      line-height: 1.55;
    }}
    h1, h2 {{
      line-height: 1.1;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 18px;
      margin: 16px 0;
      box-shadow: 0 10px 30px rgba(15, 23, 42, 0.05);
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
    }}
    .step {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 28px;
      height: 28px;
      border-radius: 999px;
      background: var(--accent);
      color: white;
      font-weight: 700;
      margin-right: 8px;
    }}
    code, pre {{
      font-family: "SFMono-Regular", Menlo, monospace;
      background: var(--code);
      border-radius: 10px;
    }}
    code {{
      padding: 2px 6px;
    }}
    pre {{
      padding: 14px;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .row {{
      display: grid;
      gap: 8px;
      margin: 12px 0;
    }}
    button {{
      width: fit-content;
      border: 0;
      border-radius: 999px;
      padding: 10px 14px;
      background: var(--accent);
      color: white;
      font: inherit;
      cursor: pointer;
    }}
    .muted {{
      color: var(--muted);
    }}
    img.qr {{
      width: min(100%, 220px);
      height: auto;
      border-radius: 12px;
      background: white;
      border: 1px solid var(--line);
      padding: 8px;
    }}
  </style>
</head>
<body>
  <main>
    <h1>Sahara iPhone Setup</h1>
    <p class="muted">This bundle is preconfigured for <strong>{html.escape(pairing.name)}</strong>. Treat it like a secret because it contains the mobile bearer token.</p>

    <section class="panel">
      <h2><span class="step">1</span>Start Sahara</h2>
      <p>Run the mobile API on the desktop Sahara machine before touching the phone.</p>
      <pre>{html.escape(serve_command)}</pre>
      <div class="row">
        <div><strong>Health check</strong></div>
        <code id="health">{html.escape(health_url)}</code>
        <button data-copy="health">Copy health URL</button>
      </div>
    </section>

    <section class="panel">
      <h2><span class="step">2</span>Verify from iPhone</h2>
      <p>Open the health URL or scan the QR code from the phone. A successful response is <code>{{"status":"ok"}}</code>.</p>
      <div class="grid">
        <div class="card">
          <div><strong>Health URL</strong></div>
          <code id="health-duplicate">{html.escape(health_url)}</code>
          <p><img class="qr" src="healthcheck-qr.svg" alt="QR code for Sahara health check"></p>
          <button data-copy="health-duplicate">Copy health URL</button>
        </div>
        <div class="card">
          <div><strong>Pairing URI</strong></div>
          <code id="pairing-uri">{html.escape(pairing_value)}</code>
          <p><img class="qr" src="pairing-qr.svg" alt="QR code for Sahara pairing URI"></p>
          <button data-copy="pairing-uri">Copy pairing URI</button>
        </div>
      </div>
    </section>

    <section class="panel">
      <h2><span class="step">3</span>Wire the Shortcuts</h2>
      <p>The configured blueprint files live in the <code>shortcuts/</code> folder. If you still need to edit values manually, these are the three fields that matter most.</p>
      <div class="row">
        <div><strong>Capture URL</strong></div>
        <code id="capture">{html.escape(capture_url)}</code>
        <button data-copy="capture">Copy capture URL</button>
      </div>
      <div class="row">
        <div><strong>Recall URL</strong></div>
        <code id="recall">{html.escape(recall_url)}</code>
        <button data-copy="recall">Copy recall URL</button>
      </div>
      <div class="row">
        <div><strong>Authorization header value</strong></div>
        <code id="auth">{html.escape(auth_header)}</code>
        <button data-copy="auth">Copy Authorization value</button>
      </div>
    </section>

    <section class="panel">
      <h2><span class="step">4</span>Run Smoke Tests</h2>
      <p>These JSON bodies are ready for a first capture and first recall test from the phone.</p>
      <div class="grid">
        <div class="card">
          <div><strong>Capture test body</strong></div>
          <pre id="capture-payload">{capture_payload}</pre>
          <button data-copy="capture-payload">Copy capture JSON</button>
        </div>
        <div class="card">
          <div><strong>Recall test body</strong></div>
          <pre id="recall-payload">{recall_payload}</pre>
          <button data-copy="recall-payload">Copy recall JSON</button>
        </div>
      </div>
    </section>
  </main>
  <script>
    document.querySelectorAll("[data-copy]").forEach((button) => {{
      button.addEventListener("click", async () => {{
        const id = button.getAttribute("data-copy");
        const value = document.getElementById(id)?.textContent ?? "";
        await navigator.clipboard.writeText(value);
        const original = button.textContent;
        button.textContent = "Copied";
        setTimeout(() => {{
          button.textContent = original ?? "Copy";
        }}, 1200);
      }});
    }});
  </script>
</body>
</html>
"""


def _serve_command(endpoint: EndpointRecommendation) -> str:
    parts = ["sahara", "mobile", "serve", "--host", endpoint.host, "--port", str(endpoint.port)]
    if endpoint.requires_private_bind:
        parts.append("--allow-private-network")
    return " ".join(shlex.quote(part) for part in parts)


def _capture_test_payload() -> str:
    return json.dumps(
        {
            "text": "Testing Sahara mobile capture from iPhone setup.",
            "source_type": "mobile",
            "tags": "phone-test",
            "idempotency_key": "iphone-setup-test-1",
        },
        indent=2,
    )


def _recall_test_payload() -> str:
    return json.dumps(
        {
            "query": "Testing Sahara mobile capture from iPhone setup",
            "top_k": 3,
        },
        indent=2,
    )


def _build_qr_svg(data: str) -> str:
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode("utf-8")


def _endpoint_rank(ip: ipaddress.IPv4Address) -> tuple[int, str]:
    if _is_cgnat(ip):
        return (0, str(ip))
    if ip.is_private:
        return (1, str(ip))
    if ip.is_link_local:
        return (2, str(ip))
    return (3, str(ip))


def _candidate_private_ipv4s() -> Iterable[ipaddress.IPv4Address]:
    seen: set[ipaddress.IPv4Address] = set()
    for candidate in _socket_candidates():
        if candidate not in seen and _is_allowed_private(candidate):
            seen.add(candidate)
            yield candidate
    for candidate in _command_candidates():
        if candidate not in seen and _is_allowed_private(candidate):
            seen.add(candidate)
            yield candidate


def _socket_candidates() -> Iterable[ipaddress.IPv4Address]:
    host = socket.gethostname()
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        infos = []
    for info in infos:
        address = info[4][0]
        if not isinstance(address, str):
            continue
        ip = _to_ipv4(address)
        if ip is not None:
            yield ip

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("198.18.0.1", 80))
            ip = _to_ipv4(sock.getsockname()[0])
            if ip is not None:
                yield ip
    except OSError:
        return


def _command_candidates() -> Iterable[ipaddress.IPv4Address]:
    commands: list[list[str]] = []
    if shutil.which("ifconfig"):
        commands.append(["ifconfig"])
    if shutil.which("ip"):
        commands.append(["ip", "-4", "addr", "show"])
    for command in commands:
        try:
            output = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            continue
        for match in re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)\b", output):
            ip = _to_ipv4(match)
            if ip is not None:
                yield ip


def _to_ipv4(value: str) -> ipaddress.IPv4Address | None:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    return ip if isinstance(ip, ipaddress.IPv4Address) else None


def _is_allowed_private(ip: ipaddress.IPv4Address) -> bool:
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        return False
    return ip.is_private or ip.is_link_local or _is_cgnat(ip)


def _is_cgnat(ip: ipaddress.IPv4Address) -> bool:
    return ip in ipaddress.ip_network("100.64.0.0/10")


def _url_host(host: str) -> str:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    return f"[{host}]" if ip.version == 6 else host
