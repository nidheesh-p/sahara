"""Tests for LocalDriveClient and DualWriteBackend."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sahara.config import SaharaConfig
from sahara.storage.dual_write_backend import DualWriteBackend
from sahara.storage.local_drive_client import LocalDriveClient
from sahara.storage.s3_client import ManifestConflictError, S3ClientError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(drive_paths: list[str], storage_mode: str = "local") -> SaharaConfig:
    return SaharaConfig(
        sync_folder="/tmp/sahara-test",
        storage_mode=storage_mode,
        drive_paths=drive_paths,
    )


@pytest.fixture
def single_drive(tmp_path: Path) -> tuple[LocalDriveClient, Path]:
    drive = tmp_path / "drive1"
    drive.mkdir()
    config = _make_config([str(drive)])
    return LocalDriveClient(config), drive


@pytest.fixture
def dual_drive(tmp_path: Path) -> tuple[LocalDriveClient, Path, Path]:
    d1 = tmp_path / "drive1"
    d2 = tmp_path / "drive2"
    d1.mkdir()
    d2.mkdir()
    config = _make_config([str(d1), str(d2)])
    return LocalDriveClient(config), d1, d2


@pytest.fixture
def src_file(tmp_path: Path) -> Path:
    f = tmp_path / "hello.txt"
    f.write_bytes(b"hello sahara")
    return f


# ---------------------------------------------------------------------------
# LocalDriveClient — constructor
# ---------------------------------------------------------------------------


def test_no_drive_paths_raises():
    config = _make_config([])
    with pytest.raises(S3ClientError, match="drive_paths is empty"):
        LocalDriveClient(config)


# ---------------------------------------------------------------------------
# LocalDriveClient — upload / download roundtrip
# ---------------------------------------------------------------------------


def test_upload_creates_file_on_drive(single_drive, src_file):
    client, drive = single_drive
    etag = client.upload_file(src_file, "docs/hello.txt")
    assert (drive / "docs" / "hello.txt").read_bytes() == b"hello sahara"
    # etag should be SHA-256 of the content
    expected = hashlib.sha256(b"hello sahara").hexdigest()
    assert etag == expected


def test_upload_writes_to_all_drives(dual_drive, src_file):
    client, d1, d2 = dual_drive
    client.upload_file(src_file, "hello.txt")
    assert (d1 / "hello.txt").exists()
    assert (d2 / "hello.txt").exists()


def test_download_reads_from_first_drive(single_drive, src_file, tmp_path):
    client, drive = single_drive
    client.upload_file(src_file, "hello.txt")
    dest = tmp_path / "out.txt"
    sha = client.download_file("hello.txt", dest)
    assert dest.read_bytes() == b"hello sahara"
    assert sha == hashlib.sha256(b"hello sahara").hexdigest()


def test_download_falls_back_to_second_drive(dual_drive, src_file, tmp_path):
    client, d1, d2 = dual_drive
    # Only put file on second drive
    (d2 / "hello.txt").write_bytes(b"hello sahara")
    dest = tmp_path / "out.txt"
    client.download_file("hello.txt", dest)
    assert dest.read_bytes() == b"hello sahara"


def test_download_missing_raises(single_drive, tmp_path):
    client, _ = single_drive
    with pytest.raises(S3ClientError, match="not found"):
        client.download_file("missing.txt", tmp_path / "out.txt")


# ---------------------------------------------------------------------------
# LocalDriveClient — delete
# ---------------------------------------------------------------------------


def test_delete_removes_from_all_drives(dual_drive, src_file):
    client, d1, d2 = dual_drive
    client.upload_file(src_file, "hello.txt")
    assert (d1 / "hello.txt").exists()
    assert (d2 / "hello.txt").exists()

    client.delete_object("hello.txt")
    assert not (d1 / "hello.txt").exists()
    assert not (d2 / "hello.txt").exists()


def test_delete_nonexistent_is_noop(single_drive):
    client, _ = single_drive
    # Should not raise
    client.delete_object("does-not-exist.txt")


# ---------------------------------------------------------------------------
# LocalDriveClient — copy
# ---------------------------------------------------------------------------


def test_copy_object_on_all_drives(dual_drive, src_file):
    client, d1, d2 = dual_drive
    client.upload_file(src_file, "orig.txt")
    etag = client.copy_object("orig.txt", "copy.txt")
    assert (d1 / "copy.txt").read_bytes() == b"hello sahara"
    assert (d2 / "copy.txt").read_bytes() == b"hello sahara"
    assert etag == hashlib.sha256(b"hello sahara").hexdigest()


def test_copy_missing_source_raises(single_drive):
    client, _ = single_drive
    with pytest.raises(S3ClientError, match="not found"):
        client.copy_object("ghost.txt", "copy.txt")


# ---------------------------------------------------------------------------
# LocalDriveClient — head_object
# ---------------------------------------------------------------------------


def test_head_object_returns_metadata(single_drive, src_file):
    client, _ = single_drive
    client.upload_file(src_file, "hello.txt")
    meta = client.head_object("hello.txt")
    assert meta["ContentLength"] == len(b"hello sahara")
    assert meta["StorageClass"] == "STANDARD"
    assert meta["ETag"] == hashlib.sha256(b"hello sahara").hexdigest()


def test_head_object_missing_raises(single_drive):
    client, _ = single_drive
    with pytest.raises(S3ClientError, match="not found"):
        client.head_object("ghost.txt")


# ---------------------------------------------------------------------------
# LocalDriveClient — manifest
# ---------------------------------------------------------------------------


def test_get_manifest_returns_none_when_absent(single_drive):
    client, _ = single_drive
    manifest, etag = client.get_manifest()
    assert manifest is None
    assert etag is None


def test_put_and_get_manifest_roundtrip(single_drive):
    client, _ = single_drive
    data = {"file.txt": {"sha256": "abc", "size": 10}}
    etag = client.put_manifest(data)
    manifest, got_etag = client.get_manifest()
    assert manifest == data
    assert got_etag == etag


def test_manifest_written_to_all_drives(dual_drive):
    client, d1, d2 = dual_drive
    data = {"x.txt": {"sha256": "xyz"}}
    client.put_manifest(data)
    assert (d1 / ".sahara" / "manifest.json").exists()
    assert (d2 / ".sahara" / "manifest.json").exists()


def test_put_manifest_conditional_success(single_drive):
    client, _ = single_drive
    data = {"a": 1}
    etag1 = client.put_manifest(data)
    # Updating with correct etag should succeed
    etag2 = client.put_manifest({"a": 2}, if_match_etag=etag1)
    manifest, _ = client.get_manifest()
    assert manifest == {"a": 2}
    assert etag2 != etag1


def test_put_manifest_conditional_conflict(single_drive):
    client, _ = single_drive
    client.put_manifest({"a": 1})
    with pytest.raises(ManifestConflictError):
        client.put_manifest({"a": 2}, if_match_etag="wrong-etag")


# ---------------------------------------------------------------------------
# LocalDriveClient — list_all_objects
# ---------------------------------------------------------------------------


def test_list_all_objects_empty(single_drive):
    client, _ = single_drive
    assert client.list_all_objects() == []


def test_list_all_objects_finds_files(single_drive, src_file):
    client, drive = single_drive
    client.upload_file(src_file, "folder/hello.txt")
    objects = client.list_all_objects()
    assert len(objects) == 1
    assert objects[0]["Key"] == "folder/hello.txt"
    assert objects[0]["Size"] == len(b"hello sahara")
    assert objects[0]["StorageClass"] == "STANDARD"


def test_list_all_objects_excludes_manifest(single_drive):
    client, _ = single_drive
    client.put_manifest({"x": 1})
    objects = client.list_all_objects()
    assert all(".sahara" not in o["Key"] for o in objects)


# ---------------------------------------------------------------------------
# LocalDriveClient — validate_bucket_access
# ---------------------------------------------------------------------------


def test_validate_bucket_access_ok(single_drive):
    client, _ = single_drive
    client.validate_bucket_access()  # should not raise


def test_validate_bucket_access_missing_drive(tmp_path):
    config = _make_config([str(tmp_path / "nonexistent")])
    client = LocalDriveClient(config)
    with pytest.raises(S3ClientError, match="does not exist"):
        client.validate_bucket_access()


# ---------------------------------------------------------------------------
# LocalDriveClient — check_conditional_put_support / restore_object
# ---------------------------------------------------------------------------


def test_check_conditional_put_support_true(single_drive):
    client, _ = single_drive
    assert client.check_conditional_put_support() is True


def test_restore_object_raises(single_drive):
    client, _ = single_drive
    with pytest.raises(S3ClientError, match="not supported"):
        client.restore_object("file.txt")


# ---------------------------------------------------------------------------
# DualWriteBackend
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_primary():
    m = MagicMock()
    m.upload_file.return_value = "primary-etag"
    m.download_file.return_value = "primary-sha256"
    m.copy_object.return_value = "primary-copy-etag"
    m.get_manifest.return_value = ({"key": "val"}, "etag-abc")
    m.put_manifest.return_value = "new-etag"
    m.list_all_objects.return_value = [{"Key": "file.txt"}]
    m.head_object.return_value = {"ContentLength": 10}
    m.check_conditional_put_support.return_value = True
    return m


@pytest.fixture
def mock_secondary():
    m = MagicMock()
    m.upload_file.return_value = "secondary-etag"
    m.copy_object.return_value = "secondary-copy-etag"
    return m


@pytest.fixture
def dual_write(mock_primary, mock_secondary):
    return DualWriteBackend(mock_primary, mock_secondary, glacier_keep_deleted=True)


def test_dual_upload_goes_to_both(dual_write, mock_primary, mock_secondary, tmp_path):
    f = tmp_path / "f.txt"
    f.write_bytes(b"data")
    etag = dual_write.upload_file(f, "f.txt")
    mock_primary.upload_file.assert_called_once()
    mock_secondary.upload_file.assert_called_once()
    # Returns primary's etag
    assert etag == "primary-etag"
    # Secondary gets DEEP_ARCHIVE storage class (4th positional arg)
    args = mock_secondary.upload_file.call_args[0]
    assert args[3] == "DEEP_ARCHIVE"


def test_dual_upload_secondary_failure_is_noncritical(mock_primary, mock_secondary, tmp_path):
    mock_secondary.upload_file.side_effect = Exception("Glacier unavailable")
    backend = DualWriteBackend(mock_primary, mock_secondary)
    f = tmp_path / "f.txt"
    f.write_bytes(b"data")
    # Should not raise
    etag = backend.upload_file(f, "f.txt")
    assert etag == "primary-etag"


def test_dual_download_from_primary_only(dual_write, mock_primary, mock_secondary, tmp_path):
    dst = tmp_path / "out.txt"
    sha = dual_write.download_file("f.txt", dst)
    mock_primary.download_file.assert_called_once()
    mock_secondary.download_file.assert_not_called()
    assert sha == "primary-sha256"


def test_dual_delete_glacier_kept_by_default(dual_write, mock_primary, mock_secondary):
    dual_write.delete_object("file.txt")
    mock_primary.delete_object.assert_called_once_with("file.txt")
    mock_secondary.delete_object.assert_not_called()


def test_dual_delete_glacier_deleted_when_configured(mock_primary, mock_secondary):
    backend = DualWriteBackend(mock_primary, mock_secondary, glacier_keep_deleted=False)
    backend.delete_object("file.txt")
    mock_primary.delete_object.assert_called_once_with("file.txt")
    mock_secondary.delete_object.assert_called_once_with("file.txt")


def test_dual_copy_goes_to_both(dual_write, mock_primary, mock_secondary):
    etag = dual_write.copy_object("src.txt", "dst.txt")
    mock_primary.copy_object.assert_called_once()
    mock_secondary.copy_object.assert_called_once()
    assert etag == "primary-copy-etag"


def test_dual_copy_secondary_failure_is_noncritical(mock_primary, mock_secondary):
    mock_secondary.copy_object.side_effect = Exception("Glacier error")
    backend = DualWriteBackend(mock_primary, mock_secondary)
    etag = backend.copy_object("src.txt", "dst.txt")
    assert etag == "primary-copy-etag"


def test_dual_get_manifest_from_primary(dual_write, mock_primary, mock_secondary):
    manifest, etag = dual_write.get_manifest()
    mock_primary.get_manifest.assert_called_once()
    mock_secondary.get_manifest.assert_not_called()
    assert manifest == {"key": "val"}


def test_dual_put_manifest_to_primary_only(dual_write, mock_primary, mock_secondary):
    etag = dual_write.put_manifest({"a": 1})
    mock_primary.put_manifest.assert_called_once()
    mock_secondary.put_manifest.assert_not_called()
    assert etag == "new-etag"


def test_dual_list_objects_from_primary(dual_write, mock_primary, mock_secondary):
    objects = dual_write.list_all_objects()
    mock_primary.list_all_objects.assert_called_once()
    mock_secondary.list_all_objects.assert_not_called()
    assert objects == [{"Key": "file.txt"}]


def test_dual_head_object_from_primary(dual_write, mock_primary, mock_secondary):
    meta = dual_write.head_object("file.txt")
    mock_primary.head_object.assert_called_once_with("file.txt")
    mock_secondary.head_object.assert_not_called()
    assert meta == {"ContentLength": 10}


def test_dual_validate_checks_both(dual_write, mock_primary, mock_secondary):
    dual_write.validate_bucket_access()
    mock_primary.validate_bucket_access.assert_called_once()
    mock_secondary.validate_bucket_access.assert_called_once()


def test_dual_validate_secondary_failure_raises(mock_primary, mock_secondary):
    mock_secondary.validate_bucket_access.side_effect = S3ClientError("no access")
    backend = DualWriteBackend(mock_primary, mock_secondary)
    with pytest.raises(S3ClientError, match="Glacier secondary"):
        backend.validate_bucket_access()


def test_dual_restore_object_raises(dual_write):
    with pytest.raises(S3ClientError, match="not available"):
        dual_write.restore_object("file.txt")


def test_dual_conditional_put_support_from_primary(dual_write, mock_primary):
    result = dual_write.check_conditional_put_support()
    mock_primary.check_conditional_put_support.assert_called_once()
    assert result is True


# ---------------------------------------------------------------------------
# Config: storage_mode defaults and is_local_drive_mode
# ---------------------------------------------------------------------------


def test_config_default_storage_mode():
    c = SaharaConfig()
    assert c.storage_mode == "s3"
    assert c.is_local_drive_mode is False


def test_config_local_drive_mode():
    c = SaharaConfig(storage_mode="local", drive_paths=["/Volumes/Drive1"])
    assert c.is_local_drive_mode is True
    assert c.default_storage_class == "STANDARD"


def test_config_local_glacier_mode():
    c = SaharaConfig(storage_mode="local+glacier", drive_paths=["/Volumes/Drive1"])
    assert c.is_local_drive_mode is True
    assert c.default_storage_class == "STANDARD"


def test_config_s3_mode_keeps_glacier_class():
    c = SaharaConfig(storage_mode="s3")
    assert c.default_storage_class == "GLACIER_IR"


def test_config_drive_paths_roundtrip(tmp_path):
    from sahara.config import load_config, save_config
    config = SaharaConfig(
        sync_folder=str(tmp_path / "sync"),
        storage_mode="local",
        drive_paths=[str(tmp_path / "d1"), str(tmp_path / "d2")],
    )
    cfg_path = tmp_path / "config.toml"
    save_config(config, cfg_path)
    loaded = load_config(cfg_path)
    assert loaded.storage_mode == "local"
    assert loaded.drive_paths == config.drive_paths
