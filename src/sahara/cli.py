"""Sahara CLI — complete Click command tree."""

from __future__ import annotations

import datetime
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import click

from sahara import __version__
from sahara.config import (
    DEFAULT_CONFIG_PATH,
    SaharaConfig,
    load_config,
    save_config,
)

__all__ = ["main"]

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


def _abort(msg: str) -> None:
    _err(msg)
    sys.exit(1)


def _load_cfg(config_path: Optional[Path]) -> SaharaConfig:
    return load_config(config_path or DEFAULT_CONFIG_PATH)


def _create_backend(config: SaharaConfig):
    """Instantiate the appropriate StorageBackend for config.storage_mode."""
    from sahara.storage.s3_client import S3Client
    from sahara.storage.local_drive_client import LocalDriveClient
    from sahara.storage.dual_write_backend import DualWriteBackend

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
    sync_folder: Optional[Path] = None,
    s3_prefix: str = "",
):
    from sahara.storage.state_db import StateDB
    from sahara.sync.sync_engine import SyncEngine
    from sahara.sync.ignore_rules import IgnoreRules

    folder = sync_folder or config.get_sync_folder_path()
    db = StateDB().connect()
    backend = _create_backend(config)
    ignore = IgnoreRules(folder, extra_patterns=config.exclude_patterns)
    return SyncEngine(config, db, backend, ignore, sync_folder=folder, s3_prefix=s3_prefix), db, backend


def _require_config(config: SaharaConfig) -> None:
    if not config.bucket or not config.sync_folder:
        _abort(
            "Sahara is not initialised. Run `sahara init` to set up."
        )


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
def main(ctx: click.Context, config_path: Optional[Path]) -> None:
    """Sahara — personal cloud storage backed by AWS S3."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["config"] = _load_cfg(config_path)

    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@main.command()
@click.pass_context
def init(ctx: click.Context) -> None:
    """Interactive setup wizard — configure bucket, folder, encryption, etc."""
    _section("Sahara Setup Wizard")
    click.echo("  This wizard will configure Sahara for first use.\n")

    config = SaharaConfig()

    # Sync folder
    default_folder = str(Path.home() / "Sahara")
    sync_folder = click.prompt("  Sync folder", default=default_folder)
    config.sync_folder = str(Path(sync_folder).expanduser())
    Path(config.sync_folder).mkdir(parents=True, exist_ok=True)

    # Storage backend
    click.echo()
    backend_choice = click.prompt(
        "  Storage backend",
        type=click.Choice(["aws", "minio", "local", "local+glacier"], case_sensitive=False),
        default="aws",
        show_default=True,
        prompt_suffix="\n"
        "    aws           — Amazon S3 with Glacier tiering (pay-per-use cloud)\n"
        "    minio         — Self-hosted MinIO / S3-compatible server\n"
        "    local         — Locally mounted hard drives (no cloud)\n"
        "    local+glacier — Drives as primary + S3 Glacier as cold backup\n"
        "  Choice",
    )

    is_local = backend_choice in ("local", "local+glacier")
    is_minio = backend_choice == "minio"
    config.storage_mode = "s3" if backend_choice in ("aws", "minio") else backend_choice

    # --- Local drive path(s) ---
    if is_local:
        _info(
            "Enter the absolute path(s) to your mounted drives. "
            "Files will be written to ALL drives independently."
        )
        drive_paths: list[str] = []
        while True:
            default_drive = "" if drive_paths else "/Volumes/Drive1/Sahara"
            prompt_text = (
                "  Drive path (press Enter to finish)"
                if drive_paths
                else f"  Drive path 1"
            )
            dp = click.prompt(prompt_text, default=default_drive if not drive_paths else "")
            if not dp.strip():
                if not drive_paths:
                    _warn("At least one drive path is required.")
                    continue
                break
            drive_paths.append(str(Path(dp.strip()).expanduser()))
        config.drive_paths = drive_paths
        # Drives are append-only by default — deletions from sync folder do NOT
        # propagate to drives, keeping them as a complete historical copy.
        config.delete_remote_on_local_delete = False
        _info(
            "Drives set to append-only mode. "
            "Deleting a file from your sync folder will NOT remove it from drives. "
            "(Change delete_remote_on_local_delete in config to override.)"
        )

    # --- MinIO endpoint ---
    if is_minio:
        _info("MinIO mode: files will be stored on your self-hosted server.")
        endpoint_url = click.prompt("  MinIO endpoint URL (e.g. http://100.x.x.1:9000)")
        config.endpoint_url = endpoint_url.strip().rstrip("/")
        config.default_storage_class = "STANDARD"

    # --- S3 / MinIO bucket ---
    if not is_local or backend_choice == "local+glacier":
        bucket_prompt = (
            "  Glacier backup bucket name"
            if backend_choice == "local+glacier"
            else ("  Bucket name" if is_minio else "  S3 bucket name")
        )
        bucket = click.prompt(bucket_prompt, default="sahara" if is_minio else "")
        config.bucket = bucket.strip()

        if not is_minio and backend_choice != "local+glacier":
            region = click.prompt("  AWS region", default="us-east-1")
            config.region = region.strip()
        elif backend_choice == "local+glacier":
            region = click.prompt("  AWS region for Glacier bucket", default="us-east-1")
            config.region = region.strip()

        prefix = click.prompt("  Key prefix (leave blank for root)", default="")
        config.prefix = prefix.strip()

    # --- Credentials ---
    click.echo()
    if is_minio:
        access_key = click.prompt("  MinIO access key (root user)")
        secret_key = click.prompt("  MinIO secret key (root password)", hide_input=True)
        config.aws_access_key_id = access_key.strip()
        config.aws_secret_access_key = secret_key.strip()
    elif not is_local or backend_choice == "local+glacier":
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
            profile = click.prompt("  AWS profile name")
            config.aws_profile = profile.strip()
        elif cred_method == "keys":
            access_key = click.prompt("  AWS access key ID")
            secret_key = click.prompt("  AWS secret access key", hide_input=True)
            config.aws_access_key_id = access_key.strip()
            config.aws_secret_access_key = secret_key.strip()
            _warn("Access keys saved to config file. Using env vars or a profile is more secure.")
        else:
            _info("Make sure AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are set before running sahara.")

    # --- local+glacier: Glacier keep-deleted ---
    if backend_choice == "local+glacier":
        click.echo()
        keep = click.confirm(
            "  Keep Glacier copies when files are deleted locally? (recommended)",
            default=True,
        )
        config.glacier_keep_deleted = keep
        if keep:
            _info("Glacier archive is immutable — local deletions will NOT remove Glacier copies.")

    # --- Encryption ---
    encrypt = click.confirm("\n  Enable client-side encryption (AES-256-GCM)?", default=False)
    config.encryption_enabled = encrypt
    if encrypt:
        passphrase = click.prompt(
            "  Encryption passphrase", hide_input=True, confirmation_prompt=True
        )
        from sahara.utils.encryption import set_passphrase
        set_passphrase(passphrase)
        _ok("Passphrase stored in system keyring.")

    # --- Conflict strategy ---
    strategy = click.prompt("  Conflict strategy [backup/local/remote]", default="backup")
    config.conflict_strategy = strategy.strip()

    # --- Upload-only ---
    click.echo()
    upload_only = click.confirm(
        "  Upload-only mode? (this machine only pushes files,\n"
        "  never pulls files uploaded by other machines)",
        default=False,
    )
    config.upload_only = upload_only
    if upload_only:
        _info("Upload-only enabled — this machine will only back up its own files.")

    config_path = ctx.obj.get("config_path") or DEFAULT_CONFIG_PATH
    save_config(config, config_path)
    _ok(f"Configuration saved to {config_path}")

    # --- Validate storage access ---
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
            _info("No existing manifest found. A new one will be created on first sync.")
        else:
            _ok(f"Manifest found with {len(manifest)} file(s).")
    except Exception as exc:
        _warn(f"Storage validation failed: {exc}")
        _warn("You can re-run `sahara doctor` after fixing the issue.")

    # Create .saharaignore if absent
    ignore_path = Path(config.sync_folder) / ".saharaignore"
    if not ignore_path.exists():
        template = Path(__file__).parent.parent.parent / ".saharaignore.template"
        if template.exists():
            import shutil

            shutil.copy(str(template), str(ignore_path))
            _ok("Created .saharaignore from template.")
        else:
            ignore_path.write_text("# Sahara ignore rules (gitignore syntax)\n")
            _ok("Created empty .saharaignore.")

    click.echo()
    _ok("Sahara initialised! Run `sahara sync` to start.")


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
    if config.is_local_drive_mode:
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
    from sahara.storage.state_db import StateDB, DB_PATH

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
        _info("State DB not yet initialised (will be created on first sync).")

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

    from sahara.utils.encryption import get_passphrase, set_passphrase, derive_key
    from sahara.utils.encryption import generate_salt, encrypt_file, decrypt_file

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
    from sahara.storage.state_db import StateDB
    from sahara.storage.s3_client import S3Client
    import tempfile

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
                    from sahara.utils.encryption import _HEADER_LEN, _MAGIC, _SALT_LEN

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
# Multi-folder management: add / remove / folders
# ---------------------------------------------------------------------------


@main.command("add")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
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
def add_folder(ctx: click.Context, path: Path, name: Optional[str], dest: Optional[str]) -> None:
    """Register an additional folder for sync."""
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
        # Check against primary folder
        if resolved == config.get_sync_folder_path():
            _abort("That is the primary sync folder — it is always synced.")

        existing = db.list_sync_targets()
        for t in existing:
            if Path(t["local_path"]) == resolved:
                _abort(f"Folder already registered: {resolved}")
            if t["s3_prefix"] == s3_prefix:
                _abort(
                    f"S3 prefix '{s3_prefix}' is already used by {t['local_path']}. "
                    "Use --as <name> to choose a different name."
                )

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

        file_count = len(db.list_files(s3_prefix=target["s3_prefix"]))
        if file_count > 0:
            _warn(
                f"{file_count} file(s) tracked for this folder remain in S3 "
                f"under prefix '{target['s3_prefix']}/'."
            )
            _warn("Removing this registration does NOT delete them from S3.")
            if not force and not click.confirm("  Continue?", default=False):
                return

        db.remove_sync_target(str(resolved))
        _ok(f"Unregistered: {resolved}")
    finally:
        db.close()


@main.command("folders")
@click.pass_context
def folders_cmd(ctx: click.Context) -> None:
    """List all folders registered for sync."""
    from sahara.storage.state_db import StateDB

    config: SaharaConfig = ctx.obj["config"]
    _require_config(config)

    _section("Sync Folders")
    primary = config.get_sync_folder_path()
    exists_mark = "" if primary.exists() else " (missing)"
    click.echo(
        click.style(f"  * {primary}", fg="green", bold=True)
        + f"  →  s3://{config.bucket}/{config.prefix or '(root)'}  [primary]{exists_mark}"
    )

    db = StateDB().connect()
    try:
        additional = db.list_sync_targets()
        if not additional:
            _info("No additional folders registered.")
            _info("Use `sahara add <path>` to register one.")
        else:
            for t in additional:
                p = Path(t["local_path"])
                exists_mark = "" if p.exists() else " (missing)"
                s3_url = f"s3://{config.bucket}/{t['s3_prefix']}/"
                click.echo(f"  + {p}  →  {s3_url}{exists_mark}")
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
    folder: Optional[str] = None,
) -> None:
    from sahara.storage.state_db import StateDB
    from sahara.models import SyncResult

    config: SaharaConfig = ctx.obj["config"]
    _require_config(config)

    if dry_run:
        click.echo(click.style("  [DRY RUN — no changes will be made]", fg="yellow"))

    # Build target list: primary folder + all registered additional targets
    db_main = StateDB().connect()
    try:
        all_targets: list[tuple[Path, str]] = [(config.get_sync_folder_path(), "")]
        for row in db_main.list_sync_targets():
            all_targets.append((Path(row["local_path"]), row["s3_prefix"]))
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
    for folder, prefix in targets:
        label = f"  Syncing {folder}" + (f" (→ {prefix}/)" if prefix else "")
        click.echo(click.style(label, fg="cyan"))
        engine, db, s3 = _build_engine(config, sync_folder=folder, s3_prefix=prefix)
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
def sync(ctx: click.Context, dry_run: bool, verify: bool, wait: bool, folder: Optional[str]) -> None:
    """Sync local folder(s) with S3 (bidirectional)."""
    _run_sync(ctx, dry_run=dry_run, verify=verify, wait=wait, folder=folder)


@main.command()
@click.option("--dry-run", is_flag=True)
@click.option("--verify", is_flag=True)
@click.option("--folder", "-f", default=None, help="Push only this folder (local path).")
@click.pass_context
def push(ctx: click.Context, dry_run: bool, verify: bool, folder: Optional[str]) -> None:
    """Push local changes to S3 (upload only)."""
    _run_sync(ctx, push_only=True, dry_run=dry_run, verify=verify, folder=folder)


@main.command()
@click.option("--dry-run", is_flag=True)
@click.option("--wait", is_flag=True)
@click.option("--folder", "-f", default=None, help="Pull only this folder (local path).")
@click.pass_context
def pull(ctx: click.Context, dry_run: bool, wait: bool, folder: Optional[str]) -> None:
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
def resolve(ctx: click.Context, path: Optional[str], keep: str) -> None:
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
    ctx: click.Context, prefix: str, tier: Optional[str], long: bool, show_all: bool
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

        all_rows: list[tuple[str, object]] = []  # (display_path, FileRecord)
        for display_prefix, s3_pref in s3_prefixes:
            if tier:
                files = db.list_files_by_tier(tier, s3_prefix=s3_pref)  # type: ignore[arg-type]
            else:
                files = db.list_files(s3_prefix=s3_pref)

            for f in files:
                display_path = display_prefix + f.relative_path
                if not prefix or display_path.startswith(prefix):
                    all_rows.append((display_path, f))

        if not all_rows:
            _info("No files found.")
            return

        if long:
            _section(f"Files ({len(all_rows)})")
            click.echo(
                f"  {'Path':<50} {'Size':>10} {'Tier':<15} {'SHA256':>12}  {'Modified'}"
            )
            click.echo("  " + "─" * 110)
            for display_path, f in sorted(all_rows, key=lambda x: x[0]):
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
                click.echo(
                    f"  {display_path:<50} {size_str:>10} {tier_str:<24}  {sha_short}  {mtime}"
                )
        else:
            for display_path, _ in sorted(all_rows, key=lambda x: x[0]):
                click.echo(f"  {display_path}")
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

    from sahara.storage.state_db import StateDB
    from sahara.storage.s3_client import S3Client

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

    from sahara.storage.state_db import StateDB
    from sahara.storage.s3_client import S3Client
    import shutil

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
            s3.copy_object(src_key, dst_key)
            s3.delete_object(src_key)
            _ok(f"Moved in S3: {src} → {dst}")
        except Exception as exc:
            _warn(f"S3 move failed (local move succeeded): {exc}")

        rec = db.get_file(src)
        if rec:
            db.delete_file(src)
            import datetime

            now = datetime.datetime.now(datetime.timezone.utc)
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
    older_than: Optional[int],
    dry_run: bool,
    force: bool,
    storage_class: str,
    folder: Optional[str],
) -> None:
    """Archive files to Glacier / Deep Archive."""
    config: SaharaConfig = ctx.obj["config"]
    _require_config(config)
    _require_s3_tiers(config, "archive")

    from sahara.storage.state_db import StateDB
    from pathlib import Path as _Path

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
            cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
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
def restore_status_cmd(ctx: click.Context, path: Optional[str]) -> None:
    """Check the status of a Glacier restore."""
    config: SaharaConfig = ctx.obj["config"]
    _require_config(config)
    _require_s3_tiers(config, "restore-status")

    from sahara.storage.state_db import StateDB
    from sahara.storage.s3_client import S3Client

    db = StateDB()
    db.connect()
    s3 = S3Client(config)
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
    standard_gb: Optional[float],
    glacier_gb: Optional[float],
    deep_archive_gb: Optional[float],
    monthly_puts: int,
    monthly_gets: int,
    monthly_egress_gb: float,
) -> None:
    """Show storage usage and cost estimates."""
    config: SaharaConfig = ctx.obj["config"]

    from sahara.storage.cost_estimator import CostEstimator
    from sahara.storage.state_db import StateDB
    from sahara.storage.s3_client import S3Client

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
def history(ctx: click.Context, path: Optional[str], limit: int) -> None:
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
def index_cmd(ctx: click.Context, folder: Optional[str], force: bool) -> None:
    """Index file contents for semantic search."""
    config: SaharaConfig = ctx.obj["config"]
    _require_config(config)

    from sahara.storage.state_db import StateDB
    from sahara.search.search_engine import SearchEngine
    from pathlib import Path as _Path

    db = StateDB().connect()
    engine = SearchEngine(db)

    try:
        # Build list of (local_folder, s3_prefix) to index
        all_targets: list[tuple[_Path, str]] = [(config.get_sync_folder_path(), "")]
        for row in db.list_sync_targets():
            all_targets.append((_Path(row["local_path"]), row["s3_prefix"]))

        if folder:
            resolved = str(_Path(folder).expanduser().resolve())
            targets = [(f, p) for f, p in all_targets if str(f) == resolved]
            if not targets:
                _abort(f"'{folder}' is not a registered sync folder.")
        else:
            targets = all_targets

        total_indexed = 0
        total_skipped = 0
        total_failed = 0

        for sync_folder, s3_prefix in targets:
            label = s3_prefix or "(primary)"
            files = db.list_files(s3_prefix=s3_prefix)
            if not files:
                continue

            _section(f"Indexing {label} — {len(files)} file(s)")

            for i, record in enumerate(files, 1):
                file_path = sync_folder / record.relative_path
                click.echo(
                    f"  [{i:>4}/{len(files)}] {record.relative_path[:60]}",
                    nl=False,
                )
                if not file_path.exists():
                    click.echo(click.style("  [missing]", fg="yellow"))
                    total_skipped += 1
                    continue
                try:
                    reindexed = engine.index_file(
                        file_path, s3_prefix, record.relative_path, force=force
                    )
                    if reindexed:
                        click.echo(click.style("  ✓", fg="green"))
                        total_indexed += 1
                    else:
                        click.echo(click.style("  –", fg="white"))  # unchanged
                        total_skipped += 1
                except Exception as exc:
                    click.echo(click.style(f"  ✗ {exc}", fg="red"))
                    total_failed += 1

        db.conn.commit()
        click.echo()
        _ok(
            f"Done — {total_indexed} indexed, {total_skipped} unchanged, {total_failed} failed."
        )
        _info(f"Total in index: {db.count_embeddings()} file(s).")
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
    ctx: click.Context, query: str, top: int, folder: Optional[str], snippet: bool
) -> None:
    """Search files by content using natural language."""
    config: SaharaConfig = ctx.obj["config"]
    _require_config(config)

    from sahara.storage.state_db import StateDB
    from sahara.search.search_engine import SearchEngine
    from pathlib import Path as _Path

    db = StateDB().connect()
    engine = SearchEngine(db)

    try:
        # Resolve optional folder filter to s3_prefix
        s3_prefix_filter: Optional[str] = None
        if folder:
            resolved = str(_Path(folder).expanduser().resolve())
            all_targets: list[tuple[_Path, str]] = [(config.get_sync_folder_path(), "")]
            for row in db.list_sync_targets():
                all_targets.append((_Path(row["local_path"]), row["s3_prefix"]))
            match = [(f, p) for f, p in all_targets if str(f) == resolved]
            if not match:
                _abort(f"'{folder}' is not a registered sync folder.")
            s3_prefix_filter = match[0][1]

        total = db.count_embeddings(s3_prefix=s3_prefix_filter)
        if total == 0:
            _warn("No files indexed yet. Run `sahara index` first.")
            return

        _info(f"Searching {total} indexed file(s)…")
        results = engine.search(query, top_k=top, s3_prefix=s3_prefix_filter)

        if not results:
            _info("No results found.")
            return

        _section(f"Results for: \"{query}\"")
        for i, r in enumerate(results, 1):
            prefix = r["s3_prefix"]
            display = f"{prefix}/{r['relative_path']}" if prefix else r["relative_path"]
            score_pct = int(r["score"] * 100)
            score_color = "green" if score_pct >= 70 else "yellow" if score_pct >= 50 else "white"
            score_str = click.style(f"{score_pct}%", fg=score_color)
            click.echo(f"  {i}. [{score_str}] {display}")
            if snippet and r["snippet"]:
                # Print first 150 chars of snippet, indented
                snip = r["snippet"].replace("\n", " ")[:150].strip()
                click.echo(click.style(f"       {snip}…", fg="bright_black"))
    finally:
        db.close()


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
    from sahara.sync.daemon import start_daemon, is_daemon_running, install_autostart

    if is_daemon_running():
        _warn("Daemon is already running.")
        return

    config_path: Optional[Path] = ctx.obj.get("config_path")
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
