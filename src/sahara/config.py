"""Configuration loading and management for Sahara."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "SaharaConfig",
    "load_config",
    "save_config",
    "DEFAULT_EXCLUDES",
    "DEFAULT_CONFIG_PATH",
]

DEFAULT_CONFIG_PATH = Path.home() / ".sahara" / "config.toml"

DEFAULT_EXCLUDES: list[str] = [
    # Version control
    ".git/",
    ".hg/",
    ".svn/",
    # Python
    "__pycache__/",
    "*.py[cod]",
    "*.pyo",
    ".venv/",
    "venv/",
    ".env/",
    "env/",
    "*.egg-info/",
    "dist/",
    "build/",
    # Node
    "node_modules/",
    ".npm/",
    # macOS
    ".DS_Store",
    ".AppleDouble",
    ".LSOverride",
    "._*",
    # Windows
    "Thumbs.db",
    "Desktop.ini",
    "ehthumbs.db",
    # Linux
    "*~",
    # Editors
    ".idea/",
    ".vscode/",
    "*.swp",
    "*.swo",
    "*.bak",
    "*.tmp",
    # Sahara own files
    ".sahara/",
    ".saharaignore",
]


@dataclass
class SaharaConfig:
    """Complete configuration for the Sahara application."""

    # Core settings
    sync_folder: str = ""
    bucket: str = ""
    region: str = "us-east-1"
    prefix: str = ""

    # AWS credentials (optional — can use env vars / profiles)
    aws_profile: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""

    # Self-hosted / MinIO endpoint (empty = use AWS)
    endpoint_url: str = ""

    # Storage mode: none | s3 | local | local+glacier
    # none         — local semantic indexing only; no storage backend
    # s3           — AWS S3 or MinIO (via endpoint_url)
    # local        — locally mounted drives (drive_paths); no cloud
    # local+glacier— drives as primary + S3 Glacier Deep Archive as cold backup
    storage_mode: str = ""

    # Absolute paths to mounted drives for local / local+glacier modes
    drive_paths: list[str] = field(default_factory=list)

    # local+glacier: keep Glacier copies when files are deleted locally (recommended)
    glacier_keep_deleted: bool = True

    # Encryption
    encryption_enabled: bool = False
    encryption_key_id: str = ""  # keyring service identifier

    # Sync behaviour
    max_workers: int = 8
    multipart_threshold_mb: int = 100
    multipart_chunk_size_mb: int = 8
    conflict_strategy: str = "backup"  # backup | local | remote | ask
    delete_remote_on_local_delete: bool = True
    delete_local_on_remote_delete: bool = True
    upload_only: bool = False  # push local changes only; never pull remote files

    # Storage
    default_storage_class: str = "GLACIER_IR"
    archive_storage_class: str = "DEEP_ARCHIVE"

    # Archiving auto-rules
    archive_after_days: int = 0  # 0 = disabled
    archive_min_size_mb: int = 0  # 0 = no minimum

    # Restore defaults
    restore_days: int = 7
    restore_tier: str = "Bulk"  # Expedited | Standard | Bulk

    # Watcher / daemon
    debounce_seconds: float = 2.0
    poll_interval_seconds: int = 300  # daemon heartbeat

    # Notifications
    notifications_enabled: bool = True

    # Answer generation (API keys remain in environment variables)
    answer_provider: str = "none"  # none | ollama | openai
    answer_model: str = ""  # empty = provider default

    # Exclude patterns (appended to DEFAULT_EXCLUDES)
    exclude_patterns: list[str] = field(default_factory=list)

    # Internal: manifest S3 key
    manifest_key: str = ".sahara/manifest.json"

    # Daemon
    pid_file: str = ""  # defaults to ~/.sahara/daemon.pid

    def __post_init__(self) -> None:
        if not self.storage_mode:
            self.storage_mode = "s3" if self.bucket or self.endpoint_url else "none"
        if not self.pid_file:
            self.pid_file = str(Path.home() / ".sahara" / "daemon.pid")
        # Non-S3 modes have no storage tiers; coerce Glacier classes to STANDARD.
        minio_with_glacier = self.endpoint_url and self.default_storage_class not in ("STANDARD", "")
        if self.is_local_drive_mode or minio_with_glacier:
            self.default_storage_class = "STANDARD"

    @property
    def is_self_hosted(self) -> bool:
        """True when using a self-hosted S3-compatible server (MinIO etc.) instead of AWS."""
        return bool(self.endpoint_url)

    @property
    def is_local_drive_mode(self) -> bool:
        """True when syncing to locally mounted drives (local or local+glacier modes)."""
        return self.storage_mode in ("local", "local+glacier")

    @property
    def has_storage_backend(self) -> bool:
        """True when Sahara is configured to sync to storage."""
        return self.storage_mode != "none"

    @property
    def is_index_only_mode(self) -> bool:
        """True when Sahara is configured only for local semantic indexing."""
        return not self.has_storage_backend

    def get_sync_folder_path(self) -> Path:
        if not self.sync_folder:
            raise ValueError(
                "sync_folder is not configured. Run `sahara init` to set up."
            )
        return Path(self.sync_folder).expanduser().resolve()

    def get_s3_key(self, relative_path: str) -> str:
        """Produce the full S3 key for a relative path."""
        if self.prefix:
            prefix = self.prefix.rstrip("/")
            return f"{prefix}/{relative_path}"
        return relative_path

    def get_all_exclude_patterns(self) -> list[str]:
        return DEFAULT_EXCLUDES + self.exclude_patterns


def _flatten_toml(data: dict, parent_key: str = "", sep: str = "_") -> dict:
    """Flatten nested TOML dict into a single-level dict."""
    items: list = []
    for k, v in data.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(_flatten_toml(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


def load_config(path: Path | None = None) -> SaharaConfig:
    """Load SaharaConfig from a TOML file.

    Falls back to DEFAULT_CONFIG_PATH if path is None.
    Returns a SaharaConfig with defaults if the file does not exist.
    """
    config_path = path or DEFAULT_CONFIG_PATH

    if not config_path.exists():
        return SaharaConfig()

    with open(config_path, "rb") as fh:
        raw = tomllib.load(fh)

    flat = _flatten_toml(raw)

    known_fields = {
        f_name
        for f_name in SaharaConfig.__dataclass_fields__  # type: ignore[attr-defined]
    }

    kwargs: dict = {}
    for key, value in flat.items():
        if key in known_fields:
            kwargs[key] = value

    # Lists are not flattened by _flatten_toml
    if "exclude_patterns" in raw:
        kwargs["exclude_patterns"] = raw["exclude_patterns"]
    if "drive_paths" in raw:
        kwargs["drive_paths"] = raw["drive_paths"]

    # Configs written before index-only mode did not contain storage_mode and
    # always represented an S3 setup. Preserve that behavior during migration.
    if "storage_mode" not in flat:
        kwargs["storage_mode"] = "s3"

    return SaharaConfig(**kwargs)


def save_config(config: SaharaConfig, path: Path | None = None) -> None:
    """Persist a SaharaConfig to a TOML file."""
    config_path = path or DEFAULT_CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [
        "# Sahara configuration — generated by `sahara config`\n",
        "# Edit manually or use `sahara config set <key> <value>`\n\n",
    ]

    for f_name in SaharaConfig.__dataclass_fields__:  # type: ignore[attr-defined]
        value = getattr(config, f_name)
        if isinstance(value, bool):
            lines.append(f"{f_name} = {str(value).lower()}\n")
        elif isinstance(value, int):
            lines.append(f"{f_name} = {value}\n")
        elif isinstance(value, float):
            lines.append(f"{f_name} = {value}\n")
        elif isinstance(value, str):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{f_name} = "{escaped}"\n')
        elif isinstance(value, list):
            if not value:
                lines.append(f"{f_name} = []\n")
            else:
                items_str = ", ".join(f'"{v}"' for v in value)
                lines.append(f"{f_name} = [{items_str}]\n")

    config_path.write_text("".join(lines), encoding="utf-8")
