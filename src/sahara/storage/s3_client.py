"""boto3 S3 wrapper for Sahara with multipart upload, manifest management, and retry."""

from __future__ import annotations

import json
import logging
import random
import shutil
import time
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar

import boto3
import botocore.exceptions
from botocore.config import Config as BotoConfig

from sahara.config import SaharaConfig
from sahara.utils.hash import compute_sha256 as _compute_sha256

__all__ = [
    "S3Client",
    "ManifestConflictError",
    "NoSuchUploadError",
    "S3ClientError",
]

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., Any])

# Retry configuration
_MAX_RETRIES = 5
_BASE_DELAY = 0.5
_MAX_DELAY = 30.0

# Minimum size for multipart upload
_MULTIPART_MIN_PART_SIZE = 5 * 1024 * 1024  # 5 MB (AWS minimum)


class S3ClientError(Exception):
    """General S3 operation error."""


class ManifestConflictError(S3ClientError):
    """Raised when a conditional PUT fails with 412 PreconditionFailed."""

    def __init__(self, current_etag: str) -> None:
        super().__init__(
            f"Manifest was modified concurrently (current ETag: {current_etag}). "
            "Reload and retry."
        )
        self.current_etag = current_etag


class NoSuchUploadError(S3ClientError):
    """Raised when a multipart upload ID no longer exists in S3."""


# ---------------------------------------------------------------------------
# Retry decorator
# ---------------------------------------------------------------------------


def _is_retryable(exc: Exception) -> bool:
    """Return True for transient S3/network errors worth retrying."""
    if isinstance(exc, botocore.exceptions.ClientError):
        code = exc.response["Error"]["Code"]  # type: ignore[index]
        return code in {
            "RequestTimeout",
            "RequestTimeTooSkewed",
            "InternalError",
            "ServiceUnavailable",
            "SlowDown",
            "503",
            "500",
        }
    return isinstance(
        exc,
        (
            botocore.exceptions.ConnectionError,
            botocore.exceptions.ConnectTimeoutError,
            botocore.exceptions.ReadTimeoutError,
            botocore.exceptions.EndpointConnectionError,
        ),
    )


def retry(max_retries: int = _MAX_RETRIES) -> Callable[[_F], _F]:
    """Exponential back-off decorator with full jitter."""

    def decorator(fn: _F) -> _F:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            delay = _BASE_DELAY
            for attempt in range(max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    if attempt == max_retries or not _is_retryable(exc):
                        raise
                    sleep_time = min(delay * (2**attempt) + random.uniform(0, 0.5), _MAX_DELAY)
                    logger.warning(
                        "S3 operation %s failed (attempt %d/%d): %s. "
                        "Retrying in %.1fs…",
                        fn.__name__,
                        attempt + 1,
                        max_retries,
                        exc,
                        sleep_time,
                    )
                    time.sleep(sleep_time)

        return wrapper  # type: ignore[return-value]

    return decorator


# ---------------------------------------------------------------------------
# S3Client
# ---------------------------------------------------------------------------


class S3Client:
    """Thin boto3 wrapper with Sahara-specific helpers."""

    def __init__(self, config: SaharaConfig) -> None:
        self._config = config
        self._bucket = config.bucket
        self._region = config.region

        session_kwargs: dict[str, Any] = {"region_name": config.region}
        if config.aws_profile:
            session_kwargs["profile_name"] = config.aws_profile

        session = boto3.Session(**session_kwargs)

        # MinIO requires path-style addressing; AWS uses virtual-hosted-style by default.
        addressing_style = "path" if config.is_self_hosted else "auto"
        client_kwargs: dict[str, Any] = {
            "config": BotoConfig(
                retries={"max_attempts": 1, "mode": "legacy"},
                max_pool_connections=config.max_workers + 4,
                s3={"addressing_style": addressing_style},
            )
        }
        if config.aws_access_key_id and config.aws_secret_access_key:
            client_kwargs["aws_access_key_id"] = config.aws_access_key_id
            client_kwargs["aws_secret_access_key"] = config.aws_secret_access_key
        if config.endpoint_url:
            client_kwargs["endpoint_url"] = config.endpoint_url

        self._s3 = session.client("s3", **client_kwargs)
        self._multipart_threshold = config.multipart_threshold_mb * 1024 * 1024
        self._part_size = max(
            config.multipart_chunk_size_mb * 1024 * 1024,
            _MULTIPART_MIN_PART_SIZE,
        )

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    def upload_file(
        self,
        local_path: Path,
        s3_key: str,
        metadata: dict[str, str] | None = None,
        storage_class: str = "STANDARD",
        encrypt_fn: Callable[[Path], tuple[Path, str]] | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> str:
        """Upload *local_path* to S3.

        If encrypt_fn is provided it must accept the source path and return
        (encrypted_tmp_path, plaintext_sha256).

        Returns the S3 ETag of the uploaded object.
        """
        upload_path = local_path
        plaintext_sha256: str | None = None
        tmp_enc: Path | None = None

        try:
            if encrypt_fn is not None:
                tmp_enc, plaintext_sha256 = encrypt_fn(local_path)
                upload_path = tmp_enc

            file_size = upload_path.stat().st_size
            extra: dict[str, Any] = {
                "StorageClass": storage_class,
            }
            if metadata:
                extra["Metadata"] = metadata
            if plaintext_sha256:
                m = extra.setdefault("Metadata", {})
                m["sahara-sha256"] = plaintext_sha256

            if file_size >= self._multipart_threshold:
                etag = self._multipart_upload(
                    upload_path, s3_key, extra, on_progress=on_progress
                )
            else:
                etag = self._simple_upload(
                    upload_path, s3_key, extra, on_progress=on_progress
                )
        finally:
            if tmp_enc is not None and tmp_enc.exists():
                tmp_enc.unlink(missing_ok=True)

        return etag

    @retry()
    def _simple_upload(
        self,
        local_path: Path,
        s3_key: str,
        extra_args: dict,
        on_progress: Callable[[int], None] | None = None,
    ) -> str:
        with open(local_path, "rb") as fh:
            resp = self._s3.put_object(
                Bucket=self._bucket,
                Key=s3_key,
                Body=fh,
                **extra_args,
            )
        return resp["ETag"].strip('"')

    def _multipart_upload(
        self,
        local_path: Path,
        s3_key: str,
        extra_args: dict,
        upload_id: str | None = None,
        completed_parts: list[dict] | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> str:
        """Perform (or resume) a multipart upload."""
        if upload_id is None:
            create_kwargs: dict[str, Any] = {
                "Bucket": self._bucket,
                "Key": s3_key,
            }
            if "StorageClass" in extra_args:
                create_kwargs["StorageClass"] = extra_args["StorageClass"]
            if "Metadata" in extra_args:
                create_kwargs["Metadata"] = extra_args["Metadata"]

            resp = self._s3.create_multipart_upload(**create_kwargs)
            upload_id = resp["UploadId"]

        parts: list[dict] = completed_parts or []
        completed_part_numbers = {p["PartNumber"] for p in parts}

        part_number = 1

        with open(local_path, "rb") as fh:
            while True:
                offset = (part_number - 1) * self._part_size
                fh.seek(offset)
                chunk = fh.read(self._part_size)
                if not chunk:
                    break

                if part_number not in completed_part_numbers:
                    part_resp = self._upload_part_with_retry(
                        s3_key, upload_id, part_number, chunk
                    )
                    parts.append(
                        {
                            "PartNumber": part_number,
                            "ETag": part_resp["ETag"],
                        }
                    )
                    if on_progress:
                        on_progress(len(chunk))

                part_number += 1

        parts_sorted = sorted(parts, key=lambda p: p["PartNumber"])
        complete_resp = self._s3.complete_multipart_upload(
            Bucket=self._bucket,
            Key=s3_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts_sorted},
        )
        return complete_resp["ETag"].strip('"')

    @retry()
    def _upload_part_with_retry(
        self, s3_key: str, upload_id: str, part_number: int, data: bytes
    ) -> dict:
        return self._s3.upload_part(
            Bucket=self._bucket,
            Key=s3_key,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=data,
        )

    def resume_multipart_upload(
        self,
        local_path: Path,
        s3_key: str,
        upload_id: str,
        file_sha256: str,
        pending_parts_json: str,
        storage_class: str = "STANDARD",
        metadata: dict[str, str] | None = None,
    ) -> str:
        """Resume an interrupted multipart upload.

        Verifies the local file's SHA-256 matches the recorded value.
        If not, aborts the old upload and raises S3ClientError.
        """

        # Verify SHA-256 has not changed
        current_sha256 = _compute_sha256(local_path)
        if current_sha256 != file_sha256:
            self.abort_multipart_upload(s3_key, upload_id)
            raise S3ClientError(
                f"File {local_path} has changed since multipart upload was started "
                f"(expected SHA-256 {file_sha256}, got {current_sha256}). "
                "Restarting upload."
            )

        # Verify the upload still exists in S3
        try:
            existing_parts = self.list_parts(s3_key, upload_id)
        except NoSuchUploadError:
            raise S3ClientError(
                f"Multipart upload {upload_id} no longer exists in S3. "
                "Restarting upload."
            )

        completed_parts = existing_parts

        extra_args: dict[str, Any] = {"StorageClass": storage_class}
        if metadata:
            extra_args["Metadata"] = metadata

        return self._multipart_upload(
            local_path,
            s3_key,
            extra_args,
            upload_id=upload_id,
            completed_parts=completed_parts,
        )

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    @retry()
    def download_file(
        self,
        s3_key: str,
        local_path: Path,
        decrypt_fn: Callable[[Path, Path], str] | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> str:
        """Download an S3 object to *local_path* atomically.

        Uses a temp file + rename to ensure no partial writes.
        Returns the local file's SHA-256 (post-decrypt if applicable).
        """
        local_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to a sibling temp file, then rename
        tmp_path = local_path.with_suffix(local_path.suffix + ".tmp~")

        try:
            resp = self._s3.get_object(Bucket=self._bucket, Key=s3_key)
            body = resp["Body"]
            with open(tmp_path, "wb") as fh:
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    if on_progress:
                        on_progress(len(chunk))

            if decrypt_fn is not None:
                dec_tmp = local_path.with_suffix(local_path.suffix + ".dec~")
                try:
                    sha256 = decrypt_fn(tmp_path, dec_tmp)
                    tmp_path.unlink(missing_ok=True)
                    shutil.move(str(dec_tmp), str(local_path))
                except Exception:
                    dec_tmp.unlink(missing_ok=True)
                    raise
            else:
                sha256 = _compute_sha256(tmp_path)
                shutil.move(str(tmp_path), str(local_path))

        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        return sha256

    # ------------------------------------------------------------------
    # Object management
    # ------------------------------------------------------------------

    @retry()
    def delete_object(self, s3_key: str) -> None:
        self._s3.delete_object(Bucket=self._bucket, Key=s3_key)

    @retry()
    def copy_object(
        self,
        src_key: str,
        dst_key: str,
        storage_class: str = "STANDARD",
        extra_metadata: dict[str, str] | None = None,
    ) -> str:
        """Server-side copy; returns new ETag."""
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "CopySource": {"Bucket": self._bucket, "Key": src_key},
            "Key": dst_key,
            "StorageClass": storage_class,
            "MetadataDirective": "COPY",
        }
        if extra_metadata:
            kwargs["Metadata"] = extra_metadata
            kwargs["MetadataDirective"] = "REPLACE"

        resp = self._s3.copy_object(**kwargs)
        return resp["CopyObjectResult"]["ETag"].strip('"')

    @retry()
    def restore_object(
        self,
        s3_key: str,
        days: int = 7,
        tier: str = "Bulk",
    ) -> None:
        """Initiate a Glacier/Deep Archive restore."""
        self._s3.restore_object(
            Bucket=self._bucket,
            Key=s3_key,
            RestoreRequest={
                "Days": days,
                "GlacierJobParameters": {"Tier": tier},
            },
        )

    @retry()
    def head_object(self, s3_key: str) -> dict[str, Any]:
        """Return metadata dict including StorageClass and Restore header."""
        try:
            resp = self._s3.head_object(Bucket=self._bucket, Key=s3_key)
        except botocore.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                raise S3ClientError(f"Object not found: s3://{self._bucket}/{s3_key}") from exc
            raise
        return {
            "ContentLength": resp.get("ContentLength"),
            "ContentType": resp.get("ContentType"),
            "ETag": resp.get("ETag", "").strip('"'),
            "StorageClass": resp.get("StorageClass", "STANDARD"),
            "Restore": resp.get("Restore"),
            "Metadata": resp.get("Metadata", {}),
            "LastModified": resp.get("LastModified"),
        }

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------

    def get_manifest(
        self, key: str | None = None
    ) -> tuple[dict | None, str | None]:
        """Fetch the Sahara manifest from S3.

        Returns (manifest_dict, etag) or (None, None) if no manifest exists.
        If *key* is provided, it overrides config.manifest_key.
        """
        manifest_key = key or self._config.manifest_key
        try:
            resp = self._s3.get_object(
                Bucket=self._bucket, Key=manifest_key
            )
        except botocore.exceptions.ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("404", "NoSuchKey"):
                return None, None
            raise S3ClientError(f"Failed to fetch manifest: {exc}") from exc

        body = resp["Body"].read()
        etag = resp["ETag"].strip('"')
        try:
            manifest = json.loads(body)
        except json.JSONDecodeError as exc:
            raise S3ClientError(
                f"Manifest JSON is corrupt: {exc}"
            ) from exc

        return manifest, etag

    def put_manifest(
        self,
        manifest_dict: dict,
        if_match_etag: str | None = None,
        key: str | None = None,
        if_none_match: bool = False,
    ) -> str:
        """Write the manifest to S3, optionally with conditional PUT.

        Raises ManifestConflictError if if_match_etag is set and the current
        ETag does not match (HTTP 412).

        If *key* is provided, it overrides config.manifest_key.
        Returns the new ETag.
        """
        manifest_key = key or self._config.manifest_key
        body = json.dumps(manifest_dict, separators=(",", ":")).encode("utf-8")
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": manifest_key,
            "Body": body,
            "ContentType": "application/json",
        }
        if if_match_etag is not None and if_none_match:
            raise ValueError("if_match_etag and if_none_match are mutually exclusive")
        if if_match_etag is not None:
            kwargs["IfMatch"] = if_match_etag
        elif if_none_match:
            kwargs["IfNoneMatch"] = "*"

        try:
            resp = self._s3.put_object(**kwargs)
        except botocore.exceptions.ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in {"PreconditionFailed", "ConditionalRequestConflict"}:
                # Fetch current ETag for the conflict error
                try:
                    head = self._s3.head_object(
                        Bucket=self._bucket, Key=manifest_key
                    )
                    current_etag = head.get("ETag", "unknown").strip('"')
                except Exception:
                    current_etag = "unknown"
                raise ManifestConflictError(current_etag) from exc
            raise S3ClientError(f"Failed to write manifest: {exc}") from exc

        return resp["ETag"].strip('"')

    # ------------------------------------------------------------------
    # Bootstrap: list objects (only used when no manifest exists)
    # ------------------------------------------------------------------

    def list_all_objects(self, prefix: str = "") -> list[dict[str, Any]]:
        """Full bucket listing — only called during bootstrap.

        Returns list of {Key, Size, ETag, StorageClass, LastModified}.
        """
        objects: list[dict] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        kwargs: dict[str, Any] = {"Bucket": self._bucket}
        if prefix:
            kwargs["Prefix"] = prefix

        for page in paginator.paginate(**kwargs):
            for obj in page.get("Contents", []):
                objects.append(
                    {
                        "Key": obj["Key"],
                        "Size": obj["Size"],
                        "ETag": obj["ETag"].strip('"'),
                        "StorageClass": obj.get("StorageClass", "STANDARD"),
                        "LastModified": obj["LastModified"],
                    }
                )
        return objects

    # ------------------------------------------------------------------
    # Multipart management
    # ------------------------------------------------------------------

    def list_multipart_uploads(self) -> list[dict[str, Any]]:
        """Return all in-progress multipart uploads for this bucket."""
        uploads: list[dict] = []
        try:
            resp = self._s3.list_multipart_uploads(Bucket=self._bucket)
        except botocore.exceptions.ClientError as exc:
            raise S3ClientError(f"Failed to list multipart uploads: {exc}") from exc

        for u in resp.get("Uploads", []):
            uploads.append(
                {
                    "Key": u["Key"],
                    "UploadId": u["UploadId"],
                    "Initiated": u["Initiated"],
                    "StorageClass": u.get("StorageClass", "STANDARD"),
                }
            )
        return uploads

    @retry()
    def abort_multipart_upload(self, s3_key: str, upload_id: str) -> None:
        try:
            self._s3.abort_multipart_upload(
                Bucket=self._bucket, Key=s3_key, UploadId=upload_id
            )
        except botocore.exceptions.ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "NoSuchUpload":
                return  # Already gone
            raise

    def list_parts(self, s3_key: str, upload_id: str) -> list[dict[str, Any]]:
        """List already-uploaded parts for a multipart upload.

        Raises NoSuchUploadError if the upload_id is no longer valid.
        """
        parts: list[dict] = []
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": s3_key,
            "UploadId": upload_id,
        }
        try:
            while True:
                resp = self._s3.list_parts(**kwargs)
                for p in resp.get("Parts", []):
                    parts.append(
                        {
                            "PartNumber": p["PartNumber"],
                            "ETag": p["ETag"],
                            "Size": p["Size"],
                        }
                    )
                if resp.get("IsTruncated"):
                    kwargs["PartNumberMarker"] = resp["NextPartNumberMarker"]
                else:
                    break
        except botocore.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchUpload":
                raise NoSuchUploadError(
                    f"Multipart upload {upload_id} for key {s3_key} no longer exists."
                ) from exc
            raise
        return parts

    # ------------------------------------------------------------------
    # Bucket validation
    # ------------------------------------------------------------------

    def validate_bucket_access(self) -> None:
        """Raise S3ClientError if the bucket is unreachable or access is denied."""
        try:
            self._s3.head_bucket(Bucket=self._bucket)
        except botocore.exceptions.ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("403", "AccessDenied"):
                raise S3ClientError(
                    f"Access denied to bucket '{self._bucket}'. "
                    "Check your AWS credentials and IAM permissions."
                ) from exc
            if code in ("404", "NoSuchBucket"):
                raise S3ClientError(
                    f"Bucket '{self._bucket}' does not exist in region '{self._region}'."
                ) from exc
            raise S3ClientError(f"Cannot reach bucket '{self._bucket}': {exc}") from exc

    def check_conditional_put_support(self) -> bool:
        """Return True if the bucket supports conditional PUT (If-Match).

        Uses a test write + conditional overwrite to verify.
        """
        test_key = ".sahara/.conditional_put_test"
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=test_key,
                Body=b"test",
            )
            # Try conditional overwrite with wrong ETag — should 412
            try:
                self._s3.put_object(
                    Bucket=self._bucket,
                    Key=test_key,
                    Body=b"test2",
                    IfMatch="00000000000000000000000000000000",
                )
            except botocore.exceptions.ClientError as exc:
                if exc.response["Error"]["Code"] == "PreconditionFailed":
                    return True
            return False
        except Exception:
            return False
        finally:
            try:
                self._s3.delete_object(Bucket=self._bucket, Key=test_key)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# _compute_sha256 is imported from sahara.utils.hash above.
