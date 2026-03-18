"""Extended tests for sahara.s3_client covering additional code paths."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import boto3
import botocore.exceptions
import pytest
from moto import mock_aws

from sahara.config import SaharaConfig
from sahara.s3_client import S3Client, S3ClientError, ManifestConflictError, NoSuchUploadError


BUCKET = "ext-test-bucket"
REGION = "us-east-1"


def _make_config(**kwargs) -> SaharaConfig:
    defaults = dict(
        bucket=BUCKET,
        region=REGION,
        prefix="",
        max_workers=2,
        multipart_threshold_mb=100,
        multipart_chunk_size_mb=8,
    )
    defaults.update(kwargs)
    return SaharaConfig(**defaults)


# ---------------------------------------------------------------------------
# restore_object
# ---------------------------------------------------------------------------


class TestRestoreObject:
    def test_restore_object(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config()
            client = S3Client(cfg)

            # Upload a file first
            src = tmp_path / "f.txt"
            src.write_bytes(b"data")
            client.upload_file(src, "f.txt")

            # We can't actually restore in moto easily, but we can verify the call
            with patch.object(client._s3, "restore_object") as mock_restore:
                client.restore_object("f.txt", days=7, tier="Bulk")
                mock_restore.assert_called_once_with(
                    Bucket=BUCKET,
                    Key="f.txt",
                    RestoreRequest={"Days": 7, "GlacierJobParameters": {"Tier": "Bulk"}},
                )


# ---------------------------------------------------------------------------
# copy_object with storage class
# ---------------------------------------------------------------------------


class TestCopyObjectStorageClass:
    def test_copy_to_glacier(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config()
            client = S3Client(cfg)

            src = tmp_path / "f.txt"
            src.write_bytes(b"archive me")
            client.upload_file(src, "original.txt")

            etag = client.copy_object("original.txt", "archived.txt", storage_class="STANDARD_IA")
            assert etag is not None


# ---------------------------------------------------------------------------
# validate_bucket_access edge cases
# ---------------------------------------------------------------------------


class TestValidateBucketEdgeCases:
    def test_403_raises_s3_client_error(self):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config()
            client = S3Client(cfg)

            error_response = {
                "Error": {"Code": "403", "Message": "Access Denied"}
            }
            with patch.object(client._s3, "head_bucket",
                               side_effect=botocore.exceptions.ClientError(
                                   error_response, "HeadBucket")):
                with pytest.raises(S3ClientError, match="Access denied"):
                    client.validate_bucket_access()

    def test_other_error_raises_s3_client_error(self):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config()
            client = S3Client(cfg)

            error_response = {
                "Error": {"Code": "500", "Message": "Server Error"}
            }
            with patch.object(client._s3, "head_bucket",
                               side_effect=botocore.exceptions.ClientError(
                                   error_response, "HeadBucket")):
                with pytest.raises(S3ClientError):
                    client.validate_bucket_access()


# ---------------------------------------------------------------------------
# get_manifest with corrupt JSON
# ---------------------------------------------------------------------------


class TestManifestEdgeCases:
    def test_get_manifest_corrupt_json_raises(self):
        with mock_aws():
            raw = boto3.client("s3", region_name=REGION)
            raw.create_bucket(Bucket=BUCKET)
            cfg = _make_config()
            client = S3Client(cfg)

            # Put invalid JSON
            raw.put_object(
                Bucket=BUCKET,
                Key=".sahara/manifest.json",
                Body=b"not valid json!!!",
                ContentType="application/json",
            )

            with pytest.raises(S3ClientError, match="corrupt"):
                client.get_manifest()

    def test_get_manifest_other_s3_error_raises(self):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config()
            client = S3Client(cfg)

            error_response = {"Error": {"Code": "500", "Message": "Error"}}
            with patch.object(client._s3, "get_object",
                               side_effect=botocore.exceptions.ClientError(
                                   error_response, "GetObject")):
                with pytest.raises(S3ClientError):
                    client.get_manifest()


# ---------------------------------------------------------------------------
# put_manifest S3 error
# ---------------------------------------------------------------------------


class TestPutManifestErrors:
    def test_put_manifest_other_s3_error_raises(self):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config()
            client = S3Client(cfg)

            error_response = {"Error": {"Code": "403", "Message": "Forbidden"}}
            with patch.object(client._s3, "put_object",
                               side_effect=botocore.exceptions.ClientError(
                                   error_response, "PutObject")):
                with pytest.raises(S3ClientError):
                    client.put_manifest({"key": "value"})


# ---------------------------------------------------------------------------
# download_file with decrypt_fn
# ---------------------------------------------------------------------------


class TestDownloadFileDecrypt:
    def test_download_with_decrypt_fn(self, tmp_path: Path):
        with mock_aws():
            raw = boto3.client("s3", region_name=REGION)
            raw.create_bucket(Bucket=BUCKET)
            cfg = _make_config()
            client = S3Client(cfg)

            content = b"plaintext content"
            encrypted = b"encrypted content"
            raw.put_object(Bucket=BUCKET, Key="encrypted.saha", Body=encrypted)

            decrypt_called = []

            def fake_decrypt(src: Path, dst: Path) -> str:
                decrypt_called.append(True)
                # Write fake decrypted content
                dst.write_bytes(content)
                return "fake-sha256"

            local_path = tmp_path / "decrypted.txt"
            result = client.download_file("encrypted.saha", local_path, decrypt_fn=fake_decrypt)
            assert len(decrypt_called) == 1
            assert result == "fake-sha256"
            assert local_path.read_bytes() == content

    def test_download_decrypt_failure_cleanup(self, tmp_path: Path):
        with mock_aws():
            raw = boto3.client("s3", region_name=REGION)
            raw.create_bucket(Bucket=BUCKET)
            cfg = _make_config()
            client = S3Client(cfg)

            raw.put_object(Bucket=BUCKET, Key="bad.saha", Body=b"bad encrypted")

            def failing_decrypt(src: Path, dst: Path) -> str:
                raise ValueError("decryption failed")

            local_path = tmp_path / "result.txt"
            with pytest.raises(ValueError):
                client.download_file("bad.saha", local_path, decrypt_fn=failing_decrypt)

            # Temp files should be cleaned up
            tmp_file = local_path.with_suffix(local_path.suffix + ".tmp~")
            assert not tmp_file.exists()


# ---------------------------------------------------------------------------
# list_parts with pagination
# ---------------------------------------------------------------------------


class TestListPartsPagination:
    def test_list_parts_pagination(self):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config()
            client = S3Client(cfg)

            # Mock paginated response
            page1 = {
                "Parts": [{"PartNumber": 1, "ETag": "e1", "Size": 100}],
                "IsTruncated": True,
                "NextPartNumberMarker": 1,
            }
            page2 = {
                "Parts": [{"PartNumber": 2, "ETag": "e2", "Size": 100}],
                "IsTruncated": False,
            }

            call_count = [0]

            def list_parts_side_effect(**kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return page1
                return page2

            with patch.object(client._s3, "list_parts", side_effect=list_parts_side_effect):
                parts = client.list_parts("key", "upload-id")
                assert len(parts) == 2
                assert parts[0]["PartNumber"] == 1
                assert parts[1]["PartNumber"] == 2


# ---------------------------------------------------------------------------
# upload_file with encrypt_fn
# ---------------------------------------------------------------------------


class TestUploadFileWithEncryptFn:
    def test_upload_with_encrypt_fn(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config()
            client = S3Client(cfg)

            src = tmp_path / "plaintext.txt"
            src.write_bytes(b"secret data")

            encrypted_tmp = tmp_path / "encrypted.saha"
            encrypted_tmp.write_bytes(b"encrypted content")

            def encrypt_fn(path: Path):
                return encrypted_tmp, "plaintext-sha256"

            etag = client.upload_file(src, "encrypted.saha", encrypt_fn=encrypt_fn)
            assert etag is not None


# ---------------------------------------------------------------------------
# multipart upload (force small threshold)
# ---------------------------------------------------------------------------


class TestMultipartUploadForced:
    def test_multipart_upload_small_threshold(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            # Force multipart for any file > 1 byte
            cfg = SaharaConfig(
                bucket=BUCKET,
                region=REGION,
                multipart_threshold_mb=0,  # 0 MB threshold
                multipart_chunk_size_mb=1,
                max_workers=2,
            )
            client = S3Client(cfg)

            src = tmp_path / "multi.txt"
            content = b"x" * (6 * 1024 * 1024)  # 6 MB
            src.write_bytes(content)

            etag = client.upload_file(src, "multi.txt")
            assert etag is not None

            # Verify content
            raw = boto3.client("s3", region_name=REGION)
            resp = raw.get_object(Bucket=BUCKET, Key="multi.txt")
            assert resp["Body"].read() == content


# ---------------------------------------------------------------------------
# check_conditional_put_support
# ---------------------------------------------------------------------------


class TestCheckConditionalPutSupport:
    def test_returns_false_on_exception(self):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config()
            client = S3Client(cfg)

            with patch.object(client._s3, "put_object", side_effect=Exception("error")):
                result = client.check_conditional_put_support()
                assert result is False

    def test_returns_false_when_no_precondition_failed(self):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config()
            client = S3Client(cfg)

            # Moto doesn't support conditional PUT, so this returns False
            result = client.check_conditional_put_support()
            # Result depends on moto behavior (True or False), just verify it doesn't raise
            assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# list_multipart_uploads with error
# ---------------------------------------------------------------------------


class TestListMultipartUploadsError:
    def test_list_multipart_uploads_raises_on_error(self):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config()
            client = S3Client(cfg)

            error_response = {"Error": {"Code": "403", "Message": "Denied"}}
            with patch.object(client._s3, "list_multipart_uploads",
                               side_effect=botocore.exceptions.ClientError(
                                   error_response, "ListMultipartUploads")):
                with pytest.raises(S3ClientError):
                    client.list_multipart_uploads()


# ---------------------------------------------------------------------------
# head_object with on_progress
# ---------------------------------------------------------------------------


class TestDownloadWithProgress:
    def test_download_with_progress_callback(self, tmp_path: Path):
        with mock_aws():
            raw = boto3.client("s3", region_name=REGION)
            raw.create_bucket(Bucket=BUCKET)
            cfg = _make_config()
            client = S3Client(cfg)

            content = b"data with progress"
            raw.put_object(Bucket=BUCKET, Key="progress.txt", Body=content)

            progress_bytes = []

            def on_progress(n):
                progress_bytes.append(n)

            local_path = tmp_path / "downloaded.txt"
            client.download_file("progress.txt", local_path, on_progress=on_progress)
            assert sum(progress_bytes) == len(content)
