"""Tests for LocalDriveClient and DualWriteBackend."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
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
    lock_root = Path(drive_paths[0]).parent if drive_paths else Path("/tmp")
    return SaharaConfig(
        sync_folder="/tmp/sahara-test",
        storage_mode=storage_mode,
        drive_paths=drive_paths,
        pid_file=str(lock_root / "daemon.pid"),
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


@pytest.mark.parametrize(
    "key",
    ["../escape.txt", "/absolute.txt", ".", "", "dir/.."],
)
def test_storage_keys_cannot_escape_drive(single_drive, src_file, key):
    client, drive = single_drive

    with pytest.raises(S3ClientError, match="escapes configured drive"):
        client.upload_file(src_file, key)

    assert not (drive.parent / "escape.txt").exists()


def test_storage_key_cannot_escape_through_drive_symlink(single_drive, src_file):
    client, drive = single_drive
    outside = drive.parent / "outside"
    outside.mkdir()
    link = drive / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(S3ClientError, match="escapes configured drive"):
        client.upload_file(src_file, "linked/escape.txt")

    assert not (outside / "escape.txt").exists()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX directory descriptors")
def test_upload_survives_parent_symlink_swap(single_drive, src_file, monkeypatch):
    client, drive = single_drive
    safe = drive / "safe"
    safe.mkdir()
    detached = drive / "detached"
    outside = drive.parent / "outside"
    outside.mkdir()
    original_open = os.open
    swapped = False

    def swap_then_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and dir_fd is not None and str(path).endswith(".tmp"):
            safe.rename(detached)
            safe.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_then_open)
    client.upload_file(src_file, "safe/hello.txt")

    assert (detached / "hello.txt").read_bytes() == b"hello sahara"
    assert not (outside / "hello.txt").exists()


def test_download_reads_from_first_drive(single_drive, src_file, tmp_path):
    client, drive = single_drive
    client.upload_file(src_file, "hello.txt")
    dest = tmp_path / "out.txt"
    sha = client.download_file("hello.txt", dest)
    assert dest.read_bytes() == b"hello sahara"
    assert sha == hashlib.sha256(b"hello sahara").hexdigest()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX file modes")
def test_download_installs_private_file(single_drive, src_file, tmp_path):
    client, _ = single_drive
    client.upload_file(src_file, "private.txt")
    dest = tmp_path / "private.txt"

    client.download_file("private.txt", dest)

    assert dest.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX file modes")
def test_decrypted_download_installs_private_file(
    single_drive,
    src_file,
    tmp_path,
):
    client, _ = single_drive
    client.upload_file(src_file, "encrypted.txt")
    dest = tmp_path / "decrypted.txt"

    def decrypt(source: Path, destination: Path) -> str:
        destination.write_bytes(b"plaintext")
        return hashlib.sha256(b"plaintext").hexdigest()

    client.download_file("encrypted.txt", dest, decrypt_fn=decrypt)

    assert dest.stat().st_mode & 0o777 == 0o600


def test_download_falls_back_to_second_drive(dual_drive, src_file, tmp_path):
    client, d1, d2 = dual_drive
    # Only put file on second drive
    (d2 / "hello.txt").write_bytes(b"hello sahara")
    dest = tmp_path / "out.txt"
    client.download_file("hello.txt", dest)
    assert dest.read_bytes() == b"hello sahara"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX directory descriptors")
def test_download_survives_parent_symlink_swap(single_drive, tmp_path, monkeypatch):
    client, drive = single_drive
    safe = drive / "safe"
    safe.mkdir()
    (safe / "value.txt").write_bytes(b"inside")
    detached = drive / "detached"
    outside = drive.parent / "outside"
    outside.mkdir()
    (outside / "value.txt").write_bytes(b"outside")
    original_copy = shutil.copyfileobj
    swapped = False

    def swap_then_copy(source, target, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            safe.rename(detached)
            safe.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_copy(source, target, *args, **kwargs)

    monkeypatch.setattr(shutil, "copyfileobj", swap_then_copy)
    destination = tmp_path / "download.txt"
    client.download_file("safe/value.txt", destination)

    assert destination.read_bytes() == b"inside"


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


def test_put_manifest_if_match_conflicts_when_all_replicas_missing(single_drive):
    client, _ = single_drive

    with pytest.raises(ManifestConflictError):
        client.put_manifest({"a": 2}, if_match_etag="stale-etag")


def test_put_manifest_create_only_conflict(single_drive):
    client, _ = single_drive
    client.put_manifest({"a": 1})
    with pytest.raises(ManifestConflictError):
        client.put_manifest({"a": 2}, if_none_match=True)


def test_put_manifest_rejects_conflicting_conditions(single_drive):
    client, _ = single_drive
    with pytest.raises(ValueError, match="mutually exclusive"):
        client.put_manifest(
            {"a": 1},
            if_match_etag="etag",
            if_none_match=True,
        )


def test_put_manifest_conditional_writers_are_serialized(single_drive):
    client, _ = single_drive
    original_etag = client.put_manifest({"version": 1})

    def update(version: int) -> str:
        return client.put_manifest(
            {"version": version},
            if_match_etag=original_etag,
        )

    successes: list[str] = []
    conflicts = 0
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(update, 2), executor.submit(update, 3)]
        for future in futures:
            try:
                successes.append(future.result())
            except ManifestConflictError:
                conflicts += 1

    assert len(successes) == 1
    assert conflicts == 1


def test_put_manifest_rolls_back_partial_multi_drive_write(
    dual_drive,
    monkeypatch,
):
    client, d1, d2 = dual_drive
    original_etag = client.put_manifest({"version": 1})
    original_write = client._atomic_write_to_key
    failed = False

    def fail_second_drive_once(drive, key, data):
        nonlocal failed
        if drive.resolve() == d2.resolve() and not failed:
            failed = True
            raise OSError("simulated second-drive failure")
        return original_write(drive, key, data)

    monkeypatch.setattr(client, "_atomic_write_to_key", fail_second_drive_once)

    with pytest.raises(OSError, match="second-drive failure"):
        client.put_manifest({"version": 2}, if_match_etag=original_etag)

    assert json.loads((d1 / ".sahara" / "manifest.json").read_text()) == {
        "version": 1
    }
    assert json.loads((d2 / ".sahara" / "manifest.json").read_text()) == {
        "version": 1
    }


def test_manifest_lock_is_independent_of_drive_order(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    forward = LocalDriveClient(_make_config([str(first), str(second)]))
    reverse = LocalDriveClient(_make_config([str(second), str(first)]))
    original_etag = forward.put_manifest({"version": 1})

    def update(client, version):
        return client.put_manifest(
            {"version": version},
            if_match_etag=original_etag,
        )

    successes = 0
    conflicts = 0
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(update, forward, 2),
            executor.submit(update, reverse, 3),
        ]
        for future in futures:
            try:
                future.result()
                successes += 1
            except ManifestConflictError:
                conflicts += 1

    assert successes == 1
    assert conflicts == 1
    lock_files = list((first / ".sahara" / "locks").glob("manifest-*.lock"))
    assert len(lock_files) == 1


def test_manifest_lock_is_shared_across_machine_local_pid_paths(
    tmp_path: Path,
) -> None:
    drive = tmp_path / "shared-drive"
    drive.mkdir()
    first_config = _make_config([str(drive)])
    second_config = _make_config([str(drive)])
    first_config.pid_file = str(tmp_path / "machine-a" / "daemon.pid")
    second_config.pid_file = str(tmp_path / "machine-b" / "daemon.pid")
    first = LocalDriveClient(first_config)
    second = LocalDriveClient(second_config)
    original_etag = first.put_manifest({"version": 1})

    def update(client, version):
        return client.put_manifest(
            {"version": version},
            if_match_etag=original_etag,
        )

    successes = 0
    conflicts = 0
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(update, first, 2),
            executor.submit(update, second, 3),
        ]
        for future in futures:
            try:
                future.result()
                successes += 1
            except ManifestConflictError:
                conflicts += 1

    assert successes == 1
    assert conflicts == 1


def test_manifest_lock_serializes_overlapping_drive_subsets(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    pair = LocalDriveClient(_make_config([str(first), str(second)]))
    subset = LocalDriveClient(_make_config([str(first)]))
    original_etag = pair.put_manifest({"version": 1})

    def update(client, version):
        return client.put_manifest(
            {"version": version},
            if_match_etag=original_etag,
        )

    successes = 0
    conflicts = 0
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(update, pair, 2),
            executor.submit(update, subset, 3),
        ]
        for future in futures:
            try:
                future.result()
                successes += 1
            except ManifestConflictError:
                conflicts += 1

    assert successes == 1
    assert conflicts == 1


def test_manifest_lock_normalizes_alternate_drive_paths(
    tmp_path: Path,
) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    alias = tmp_path / "drive-alias"
    alias.symlink_to(drive, target_is_directory=True)
    direct = LocalDriveClient(_make_config([str(drive)]))
    via_alias = LocalDriveClient(_make_config([str(alias)]))
    original_etag = direct.put_manifest({"version": 1})

    def update(client, version):
        return client.put_manifest(
            {"version": version},
            if_match_etag=original_etag,
        )

    successes = 0
    conflicts = 0
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(update, direct, 2),
            executor.submit(update, via_alias, 3),
        ]
        for future in futures:
            try:
                future.result()
                successes += 1
            except ManifestConflictError:
                conflicts += 1

    assert successes == 1
    assert conflicts == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor hardening")
def test_list_objects_skips_fifo_without_blocking(single_drive):
    client, drive = single_drive
    os.mkfifo(drive / "pipe")

    assert client.list_all_objects() == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor hardening")
def test_validate_access_rejects_symlinked_internal_directory(
    single_drive,
    tmp_path: Path,
):
    client, drive = single_drive
    outside = tmp_path / "outside"
    outside.mkdir()
    (drive / ".sahara").symlink_to(outside, target_is_directory=True)

    with pytest.raises(S3ClientError):
        client.validate_bucket_access()

    assert list(outside.iterdir()) == []


# ---------------------------------------------------------------------------
# LocalDriveClient — list_all_objects
# ---------------------------------------------------------------------------


def test_list_all_objects_empty(single_drive):
    client, _ = single_drive
    assert client.list_all_objects() == []


def test_list_all_objects_skips_symlinks_outside_drive(single_drive):
    client, drive = single_drive
    outside = drive.parent / "outside.txt"
    outside.write_bytes(b"private")
    link = drive / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are unavailable")

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
    assert c.storage_mode == "none"
    assert c.is_local_drive_mode is False
    assert c.is_index_only_mode is True


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
