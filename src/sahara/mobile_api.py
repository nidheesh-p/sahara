"""Private mobile capture API for paired Sahara devices."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from sahara.config import DEFAULT_CONFIG_PATH, SaharaConfig, load_config
from sahara.memory import CaptureRequest, MemoryFilters, MemoryService
from sahara.memory.format import MAX_MEMORY_CHARS, SOURCE_TYPES
from sahara.storage.state_db import StateDB

CAPTURE_SCOPE = "memory:capture"
RECALL_SCOPE = "memory:recall"
SUPPORTED_SCOPES = frozenset({CAPTURE_SCOPE, RECALL_SCOPE})
MAX_JSON_BYTES = min(MAX_MEMORY_CHARS + 4096, 80_000)
DEFAULT_RATE_LIMIT = 60
DEFAULT_RATE_WINDOW_SECONDS = 60

__all__ = [
    "CAPTURE_SCOPE",
    "RECALL_SCOPE",
    "DevicePairing",
    "MobileAPIError",
    "MobileAPIService",
    "RateLimiter",
    "create_mobile_device_pairing",
    "hash_device_token",
    "serve_mobile_api",
    "validate_bind_host",
]


class MobileAPIError(Exception):
    """HTTP-aware mobile API error."""

    def __init__(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DevicePairing:
    device_id: str
    name: str
    token: str
    scopes: tuple[str, ...]
    endpoint: str

    def payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "type": "sahara-mobile-pairing",
            "device_id": self.device_id,
            "name": self.name,
            "endpoint": self.endpoint,
            "token": self.token,
            "scopes": list(self.scopes),
        }


class RateLimiter:
    """Tiny in-process token bucket suitable for local private API use."""

    def __init__(
        self,
        *,
        limit: int = DEFAULT_RATE_LIMIT,
        window_seconds: int = DEFAULT_RATE_WINDOW_SECONDS,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            events = [seen for seen in self._events.get(key, []) if seen >= cutoff]
            if len(events) >= self._limit:
                self._events[key] = events
                return False
            events.append(now)
            self._events[key] = events
            return True


def hash_device_token(token: str) -> str:
    """Return the stable hash stored for a mobile bearer token."""
    return hashlib.sha256(f"sahara-mobile:{token}".encode()).hexdigest()


def _normalize_scopes(scopes: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in scopes:
        scope = raw.strip()
        if scope not in SUPPORTED_SCOPES:
            raise ValueError(
                "Unsupported mobile scope. Use memory:capture or memory:recall."
            )
        if scope not in seen:
            normalized.append(scope)
            seen.add(scope)
    if not normalized:
        normalized.append(CAPTURE_SCOPE)
    return tuple(normalized)


def create_mobile_device_pairing(
    db: StateDB,
    *,
    name: str,
    endpoint: str,
    scopes: tuple[str, ...] = (CAPTURE_SCOPE,),
) -> DevicePairing:
    """Create a paired device and return the one-time token payload."""
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Device name is required")
    normalized_scopes = _normalize_scopes(scopes)
    token = "sahara_" + secrets.token_urlsafe(32)
    pairing = DevicePairing(
        device_id=str(uuid.uuid4()),
        name=clean_name,
        token=token,
        scopes=normalized_scopes,
        endpoint=endpoint,
    )
    db.create_mobile_device(
        device_id=pairing.device_id,
        name=pairing.name,
        token_hash=hash_device_token(token),
        scopes=pairing.scopes,
    )
    return pairing


def validate_bind_host(host: str, *, allow_private_network: bool = False) -> str:
    """Return normalized host or raise if it would expose the API unsafely."""
    clean = host.strip() or "127.0.0.1"
    if clean == "localhost":
        return clean
    try:
        ip = ipaddress.ip_address(clean)
    except ValueError as exc:
        raise ValueError(
            "Mobile API host must be localhost or an IP address"
        ) from exc

    if ip.is_loopback:
        return clean
    if not allow_private_network:
        raise ValueError(
            "Mobile API binds to loopback by default. Pass the explicit private "
            "network option only for trusted VPN/LAN addresses."
        )
    if ip.is_unspecified or ip.is_multicast or ip.is_global:
        raise ValueError("Mobile API refuses public or wildcard bind addresses")
    if ip.is_private or ip.is_link_local or _is_cgnat(ip):
        return clean
    raise ValueError("Mobile API host is not a private-network address")


def _is_cgnat(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network(
        "100.64.0.0/10"
    )


class MobileAPIService:
    """Authenticate and handle mobile memory API requests."""

    def __init__(
        self,
        config: SaharaConfig,
        db: StateDB | None = None,
        *,
        db_factory: Callable[[], StateDB] | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        if db is None and db_factory is None:
            raise ValueError("MobileAPIService requires a database or db_factory")
        if db is not None and db_factory is not None:
            raise ValueError("MobileAPIService accepts either db or db_factory")
        self._config = config
        self._db = db
        self._db_factory = db_factory
        self._rate_limiter = rate_limiter or RateLimiter()

    def handle_capture(
        self,
        headers: dict[str, str],
        body: bytes,
        *,
        client_addr: str = "",
    ) -> tuple[HTTPStatus, dict[str, Any]]:
        with self._db_session() as db:
            device = self._authenticate(db, headers, CAPTURE_SCOPE)
            audit_id = self._begin_audit(
                db,
                device,
                body,
                scope=CAPTURE_SCOPE,
                client_addr=client_addr,
            )
            try:
                payload = self._json_body(body)
                self._reject_dangerous_fields(payload)
                text = self._required_string(payload, "text")
                if len(text) > MAX_MEMORY_CHARS:
                    raise MobileAPIError(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        "request_too_large",
                        f"Memory text exceeds the {MAX_MEMORY_CHARS:,}-character limit",
                    )
                source_type = str(payload.get("source_type") or "mobile").strip().lower()
                if source_type not in SOURCE_TYPES:
                    raise MobileAPIError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_source_type",
                        "Unsupported memory source_type",
                    )
                idempotency_key = self._required_string(payload, "idempotency_key")
                result = MemoryService(self._config, db).capture(
                    CaptureRequest(
                        text=text,
                        title=self._optional_string(payload, "title"),
                        source_type=source_type,
                        source_url=self._optional_string(payload, "source_url") or "",
                        source_id=self._optional_string(payload, "source_id") or "",
                        tags=self._tags(payload),
                        idempotency_key=idempotency_key,
                    )
                )
                status = "already_saved" if result.deduplicated else (
                    "saved_and_indexed" if result.indexed else "saved_index_pending"
                )
                db.finish_mobile_memory_audit(
                    audit_id,
                    outcome=status,
                    memory_id=result.item.memory_id,
                )
                db.mark_mobile_device_used(device["device_id"])
                return HTTPStatus.CREATED, {
                    "status": status,
                    "memory_id": result.item.memory_id,
                    "title": result.item.title,
                    "relative_path": result.item.relative_path,
                    "indexed": result.indexed,
                    "index_reason": result.index_reason,
                }
            except MobileAPIError as exc:
                db.finish_mobile_memory_audit(
                    audit_id,
                    outcome="rejected",
                    details=exc.code,
                )
                raise
            except ValueError as exc:
                db.finish_mobile_memory_audit(
                    audit_id,
                    outcome="rejected",
                    details="validation_error",
                )
                raise MobileAPIError(
                    HTTPStatus.BAD_REQUEST,
                    "validation_error",
                    str(exc),
                ) from exc
            except Exception as exc:
                db.finish_mobile_memory_audit(
                    audit_id,
                    outcome="failed",
                    details="capture_error",
                )
                raise MobileAPIError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "capture_error",
                    "Could not save memory",
                ) from exc

    def handle_recall(
        self,
        headers: dict[str, str],
        body: bytes,
        *,
        client_addr: str = "",
    ) -> tuple[HTTPStatus, dict[str, Any]]:
        with self._db_session() as db:
            device = self._authenticate(db, headers, RECALL_SCOPE)
            audit_id = self._begin_audit(
                db,
                device,
                body,
                scope=RECALL_SCOPE,
                client_addr=client_addr,
            )
            try:
                payload = self._json_body(body)
                self._reject_dangerous_fields(payload)
                query = self._required_string(payload, "query")
                try:
                    top_k = int(payload.get("top_k") or 5)
                except (TypeError, ValueError) as exc:
                    raise MobileAPIError(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_top_k",
                        "top_k must be an integer",
                    ) from exc
                top_k = min(max(top_k, 1), 20)
                results = MemoryService(self._config, db).search(
                    query,
                    MemoryFilters(
                        source_types=tuple(payload.get("source_types") or ()),
                        tags=tuple(payload.get("tags") or ()),
                    ),
                    top_k=top_k,
                )
                db.finish_mobile_memory_audit(audit_id, outcome="recalled")
                db.mark_mobile_device_used(device["device_id"])
                return HTTPStatus.OK, {
                    "results": [
                        {
                            "memory_id": result.item.memory_id,
                            "title": result.item.title,
                            "score": result.score,
                            "snippet": result.snippet,
                            "relative_path": result.item.relative_path,
                            "source_type": result.item.source_type,
                            "source_url": result.item.source_url,
                            "tags": list(result.item.tags),
                            "updated_at": result.item.updated_at,
                        }
                        for result in results
                    ]
                }
            except MobileAPIError as exc:
                db.finish_mobile_memory_audit(
                    audit_id,
                    outcome="rejected",
                    details=exc.code,
                )
                raise
            except Exception as exc:
                db.finish_mobile_memory_audit(
                    audit_id,
                    outcome="failed",
                    details="recall_error",
                )
                raise MobileAPIError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "recall_error",
                    "Could not recall memories",
                ) from exc

    @contextmanager
    def _db_session(self) -> Iterator[StateDB]:
        if self._db is not None:
            yield self._db
            return
        assert self._db_factory is not None
        db = self._db_factory()
        try:
            yield db
        finally:
            db.close()

    def _authenticate(self, db: StateDB, headers: dict[str, str], scope: str) -> dict:
        auth = headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise MobileAPIError(
                HTTPStatus.UNAUTHORIZED,
                "missing_token",
                "Bearer token is required",
            )
        token = auth.removeprefix("Bearer ").strip()
        token_hash = hash_device_token(token)
        device = db.get_mobile_device_by_hash(token_hash)
        if device is None or not hmac.compare_digest(
            device["token_hash"],
            token_hash,
        ):
            raise MobileAPIError(
                HTTPStatus.UNAUTHORIZED,
                "invalid_token",
                "Bearer token is invalid",
            )
        if scope not in device["scopes"]:
            raise MobileAPIError(
                HTTPStatus.FORBIDDEN,
                "insufficient_scope",
                f"Device token does not include {scope}",
            )
        if not self._rate_limiter.allow(device["device_id"]):
            raise MobileAPIError(
                HTTPStatus.TOO_MANY_REQUESTS,
                "rate_limited",
                "Too many mobile API requests",
            )
        return device

    def _begin_audit(
        self,
        db: StateDB,
        device: dict,
        body: bytes,
        *,
        scope: str,
        client_addr: str,
    ) -> int:
        source_type = "mobile"
        idempotency_key = ""
        try:
            payload = json.loads(body.decode("utf-8"))
            if isinstance(payload, dict):
                source_type = str(payload.get("source_type") or "mobile")
                idempotency_key = str(payload.get("idempotency_key") or "")
        except Exception:
            pass
        return db.begin_mobile_memory_audit(
            scope=scope,
            source_type=source_type if source_type in SOURCE_TYPES else "invalid",
            idempotency_key_hash=hashlib.sha256(
                idempotency_key.encode("utf-8")
            ).hexdigest(),
            device_id=device["device_id"],
            device_name=device["name"],
            client_addr=client_addr,
        )

    @staticmethod
    def _json_body(body: bytes) -> dict[str, Any]:
        if len(body) > MAX_JSON_BYTES:
            raise MobileAPIError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request_too_large",
                f"Request exceeds the {MAX_JSON_BYTES:,}-byte limit",
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise MobileAPIError(
                HTTPStatus.BAD_REQUEST,
                "invalid_encoding",
                "Request body must be UTF-8 JSON",
            ) from exc
        except json.JSONDecodeError as exc:
            raise MobileAPIError(
                HTTPStatus.BAD_REQUEST,
                "invalid_json",
                "Request body must be valid JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise MobileAPIError(
                HTTPStatus.BAD_REQUEST,
                "invalid_json",
                "Request body must be a JSON object",
            )
        return payload

    @staticmethod
    def _reject_dangerous_fields(payload: dict[str, Any]) -> None:
        blocked = {
            "path",
            "file_path",
            "relative_path",
            "storage_prefix",
            "sync_enabled",
            "sync",
            "memory_folder",
        }
        if blocked.intersection(payload):
            raise MobileAPIError(
                HTTPStatus.BAD_REQUEST,
                "unsupported_field",
                "Mobile requests cannot select paths or storage behavior",
            )

    @staticmethod
    def _required_string(payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise MobileAPIError(
                HTTPStatus.BAD_REQUEST,
                f"{key}_required",
                f"{key} is required",
            )
        return value

    @staticmethod
    def _optional_string(payload: dict[str, Any], key: str) -> str | None:
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise MobileAPIError(
                HTTPStatus.BAD_REQUEST,
                f"invalid_{key}",
                f"{key} must be a string",
            )
        return value

    @staticmethod
    def _tags(payload: dict[str, Any]) -> tuple[str, ...]:
        raw = payload.get("tags")
        if raw is None:
            return ()
        if isinstance(raw, str):
            value = raw.strip()
            if not value or value == "[]":
                return ()
            return tuple(tag.strip() for tag in value.split(",") if tag.strip())
        if not isinstance(raw, list):
            raise MobileAPIError(
                HTTPStatus.BAD_REQUEST,
                "invalid_tags",
                "tags must be a JSON array",
            )
        if not all(isinstance(tag, str) for tag in raw):
            raise MobileAPIError(
                HTTPStatus.BAD_REQUEST,
                "invalid_tags",
                "tags must contain only strings",
            )
        return tuple(raw)


class _MobileRequestHandler(BaseHTTPRequestHandler):
    server: _MobileHTTPServer

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found")

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length > MAX_JSON_BYTES:
            self._send_error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request_too_large",
                f"Request exceeds the {MAX_JSON_BYTES:,}-byte limit",
            )
            return
        body = self.rfile.read(content_length)
        headers = {key.lower(): value for key, value in self.headers.items()}
        try:
            if self.path == "/v1/memories":
                status, payload = self.server.service.handle_capture(
                    headers,
                    body,
                    client_addr=self.client_address[0],
                )
            elif self.path == "/v1/recall":
                status, payload = self.server.service.handle_recall(
                    headers,
                    body,
                    client_addr=self.client_address[0],
                )
            else:
                self._send_error(
                    HTTPStatus.NOT_FOUND,
                    "not_found",
                    "Endpoint not found",
                )
                return
        except MobileAPIError as exc:
            self._send_error(exc.status, exc.code, exc.message)
            return
        self._send_json(status, payload)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_error(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
    ) -> None:
        self._send_json(status, {"error": {"code": code, "message": message}})

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class _MobileHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        service: MobileAPIService,
    ) -> None:
        super().__init__(server_address, _MobileRequestHandler)
        self.service = service


def serve_mobile_api(
    *,
    config_path: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    allow_private_network: bool = False,
) -> None:
    """Run the mobile API server until interrupted."""
    bind_host = validate_bind_host(host, allow_private_network=allow_private_network)
    config = load_config(config_path or DEFAULT_CONFIG_PATH)
    db_path = StateDB().path

    def _open_db() -> StateDB:
        return StateDB(db_path).connect()

    service = MobileAPIService(config, db_factory=_open_db)
    server = _MobileHTTPServer((bind_host, port), service)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def pairing_uri(payload: dict[str, Any]) -> str:
    """Return a compact URI that QR generators or Shortcuts can encode."""
    encoded = json.dumps(payload, separators=(",", ":"))
    return "sahara://pair?" + urlencode({"payload": encoded})
