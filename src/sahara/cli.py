"""Sahara CLI — complete Click command tree."""

from __future__ import annotations

import datetime
import logging
import os
import sys
from importlib import resources
from pathlib import Path
from typing import Any, Literal, NoReturn, cast

import click

from sahara import __version__
from sahara.config import (
    DEFAULT_CONFIG_PATH,
    SaharaConfig,
    load_config,
    save_config,
)

__all__ = ["main"]


def _load_dotenv() -> None:
    """Load key=value pairs from .env in the project root into os.environ.

    Only sets variables that are not already present in the environment,
    so shell exports and CI secrets always take precedence.
    """
    dotenv = Path(__file__).parent.parent.parent.parent / ".env"
    if not dotenv.is_file():
        # Also check current working directory (handy when running from the repo root)
        dotenv = Path.cwd() / ".env"
    if not dotenv.is_file():
        return
    for line in dotenv.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)-8s %(name)s %(message)s",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _styled(text: str, fg: str = "white", bold: bool = False) -> str:
    return click.style(text, fg=fg, bold=bold)


def _ok(msg: str) -> None:
    click.echo(click.style("  ✓ " + msg, fg="green"))


def _warn(msg: str) -> None:
    click.echo(click.style("  ⚠ " + msg, fg="yellow"))


def _err(msg: str) -> None:
    click.echo(click.style("  ✗ " + msg, fg="red"), err=True)


def _info(msg: str) -> None:
    click.echo("  " + msg)


def _section(title: str) -> None:
    click.echo("\n" + click.style(title, fg="cyan", bold=True))
    click.echo("  " + "─" * (len(title) + 2))


def _abort(msg: str) -> NoReturn:
    _err(msg)
    sys.exit(1)


def _load_cfg(config_path: Path | None) -> SaharaConfig:
    return load_config(config_path or DEFAULT_CONFIG_PATH)


def _create_backend(config: SaharaConfig):
    """Instantiate the appropriate StorageBackend for config.storage_mode."""
    from sahara.storage.dual_write_backend import DualWriteBackend
    from sahara.storage.local_drive_client import LocalDriveClient
    from sahara.storage.s3_client import S3Client

    if config.storage_mode == "none":
        raise ValueError(
            "No storage backend is configured. Sahara can still index and search "
            "local content in basic mode."
        )
    if config.storage_mode == "local":
        return LocalDriveClient(config)
    elif config.storage_mode == "local+glacier":
        primary = LocalDriveClient(config)
        secondary = S3Client(config)
        return DualWriteBackend(
            primary, secondary, glacier_keep_deleted=config.glacier_keep_deleted
        )
    else:  # "s3" — AWS or MinIO via endpoint_url
        return S3Client(config)


def _build_engine(
    config: SaharaConfig,
    sync_folder: Path | None = None,
    s3_prefix: str = "",
):
    from sahara.storage.state_db import StateDB
    from sahara.sync.ignore_rules import IgnoreRules
    from sahara.sync.sync_engine import SyncEngine

    folder = sync_folder or config.get_sync_folder_path()
    db = StateDB().connect()
    backend = _create_backend(config)
    ignore = IgnoreRules(folder, extra_patterns=config.exclude_patterns)
    return SyncEngine(config, db, backend, ignore, sync_folder=folder, s3_prefix=s3_prefix), db, backend


def _require_library_config(config: SaharaConfig) -> None:
    if not config.sync_folder:
        _abort("Sahara is not initialised. Run `sahara init` to set up.")


def _require_storage_config(config: SaharaConfig) -> None:
    _require_library_config(config)
    if not config.has_storage_backend:
        _abort(
            "No storage backend is configured. Basic mode supports indexing, search, "
            "ask, and MCP. Configure local-drive or AWS storage before syncing."
        )
    if config.storage_mode == "s3" and not config.bucket:
        _abort("No S3 bucket configured. Run `sahara init` to set up.")
    if config.is_local_drive_mode and not config.drive_paths:
        _abort("No local storage drive configured. Run `sahara init` to set up.")


def _require_config(config: SaharaConfig) -> None:
    """Compatibility alias for commands that require a storage backend."""
    _require_storage_config(config)


_SAHARAIGNORE_FALLBACK = b"# Sahara ignore rules (gitignore syntax)\n"


def _ensure_saharaignore(folder: Path) -> bool:
    """Create a `.saharaignore` file in *folder* if one doesn't exist yet.

    Prints a confirmation message on creation. Returns True if a file was
    created, False if one already existed.
    """
    if not folder.is_dir():
        return False

    ignore_path = folder / ".saharaignore"
    if ignore_path.exists():
        return False

    try:
        template = resources.files("sahara").joinpath("data", "saharaignore.template")
        content = template.read_bytes()
        from_template = True
    except (OSError, ModuleNotFoundError):
        content = _SAHARAIGNORE_FALLBACK
        from_template = False

    try:
        with open(ignore_path, "xb") as fh:
            fh.write(content)
    except FileExistsError:
        return False

    _ok(
        "Created .saharaignore from template."
        if from_template
        else "Created empty .saharaignore."
    )
    return True


def _content_roots(config: SaharaConfig, db: Any) -> list[Any]:
    from sahara.library import ensure_content_roots

    return ensure_content_roots(config, db)


def _resolve_content_prefix(config: SaharaConfig, db: Any, folder: str) -> str:
    resolved = Path(folder).expanduser().resolve()
    match = next(
        (root for root in _content_roots(config, db) if root.local_path == resolved),
        None,
    )
    if match is None:
        _abort(f"'{folder}' is not a registered content root.")
    return match.storage_prefix


def _require_s3_tiers(config: SaharaConfig, feature: str) -> None:
    """Abort when a Glacier-tiered feature is used in a mode that doesn't support it."""
    if config.is_local_drive_mode:
        _abort(
            f"{feature} is not supported in local drive mode. "
            "This feature requires AWS S3 Glacier tiered storage."
        )
    if config.is_self_hosted:
        _abort(
            f"{feature} is not supported with a self-hosted (MinIO) backend. "
            "This feature requires AWS S3 Glacier tiered storage."
        )


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
)
@click.version_option(__version__, prog_name="sahara")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    envvar="SAHARA_CONFIG",
    help="Path to config.toml (default: ~/.sahara/config.toml).",
)
@click.pass_context
def main(ctx: click.Context, config_path: Path | None) -> None:
    """Sahara — extended storage, searchable memory and instant retrieval."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["config"] = _load_cfg(config_path)

    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--mode",
    type=click.Choice(
        ["basic", "local", "aws", "minio", "local+glacier"],
        case_sensitive=False,
    ),
    default=None,
    help="Setup mode. Supplying this option runs init non-interactively.",
)
@click.option(
    "--folder",
    type=click.Path(path_type=Path),
    default=None,
    help="Primary local folder to index.",
)
@click.option(
    "--storage-drive",
    type=click.Path(path_type=Path),
    multiple=True,
    help="Local storage destination. Repeat for multiple drives.",
)
@click.option("--bucket", default=None, help="AWS S3 bucket name.")
@click.option("--region", default=None, help="AWS region.")
@click.pass_context
def init(
    ctx: click.Context,
    mode: str | None,
    folder: Path | None,
    storage_drive: tuple[Path, ...],
    bucket: str | None,
    region: str | None,
) -> None:
    """Set up local indexing with optional local-drive or AWS storage."""
    _section("Sahara Setup Wizard")
    click.echo("  Configure local indexing first, with storage as an optional extension.\n")

    config = SaharaConfig()
    non_interactive = any(
        value is not None and value != ()
        for value in (mode, folder, storage_drive, bucket, region)
    )
    default_folder = Path.home() / "Sahara"

    if non_interactive:
        selected_folder = folder or default_folder
        backend_choice = (mode or "basic").lower()
    else:
        selected_folder = Path(
            click.prompt("  Primary folder", default=str(default_folder))
        )
        click.echo()
        backend_choice = click.prompt(
            "  Setup",
            type=click.Choice(
                ["basic", "local", "aws", "minio", "local+glacier"],
                case_sensitive=False,
            ),
            default="basic",
            show_default=True,
            prompt_suffix="\n"
            "    basic         — local semantic indexing, no storage required\n"
            "    local         — indexing plus a local/external drive\n"
            "    aws           — indexing plus Amazon S3\n"
            "    minio         — indexing plus self-hosted S3-compatible storage\n"
            "    local+glacier — local drives plus S3 Glacier cold backup\n"
            "  Choice",
        )

    config.sync_folder = str(selected_folder.expanduser().resolve())
    Path(config.sync_folder).mkdir(parents=True, exist_ok=True)

    is_local = backend_choice in ("local", "local+glacier")
    is_minio = backend_choice == "minio"
    config.storage_mode = (
        "none"
        if backend_choice == "basic"
        else ("s3" if backend_choice in ("aws", "minio") else backend_choice)
    )

    if is_local:
        drive_paths: list[str]
        if non_interactive:
            if not storage_drive:
                raise click.UsageError(
                    "--storage-drive is required for local and local+glacier modes."
                )
            drive_paths = [
                str(path.expanduser().resolve()) for path in storage_drive
            ]
        else:
            _info(
                "Enter the absolute path(s) to your mounted drives. "
                "Files will be written to ALL drives independently."
            )
            drive_paths = []
            while True:
                default_drive = "" if drive_paths else "/Volumes/Drive1/Sahara"
                prompt_text = (
                    "  Drive path (press Enter to finish)"
                    if drive_paths
                    else "  Drive path 1"
                )
                drive_path = click.prompt(
                    prompt_text,
                    default=default_drive if not drive_paths else "",
                )
                if not drive_path.strip():
                    if not drive_paths:
                        _warn("At least one drive path is required.")
                        continue
                    break
                drive_paths.append(
                    str(Path(drive_path.strip()).expanduser().resolve())
                )
        config.drive_paths = drive_paths
        config.delete_remote_on_local_delete = False
        _info(
            "Drives use append-only deletion behavior by default. "
            "Deleting a source file will not remove its drive copy."
        )

    if is_minio:
        if non_interactive:
            raise click.UsageError(
                "Non-interactive MinIO setup is not available yet; run `sahara init`."
            )
        _info("MinIO mode: files will be stored on your self-hosted server.")
        endpoint_url = click.prompt(
            "  MinIO endpoint URL (e.g. http://100.x.x.1:9000)"
        )
        config.endpoint_url = endpoint_url.strip().rstrip("/")
        config.default_storage_class = "STANDARD"

    needs_bucket = backend_choice in ("aws", "minio", "local+glacier")
    if needs_bucket:
        bucket_prompt = (
            "  Glacier backup bucket name"
            if backend_choice == "local+glacier"
            else ("  Bucket name" if is_minio else "  S3 bucket name")
        )
        if non_interactive:
            if not bucket:
                raise click.UsageError(
                    "--bucket is required for aws and local+glacier modes."
                )
            config.bucket = bucket.strip()
        else:
            config.bucket = click.prompt(
                bucket_prompt, default="sahara" if is_minio else ""
            ).strip()

        if not is_minio:
            if non_interactive:
                config.region = (region or "us-east-1").strip()
            else:
                region_prompt = (
                    "  AWS region for Glacier bucket"
                    if backend_choice == "local+glacier"
                    else "  AWS region"
                )
                config.region = click.prompt(
                    region_prompt, default="us-east-1"
                ).strip()

        if not non_interactive:
            config.prefix = click.prompt(
                "  Key prefix (leave blank for root)", default=""
            ).strip()

    if is_minio and not non_interactive:
        click.echo()
        config.aws_access_key_id = click.prompt(
            "  MinIO access key (root user)"
        ).strip()
        config.aws_secret_access_key = click.prompt(
            "  MinIO secret key (root password)", hide_input=True
        ).strip()
    elif config.storage_mode in ("s3", "local+glacier") and not non_interactive:
        click.echo()
        label = "Glacier AWS" if backend_choice == "local+glacier" else "AWS"
        cred_method = click.prompt(
            f"  {label} credential method",
            type=click.Choice(["env", "profile", "keys"], case_sensitive=False),
            default="env",
            show_default=True,
            prompt_suffix="\n"
            "    env     — use AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars\n"
            "    profile — use a named profile from ~/.aws/credentials\n"
            "    keys    — enter access key and secret now (stored in config file)\n"
            "  Choice",
        )
        if cred_method == "profile":
            config.aws_profile = click.prompt("  AWS profile name").strip()
        elif cred_method == "keys":
            config.aws_access_key_id = click.prompt(
                "  AWS access key ID"
            ).strip()
            config.aws_secret_access_key = click.prompt(
                "  AWS secret access key", hide_input=True
            ).strip()
            _warn(
                "Access keys saved to config file. "
                "Using env vars or a profile is more secure."
            )
        else:
            _info(
                "Make sure AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are set "
                "before running sahara."
            )

    if backend_choice == "local+glacier" and not non_interactive:
        click.echo()
        config.glacier_keep_deleted = click.confirm(
            "  Keep Glacier copies when files are deleted locally? (recommended)",
            default=True,
        )

    if config.has_storage_backend and not non_interactive:
        config.encryption_enabled = click.confirm(
            "\n  Enable client-side encryption (AES-256-GCM)?", default=False
        )
        if config.encryption_enabled:
            passphrase = click.prompt(
                "  Encryption passphrase", hide_input=True, confirmation_prompt=True
            )
            from sahara.utils.encryption import set_passphrase

            set_passphrase(passphrase)
            _ok("Passphrase stored in system keyring.")

        config.conflict_strategy = click.prompt(
            "  Conflict strategy [backup/local/remote]", default="backup"
        ).strip()
        click.echo()
        config.upload_only = click.confirm(
            "  Upload-only mode? (this machine only pushes files,\n"
            "  never pulls files uploaded by other machines)",
            default=False,
        )

    config_path = ctx.obj.get("config_path") or DEFAULT_CONFIG_PATH
    save_config(config, config_path)
    _ok(f"Configuration saved to {config_path}")

    if config.has_storage_backend:
        click.echo("\n  Validating storage access…")
        try:
            backend = _create_backend(config)
            backend.validate_bucket_access()
            if is_local:
                _ok(f"Drive(s) accessible: {', '.join(config.drive_paths)}")
            else:
                _ok(f"Connected to bucket '{config.bucket}'")

            manifest, _ = backend.get_manifest()
            if manifest is None:
                _info(
                    "No existing manifest found. "
                    "A new one will be created on first sync."
                )
            else:
                _ok(f"Manifest found with {len(manifest)} file(s).")
        except Exception as exc:
            _warn(f"Storage validation failed: {exc}")
            _warn("You can re-run `sahara doctor` after fixing the issue.")

    _ensure_saharaignore(Path(config.sync_folder))

    click.echo()
    if config.is_index_only_mode:
        _ok("Sahara initialised in basic mode! Run `sahara index` to start.")
    else:
        _ok("Sahara initialised! Run `sahara sync`, then `sahara index`.")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@main.command()
@click.option("--repair", is_flag=True, help="Attempt to fix detected issues.")
@click.pass_context
def doctor(ctx: click.Context, repair: bool) -> None:
    """Check Sahara configuration and connectivity."""
    config: SaharaConfig = ctx.obj["config"]
    _section("Sahara Doctor")
    issues = 0

    # Config file
    config_path = ctx.obj.get("config_path") or DEFAULT_CONFIG_PATH
    if config_path.exists():
        _ok(f"Config file found: {config_path}")
    else:
        _warn(f"Config file not found: {config_path}")
        _info("Run `sahara init` to create one.")
        issues += 1

    # Sync folder
    if config.sync_folder:
        sf = Path(config.sync_folder)
        if sf.exists():
            _ok(f"Sync folder exists: {sf}")
        else:
            _warn(f"Sync folder missing: {sf}")
            if repair:
                sf.mkdir(parents=True, exist_ok=True)
                _ok("Created sync folder.")
            issues += 1
    else:
        _warn("sync_folder not configured.")
        issues += 1

    # Storage connectivity check
    if config.is_index_only_mode:
        _ok("Storage: not configured (basic index-only mode).")
    elif config.is_local_drive_mode:
        # Check drive paths
        if config.drive_paths:
            click.echo(f"  Checking {len(config.drive_paths)} drive path(s)…")
            try:
                from sahara.storage.local_drive_client import LocalDriveClient
                ldc = LocalDriveClient(config)
                ldc.validate_bucket_access()
                _ok(f"All drives accessible: {', '.join(config.drive_paths)}")
            except Exception as exc:
                _warn(f"Drive access failed: {exc}")
                issues += 1
        else:
            _warn("drive_paths not configured.")
            issues += 1
        # For local+glacier, also check S3
        if config.storage_mode == "local+glacier":
            if config.bucket:
                click.echo(f"  Checking Glacier S3 access to s3://{config.bucket}…")
                try:
                    from sahara.storage.s3_client import S3Client
                    s3 = S3Client(config)
                    s3.validate_bucket_access()
                    _ok("Glacier bucket accessible.")
                except Exception as exc:
                    _warn(f"Glacier S3 access failed: {exc}")
                    issues += 1
            else:
                _warn("bucket not configured for Glacier backup.")
                issues += 1
    elif config.bucket:
        if config.is_self_hosted:
            click.echo(f"  Checking MinIO access at {config.endpoint_url}, bucket '{config.bucket}'…")
        else:
            click.echo(f"  Checking S3 access to s3://{config.bucket}…")
        try:
            from sahara.storage.s3_client import S3Client

            s3 = S3Client(config)
            s3.validate_bucket_access()
            _ok("Bucket accessible.")

            supports_cput = s3.check_conditional_put_support()
            if supports_cput:
                _ok("Conditional PUT (If-Match) supported.")
            else:
                _warn("Conditional PUT not supported. Concurrent sync safety reduced.")

        except Exception as exc:
            backend = "MinIO" if config.is_self_hosted else "S3"
            _warn(f"{backend} access failed: {exc}")
            issues += 1
    else:
        _warn("bucket not configured.")
        issues += 1

    # Encryption
    if config.encryption_enabled:
        from sahara.utils.encryption import get_passphrase

        pp = get_passphrase()
        if pp:
            _ok("Encryption passphrase found in keyring.")
        else:
            _warn("Encryption enabled but no passphrase found in keyring.")
            if repair:
                pp = click.prompt(
                    "  Enter passphrase to store",
                    hide_input=True,
                    confirmation_prompt=True,
                )
                from sahara.utils.encryption import set_passphrase

                set_passphrase(pp)
                _ok("Passphrase stored.")
            issues += 1
    else:
        _info("Encryption: disabled.")

    # DB
    from sahara.storage.state_db import DB_PATH, StateDB

    db_path = DB_PATH
    if db_path.exists():
        try:
            with StateDB(db_path) as db:
                count = len(db.list_files())
            _ok(f"State DB OK ({count} file records).")
        except Exception as exc:
            _warn(f"State DB error: {exc}")
            issues += 1
    else:
        _info("State DB not yet initialised (will be created on first index or sync).")

    # Stale multipart uploads (AWS only — not applicable for MinIO or local drive modes)
    if config.bucket and not config.is_self_hosted and not config.is_local_drive_mode:
        try:
            from sahara.storage.s3_client import S3Client

            s3 = S3Client(config)
            uploads = s3.list_multipart_uploads()
            if uploads:
                _warn(f"{len(uploads)} stale multipart upload(s) found.")
                if repair:
                    for u in uploads:
                        s3.abort_multipart_upload(u["Key"], u["UploadId"])
                        _ok(f"Aborted upload for {u['Key']}")
            else:
                _ok("No stale multipart uploads.")
        except Exception as exc:
            _warn(f"Could not check multipart uploads: {exc}")

    click.echo()
    if issues == 0:
        click.echo(click.style("  All checks passed.", fg="green", bold=True))
    else:
        click.echo(
            click.style(
                f"  {issues} issue(s) found. Run `sahara doctor --repair` to fix.",
                fg="yellow",
                bold=True,
            )
        )


# ---------------------------------------------------------------------------
# encryption group
# ---------------------------------------------------------------------------


@main.group()
def encryption() -> None:
    """Manage client-side encryption."""


@encryption.command("setup")
@click.pass_context
def encryption_setup(ctx: click.Context) -> None:
    """Enable encryption and store a passphrase in the system keyring."""
    config: SaharaConfig = ctx.obj["config"]

    passphrase = click.prompt(
        "  Encryption passphrase",
        hide_input=True,
        confirmation_prompt=True,
    )
    from sahara.utils.encryption import set_passphrase

    set_passphrase(passphrase)

    config.encryption_enabled = True
    config_path = ctx.obj.get("config_path") or DEFAULT_CONFIG_PATH
    save_config(config, config_path)

    _ok("Encryption enabled. New uploads will be encrypted.")
    _warn("Existing files will NOT be re-encrypted automatically.")
    _info("Run `sahara sync` to upload new encrypted versions.")


@encryption.command("rotate")
@click.pass_context
def encryption_rotate(ctx: click.Context) -> None:
    """Rotate the encryption passphrase (re-encrypts all remote files)."""
    config: SaharaConfig = ctx.obj["config"]
    _require_config(config)

    from sahara.utils.encryption import (
        decrypt_file,
        derive_key,
        encrypt_file,
        generate_salt,
        get_passphrase,
        set_passphrase,
    )

    old_pp = get_passphrase()
    if not old_pp:
        _abort("No current passphrase found. Run `sahara encryption setup` first.")

    click.echo("  This will re-encrypt all files in S3 with a new passphrase.")
    if not click.confirm("  Continue?", default=False):
        return

    new_pp = click.prompt(
        "  New passphrase", hide_input=True, confirmation_prompt=True
    )

    _section("Re-encrypting files")
    import tempfile

    from sahara.storage.s3_client import S3Client
    from sahara.storage.state_db import StateDB

    db = StateDB().connect()
    s3 = S3Client(config)
    files = db.list_files()
    failed = 0

    with click.progressbar(files, label="  Re-encrypting") as bar:
        for record in bar:
            s3_key = config.get_s3_key(record.relative_path)
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    tmp = Path(tmpdir)
                    enc_dl = tmp / "downloaded.saha"
                    dec_tmp = tmp / "decrypted"
                    new_enc = tmp / "reencrypted.saha"

                    # Download
                    s3.download_file(s3_key, enc_dl)

                    # Decrypt with old key
                    from sahara.utils.encryption import _HEADER_LEN, _SALT_LEN

                    with open(enc_dl, "rb") as fh:
                        hdr = fh.read(_HEADER_LEN)
                    old_salt = hdr[5 : 5 + _SALT_LEN]
                    old_key = derive_key(old_pp, old_salt)
                    decrypt_file(enc_dl, dec_tmp, old_key)

                    # Re-encrypt with new key
                    new_salt = generate_salt()
                    new_key = derive_key(new_pp, new_salt)
                    sha256 = encrypt_file(dec_tmp, new_enc, new_key, new_salt)

                    metadata = {
                        "sahara-sha256": sha256,
                        "sahara-encrypted": "1",
                        "sahara-salt": new_salt.hex(),
                    }
                    s3.upload_file(new_enc, s3_key, metadata=metadata)
            except Exception as exc:
                _warn(f"Failed to re-encrypt {record.relative_path}: {exc}")
                failed += 1

    set_passphrase(new_pp)
    if failed:
        _warn(f"Re-encryption complete with {failed} failure(s).")
    else:
        _ok("All files re-encrypted successfully. New passphrase stored.")

    db.close()


# ---------------------------------------------------------------------------
# config group
# ---------------------------------------------------------------------------


@main.group("config")
def config_group() -> None:
    """View and modify configuration values."""


@config_group.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Show all configuration values."""
    config: SaharaConfig = ctx.obj["config"]
    config_path = ctx.obj.get("config_path") or DEFAULT_CONFIG_PATH
    _section(f"Configuration ({config_path})")
    for f_name in SaharaConfig.__dataclass_fields__:  # type: ignore[attr-defined]
        value = getattr(config, f_name)
        click.echo(f"  {f_name:<35} = {value!r}")


@config_group.command("get")
@click.argument("key")
@click.pass_context
def config_get(ctx: click.Context, key: str) -> None:
    """Get the value of a configuration key."""
    config: SaharaConfig = ctx.obj["config"]
    if not hasattr(config, key):
        _abort(f"Unknown config key: {key!r}")
    click.echo(getattr(config, key))


@config_group.command("set")
@click.argument("key")
@click.argument("value")
@click.pass_context
def config_set(ctx: click.Context, key: str, value: str) -> None:
    """Set a configuration value."""
    config: SaharaConfig = ctx.obj["config"]
    config_path = ctx.obj.get("config_path") or DEFAULT_CONFIG_PATH

    if not hasattr(config, key):
        _abort(f"Unknown config key: {key!r}")
    if key == "answer_provider" and value.lower() not in ("none", "ollama", "openai"):
        _abort("answer_provider must be 'none', 'ollama', or 'openai'.")
    if key == "answer_provider":
        value = value.lower()
    if key == "memory_folder":
        from sahara.memory.format import validate_memory_root_marker
        from sahara.storage.state_db import StateDB

        memory_path = Path(value).expanduser()
        if not memory_path.is_absolute():
            memory_path = Path.home() / memory_path
        value = str(memory_path.resolve())
        db = StateDB().connect()
        try:
            managed_roots = [
                Path(root["local_path"]).resolve()
                for root in db.list_content_roots()
                if validate_memory_root_marker(Path(root["local_path"]))
            ]
        finally:
            db.close()
        if managed_roots and Path(value) not in managed_roots:
            _abort(
                "memory_folder cannot be changed after managed memory has "
                "been initialized."
            )

    # Type coerce
    existing = getattr(config, key)
    try:
        if isinstance(existing, bool):
            coerced: object = value.lower() in ("1", "true", "yes")
        elif isinstance(existing, int):
            coerced = int(value)
        elif isinstance(existing, float):
            coerced = float(value)
        else:
            coerced = value
    except ValueError:
        _abort(f"Cannot convert {value!r} to type {type(existing).__name__}")

    setattr(config, key, coerced)
    save_config(config, config_path)
    _ok(f"{key} = {coerced!r}")


# ---------------------------------------------------------------------------
# Optional storage configuration
# ---------------------------------------------------------------------------


@main.group("storage")
def storage_group() -> None:
    """Configure optional storage for an existing Sahara library."""


@storage_group.command("configure")
@click.argument(
    "backend",
    type=click.Choice(["local", "aws"], case_sensitive=False),
)
@click.option(
    "--drive",
    "drives",
    type=click.Path(path_type=Path),
    multiple=True,
    help="Local drive, NAS, or network-share destination. Repeatable.",
)
@click.option("--bucket", default=None, help="AWS S3 bucket name.")
@click.option("--region", default="us-east-1", show_default=True)
@click.pass_context
def storage_configure(
    ctx: click.Context,
    backend: str,
    drives: tuple[Path, ...],
    bucket: str | None,
    region: str,
) -> None:
    """Attach local-drive or AWS storage without rebuilding the index."""
    config: SaharaConfig = ctx.obj["config"]
    _require_library_config(config)

    if backend == "local":
        if not drives:
            raise click.UsageError("At least one --drive is required for local storage.")
        config.storage_mode = "local"
        config.drive_paths = [
            str(path.expanduser().resolve()) for path in drives
        ]
        config.bucket = ""
        config.endpoint_url = ""
        config.delete_remote_on_local_delete = False
    else:
        if not bucket:
            raise click.UsageError("--bucket is required for AWS storage.")
        config.storage_mode = "s3"
        config.bucket = bucket.strip()
        config.region = region.strip()
        config.endpoint_url = ""
        config.drive_paths = []

    try:
        storage_backend = _create_backend(config)
        storage_backend.validate_bucket_access()
    except Exception as exc:
        raise click.ClickException(
            f"Storage validation failed; configuration was not changed: {exc}"
        ) from exc

    config_path = ctx.obj.get("config_path") or DEFAULT_CONFIG_PATH
    save_config(config, config_path)
    ctx.obj["config"] = config
    _ok(f"Configured {backend} storage.")
    _info(
        "Indexed folders remain index-only until enabled with "
        "`sahara folder sync <path> --enable`."
    )


@storage_group.command("status")
@click.pass_context
def storage_status(ctx: click.Context) -> None:
    """Show the active backend, sync roots, and offloaded-file count."""
    from sahara.storage.state_db import StateDB

    config: SaharaConfig = ctx.obj["config"]
    _require_library_config(config)
    db = StateDB().connect()
    try:
        roots = _content_roots(config, db)
        _section("Storage Status")
        _info(f"Mode       : {config.storage_mode}")
        _info(
            f"Sync roots : {sum(1 for root in roots if root.sync_enabled)}"
        )
        _info(
            "Offloaded  : "
            f"{db.count_index_entries(status='offloaded')} file(s)"
        )
    finally:
        db.close()


@storage_group.command("disable")
@click.option("--force", is_flag=True, help="Disable without confirmation.")
@click.pass_context
def storage_disable(ctx: click.Context, force: bool) -> None:
    """Disable storage without deleting any stored objects."""
    from sahara.storage.state_db import StateDB

    config: SaharaConfig = ctx.obj["config"]
    _require_library_config(config)
    if config.is_index_only_mode:
        _info("Storage is already disabled.")
        return
    if not force and not click.confirm(
        "  Disable storage sync? Existing stored data will be retained.",
        default=False,
    ):
        return

    db = StateDB().connect()
    try:
        for root in _content_roots(config, db):
            db.set_content_root_sync(str(root.local_path), False)
            if not root.is_primary:
                db.remove_sync_target(str(root.local_path))
    finally:
        db.close()

    config.storage_mode = "none"
    config_path = ctx.obj.get("config_path") or DEFAULT_CONFIG_PATH
    save_config(config, config_path)
    ctx.obj["config"] = config
    _ok("Storage disabled. Existing stored data was not deleted.")


# ---------------------------------------------------------------------------
# Canonical content-root management
# ---------------------------------------------------------------------------


@main.group("folder")
def folder_group() -> None:
    """Manage folders Sahara indexes and optionally syncs."""


@folder_group.command("add")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--name",
    default=None,
    help="Stable storage prefix (defaults to the folder name).",
)
@click.pass_context
def folder_add(ctx: click.Context, path: Path, name: str | None) -> None:
    """Add a folder to the local semantic index."""
    from sahara.library import (
        register_content_root,
        validate_content_root_path,
        validate_storage_prefix,
    )
    from sahara.storage.state_db import StateDB

    config: SaharaConfig = ctx.obj["config"]
    _require_library_config(config)
    resolved = path.expanduser().resolve()
    storage_prefix = name or resolved.name
    if not storage_prefix.strip("/"):
        _abort("Folder name cannot be empty.")

    db = StateDB().connect()
    try:
        try:
            roots = _content_roots(config, db)
            validate_content_root_path(resolved, roots)
            storage_prefix = validate_storage_prefix(
                storage_prefix,
                roots,
                owned_prefixes=db.list_storage_ownership_prefixes(),
            )
            _ensure_saharaignore(resolved)
            register_content_root(
                config,
                db,
                resolved,
                storage_prefix,
            )
        except ValueError as exc:
            _abort(str(exc))
        _ok(f"Added content root: {resolved}")
        _info("Mode: index only")
        _info("Run `sahara index` to add its contents to search.")
    finally:
        db.close()


@folder_group.command("list")
@click.pass_context
def folder_list(ctx: click.Context) -> None:
    """List indexed folders and their sync state."""
    ctx.invoke(folders_cmd)


@folder_group.command("remove")
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--force", is_flag=True, help="Remove without confirmation.")
@click.pass_context
def folder_remove(
    ctx: click.Context,
    path: Path,
    force: bool,
) -> None:
    """Remove a non-primary folder from Sahara's library."""
    from sahara.library import unregister_content_root
    from sahara.memory.format import validate_memory_root_marker
    from sahara.storage.state_db import StateDB

    config: SaharaConfig = ctx.obj["config"]
    _require_library_config(config)
    resolved = path.expanduser().resolve()

    db = StateDB().connect()
    try:
        _content_roots(config, db)
        root = db.get_content_root(str(resolved))
        if root is None:
            _abort(f"Folder not registered: {resolved}")
        if root["is_primary"]:
            _abort("The primary folder cannot be removed.")
        if validate_memory_root_marker(resolved):
            _abort("The managed Sahara memory folder cannot be removed.")
        if not force and not click.confirm(
            "  Remove this folder from Sahara's index?", default=False
        ):
            return
        unregister_content_root(db, resolved, root["storage_prefix"])
        _ok(f"Removed content root: {resolved}")
    finally:
        db.close()


@folder_group.command("sync")
@click.argument("path", type=click.Path(path_type=Path))
@click.option(
    "--enable/--disable",
    default=None,
    help="Enable or disable storage sync for this indexed folder.",
)
@click.pass_context
def folder_sync(
    ctx: click.Context,
    path: Path,
    enable: bool | None,
) -> None:
    """Change whether an indexed folder participates in storage sync."""
    from sahara.storage.state_db import StateDB

    config: SaharaConfig = ctx.obj["config"]
    _require_library_config(config)
    if enable is None:
        raise click.UsageError("Choose either --enable or --disable.")
    if enable:
        _require_storage_config(config)

    resolved = path.expanduser().resolve()
    db = StateDB().connect()
    try:
        _content_roots(config, db)
        root = db.get_content_root(str(resolved))
        if root is None:
            _abort(f"Folder not registered: {resolved}")
        db.set_content_root_sync(str(resolved), enable)
        if enable and not root["is_primary"]:
            db.add_sync_target(str(resolved), root["storage_prefix"])
        elif not enable:
            db.remove_sync_target(str(resolved))
        _ok(
            f"Sync {'enabled' if enable else 'disabled'} for: {resolved}"
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Legacy multi-folder sync commands: add / remove / folders
# ---------------------------------------------------------------------------


@main.command("add")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--as",
    "name",
    default=None,
    help="S3 prefix name (defaults to basename of the folder).",
)
@click.option(
    "--dest",
    "dest",
    default=None,
    help="Destination folder path under the bucket (e.g. 'archive/2024'). "
         "The source folder is placed inside it. Ignored if --as is provided.",
)
@click.pass_context
def add_folder(ctx: click.Context, path: Path, name: str | None, dest: str | None) -> None:
    """Register an additional folder for sync."""
    from sahara.library import (
        register_content_root,
        validate_content_root_path,
        validate_storage_prefix,
    )
    from sahara.storage.state_db import StateDB

    config: SaharaConfig = ctx.obj["config"]
    _require_config(config)

    resolved = path.expanduser().resolve()
    if name:
        s3_prefix = name
    elif dest:
        s3_prefix = dest.strip("/") + "/" + resolved.name
    else:
        s3_prefix = resolved.name

    db = StateDB().connect()
    try:
        try:
            roots = _content_roots(config, db)
            validate_content_root_path(resolved, roots)
            s3_prefix = validate_storage_prefix(
                s3_prefix,
                roots,
                owned_prefixes=db.list_storage_ownership_prefixes(),
            )
            _ensure_saharaignore(resolved)
            root = register_content_root(
                config,
                db,
                resolved,
                s3_prefix,
                sync_enabled=True,
            )
            s3_prefix = root.storage_prefix
        except ValueError as exc:
            _abort(str(exc))

        db.add_sync_target(str(resolved), s3_prefix)
        _ok(f"Registered: {resolved}")
        _info(f"S3 prefix  : {s3_prefix}/")
        _info(f"S3 location: s3://{config.bucket}/{s3_prefix}/")
        _info("Run `sahara sync` to sync this folder now.")
    finally:
        db.close()


@main.command("remove")
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--force", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def remove_folder(ctx: click.Context, path: Path, force: bool) -> None:
    """Unregister an additional sync folder (does not delete S3 data)."""
    from sahara.library import unregister_content_root
    from sahara.memory.format import validate_memory_root_marker
    from sahara.storage.state_db import StateDB

    config: SaharaConfig = ctx.obj["config"]
    _require_config(config)

    resolved = path.expanduser().resolve()
    db = StateDB().connect()
    try:
        targets = db.list_sync_targets()
        target = next(
            (t for t in targets if Path(t["local_path"]) == resolved), None
        )
        if target is None:
            _abort(f"Folder not registered: {resolved}")
        if validate_memory_root_marker(resolved):
            _abort("The managed Sahara memory folder cannot be removed.")

        file_count = len(db.list_files(s3_prefix=target["s3_prefix"]))
        if file_count > 0:
            _warn(
                f"{file_count} file(s) tracked for this folder remain in S3 "
                f"under prefix '{target['s3_prefix']}/'."
            )
            _warn("Removing this registration does NOT delete them from S3.")
            if not force and not click.confirm("  Continue?", default=False):
                return

        unregister_content_root(db, resolved, target["s3_prefix"])
        _ok(f"Unregistered: {resolved}")
    finally:
        db.close()


@main.command("folders")
@click.pass_context
def folders_cmd(ctx: click.Context) -> None:
    """List all folders registered for indexing and optional sync."""
    from sahara.storage.state_db import StateDB

    config: SaharaConfig = ctx.obj["config"]
    _require_library_config(config)

    db = StateDB().connect()
    try:
        roots = _content_roots(config, db)
        _section("Content Roots")
        for root in roots:
            exists_mark = "" if root.local_path.exists() else " (missing)"
            role = "primary" if root.is_primary else "additional"
            sync_state = "sync enabled" if root.sync_enabled else "index only"
            click.echo(
                click.style(
                    f"  {'*' if root.is_primary else '+'} {root.local_path}",
                    fg="green" if root.is_primary else "white",
                    bold=root.is_primary,
                )
                + f"  [{role}, {sync_state}]{exists_mark}"
            )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# sync / push / pull
# ---------------------------------------------------------------------------


def _run_sync(
    ctx: click.Context,
    push_only: bool = False,
    pull_only: bool = False,
    dry_run: bool = False,
    verify: bool = False,
    wait: bool = False,
    folder: str | None = None,
) -> None:
    from sahara.models import SyncResult
    from sahara.storage.state_db import StateDB

    config: SaharaConfig = ctx.obj["config"]
    _require_storage_config(config)

    if dry_run:
        click.echo(click.style("  [DRY RUN — no changes will be made]", fg="yellow"))

    # Build target list: primary folder + all registered additional targets
    db_main = StateDB().connect()
    try:
        all_targets = [
            (root.local_path, root.storage_prefix)
            for root in _content_roots(config, db_main)
            if root.sync_enabled
        ]
        if not all_targets:
            # Compatibility fallback for older databases and lightweight test
            # doubles that do not expose the content_roots API yet.
            all_targets = [(config.get_sync_folder_path(), "")]
    finally:
        db_main.close()

    # Filter to a specific folder if requested
    if folder:
        resolved = str(Path(folder).expanduser().resolve())
        targets = [(f, p) for f, p in all_targets if str(f) == resolved]
        if not targets:
            _abort(
                f"'{folder}' is not a registered sync folder. "
                "Use `sahara folders` to see all registered folders."
            )
    else:
        targets = all_targets

    aggregate = SyncResult()
    any_failed = False
    for target_folder, prefix in targets:
        label = f"  Syncing {target_folder}" + (f" (→ {prefix}/)" if prefix else "")
        click.echo(click.style(label, fg="cyan"))
        engine, db, s3 = _build_engine(config, sync_folder=target_folder, s3_prefix=prefix)
        try:
            result = engine.sync(
                push_only=push_only or config.upload_only,
                pull_only=pull_only,
                dry_run=dry_run,
                verify=verify,
            )
            aggregate.uploaded.extend(result.uploaded)
            aggregate.downloaded.extend(result.downloaded)
            aggregate.deleted.extend(result.deleted)
            aggregate.moved.extend(result.moved)
            aggregate.failed.extend(result.failed)
            aggregate.conflicts.extend(result.conflicts)
            aggregate.skipped.extend(result.skipped)
        except Exception as exc:
            click.echo(click.style(f"  Sync failed for {folder}: {exc}", fg="red"))
            any_failed = True
        finally:
            db.close()

    _section("Sync Result")
    for line in aggregate.summary_lines():
        click.echo(line)

    if aggregate.had_errors:
        _section("Errors")
        for path, error in aggregate.failed:
            _err(f"{path}: {error}")

    if aggregate.conflicts and config.conflict_strategy == "ask":
        click.echo()
        _warn(
            f"{len(aggregate.conflicts)} conflict(s) found. "
            "Run `sahara conflicts` to review and `sahara resolve` to fix."
        )

    if any_failed:
        sys.exit(1)


@main.command()
@click.option("--dry-run", is_flag=True, help="Show what would change without doing it.")
@click.option("--verify", is_flag=True, help="Verify uploaded files via HEAD check.")
@click.option("--wait", is_flag=True, help="Wait for all restores to complete.")
@click.option("--folder", "-f", default=None, help="Sync only this folder (local path).")
@click.pass_context
def sync(ctx: click.Context, dry_run: bool, verify: bool, wait: bool, folder: str | None) -> None:
    """Sync local folder(s) with S3 (bidirectional)."""
    _run_sync(ctx, dry_run=dry_run, verify=verify, wait=wait, folder=folder)


@main.command()
@click.option("--dry-run", is_flag=True)
@click.option("--verify", is_flag=True)
@click.option("--folder", "-f", default=None, help="Push only this folder (local path).")
@click.pass_context
def push(ctx: click.Context, dry_run: bool, verify: bool, folder: str | None) -> None:
    """Push local changes to S3 (upload only)."""
    _run_sync(ctx, push_only=True, dry_run=dry_run, verify=verify, folder=folder)


@main.command()
@click.option("--dry-run", is_flag=True)
@click.option("--wait", is_flag=True)
@click.option("--folder", "-f", default=None, help="Pull only this folder (local path).")
@click.pass_context
def pull(ctx: click.Context, dry_run: bool, wait: bool, folder: str | None) -> None:
    """Pull remote changes from S3 (download only)."""
    _run_sync(ctx, pull_only=True, dry_run=dry_run, wait=wait, folder=folder)


# ---------------------------------------------------------------------------
# status / diff
# ---------------------------------------------------------------------------


@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show pending changes without executing them."""
    config: SaharaConfig = ctx.obj["config"]
    _require_config(config)

    engine, db, s3 = _build_engine(config)
    try:
        diff = engine.get_status()
    finally:
        db.close()

    if diff.is_empty():
        click.echo(click.style("  Everything up to date.", fg="green"))
        return

    _section("Pending Changes")
    for path in diff.local_new:
        click.echo(click.style(f"  + {path}", fg="green"))
    for path in diff.local_modified:
        click.echo(click.style(f"  M {path}", fg="yellow"))
    for path in diff.remote_new:
        click.echo(click.style(f"  D {path}", fg="cyan"))
    for path in diff.remote_modified:
        click.echo(click.style(f"  U {path}", fg="cyan"))
    for path in diff.local_deleted:
        click.echo(click.style(f"  - {path}", fg="red"))
    for path in diff.remote_deleted:
        click.echo(click.style(f"  r {path}", fg="magenta"))
    for path in diff.conflict:
        click.echo(click.style(f"  ! {path}", fg="red", bold=True))
    for old, new in diff.local_moves:
        click.echo(click.style(f"  → {old} → {new}", fg="blue"))


@main.command("diff")
@click.pass_context
def diff_cmd(ctx: click.Context) -> None:
    """Alias for `status` — show pending changes."""
    ctx.invoke(status)


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


@main.group()
def mcp() -> None:
    """Run Sahara MCP integrations."""


@mcp.command("install-claude")
@click.option(
    "--claude-config",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override the detected Claude Desktop config file.",
)
@click.option(
    "--executable",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override the detected Sahara executable.",
)
@click.option(
    "--enable-memory-write",
    is_flag=True,
    help="Opt in to the create-only sahara_remember tool for this local client.",
)
@click.pass_context
def mcp_install_claude(
    ctx: click.Context,
    claude_config: Path | None,
    executable: Path | None,
    enable_memory_write: bool,
) -> None:
    """Install Sahara as a local MCP server in Claude Desktop."""
    from sahara.claude_desktop import (
        detect_claude_config_path,
        install_claude_server,
        resolve_sahara_executable,
    )

    try:
        config_path = claude_config or detect_claude_config_path()
        executable_path = resolve_sahara_executable(executable)
        result = install_claude_server(
            config_path,
            executable_path,
            sahara_config_path=ctx.obj.get("config_path"),
            enable_memory_write=enable_memory_write,
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    if result.changed:
        _ok(f"Installed Sahara in Claude Desktop: {result.config_path}")
        if result.backup_path is not None:
            _info(f"Backup: {result.backup_path}")
    else:
        _ok(f"Sahara is already configured in Claude Desktop: {result.config_path}")
    _info(f"Command: {result.executable_path}")
    if enable_memory_write:
        _warn(
            "Memory capture is enabled for this local Claude Desktop connection. "
            "Recall remains read-only unless the user explicitly asks to save something."
        )
    _info("Fully quit and reopen Claude Desktop, then look for Sahara in Connectors.")


@mcp.command("serve")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http", "streamable-http", "sse"]),
    default="stdio",
    show_default=True,
    help="MCP transport. Use 'http' for remote/mobile clients.",
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Host for HTTP/SSE transports.",
)
@click.option(
    "--port",
    default=8765,
    show_default=True,
    type=int,
    help="Port for HTTP/SSE transports.",
)
@click.option(
    "--auth-token",
    envvar="SAHARA_MCP_AUTH_TOKEN",
    default=None,
    help="Bearer token required by HTTP/SSE transports. Can also be set with SAHARA_MCP_AUTH_TOKEN.",
)
@click.option(
    "--allow-insecure-http",
    is_flag=True,
    help="Allow HTTP/SSE transports without a bearer token. For local experiments only.",
)
@click.option(
    "--allow-tool",
    "allowed_tools",
    multiple=True,
    type=click.Choice(
        [
            "sahara_search",
            "sahara_ask",
            "sahara_read_chunk",
            "sahara_list_folders",
            "sahara_index_status",
            "sahara_recall",
            "sahara_remember",
        ]
    ),
    help="Expose only this MCP tool. Repeat to allow multiple tools.",
)
@click.option(
    "--allow-storage-prefix",
    "allowed_storage_prefixes",
    multiple=True,
    help="Allow only this Sahara storage prefix/folder scope. Repeat to allow multiple prefixes.",
)
@click.option(
    "--max-snippet-chars",
    default=500,
    show_default=True,
    type=click.IntRange(min=0),
    help="Maximum text characters returned per snippet/chunk by MCP tools.",
)
@click.option(
    "--enable-memory-write",
    is_flag=True,
    help="Expose create-only sahara_remember over local stdio.",
)
@click.pass_context
def mcp_serve(
    ctx: click.Context,
    transport: str,
    host: str,
    port: int,
    auth_token: str | None,
    allow_insecure_http: bool,
    allowed_tools: tuple[str, ...],
    allowed_storage_prefixes: tuple[str, ...],
    max_snippet_chars: int,
    enable_memory_write: bool,
) -> None:
    """Serve Sahara retrieval tools and optional local memory capture."""
    from sahara.mcp_server import serve

    config_path = ctx.obj.get("config_path")
    mcp_transport = cast(
        Literal["stdio", "sse", "streamable-http"],
        "streamable-http" if transport == "http" else transport,
    )
    remote_transport = mcp_transport != "stdio"
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}

    if remote_transport and not auth_token and not allow_insecure_http:
        raise click.ClickException(
            "HTTP/SSE MCP transports require --auth-token or SAHARA_MCP_AUTH_TOKEN. "
            "Use --allow-insecure-http only for temporary local experiments."
        )

    if enable_memory_write and remote_transport:
        raise click.ClickException(
            "MCP memory writes are available only over the local stdio transport."
        )
    if "sahara_remember" in allowed_tools and not enable_memory_write:
        raise click.ClickException(
            "sahara_remember requires --enable-memory-write."
        )

    if remote_transport and host not in loopback_hosts:
        click.secho(
            f"WARNING: Sahara MCP is binding to {host}:{port}. "
            "Use 127.0.0.1 with a secure tunnel unless you intentionally want LAN/public access.",
            fg="yellow",
            err=True,
        )

    if remote_transport and not auth_token and allow_insecure_http:
        click.secho(
            "WARNING: HTTP/SSE MCP is running without bearer-token authentication.",
            fg="yellow",
            err=True,
        )

    try:
        serve(
            str(config_path) if config_path else None,
            transport=mcp_transport,
            host=host,
            port=port,
            auth_token=auth_token,
            allowed_tools=cast(tuple[Any, ...], allowed_tools) if allowed_tools else None,
            allowed_storage_prefixes=allowed_storage_prefixes or None,
            max_snippet_chars=max_snippet_chars,
            enable_memory_write=enable_memory_write,
        )
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


# ---------------------------------------------------------------------------
# conflicts / resolve
# ---------------------------------------------------------------------------


@main.command()
@click.pass_context
def conflicts(ctx: click.Context) -> None:
    """List all unresolved conflicts."""
    config: SaharaConfig = ctx.obj["config"]
    _require_config(config)

    engine, db, s3 = _build_engine(config)
    try:
        diff = engine.get_status()
    finally:
        db.close()

    if not diff.conflict:
        click.echo(click.style("  No conflicts found.", fg="green"))
        return

    _section("Conflicts")
    for path in diff.conflict:
        click.echo(click.style(f"  ! {path}", fg="red", bold=True))
    click.echo()
    _info("Use `sahara resolve --keep local|remote|backup <path>` to resolve.")


@main.command()
@click.argument("path", required=False)
@click.option(
    "--keep",
    type=click.Choice(["local", "remote", "backup"]),
    default="backup",
    show_default=True,
    help="Which version to keep.",
)
@click.pass_context
def resolve(ctx: click.Context, path: str | None, keep: str) -> None:
    """Resolve a conflict for a specific file."""
    config: SaharaConfig = ctx.obj["config"]
    _require_config(config)
    config.conflict_strategy = keep
    ctx.obj["config"] = config

    if path:
        _info(f"Resolving conflict for {path} (keep={keep})…")
        _run_sync(ctx, push_only=(keep == "local"), pull_only=(keep == "remote"))
    else:
        _run_sync(ctx)


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------


@main.command("ls")
@click.argument("prefix", default="", required=False)
@click.option(
    "--tier",
    type=click.Choice(["GLACIER_IR", "STANDARD", "GLACIER", "DEEP_ARCHIVE", "HOT_TEMP"]),
    default=None,
    help="Filter by storage tier (GLACIER_IR=Normal, STANDARD=Premium, DEEP_ARCHIVE=Archive).",
)
@click.option("--long", "-l", is_flag=True, help="Long listing with metadata.")
@click.option("--all", "show_all", is_flag=True, help="Show files from all registered folders.")
@click.pass_context
def ls_cmd(
    ctx: click.Context, prefix: str, tier: str | None, long: bool, show_all: bool
) -> None:
    """List tracked files."""
    config: SaharaConfig = ctx.obj["config"]
    _require_config(config)

    from sahara.storage.state_db import StateDB

    db = StateDB()
    db.connect()
    try:
        # Build list of (display_prefix, s3_prefix) pairs to query
        s3_prefixes: list[tuple[str, str]] = [("", "")]  # primary folder
        if show_all:
            for t in db.list_sync_targets():
                s3_prefixes.append((t["s3_prefix"] + "/", t["s3_prefix"]))

        all_rows: list[tuple[str, Any, str]] = []
        for display_prefix, s3_pref in s3_prefixes:
            if tier:
                files = db.list_files_by_tier(tier, s3_prefix=s3_pref)  # type: ignore[arg-type]
            else:
                files = db.list_files(s3_prefix=s3_pref)

            for f in files:
                display_path = display_prefix + f.relative_path
                if not prefix or display_path.startswith(prefix):
                    all_rows.append((display_path, f, s3_pref))

        if not all_rows:
            _info("No files found.")
            return

        if long:
            _section(f"Files ({len(all_rows)})")
            click.echo(
                f"  {'Path':<50} {'Size':>10} {'Tier':<15} {'SHA256':>12}  {'Modified'}"
            )
            click.echo("  " + "─" * 110)
            for display_path, f, s3_pref in sorted(all_rows, key=lambda x: x[0]):
                size_str = _human_size(f.size_bytes)
                sha_short = f.sha256_checksum[:8] + "…" if f.sha256_checksum else "—"
                mtime = f.local_modified_at.strftime("%Y-%m-%d %H:%M")
                from sahara.models import TIER_LABELS

                tier_color = {
                    "GLACIER_IR": "green",
                    "STANDARD": "bright_green",
                    "GLACIER": "blue",
                    "DEEP_ARCHIVE": "magenta",
                    "HOT_TEMP": "cyan",
                }.get(f.tier, "white")
                tier_label = TIER_LABELS.get(f.tier, f.tier)
                tier_str = click.style(tier_label, fg=tier_color)
                residency = db.get_storage_residency(
                    s3_pref, f.relative_path
                )
                state = (
                    " offloaded"
                    if residency and residency["local_state"] == "offloaded"
                    else ""
                )
                click.echo(
                    f"  {display_path:<50} {size_str:>10} {tier_str:<24}  "
                    f"{sha_short}  {mtime}{state}"
                )
        else:
            for display_path, file_record, s3_pref in sorted(
                all_rows, key=lambda x: x[0]
            ):
                residency = db.get_storage_residency(
                    s3_pref, file_record.relative_path
                )
                marker = (
                    " [offloaded]"
                    if residency and residency["local_state"] == "offloaded"
                    else ""
                )
                click.echo(f"  {display_path}{marker}")
    finally:
        db.close()


def _human_size(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} PB"


# ---------------------------------------------------------------------------
# rm
# ---------------------------------------------------------------------------


@main.command("rm")
@click.argument("path")
@click.option("--force", is_flag=True, help="Skip confirmation prompt.")
@click.option("--local", "local_only", is_flag=True, help="Delete locally only (keep S3 copy).")
@click.pass_context
def rm_cmd(ctx: click.Context, path: str, force: bool, local_only: bool) -> None:
    """Remove a file from S3 (and optionally locally)."""
    config: SaharaConfig = ctx.obj["config"]
    _require_config(config)

    if not force:
        if local_only:
            target = f"local file '{path}'"
        else:
            target = f"'{path}' from S3 and locally"
        if not click.confirm(f"  Delete {target}?"):
            return

    from sahara.storage.s3_client import S3Client
    from sahara.storage.state_db import StateDB

    db = StateDB()
    db.connect()
    s3 = S3Client(config)
    try:
        if not local_only:
            s3_key = config.get_s3_key(path)
            try:
                s3.delete_object(s3_key)
                _ok(f"Deleted from S3: {path}")
            except Exception as exc:
                _abort(f"S3 delete failed: {exc}")

        local_abs = config.get_sync_folder_path() / path
        if local_abs.exists():
            local_abs.unlink()
            _ok(f"Deleted locally: {path}")

        db.mark_deleted(path)
        db.add_history(path, "manual_delete", details="via rm command")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# mv
# ---------------------------------------------------------------------------


@main.command("mv")
@click.argument("src")
@click.argument("dst")
@click.pass_context
def mv_cmd(ctx: click.Context, src: str, dst: str) -> None:
    """Rename/move a file within the sync folder and on S3."""
    config: SaharaConfig = ctx.obj["config"]
    _require_config(config)

    import shutil

    from sahara.storage.s3_client import S3Client
    from sahara.storage.state_db import StateDB

    db = StateDB()
    db.connect()
    s3 = S3Client(config)
    try:
        src_abs = config.get_sync_folder_path() / src
        dst_abs = config.get_sync_folder_path() / dst

        if not src_abs.exists():
            _abort(f"Source file not found: {src}")

        dst_abs.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_abs), str(dst_abs))
        _ok(f"Moved locally: {src} → {dst}")

        src_key = config.get_s3_key(src)
        dst_key = config.get_s3_key(dst)
        try:
            rec_before = db.get_file(src)
            storage_class = rec_before.tier if rec_before else config.default_storage_class
            s3.copy_object(src_key, dst_key, storage_class=storage_class)
            s3.delete_object(src_key)
            _ok(f"Moved in S3: {src} → {dst}")
        except Exception as exc:
            _warn(f"S3 move failed (local move succeeded): {exc}")

        rec = db.get_file(src)
        if rec:
            db.delete_file(src)
            import datetime

            now = datetime.datetime.now(datetime.UTC)
            rec.relative_path = dst
            rec.last_sync_at = now
            db.upsert_file(rec)
            db.add_history(dst, "move", details=f"from:{src}")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------


@main.command()
@click.argument("paths", nargs=-1)
@click.option(
    "--older-than",
    type=int,
    default=None,
    help="Archive files not modified in N days.",
)
@click.option("--dry-run", is_flag=True, help="Show what would be archived.")
@click.option("--force", is_flag=True, help="Skip confirmation.")
@click.option(
    "--storage-class",
    type=click.Choice(["DEEP_ARCHIVE", "GLACIER_IR", "STANDARD"]),
    default="DEEP_ARCHIVE",
    show_default=True,
    help="Target storage class (DEEP_ARCHIVE=Archive, GLACIER_IR=Normal, STANDARD=Premium).",
)
@click.option(
    "--folder",
    default=None,
    help="Local path of a registered folder to archive files from (defaults to primary folder).",
)
@click.pass_context
def archive(
    ctx: click.Context,
    paths: tuple[str, ...],
    older_than: int | None,
    dry_run: bool,
    force: bool,
    storage_class: str,
    folder: str | None,
) -> None:
    """Archive files to Glacier / Deep Archive."""
    config: SaharaConfig = ctx.obj["config"]
    _require_config(config)
    _require_s3_tiers(config, "archive")

    from pathlib import Path as _Path

    from sahara.storage.state_db import StateDB

    db = StateDB()
    db.connect()
    try:
        # Resolve which folder/engine to use
        sync_folder = None
        s3_prefix = ""
        if folder:
            resolved = str(_Path(folder).expanduser().resolve())
            targets = {t["local_path"]: t for t in db.list_sync_targets()}
            if resolved not in targets:
                _abort(f"Folder '{folder}' is not a registered sync target. Use `sahara folders` to list.")
            sync_folder = _Path(resolved)
            s3_prefix = targets[resolved]["s3_prefix"]

        if older_than is not None:
            cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
                days=older_than
            )
            # Collect files from active tiers (Normal + Premium) eligible for archiving
            all_files = (
                db.list_files_by_tier("GLACIER_IR", s3_prefix=s3_prefix)
                + db.list_files_by_tier("STANDARD", s3_prefix=s3_prefix)
            )
            target_paths = [
                f.relative_path
                for f in all_files
                if f.local_modified_at < cutoff
            ]
        elif paths:
            target_paths = list(paths)
        else:
            # No paths and no --older-than: archive ALL files in that folder
            all_files = (
                db.list_files_by_tier("GLACIER_IR", s3_prefix=s3_prefix)
                + db.list_files_by_tier("STANDARD", s3_prefix=s3_prefix)
            )
            target_paths = [f.relative_path for f in all_files]

        if not target_paths:
            _info("No files to archive.")
            return

        click.echo(f"  Files to archive: {len(target_paths)}")
        for p in target_paths[:10]:
            _info(f"  {p}")
        if len(target_paths) > 10:
            _info(f"  … and {len(target_paths) - 10} more")

        if dry_run:
            click.echo(click.style("  [DRY RUN — no changes made]", fg="yellow"))
            return

        if not force and not click.confirm(
            f"  Archive {len(target_paths)} file(s) to {storage_class}?"
        ):
            return

        engine, _db2, s3 = _build_engine(config, sync_folder=sync_folder, s3_prefix=s3_prefix)
        archived = engine.archive_files(target_paths, storage_class=storage_class)
        _ok(f"Archived {len(archived)} file(s) to {storage_class}.")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# restore / restore-status / restore-download
# ---------------------------------------------------------------------------


@main.command("restore")
@click.argument("path")
@click.option("--days", default=7, show_default=True, help="Number of days to keep restored.")
@click.option(
    "--tier",
    type=click.Choice(["Expedited", "Standard", "Bulk"]),
    default="Bulk",
    show_default=True,
)
@click.pass_context
def restore_cmd(ctx: click.Context, path: str, days: int, tier: str) -> None:
    """Request a Glacier restore for a file."""
    config: SaharaConfig = ctx.obj["config"]
    _require_config(config)
    _require_s3_tiers(config, "restore")

    engine, db, s3 = _build_engine(config)
    try:
        engine.request_restore(path, days=days, tier=tier)
        _ok(f"Restore requested for '{path}' (tier={tier}, days={days}).")
        _info("Use `sahara restore-status` to check progress.")
    except Exception as exc:
        _abort(f"Restore request failed: {exc}")
    finally:
        db.close()


@main.command("restore-status")
@click.argument("path", required=False)
@click.pass_context
def restore_status_cmd(ctx: click.Context, path: str | None) -> None:
    """Check the status of a Glacier restore."""
    config: SaharaConfig = ctx.obj["config"]
    _require_config(config)
    _require_s3_tiers(config, "restore-status")

    from sahara.storage.state_db import StateDB

    db = StateDB()
    db.connect()
    try:
        if path:
            files_to_check = [path]
        else:
            pending = db.list_pending_restores()
            files_to_check = [r.relative_path for r in pending]

        if not files_to_check:
            _info("No pending restores found.")
            return

        _section("Restore Status")
        for p in files_to_check:
            engine, _db2, _s3 = _build_engine(config)
            try:
                status = engine.check_restore_status(p)
                ready = status["ready"]
                status_str = click.style("READY", fg="green") if ready else click.style("PENDING", fg="yellow")
                click.echo(f"  {p}")
                click.echo(f"    Status   : {status_str}")
                click.echo(f"    Tier     : {status['tier']}")
                if status["expires_at"]:
                    click.echo(f"    Expires  : {status['expires_at']}")
            except Exception as exc:
                _warn(f"{p}: {exc}")
    finally:
        db.close()


@main.command("restore-download")
@click.argument("path")
@click.pass_context
def restore_download_cmd(ctx: click.Context, path: str) -> None:
    """Download a file that has been restored from Glacier."""
    config: SaharaConfig = ctx.obj["config"]
    _require_config(config)
    _require_s3_tiers(config, "restore-download")

    engine, db, s3 = _build_engine(config)
    try:
        result = engine.download_restored(path)
        if result:
            _ok(f"Downloaded: {path}")
        else:
            _warn(f"File '{path}' is not yet available. Check status with `sahara restore-status`.")
    except Exception as exc:
        _abort(f"Download failed: {exc}")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------


@main.command()
@click.option("--simulate", is_flag=True, help="Run an interactive cost simulation.")
@click.option("--standard-gb", type=float, default=None)
@click.option("--glacier-gb", type=float, default=None)
@click.option("--deep-archive-gb", type=float, default=None)
@click.option("--monthly-puts", type=int, default=1000)
@click.option("--monthly-gets", type=int, default=1000)
@click.option("--monthly-egress-gb", type=float, default=1.0)
@click.pass_context
def usage(
    ctx: click.Context,
    simulate: bool,
    standard_gb: float | None,
    glacier_gb: float | None,
    deep_archive_gb: float | None,
    monthly_puts: int,
    monthly_gets: int,
    monthly_egress_gb: float,
) -> None:
    """Show storage usage and cost estimates."""
    config: SaharaConfig = ctx.obj["config"]

    from sahara.storage.cost_estimator import CostEstimator
    from sahara.storage.s3_client import S3Client
    from sahara.storage.state_db import StateDB

    estimator = CostEstimator()

    if simulate:
        if standard_gb is None:
            standard_gb = click.prompt("  Standard storage (GB)", type=float, default=0.0)
        if glacier_gb is None:
            glacier_gb = click.prompt("  Glacier storage (GB)", type=float, default=0.0)
        if deep_archive_gb is None:
            deep_archive_gb = click.prompt(
                "  Deep Archive storage (GB)", type=float, default=0.0
            )
        report = estimator.simulate_cost(
            standard_gb=standard_gb,
            glacier_gb=glacier_gb,
            deep_archive_gb=deep_archive_gb,
            monthly_puts=monthly_puts,
            monthly_gets=monthly_gets,
            monthly_egress_gb=monthly_egress_gb,
        )
        click.echo(report)
    else:
        if not config.bucket:
            _abort("No bucket configured. Run `sahara init` first.")

        db = StateDB()
        db.connect()
        s3 = S3Client(config)
        try:
            report = estimator.get_usage_report(db, s3)
            click.echo(report)
        finally:
            db.close()


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


@main.command()
@click.argument("path", required=False)
@click.option("--limit", default=50, show_default=True)
@click.pass_context
def history(ctx: click.Context, path: str | None, limit: int) -> None:
    """Show sync history (optionally for a specific file)."""
    from sahara.storage.state_db import StateDB

    db = StateDB()
    db.connect()
    try:
        entries = db.get_history(relative_path=path, limit=limit)
        if not entries:
            _info("No history found.")
            return

        _section("Sync History")
        click.echo(f"  {'Time':<25} {'Operation':<18} {'Path'}")
        click.echo("  " + "─" * 80)
        for e in entries:
            op_color = {
                "upload": "green",
                "download": "cyan",
                "delete_remote": "red",
                "delete_local": "red",
                "move": "blue",
                "archive": "magenta",
                "restore_request": "yellow",
            }.get(e["operation"], "white")
            op_str = click.style(f"{e['operation']:<18}", fg=op_color)
            ts = e["occurred_at"][:19].replace("T", " ")
            click.echo(f"  {ts:<25} {op_str} {e['relative_path']}")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------


@main.command("index")
@click.option("--folder", "-f", default=None, help="Index only this folder (local path).")
@click.option("--force", is_flag=True, help="Re-index all files even if unchanged.")
@click.pass_context
def index_cmd(ctx: click.Context, folder: str | None, force: bool) -> None:
    """Index file contents for semantic search."""
    config: SaharaConfig = ctx.obj["config"]
    _require_library_config(config)

    from sahara.library import IndexingService
    from sahara.storage.state_db import StateDB

    db = StateDB().connect()
    try:
        service = IndexingService(config, db)
        root_path = Path(folder) if folder else None
        if db.count_embeddings() == 0:
            _info(
                "Preparing semantic search. First use may download the local "
                "embedding model (~200 MB)."
            )
            _info(
                "Hugging Face authentication is optional; its anonymous-download "
                "warning is harmless."
            )
        try:
            result = service.index(root_path=root_path, force=force)
        except ValueError as exc:
            _abort(str(exc))

        click.echo()
        _ok(
            f"Done — {result.indexed} indexed, {result.skipped} skipped, "
            f"{result.failed} failed."
        )
        reasons = {
            "unchanged": result.unchanged,
            "unsupported": result.unsupported,
            "no_text": result.no_text,
            "missing": result.missing,
        }
        reason_text = ", ".join(
            f"{reason}={count}" for reason, count in reasons.items() if count
        )
        if reason_text:
            _info(f"Details: {reason_text}")
        _info(f"Total in index: {db.count_embeddings()} file(s).")
    finally:
        db.close()


@main.command("index-report")
@click.option("--top", default=10, show_default=True, help="Number of extensions to show.")
@click.option("--sample", default=20, show_default=True, help="Number of unindexed files to list.")
@click.pass_context
def index_report_cmd(ctx: click.Context, top: int, sample: int) -> None:
    """Show indexed/unindexed file counts and sample gaps."""
    config: SaharaConfig = ctx.obj["config"]
    _require_library_config(config)

    from sahara.storage.state_db import StateDB

    db = StateDB().connect()
    try:
        _content_roots(config, db)
        tracked = db.count_index_entries()
        statuses = db.count_index_entries_by_status()
        indexed = statuses.get("indexed", 0)
        chunks = db.count_chunks()
        unindexed = max(0, tracked - indexed)

        _section("Index Report")
        _info(f"Tracked files : {tracked}")
        _info(f"Indexed files : {indexed}")
        _info(f"Indexed chunks: {chunks}")
        _info(f"Unindexed     : {unindexed}")
        _info(f"Vector index  : {'available' if db.has_vec_table() else 'not available'}")

        if statuses:
            click.echo()
            _section("Inventory Status")
            for status_name, count in statuses.items():
                _info(f"{status_name}: {count}")

        ext_counts = db.count_unindexed_entries_by_extension()
        if ext_counts:
            click.echo()
            _section("Unindexed by Extension")
            for ext, count in list(ext_counts.items())[: max(1, top)]:
                _info(f"{ext}: {count}")

        samples = [
            row
            for row in db.list_index_entries(limit=None)
            if row["status"] != "indexed"
        ][: max(0, sample)]
        if samples:
            click.echo()
            _section("Sample Unindexed Files")
            for row in samples:
                prefix = (
                    f"{row['storage_prefix']}/" if row["storage_prefix"] else ""
                )
                _info(f"{prefix}{row['relative_path']}")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# captured knowledge
# ---------------------------------------------------------------------------


@main.command("remember")
@click.argument("text", nargs=-1)
@click.option("--title", default=None, help="Optional short title.")
@click.option(
    "--source",
    "source_type",
    type=click.Choice(
        ["manual", "web", "conversation", "ai-chat", "mobile"],
        case_sensitive=False,
    ),
    default="manual",
    show_default=True,
    help="Where this knowledge came from.",
)
@click.option("--url", "source_url", default="", help="Optional HTTP(S) source URL.")
@click.option("--source-id", default="", help="Optional identifier from the source system.")
@click.option(
    "--idempotency-key",
    default="",
    help="Optional retry key used to prevent duplicate captures.",
)
@click.option("--tag", "tags", multiple=True, help="Tag this memory. Repeat as needed.")
@click.option(
    "--editor",
    "from_editor",
    is_flag=True,
    help="Compose the memory in $EDITOR.",
)
@click.option(
    "--clipboard",
    "from_clipboard",
    is_flag=True,
    help="Capture memory text from the system clipboard.",
)
@click.pass_context
def remember_cmd(
    ctx: click.Context,
    text: tuple[str, ...],
    title: str | None,
    source_type: str,
    source_url: str,
    source_id: str,
    idempotency_key: str,
    tags: tuple[str, ...],
    from_editor: bool,
    from_clipboard: bool,
) -> None:
    """Save knowledge as a durable Markdown memory."""
    config: SaharaConfig = ctx.obj["config"]
    _require_library_config(config)

    if from_editor and from_clipboard:
        raise click.UsageError("Use either --editor or --clipboard, not both.")
    if from_clipboard and text:
        raise click.UsageError("--clipboard cannot be combined with text arguments.")

    content = " ".join(text)
    if from_clipboard:
        content = _read_clipboard_text()
    elif from_editor:
        edited = click.edit(content)
        if edited is None:
            raise click.UsageError("Editor closed without saving memory text.")
        content = edited
    if not content.strip() and not sys.stdin.isatty():
        from sahara.memory.format import MAX_MEMORY_CHARS

        content = click.get_text_stream("stdin").read(MAX_MEMORY_CHARS + 1)
        if len(content) > MAX_MEMORY_CHARS:
            raise click.UsageError(
                f"Memory text exceeds the {MAX_MEMORY_CHARS:,}-character limit."
            )
    if not content.strip():
        raise click.UsageError(
            "Provide memory text as an argument or pipe it through standard input."
        )

    from sahara.memory import CaptureRequest, MemoryService
    from sahara.storage.state_db import StateDB

    db = StateDB().connect()
    try:
        try:
            result = MemoryService(config, db).capture(
                CaptureRequest(
                    text=content,
                    title=title,
                    source_type=source_type,
                    source_url=source_url,
                    source_id=source_id,
                    tags=tags,
                    idempotency_key=idempotency_key,
                )
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

        if result.deduplicated:
            _ok(f"Memory already saved: {result.item.memory_id}")
        else:
            _ok(f"Saved memory {result.item.memory_id}")
        _info(f"Path: {result.item.path}")
        if result.deduplicated:
            _info("Duplicate capture was not written again.")
        elif result.indexed:
            _ok("Indexed for semantic retrieval.")
        else:
            _warn("Saved successfully; semantic indexing is pending.")
            if result.index_error:
                _info(f"Indexing detail: {result.index_error}")
    finally:
        db.close()


def _read_clipboard_text() -> str:
    """Read text from the platform clipboard using native command-line tools."""
    import shutil
    import subprocess

    commands: list[list[str]] = []
    if sys.platform == "darwin" and shutil.which("pbpaste"):
        commands.append(["pbpaste"])
    elif os.name == "nt" and shutil.which("powershell"):
        commands.append(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Clipboard -Raw",
            ]
        )
    else:
        if shutil.which("wl-paste"):
            commands.append(["wl-paste", "--no-newline"])
        if shutil.which("xclip"):
            commands.append(["xclip", "-selection", "clipboard", "-out"])
        if shutil.which("xsel"):
            commands.append(["xsel", "--clipboard", "--output"])

    for command in commands:
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
            return completed.stdout
        except (OSError, subprocess.CalledProcessError):
            continue
    raise click.ClickException(
        "Could not read the clipboard. Pipe text into `sahara remember` instead."
    )


# ---------------------------------------------------------------------------
# memory recall and lifecycle
# ---------------------------------------------------------------------------


def _memory_filters(
    sources: tuple[str, ...],
    tags: tuple[str, ...],
    since: str | None,
    until: str | None,
):
    from sahara.memory import MemoryFilters

    return MemoryFilters(
        source_types=sources,
        tags=tags,
        since=since,
        until=until,
    )


@main.command("recall")
@click.argument("query")
@click.option("--top", "-n", default=5, show_default=True)
@click.option(
    "--source",
    "sources",
    multiple=True,
    type=click.Choice(
        ["manual", "web", "conversation", "ai-chat", "mobile"],
        case_sensitive=False,
    ),
)
@click.option("--tag", "tags", multiple=True)
@click.option("--since", default=None, help="Only memories updated on or after this date.")
@click.option("--until", default=None, help="Only memories updated on or before this date.")
@click.pass_context
def recall_cmd(
    ctx: click.Context,
    query: str,
    top: int,
    sources: tuple[str, ...],
    tags: tuple[str, ...],
    since: str | None,
    until: str | None,
) -> None:
    """Recall captured knowledge using semantic search."""
    from sahara.memory import MemoryService
    from sahara.storage.state_db import StateDB

    config: SaharaConfig = ctx.obj["config"]
    _require_library_config(config)
    db = StateDB().connect()
    try:
        results: list[Any]
        try:
            results = MemoryService(config, db).search(
                query,
                _memory_filters(sources, tags, since, until),
                top_k=top,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        if not results:
            _info("No matching memories found.")
            return
        _section(f"Memories for: \"{query}\"")
        for index, result in enumerate(results, 1):
            score = int(result.score * 100)
            click.echo(
                f"  {index}. [{score}%] {result.item.title} "
                f"({result.item.memory_id})"
            )
            snippet = result.snippet.replace("\n", " ").strip()[:240]
            if snippet:
                click.echo(click.style(f"       {snippet}", fg="bright_black"))
            click.echo(f"       {result.item.relative_path}")
    finally:
        db.close()


@main.group("memory")
def memory_group() -> None:
    """List, inspect, edit, delete, and rebuild captured memories."""


@memory_group.command("list")
@click.option(
    "--source",
    "sources",
    multiple=True,
    type=click.Choice(
        ["manual", "web", "conversation", "ai-chat", "mobile"],
        case_sensitive=False,
    ),
)
@click.option("--tag", "tags", multiple=True)
@click.option("--since", default=None)
@click.option("--until", default=None)
@click.pass_context
def memory_list(
    ctx: click.Context,
    sources: tuple[str, ...],
    tags: tuple[str, ...],
    since: str | None,
    until: str | None,
) -> None:
    """List captured memories."""
    from sahara.memory import MemoryService
    from sahara.storage.state_db import StateDB

    config: SaharaConfig = ctx.obj["config"]
    _require_library_config(config)
    db = StateDB().connect()
    try:
        try:
            items = MemoryService(config, db).list(
                _memory_filters(sources, tags, since, until)
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        if not items:
            _info("No memories found.")
            return
        for item in items:
            tags_text = f" [{', '.join(item.tags)}]" if item.tags else ""
            click.echo(
                f"  {item.memory_id}  {item.updated_at[:10]}  "
                f"{item.title}{tags_text}"
            )
    finally:
        db.close()


@memory_group.command("show")
@click.argument("identifier")
@click.pass_context
def memory_show(ctx: click.Context, identifier: str) -> None:
    """Show one memory by UUID or exact title."""
    from sahara.memory import MemoryService
    from sahara.storage.state_db import StateDB

    config: SaharaConfig = ctx.obj["config"]
    _require_library_config(config)
    db = StateDB().connect()
    try:
        try:
            item = MemoryService(config, db).get(identifier)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(item.path.read_text(encoding="utf-8"), nl=False)
    finally:
        db.close()


@memory_group.command("edit")
@click.argument("identifier")
@click.pass_context
def memory_edit(ctx: click.Context, identifier: str) -> None:
    """Edit one memory in the configured editor."""
    from sahara.memory import MemoryService
    from sahara.storage.state_db import StateDB

    config: SaharaConfig = ctx.obj["config"]
    _require_library_config(config)
    db = StateDB().connect()
    try:
        service = MemoryService(config, db)
        try:
            item = service.get(identifier)
            edited = click.edit(
                item.path.read_text(encoding="utf-8"),
                extension=".md",
                require_save=True,
            )
            if edited is None:
                _info("Memory was not changed.")
                return
            result = service.edit(item.memory_id, edited)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        _ok(f"Updated memory {result.item.memory_id}")
        if not result.indexed:
            _warn("Saved successfully; semantic indexing is pending.")
    finally:
        db.close()


@memory_group.command("delete")
@click.argument("identifier")
@click.option("--force", is_flag=True, help="Delete without confirmation.")
@click.pass_context
def memory_delete(
    ctx: click.Context,
    identifier: str,
    force: bool,
) -> None:
    """Delete one memory by UUID or exact title."""
    from sahara.memory import MemoryService
    from sahara.storage.state_db import StateDB

    config: SaharaConfig = ctx.obj["config"]
    _require_library_config(config)
    db = StateDB().connect()
    try:
        service = MemoryService(config, db)
        try:
            item = service.get(identifier)
            if not force and not click.confirm(
                f"Delete memory '{item.title}'?",
                default=False,
            ):
                return
            service.delete(item.memory_id)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        _ok(f"Deleted memory {item.memory_id}")
    finally:
        db.close()


@memory_group.command("rebuild")
@click.pass_context
def memory_rebuild(ctx: click.Context) -> None:
    """Rebuild the memory catalog and semantic index from Markdown."""
    from sahara.memory import MemoryService
    from sahara.storage.state_db import StateDB

    config: SaharaConfig = ctx.obj["config"]
    _require_library_config(config)
    db = StateDB().connect()
    try:
        result = MemoryService(config, db).rebuild()
        _ok(f"Cataloged {result.cataloged} memory file(s).")
        _info(f"Indexed: {result.indexed}; pending: {result.pending}")
        if result.removed:
            _info(f"Removed stale search state: {result.removed}")
        for path, error in result.failed[:10]:
            _warn(f"{path}: {error}")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@main.command("search")
@click.argument("query")
@click.option("--top", "-n", default=5, show_default=True, help="Number of results to return.")
@click.option("--folder", "-f", default=None, help="Search only this folder (local path).")
@click.option("--snippet", is_flag=True, help="Show matching text snippet.")
@click.pass_context
def search_cmd(
    ctx: click.Context, query: str, top: int, folder: str | None, snippet: bool
) -> None:
    """Search files by content using natural language."""
    config: SaharaConfig = ctx.obj["config"]
    _require_library_config(config)

    from sahara.search.search_engine import SearchEngine
    from sahara.storage.state_db import StateDB

    db = StateDB().connect()
    engine = SearchEngine(db)

    try:
        # Resolve optional folder filter to s3_prefix
        s3_prefix_filter: str | None = None
        if folder:
            s3_prefix_filter = _resolve_content_prefix(config, db, folder)

        total = db.count_embeddings(s3_prefix=s3_prefix_filter)
        if total == 0:
            _warn("No files indexed yet. Run `sahara index` first.")
            return

        _info(f"Searching {total} indexed file(s)…")
        results = engine.search(query, top_k=top, storage_prefix=s3_prefix_filter)

        if not results:
            _info("No results found.")
            return

        _section(f"Results for: \"{query}\"")
        for i, r in enumerate(results, 1):
            prefix = r.get("storage_prefix", r.get("s3_prefix", ""))
            display = f"{prefix}/{r['relative_path']}" if prefix else r["relative_path"]
            score_pct = int(r["score"] * 100)
            score_color = "green" if score_pct >= 70 else "yellow" if score_pct >= 50 else "white"
            score_str = click.style(f"{score_pct}%", fg=score_color)
            residency = (
                " [offloaded]" if r.get("local_state") == "offloaded" else ""
            )
            click.echo(f"  {i}. [{score_str}] {display}{residency}")
            if snippet and r["snippet"]:
                # Print first 150 chars of snippet, indented
                snip = r["snippet"].replace("\n", " ")[:150].strip()
                click.echo(click.style(f"       {snip}…", fg="bright_black"))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------


@main.command("ask")
@click.argument("question", nargs=-1, required=True)
@click.option("--top", "-n", default=5, show_default=True, help="Number of chunks to retrieve.")
@click.option("--folder", "-f", default=None, help="Limit search to this folder.")
@click.option(
    "--model", "-m", default=None,
    help="LLM model name (e.g. gpt-4o-mini for OpenAI, mistral for Ollama).",
)
@click.option(
    "--provider", default=None,
    type=click.Choice(["none", "openai", "ollama"], case_sensitive=False),
    help="Answer provider for this question; omit to use the saved setting.",
)
@click.option(
    "--ollama-url", default=None,
    help="Ollama base URL (default: http://localhost:11434).",
)
@click.option("--snippet", is_flag=True, help="Always show source snippets even when an answer is produced.")
@click.pass_context
def ask_cmd(
    ctx: click.Context,
    question: tuple,
    top: int,
    folder: str | None,
    model: str | None,
    provider: str | None,
    ollama_url: str | None,
    snippet: bool,
) -> None:
    """Answer a natural language question about your files.

    Standalone answer generation is disabled by default, so this command returns
    ranked source snippets without contacting an LLM. Enable Ollama or OpenAI
    with `sahara config set answer_provider PROVIDER`, or use --provider once.

    The legacy 'local' prefix also selects Ollama:

        sahara ask "what is my passport expiry date?"

        sahara ask local "what is my passport expiry date?"
    """
    import os as _os

    # 'local' as the first word forces Ollama provider
    if question and question[0].lower() == "local":
        if provider is None:
            provider = "ollama"
        question = question[1:]
    if not question:
        _abort("Please provide a question.")
    question_str = " ".join(question)

    config: SaharaConfig = ctx.obj["config"]
    _require_library_config(config)

    from sahara.search.ask_engine import AskEngine
    from sahara.search.search_engine import SearchEngine
    from sahara.storage.state_db import StateDB

    db = StateDB().connect()
    search_engine = SearchEngine(db)
    selected_provider = provider or config.answer_provider
    selected_model = model
    if (
        selected_model is None
        and selected_provider == config.answer_provider
        and config.answer_model
    ):
        selected_model = config.answer_model
    if selected_model is None and selected_provider == "openai":
        selected_model = _os.environ.get("OPENAI_MODEL")
    ask_engine = AskEngine(
        search_engine,
        ollama_url=ollama_url or _os.environ.get("OLLAMA_URL", "http://localhost:11434"),
        model=selected_model,
        provider=selected_provider,
        openai_api_key=_os.environ.get("OPENAI_API_KEY"),
    )

    try:
        # Resolve folder filter
        storage_prefix_filter: str | None = None
        if folder:
            storage_prefix_filter = _resolve_content_prefix(config, db, folder)

        total = db.count_embeddings(s3_prefix=storage_prefix_filter)
        if total == 0:
            _warn("No files indexed yet. Run `sahara index` first.")
            return

        _info(f"Searching {total} indexed file(s)…")
        result = ask_engine.ask(question_str, top_k=top, storage_prefix=storage_prefix_filter)

        click.echo()
        if result.answer:
            click.echo(click.style("Answer:", fg="cyan", bold=True) + " " + result.answer)
            if result.model_used:
                provider_label = (
                    f"OpenAI ({result.model_used})"
                    if result.provider_used == "openai"
                    else f"Ollama ({result.model_used})"
                )
                click.echo(
                    click.style(
                        f"\n  Note: Answer generated by {provider_label}.",
                        fg="bright_black",
                    )
                )
        else:
            if result.error:
                _warn(result.error)
            elif selected_provider == "none":
                _info(
                    "Standalone answer generation is off. Enable Ollama or OpenAI "
                    "with `sahara config set answer_provider PROVIDER`."
                )
            _info("Showing top matching results:")

        if result.sources and (snippet or not result.answer):
            click.echo()
            _section("Sources")
            for i, r in enumerate(result.sources, 1):
                prefix = r.get("storage_prefix", r.get("s3_prefix", ""))
                display = f"{prefix}/{r['relative_path']}" if prefix else r["relative_path"]
                score_pct = int(r.get("score", 0) * 100)
                score_color = "green" if score_pct >= 70 else "yellow" if score_pct >= 50 else "white"
                score_str = click.style(f"{score_pct}%", fg=score_color)
                residency = (
                    " [offloaded]"
                    if r.get("local_state") == "offloaded"
                    else ""
                )
                click.echo(f"  {i}. [{score_str}] {display}{residency}")
                snip = r.get("snippet", "")
                if snip:
                    snip_display = snip.replace("\n", " ")[:200].strip()
                    click.echo(click.style(f'       "{snip_display}..."', fg="bright_black"))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# offload / fetch
# ---------------------------------------------------------------------------


@main.command("offload")
@click.argument("path")
@click.pass_context
def offload_cmd(ctx: click.Context, path: str) -> None:
    """Verify a stored copy, then remove the local source file."""
    from sahara.storage.lifecycle import StorageLifecycle
    from sahara.storage.state_db import StateDB

    config: SaharaConfig = ctx.obj["config"]
    _require_storage_config(config)
    db = StateDB().connect()
    try:
        lifecycle = StorageLifecycle(config, db, _create_backend(config))
        try:
            item = lifecycle.offload(path)
        except (OSError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        _ok(f"Offloaded: {item.local_path}")
        _info("Search metadata was retained. Use `sahara fetch` to restore it.")
    finally:
        db.close()


@main.command("fetch")
@click.argument("path")
@click.pass_context
def fetch_cmd(ctx: click.Context, path: str) -> None:
    """Restore an offloaded file and verify its checksum."""
    from sahara.storage.lifecycle import StorageLifecycle
    from sahara.storage.state_db import StateDB

    config: SaharaConfig = ctx.obj["config"]
    _require_storage_config(config)
    db = StateDB().connect()
    try:
        lifecycle = StorageLifecycle(config, db, _create_backend(config))
        try:
            item = lifecycle.fetch(path)
        except (OSError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        _ok(f"Fetched: {item.local_path}")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# mobile API
# ---------------------------------------------------------------------------


@main.group()
def mobile() -> None:
    """Manage the private mobile capture API."""


@mobile.command("pair")
@click.argument("name")
@click.option(
    "--scope",
    "scopes",
    multiple=True,
    default=("memory:capture",),
    help="Device scope. Repeat for memory:recall.",
)
@click.option(
    "--endpoint",
    default="http://127.0.0.1:8765",
    show_default=True,
    help="Endpoint mobile clients should call.",
)
@click.option("--json", "as_json", is_flag=True, help="Print raw pairing JSON.")
@click.pass_context
def mobile_pair(
    ctx: click.Context,
    name: str,
    scopes: tuple[str, ...],
    endpoint: str,
    as_json: bool,
) -> None:
    """Create a named, revocable mobile device token."""
    import json

    from sahara.mobile_api import create_mobile_device_pairing, pairing_uri
    from sahara.storage.state_db import StateDB

    _require_library_config(ctx.obj["config"])
    db = StateDB().connect()
    try:
        try:
            pairing = create_mobile_device_pairing(
                db,
                name=name,
                endpoint=endpoint,
                scopes=scopes,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        payload = pairing.payload()
        if as_json:
            click.echo(json.dumps(payload, indent=2))
            return
        _ok(f"Paired device: {pairing.name}")
        _info(f"Device id : {pairing.device_id}")
        _info(f"Endpoint  : {pairing.endpoint}")
        _info(f"Scopes    : {', '.join(pairing.scopes)}")
        _info(f"Token     : {pairing.token}")
        _info(f"Pair URI  : {pairing_uri(payload)}")
        _warn("The token is shown once. Store it only on the paired device.")
    finally:
        db.close()


@mobile.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True, type=int)
@click.option(
    "--allow-private-network",
    is_flag=True,
    help="Allow binding to a trusted private/VPN address. Public/wildcard binds are refused.",
)
@click.pass_context
def mobile_serve(
    ctx: click.Context,
    host: str,
    port: int,
    allow_private_network: bool,
) -> None:
    """Run the authenticated mobile capture API."""
    from sahara.mobile_api import serve_mobile_api, validate_bind_host

    _require_library_config(ctx.obj["config"])
    try:
        bind_host = validate_bind_host(
            host,
            allow_private_network=allow_private_network,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _ok(f"Mobile API listening on http://{bind_host}:{port}")
    _info("Press Ctrl+C to stop.")
    try:
        serve_mobile_api(
            config_path=ctx.obj.get("config_path"),
            host=bind_host,
            port=port,
            allow_private_network=allow_private_network,
        )
    except KeyboardInterrupt:
        click.echo()
        _ok("Mobile API stopped.")


@mobile.command("devices")
@click.option("--include-revoked", is_flag=True, help="Show revoked devices too.")
def mobile_devices(include_revoked: bool) -> None:
    """List paired mobile devices without token material."""
    from sahara.storage.state_db import StateDB

    db = StateDB().connect()
    try:
        rows = db.list_mobile_devices(include_revoked=include_revoked)
    finally:
        db.close()
    if not rows:
        _info("No paired mobile devices.")
        return
    _section("Mobile Devices")
    for row in rows:
        revoked = " revoked" if row["revoked_at"] else ""
        _info(f"{row['name']} ({row['device_id']}){revoked}")
        _info(f"  scopes: {', '.join(row['scopes'])}")
        if row["last_used_at"]:
            _info(f"  last used: {row['last_used_at']}")


@mobile.command("revoke")
@click.argument("identifier")
def mobile_revoke(identifier: str) -> None:
    """Revoke a mobile device by name or id."""
    from sahara.storage.state_db import StateDB

    db = StateDB().connect()
    try:
        revoked = db.revoke_mobile_device(identifier)
    finally:
        db.close()
    if revoked:
        _ok(f"Revoked mobile device: {identifier}")
    else:
        raise click.ClickException(f"Mobile device not found: {identifier}")


@mobile.command("audit")
@click.option("--limit", default=20, show_default=True)
def mobile_audit(limit: int) -> None:
    """Show recent metadata-only mobile API audit events."""
    from sahara.storage.state_db import StateDB

    db = StateDB().connect()
    try:
        rows = db.list_mobile_memory_audit(limit=limit)
    finally:
        db.close()
    if not rows:
        _info("No mobile API audit events.")
        return
    _section("Mobile API Audit")
    for row in rows:
        device = row["device_name"] or "unknown"
        _info(
            f"{row['requested_at']} {row['outcome']} "
            f"{device} {row['scope']} {row['details'] or ''}".rstrip()
        )


@mobile.group("shortcuts")
def mobile_shortcuts() -> None:
    """Inspect and export Apple Shortcuts artifacts."""


@mobile_shortcuts.command("list")
def mobile_shortcuts_list() -> None:
    """List packaged Apple Shortcuts artifacts."""
    from sahara.shortcuts import load_shortcut_artifacts

    _section("Apple Shortcuts")
    for artifact in load_shortcut_artifacts():
        _info(f"{artifact.name} {artifact.version}")
        _info(f"  file : {artifact.filename}")
        _info(f"  scope: {artifact.required_scope}")


@mobile_shortcuts.command("export")
@click.argument("destination", type=click.Path(path_type=Path))
def mobile_shortcuts_export(destination: Path) -> None:
    """Export versioned Apple Shortcuts blueprints to a folder."""
    from sahara.shortcuts import copy_shortcut_artifacts

    written = copy_shortcut_artifacts(destination)
    _ok(f"Exported {len(written)} Shortcut artifact(s).")
    for path in written:
        _info(str(path))


# ---------------------------------------------------------------------------
# daemon group
# ---------------------------------------------------------------------------


@main.group()
def daemon() -> None:
    """Manage the Sahara background daemon."""


@daemon.command("start")
@click.option("--autostart", is_flag=True, help="Install autostart entry for this platform.")
@click.pass_context
def daemon_start(ctx: click.Context, autostart: bool) -> None:
    """Start the background sync daemon."""
    from sahara.sync.daemon import install_autostart, is_daemon_running, start_daemon

    if is_daemon_running():
        _warn("Daemon is already running.")
        return

    config_path: Path | None = ctx.obj.get("config_path")
    try:
        start_daemon(config_path)
        _ok("Daemon started.")
    except Exception as exc:
        _abort(f"Failed to start daemon: {exc}")

    if autostart:
        try:
            path = install_autostart()
            _ok(f"Autostart installed: {path}")
        except Exception as exc:
            _warn(f"Autostart installation failed: {exc}")


@daemon.command("stop")
def daemon_stop() -> None:
    """Stop the background sync daemon."""
    from sahara.sync.daemon import stop_daemon

    try:
        stop_daemon()
        _ok("Daemon stopped.")
    except Exception as exc:
        _abort(f"Failed to stop daemon: {exc}")


@daemon.command("status")
def daemon_status() -> None:
    """Show daemon status."""
    from sahara.sync.daemon import get_daemon_status

    status = get_daemon_status()
    _section("Daemon Status")
    if status["running"]:
        _ok(f"Running (PID {status['pid']})")
    else:
        click.echo(click.style("  Stopped", fg="red"))

    if status["paused"]:
        _warn("Daemon is paused.")

    _info(f"PID file : {status['pid_file']}")
    _info(f"Log file : {status['log_file']}")


@daemon.command("pause")
def daemon_pause() -> None:
    """Pause the daemon (stop syncing without killing the process)."""
    from sahara.sync.daemon import pause_daemon

    pause_daemon()
    _ok("Daemon paused. Run `sahara daemon resume` to continue.")


@daemon.command("resume")
def daemon_resume() -> None:
    """Resume a paused daemon."""
    from sahara.sync.daemon import resume_daemon

    resume_daemon()
    _ok("Daemon resumed.")


@daemon.command("logs")
@click.option("--lines", "-n", default=50, show_default=True)
@click.option("--follow", "-f", is_flag=True, help="Follow log output (like tail -f).")
def daemon_logs(lines: int, follow: bool) -> None:
    """Show daemon log output."""
    from sahara.sync.daemon import _LOG_FILE

    if not _LOG_FILE.exists():
        _info("No daemon log file found.")
        return

    if follow:
        import subprocess

        try:
            subprocess.run(["tail", "-f", "-n", str(lines), str(_LOG_FILE)])
        except KeyboardInterrupt:
            pass
    else:
        log_lines = _LOG_FILE.read_text().splitlines()
        for line in log_lines[-lines:]:
            click.echo(line)
