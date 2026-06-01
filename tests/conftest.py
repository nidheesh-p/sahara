"""Shared pytest fixtures for Sahara test suite."""
from __future__ import annotations

import datetime
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from sahara.config import SaharaConfig
from sahara.encryption import derive_key, generate_salt
from sahara.state_db import StateDB

# ---------------------------------------------------------------------------
# Basic fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    """Return a temporary directory for tests."""
    return tmp_path


@pytest.fixture
def sample_config(tmp_dir: Path) -> SaharaConfig:
    """Return a SaharaConfig with test-safe values."""
    sync_folder = tmp_dir / "sync"
    sync_folder.mkdir(parents=True, exist_ok=True)
    return SaharaConfig(
        sync_folder=str(sync_folder),
        bucket="test-bucket",
        region="us-east-1",
        prefix="",
        max_workers=2,
        encryption_enabled=False,
        conflict_strategy="backup",
        delete_remote_on_local_delete=True,
        delete_local_on_remote_delete=True,
    )


@pytest.fixture
def in_memory_db(tmp_dir: Path) -> StateDB:
    """Return an open StateDB backed by a temp file (SQLite :memory: doesn't support WAL)."""
    db_path = tmp_dir / "state.db"
    db = StateDB(db_path)
    db.connect()
    yield db
    db.close()


@pytest.fixture
def mock_s3(sample_config: SaharaConfig):
    """Provide a moto-mocked S3 environment with the test bucket created."""
    with mock_aws():
        s3_client = boto3.client("s3", region_name="us-east-1")
        s3_client.create_bucket(Bucket=sample_config.bucket)
        yield s3_client


@pytest.fixture
def sample_files(tmp_dir: Path) -> list[Path]:
    """Create a handful of test files in tmp_dir/sync/."""
    sync_folder = tmp_dir / "sync"
    sync_folder.mkdir(parents=True, exist_ok=True)

    files = []
    data = [
        ("file_a.txt", b"Hello, Sahara!"),
        ("file_b.txt", b"Another test file"),
        ("subdir/file_c.txt", b"Nested file content"),
    ]
    for rel, content in data:
        path = sync_folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        files.append(path)

    return files


@pytest.fixture
def encryption_key_and_salt() -> tuple[bytes, bytes]:
    """Generate a deterministic test key + salt for encryption tests."""
    salt = generate_salt()
    key = derive_key("test-passphrase-123", salt)
    return key, salt


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)
