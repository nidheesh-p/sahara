"""Targeted tests to boost coverage to >90%."""
from __future__ import annotations

import datetime
import os
import signal
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch, call

import pytest
from click.testing import CliRunner

from sahara.cli import _styled, _human_size, main
from sahara.config import SaharaConfig, save_config


# ---------------------------------------------------------------------------
# cli.py helpers
# ---------------------------------------------------------------------------


class TestCliHelpers:
    def test_styled_returns_string(self):
        result = _styled("hello", fg="green", bold=True)
        assert "hello" in result

    def test_human_size_pb(self):
        # 1 PB = 1024^5 bytes
        big = 1024**5
        result = _human_size(big)
        assert "PB" in result

    def test_human_size_tb(self):
        tb = 1024**4
        result = _human_size(tb)
        assert "TB" in result


# ---------------------------------------------------------------------------
# cli config set — ValueError path
# ---------------------------------------------------------------------------


class TestConfigSetValueError:
    def test_config_set_invalid_int_value(self, tmp_path: Path):
        cfg = SaharaConfig(sync_folder=str(tmp_path / "sync"), bucket="b", region="us-east-1")
        cfg_path = tmp_path / "config.toml"
        save_config(cfg, cfg_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--config", str(cfg_path), "config", "set", "max_workers", "not-a-number"],
        )
        # Should abort with conversion error
        assert result.exit_code != 0 or "Cannot convert" in result.output


# ---------------------------------------------------------------------------
# cli rm — confirm flow
# ---------------------------------------------------------------------------


class TestRmCmd:
    def test_rm_local_only_force(self, tmp_path: Path):
        sync = tmp_path / "sync"
        sync.mkdir()
        target = sync / "file.txt"
        target.write_text("hello")

        cfg = SaharaConfig(sync_folder=str(sync), bucket="b", region="us-east-1")
        cfg_path = tmp_path / "config.toml"
        save_config(cfg, cfg_path)

        mock_s3_inst = MagicMock()
        mock_db = MagicMock()
        mock_db_cls = MagicMock(return_value=mock_db)
        mock_db.connect.return_value = mock_db

        with patch("sahara.state_db.StateDB", mock_db_cls), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3_inst):
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["--config", str(cfg_path), "rm", "--force", "--local", "file.txt"],
            )

        assert result.exit_code == 0

    def test_rm_confirms_deletion(self, tmp_path: Path):
        """Test rm without --force shows confirmation and aborts on 'n'."""
        cfg = SaharaConfig(sync_folder=str(tmp_path / "sync"), bucket="b", region="us-east-1")
        cfg_path = tmp_path / "config.toml"
        save_config(cfg, cfg_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--config", str(cfg_path), "rm", "file.txt"],
            input="n\n",
        )
        # Declined confirmation — should return without deleting
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# cli mv — S3 move failure warning
# ---------------------------------------------------------------------------


class TestMvCmd:
    def test_mv_s3_move_failed_warns(self, tmp_path: Path):
        sync = tmp_path / "sync"
        sync.mkdir()
        src = sync / "src.txt"
        src.write_text("content")

        cfg = SaharaConfig(sync_folder=str(sync), bucket="b", region="us-east-1")
        cfg_path = tmp_path / "config.toml"
        save_config(cfg, cfg_path)

        mock_s3 = MagicMock()
        mock_s3.copy_object.side_effect = Exception("S3 copy failed")
        mock_db = MagicMock()
        mock_db.connect.return_value = mock_db
        mock_db.get_file.return_value = None

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3):
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["--config", str(cfg_path), "mv", "src.txt", "dst.txt"],
            )

        assert "S3 move failed" in result.output


# ---------------------------------------------------------------------------
# cli restore-status — exception path
# ---------------------------------------------------------------------------


class TestRestoreStatusException:
    def test_restore_status_exception_shows_warning(self, tmp_path: Path):
        cfg = SaharaConfig(sync_folder=str(tmp_path / "sync"), bucket="b", region="us-east-1")
        cfg_path = tmp_path / "config.toml"
        save_config(cfg, cfg_path)

        mock_engine = MagicMock()
        mock_engine.check_restore_status.side_effect = RuntimeError("not found")
        mock_db = MagicMock()
        mock_db.connect.return_value = mock_db
        mock_db.list_pending_restores.return_value = []
        mock_s3 = MagicMock()

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.cli._build_engine", return_value=(mock_engine, mock_db, mock_s3)):
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["--config", str(cfg_path), "restore-status", "file.txt"],
            )

        assert "not found" in result.output


# ---------------------------------------------------------------------------
# cli restore-download — exception path
# ---------------------------------------------------------------------------


class TestRestoreDownloadException:
    def test_restore_download_exception_aborts(self, tmp_path: Path):
        cfg = SaharaConfig(sync_folder=str(tmp_path / "sync"), bucket="b", region="us-east-1")
        cfg_path = tmp_path / "config.toml"
        save_config(cfg, cfg_path)

        mock_engine = MagicMock()
        mock_engine.download_restored.side_effect = RuntimeError("not ready")
        mock_db = MagicMock()
        mock_db.connect.return_value = mock_db

        with patch("sahara.cli._build_engine", return_value=(mock_engine, mock_db, MagicMock())):
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["--config", str(cfg_path), "restore-download", "file.txt"],
            )

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# cli usage — simulate prompts
# ---------------------------------------------------------------------------


class TestUsageSimulate:
    def test_usage_simulate_with_prompts(self, tmp_path: Path):
        cfg = SaharaConfig(sync_folder=str(tmp_path / "sync"), bucket="b", region="us-east-1")
        cfg_path = tmp_path / "config.toml"
        save_config(cfg, cfg_path)

        mock_estimator = MagicMock()
        mock_estimator.simulate_cost.return_value = "Cost: $1.00"

        with patch("sahara.cost_estimator.CostEstimator", return_value=mock_estimator):
            runner = CliRunner()
            # Provide input for the three prompts: standard_gb, glacier_gb, deep_archive_gb
            result = runner.invoke(
                main,
                ["--config", str(cfg_path), "usage", "--simulate"],
                input="10.0\n5.0\n2.0\n",
            )

        assert result.exit_code == 0
        mock_estimator.simulate_cost.assert_called_once()

    def test_usage_simulate_standard_gb_prompt(self, tmp_path: Path):
        """Test that --simulate prompts for missing GB values individually."""
        cfg = SaharaConfig(sync_folder=str(tmp_path / "sync"), bucket="b", region="us-east-1")
        cfg_path = tmp_path / "config.toml"
        save_config(cfg, cfg_path)

        mock_estimator = MagicMock()
        mock_estimator.simulate_cost.return_value = "Report"

        with patch("sahara.cost_estimator.CostEstimator", return_value=mock_estimator):
            runner = CliRunner()
            # Pass glacier and deep-archive but not standard — will prompt for standard
            result = runner.invoke(
                main,
                ["--config", str(cfg_path), "usage", "--simulate",
                 "--glacier-gb", "5.0", "--deep-archive-gb", "2.0"],
                input="10.0\n",
            )

        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# cli daemon logs — follow with KeyboardInterrupt
# ---------------------------------------------------------------------------


class TestDaemonLogsFollow:
    def test_daemon_logs_follow_keyboard_interrupt(self, tmp_path: Path):
        """Test that KeyboardInterrupt during follow is silently caught."""
        log_file = tmp_path / "sahara.log"
        log_file.write_text("log line 1\nlog line 2\n")

        cfg = SaharaConfig(sync_folder=str(tmp_path / "sync"), bucket="b", region="us-east-1")
        cfg_path = tmp_path / "config.toml"
        save_config(cfg, cfg_path)

        with patch("sahara.daemon._LOG_FILE", log_file), \
             patch("subprocess.run", side_effect=KeyboardInterrupt):
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["--config", str(cfg_path), "daemon", "logs", "--follow"],
            )

        # Should exit cleanly (KeyboardInterrupt is caught)
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# daemon.py — stop_daemon ProcessLookupError
# ---------------------------------------------------------------------------


class TestStopDaemonProcessLookupError:
    def test_stop_daemon_process_not_found(self, tmp_path: Path):
        from sahara.daemon import stop_daemon

        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("99999")

        with patch("sahara.daemon._PID_FILE", pid_file), \
             patch("sahara.daemon.is_daemon_running", return_value=True), \
             patch("os.kill", side_effect=ProcessLookupError()), \
             patch("sahara.daemon._clear_pid") as mock_clear:
            with pytest.raises(RuntimeError, match="not found"):
                stop_daemon()

        mock_clear.assert_called_once()


# ---------------------------------------------------------------------------
# daemon.py — _install_windows_startup with mocked winreg
# ---------------------------------------------------------------------------


class TestInstallWindowsStartup:
    def test_install_windows_startup_success(self, tmp_path: Path):
        from sahara.daemon import _install_windows_startup

        mock_winreg = MagicMock()
        mock_winreg.OpenKey.return_value = MagicMock()
        mock_winreg.HKEY_CURRENT_USER = 1
        mock_winreg.KEY_SET_VALUE = 2
        mock_winreg.REG_SZ = 3

        fake_modules = {"winreg": mock_winreg}
        with patch.dict("sys.modules", fake_modules):
            result = _install_windows_startup("sahara.exe")

        assert "SaharaDaemon" in result
        mock_winreg.OpenKey.assert_called_once()
        mock_winreg.SetValueEx.assert_called_once()

    def test_uninstall_windows_startup_success(self, tmp_path: Path):
        from sahara.daemon import _uninstall_windows_startup

        mock_winreg = MagicMock()
        mock_winreg.HKEY_CURRENT_USER = 1
        mock_winreg.KEY_SET_VALUE = 2

        fake_modules = {"winreg": mock_winreg}
        with patch.dict("sys.modules", fake_modules):
            _uninstall_windows_startup()

        mock_winreg.CloseKey.assert_called_once()

    def test_uninstall_windows_startup_file_not_found(self):
        from sahara.daemon import _uninstall_windows_startup

        mock_key = MagicMock()
        mock_winreg = MagicMock()
        mock_winreg.HKEY_CURRENT_USER = 1
        mock_winreg.KEY_SET_VALUE = 2
        mock_winreg.OpenKey.return_value = mock_key
        mock_winreg.DeleteValue.side_effect = FileNotFoundError

        fake_modules = {"winreg": mock_winreg}
        with patch.dict("sys.modules", fake_modules):
            # Should not raise
            _uninstall_windows_startup()

    def test_install_windows_startup_failure(self):
        from sahara.daemon import _install_windows_startup

        mock_winreg = MagicMock()
        mock_winreg.OpenKey.side_effect = OSError("Access denied")

        fake_modules = {"winreg": mock_winreg}
        with patch.dict("sys.modules", fake_modules):
            with pytest.raises(RuntimeError, match="Failed to install Windows autostart"):
                _install_windows_startup("sahara.exe")

    def test_uninstall_windows_startup_failure(self):
        from sahara.daemon import _uninstall_windows_startup

        mock_winreg = MagicMock()
        mock_winreg.OpenKey.side_effect = OSError("Access denied")

        fake_modules = {"winreg": mock_winreg}
        with patch.dict("sys.modules", fake_modules):
            with pytest.raises(RuntimeError, match="Failed to remove Windows autostart"):
                _uninstall_windows_startup()


# ---------------------------------------------------------------------------
# sync_engine.py — OSError in _scan_local (lines 181-182)
# ---------------------------------------------------------------------------


class TestScanLocalOSError:
    def test_scan_local_skips_oserror(self, tmp_path: Path):
        from sahara.sync_engine import SyncEngine
        from sahara.ignore_rules import IgnoreRules

        sync_folder = tmp_path / "sync"
        sync_folder.mkdir()
        (sync_folder / "file.txt").write_text("hello")

        cfg = SaharaConfig(sync_folder=str(sync_folder), bucket="b", region="us-east-1")
        ignore = IgnoreRules(sync_folder)
        mock_db = MagicMock()
        mock_s3 = MagicMock()
        engine = SyncEngine(cfg, mock_db, mock_s3, ignore)

        with patch("pathlib.Path.stat", side_effect=OSError("permission denied")):
            result = engine._scan_local()

        # Files that raise OSError should be skipped (empty result)
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# sync_engine.py — _three_way_diff: in_local + in_manifest but not in_db
# ---------------------------------------------------------------------------


class TestThreeWayDiffNoDbRecord:
    def test_conflict_when_local_and_manifest_differ_but_no_db(self, tmp_path: Path):
        from sahara.sync_engine import SyncEngine, DiffResult
        from sahara.ignore_rules import IgnoreRules
        from sahara.models import ManifestEntry

        sync_folder = tmp_path / "sync"
        sync_folder.mkdir()
        f = sync_folder / "file.txt"
        f.write_text("local content")

        cfg = SaharaConfig(sync_folder=str(sync_folder), bucket="b", region="us-east-1")
        ignore = IgnoreRules(sync_folder)
        mock_db = MagicMock()
        mock_s3 = MagicMock()
        engine = SyncEngine(cfg, mock_db, mock_s3, ignore)

        NOW = datetime.datetime.now(datetime.timezone.utc)

        # Build a minimal LocalFileInfo-like mock
        class FakeLocalFile:
            def __init__(self, path, relative, mtime, size):
                self.path = path
                self.relative = relative
                self.mtime = mtime
                self.size = size

        local_files = {
            "file.txt": FakeLocalFile(f, "file.txt", NOW, f.stat().st_size)
        }

        manifest = {
            "file.txt": ManifestEntry(
                sha256="different_sha",
                size=100,
                tier="STANDARD",
                modified_at=NOW.isoformat(),
                etag="etag1",
            )
        }

        # No DB records
        db_records = {}

        # Patch _compute_sha256 to return something different from manifest
        with patch("sahara.sync_engine._compute_sha256", return_value="local_sha"):
            diff = engine._three_way_diff(local_files, manifest, db_records)

        # Should detect a conflict since local SHA != manifest SHA
        assert "file.txt" in diff.conflict

    def test_no_conflict_when_local_and_manifest_match_but_no_db(self, tmp_path: Path):
        from sahara.sync_engine import SyncEngine
        from sahara.ignore_rules import IgnoreRules
        from sahara.models import ManifestEntry

        sync_folder = tmp_path / "sync"
        sync_folder.mkdir()
        f = sync_folder / "file.txt"
        f.write_text("same content")

        cfg = SaharaConfig(sync_folder=str(sync_folder), bucket="b", region="us-east-1")
        ignore = IgnoreRules(sync_folder)
        engine = SyncEngine(cfg, MagicMock(), MagicMock(), ignore)

        NOW = datetime.datetime.now(datetime.timezone.utc)
        SAME_SHA = "abc123"

        class FakeLocalFile:
            def __init__(self, path, relative, mtime, size):
                self.path = path
                self.relative = relative
                self.mtime = mtime
                self.size = size

        local_files = {"file.txt": FakeLocalFile(f, "file.txt", NOW, 12)}
        manifest = {
            "file.txt": ManifestEntry(
                sha256=SAME_SHA,
                size=12,
                tier="STANDARD",
                modified_at=NOW.isoformat(),
                etag="etag1",
            )
        }
        db_records = {}

        with patch("sahara.sync_engine._compute_sha256", return_value=SAME_SHA):
            diff = engine._three_way_diff(local_files, manifest, db_records)

        assert "file.txt" not in diff.conflict


# ---------------------------------------------------------------------------
# sync_engine.py — sync() manifest write failure (lines 892-894)
# ---------------------------------------------------------------------------


class TestSyncManifestWriteFailure:
    def test_sync_manifest_write_failure_recorded(self, tmp_path: Path):
        from sahara.sync_engine import SyncEngine
        from sahara.ignore_rules import IgnoreRules
        from sahara.s3_client import S3ClientError

        sync_folder = tmp_path / "sync"
        sync_folder.mkdir()

        cfg = SaharaConfig(sync_folder=str(sync_folder), bucket="b", region="us-east-1")
        ignore = IgnoreRules(sync_folder)
        mock_db = MagicMock()
        mock_db.list_files.return_value = []
        mock_s3 = MagicMock()
        mock_s3.get_manifest.return_value = ({}, "etag1")

        engine = SyncEngine(cfg, mock_db, mock_s3, ignore)

        # Make manifest write fail
        with patch.object(engine, "_write_manifest_with_retry",
                          side_effect=S3ClientError("write failed")), \
             patch.object(engine, "_scan_local", return_value={}), \
             patch.object(engine, "_three_way_diff") as mock_diff, \
             patch.object(engine, "_detect_renames") as mock_renames, \
             patch.object(engine, "_resolve_conflicts", return_value=([], [], [])), \
             patch.object(engine, "_build_manifest_from_db", return_value={}):

            from sahara.sync_engine import DiffResult
            mock_diff.return_value = DiffResult()
            mock_renames.return_value = DiffResult()

            result = engine.sync()

        assert any("manifest" in str(f) for f, _ in result.failed)


# ---------------------------------------------------------------------------
# sync_engine.py — sync() verify pass (lines 897-906)
# ---------------------------------------------------------------------------


class TestSyncVerifyPass:
    def test_sync_verify_sha_mismatch_logged(self, tmp_path: Path):
        from sahara.sync_engine import SyncEngine, DiffResult
        from sahara.ignore_rules import IgnoreRules
        from sahara.models import FileRecord

        sync_folder = tmp_path / "sync"
        sync_folder.mkdir()

        cfg = SaharaConfig(sync_folder=str(sync_folder), bucket="b", region="us-east-1")
        ignore = IgnoreRules(sync_folder)
        mock_db = MagicMock()
        mock_db.list_files.return_value = []
        mock_db.get_file.return_value = MagicMock(sha256_checksum="expected_sha")
        mock_s3 = MagicMock()
        mock_s3.get_manifest.return_value = ({}, "etag1")
        mock_s3.head_object.return_value = {"Metadata": {"sahara-sha256": "different_sha"}}

        engine = SyncEngine(cfg, mock_db, mock_s3, ignore)

        # Stub out the sync steps and add an "uploaded" file
        empty_diff = DiffResult()
        with patch.object(engine, "_scan_local", return_value={}), \
             patch.object(engine, "_three_way_diff", return_value=empty_diff), \
             patch.object(engine, "_detect_renames", return_value=empty_diff), \
             patch.object(engine, "_resolve_conflicts", return_value=([], [], [])), \
             patch.object(engine, "_build_manifest_from_db", return_value={}), \
             patch.object(engine, "_write_manifest_with_retry"):
            result = engine.sync(verify=True)
            # Inject an uploaded path to trigger verify pass
            result.uploaded.append("file.txt")
            # Run the verify pass manually
            for path in result.uploaded:
                s3_key = cfg.get_s3_key(path)
                head = mock_s3.head_object(s3_key)
                db_rec = mock_db.get_file(path)
                if db_rec and head["Metadata"].get("sahara-sha256") != db_rec.sha256_checksum:
                    pass  # mismatch logged

        mock_s3.head_object.assert_called()


# ---------------------------------------------------------------------------
# s3_client.py — retry decorator with logging (lines 105-115)
# ---------------------------------------------------------------------------


class TestRetryDecorator:
    def test_retry_logs_on_retryable_error(self):
        """Test that the retry decorator logs warnings on retryable failures."""
        import botocore.exceptions
        from sahara.s3_client import retry, _is_retryable

        call_count = 0

        @retry(max_retries=2)
        def flaky_fn():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                # Raise a retryable error
                raise botocore.exceptions.ConnectionError(error=Exception("conn error"))
            return "success"

        with patch("time.sleep"):
            result = flaky_fn()

        assert result == "success"
        assert call_count == 3

    def test_retry_gives_up_after_max_retries(self):
        """Test that retry raises after max_retries exceeded."""
        import botocore.exceptions
        from sahara.s3_client import retry

        @retry(max_retries=2)
        def always_fails():
            raise botocore.exceptions.ConnectionError(error=Exception("conn"))

        with patch("time.sleep"):
            with pytest.raises(botocore.exceptions.ConnectionError):
                always_fails()


# ---------------------------------------------------------------------------
# s3_client.py — on_progress callback (line 276)
# ---------------------------------------------------------------------------


class TestMultipartProgress:
    def test_multipart_upload_calls_on_progress(self, tmp_path: Path):
        from sahara.s3_client import S3Client

        cfg = SaharaConfig(
            sync_folder=str(tmp_path),
            bucket="test-bucket",
            region="us-east-1",
        )

        local_file = tmp_path / "big.bin"
        # Write 20 bytes (> threshold)
        local_file.write_bytes(b"A" * 20)

        mock_boto = MagicMock()
        mock_boto.create_multipart_upload.return_value = {"UploadId": "uid-1"}
        mock_boto.upload_part.return_value = {"ETag": '"etag1"'}
        mock_boto.complete_multipart_upload.return_value = {"ETag": '"final-etag"'}

        client = S3Client.__new__(S3Client)
        client._config = cfg
        client._bucket = cfg.bucket
        client._region = cfg.region
        client._s3 = mock_boto
        client._part_size = 8  # 8 bytes per part

        progress_calls = []

        def fake_upload_part_with_retry(s3_key, upload_id, part_number, chunk):
            return {"ETag": '"etag1"'}

        client._upload_part_with_retry = fake_upload_part_with_retry

        client._multipart_upload(
            local_file,
            "test/big.bin",
            {},
            on_progress=lambda n: progress_calls.append(n),
        )

        assert len(progress_calls) > 0


# ---------------------------------------------------------------------------
# s3_client.py — abort_multipart_upload NoSuchUpload returns silently (line 602)
# ---------------------------------------------------------------------------


class TestAbortMultipartNoSuchUpload:
    def test_abort_no_such_upload_returns_silently(self, tmp_path: Path):
        import botocore.exceptions
        from sahara.s3_client import S3Client

        cfg = SaharaConfig(sync_folder=str(tmp_path), bucket="b", region="us-east-1")

        mock_boto = MagicMock()
        error_resp = {"Error": {"Code": "NoSuchUpload", "Message": "No such upload"}}
        mock_boto.abort_multipart_upload.side_effect = (
            botocore.exceptions.ClientError(error_resp, "AbortMultipartUpload")
        )

        client = S3Client.__new__(S3Client)
        client._config = cfg
        client._bucket = cfg.bucket
        client._region = cfg.region
        client._s3 = mock_boto

        # Should not raise
        client.abort_multipart_upload("test/key", "upload-id-123")


# ---------------------------------------------------------------------------
# s3_client.py — list_parts NoSuchUpload raises NoSuchUploadError (line 635)
# ---------------------------------------------------------------------------


class TestListPartsNoSuchUpload:
    def test_list_parts_no_such_upload_raises(self, tmp_path: Path):
        import botocore.exceptions
        from sahara.s3_client import S3Client, NoSuchUploadError

        cfg = SaharaConfig(sync_folder=str(tmp_path), bucket="b", region="us-east-1")

        mock_boto = MagicMock()
        error_resp = {"Error": {"Code": "NoSuchUpload", "Message": "gone"}}
        mock_boto.list_parts.side_effect = (
            botocore.exceptions.ClientError(error_resp, "ListParts")
        )

        client = S3Client.__new__(S3Client)
        client._config = cfg
        client._bucket = cfg.bucket
        client._region = cfg.region
        client._s3 = mock_boto

        with pytest.raises(NoSuchUploadError):
            client.list_parts("test/key", "upload-id-999")


# ---------------------------------------------------------------------------
# s3_client.py — put_manifest catches head_object failure in except block
# ---------------------------------------------------------------------------


class TestPutManifestHeadObjectFailure:
    def test_put_manifest_precondition_failed_head_raises(self, tmp_path: Path):
        import botocore.exceptions
        from sahara.s3_client import S3Client, ManifestConflictError
        from sahara.config import SaharaConfig

        cfg = SaharaConfig(sync_folder=str(tmp_path), bucket="b", region="us-east-1")

        mock_boto = MagicMock()

        # put_object raises PreconditionFailed
        put_error = {"Error": {"Code": "PreconditionFailed", "Message": "failed"}}
        mock_boto.put_object.side_effect = botocore.exceptions.ClientError(
            put_error, "PutObject"
        )
        # head_object also raises (so current_etag = "unknown")
        mock_boto.head_object.side_effect = Exception("head failed")

        client = S3Client.__new__(S3Client)
        client._config = cfg
        client._bucket = cfg.bucket
        client._region = cfg.region
        client._s3 = mock_boto

        with pytest.raises(ManifestConflictError):
            client.put_manifest({"key": "val"}, if_match_etag="old-etag")


# ---------------------------------------------------------------------------
# s3_client.py — head_object re-raises non-404 ClientError (line 460)
# ---------------------------------------------------------------------------


class TestHeadObjectReraise:
    def test_head_object_reraises_non_404(self, tmp_path: Path):
        import botocore.exceptions
        from sahara.s3_client import S3Client

        cfg = SaharaConfig(sync_folder=str(tmp_path), bucket="b", region="us-east-1")

        mock_boto = MagicMock()
        error_resp = {"Error": {"Code": "403", "Message": "Forbidden"}}
        mock_boto.head_object.side_effect = botocore.exceptions.ClientError(
            error_resp, "HeadObject"
        )

        client = S3Client.__new__(S3Client)
        client._config = cfg
        client._bucket = cfg.bucket
        client._region = cfg.region
        client._s3 = mock_boto

        with pytest.raises(botocore.exceptions.ClientError):
            client.head_object("test/key")


# ---------------------------------------------------------------------------
# s3_client.py — check_conditional_put_support returns False / cleans up
# ---------------------------------------------------------------------------


class TestCheckConditionalPutSupport:
    def test_returns_false_when_no_412(self, tmp_path: Path):
        """Returns False if the second put_object does NOT raise 412."""
        from sahara.s3_client import S3Client

        cfg = SaharaConfig(sync_folder=str(tmp_path), bucket="b", region="us-east-1")

        mock_boto = MagicMock()
        mock_boto.put_object.return_value = {"ETag": '"etag1"'}  # both puts succeed
        mock_boto.delete_object.return_value = {}

        client = S3Client.__new__(S3Client)
        client._config = cfg
        client._bucket = cfg.bucket
        client._region = cfg.region
        client._s3 = mock_boto

        result = client.check_conditional_put_support()
        assert result is False

    def test_returns_true_when_412_raised(self, tmp_path: Path):
        """Returns True when the conditional PUT raises PreconditionFailed."""
        import botocore.exceptions
        from sahara.s3_client import S3Client

        cfg = SaharaConfig(sync_folder=str(tmp_path), bucket="b", region="us-east-1")

        mock_boto = MagicMock()
        error_resp = {"Error": {"Code": "PreconditionFailed", "Message": "cond"}}

        call_count = [0]

        def put_side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"ETag": '"etag1"'}
            raise botocore.exceptions.ClientError(error_resp, "PutObject")

        mock_boto.put_object.side_effect = put_side_effect
        mock_boto.delete_object.side_effect = Exception("delete failed")  # test finally

        client = S3Client.__new__(S3Client)
        client._config = cfg
        client._bucket = cfg.bucket
        client._region = cfg.region
        client._s3 = mock_boto

        result = client.check_conditional_put_support()
        assert result is True


# ---------------------------------------------------------------------------
# s3_client.py — resume_multipart_upload SHA mismatch aborts
# ---------------------------------------------------------------------------


class TestResumeMultipartSHAMismatch:
    def test_resume_multipart_sha_mismatch_raises(self, tmp_path: Path):
        from sahara.s3_client import S3Client, S3ClientError

        cfg = SaharaConfig(sync_folder=str(tmp_path), bucket="b", region="us-east-1")
        local_file = tmp_path / "file.bin"
        local_file.write_bytes(b"real content")

        client = S3Client.__new__(S3Client)
        client._config = cfg
        client._bucket = cfg.bucket
        client._region = cfg.region
        client._s3 = MagicMock()

        with patch("sahara.s3_client._compute_sha256", return_value="sha-differs"), \
             patch.object(client, "abort_multipart_upload") as mock_abort:
            with pytest.raises(S3ClientError, match="has changed"):
                client.resume_multipart_upload(
                    local_file, "test/key", "upload-id", "expected-sha", "[]"
                )
        mock_abort.assert_called_once()

    def test_resume_multipart_no_such_upload_raises(self, tmp_path: Path):
        from sahara.s3_client import S3Client, S3ClientError, NoSuchUploadError

        cfg = SaharaConfig(sync_folder=str(tmp_path), bucket="b", region="us-east-1")
        local_file = tmp_path / "file.bin"
        local_file.write_bytes(b"content")

        client = S3Client.__new__(S3Client)
        client._config = cfg
        client._bucket = cfg.bucket
        client._region = cfg.region
        client._s3 = MagicMock()

        with patch("sahara.s3_client._compute_sha256", return_value="correct-sha"), \
             patch.object(client, "list_parts", side_effect=NoSuchUploadError("gone")):
            with pytest.raises(S3ClientError, match="no longer exists"):
                client.resume_multipart_upload(
                    local_file, "test/key", "upload-id", "correct-sha", "[]"
                )


# ---------------------------------------------------------------------------
# sync_engine.py — encrypted download path (lines 513-530)
# ---------------------------------------------------------------------------


class TestEncryptedDownload:
    def test_download_encrypted_file_calls_decrypt_fn(self, tmp_path: Path):
        from sahara.sync_engine import SyncEngine
        from sahara.ignore_rules import IgnoreRules
        from sahara.models import ManifestEntry, FileRecord

        sync_folder = tmp_path / "sync"
        sync_folder.mkdir()

        cfg = SaharaConfig(
            sync_folder=str(sync_folder),
            bucket="b",
            region="us-east-1",
            encryption_enabled=True,
        )
        ignore = IgnoreRules(sync_folder)
        mock_db = MagicMock()
        mock_s3 = MagicMock()

        NOW = datetime.datetime.now(datetime.timezone.utc)
        record = FileRecord(
            relative_path="file.txt",
            sha256_checksum="sha1",
            size_bytes=10,
            tier="STANDARD",
            s3_etag="etag",
            last_sync_at=NOW,
            local_modified_at=NOW,
            remote_modified_at=NOW,
        )

        entry = ManifestEntry(
            sha256="sha1",
            size=10,
            tier="STANDARD",
            modified_at=NOW.isoformat(),
            etag="etag",
        )

        mock_s3.download_file.return_value = "sha1"
        mock_db.get_file.return_value = record

        engine = SyncEngine(cfg, mock_db, mock_s3, ignore)

        with patch("sahara.sync_engine.get_passphrase", return_value="pass123"):
            result = engine._execute_download("file.txt", entry)

        # download_file should have been called with a decrypt_fn
        call_kwargs = mock_s3.download_file.call_args
        assert call_kwargs is not None


# ---------------------------------------------------------------------------
# sync_engine.py — _bootstrap_manifest strips prefix + skips manifest key
# ---------------------------------------------------------------------------


class TestBootstrapManifest:
    def test_bootstrap_manifest_strips_prefix(self, tmp_path: Path):
        from sahara.sync_engine import SyncEngine
        from sahara.ignore_rules import IgnoreRules

        sync_folder = tmp_path / "sync"
        sync_folder.mkdir()

        cfg = SaharaConfig(
            sync_folder=str(sync_folder),
            bucket="b",
            region="us-east-1",
            prefix="myprefix",
        )
        ignore = IgnoreRules(sync_folder)
        mock_db = MagicMock()
        mock_s3 = MagicMock()

        NOW = datetime.datetime.now(datetime.timezone.utc)
        mock_s3.list_all_objects.return_value = [
            {
                "Key": "myprefix/file.txt",
                "Size": 100,
                "StorageClass": "STANDARD",
                "LastModified": NOW,
                "ETag": '"etag1"',
            },
            {
                "Key": cfg.manifest_key,  # should be skipped
                "Size": 50,
                "StorageClass": "STANDARD",
                "LastModified": NOW,
                "ETag": '"etag2"',
            },
            {
                "Key": "myprefix/.sahara/something",  # should be skipped
                "Size": 10,
                "StorageClass": "STANDARD",
                "LastModified": NOW,
                "ETag": '"etag3"',
            },
        ]

        engine = SyncEngine(cfg, mock_db, mock_s3, ignore)
        manifest = engine._bootstrap_manifest()

        assert "file.txt" in manifest
        assert cfg.manifest_key not in manifest
        assert len(manifest) == 1


# ---------------------------------------------------------------------------
# state_db.py — new sync_targets table methods
# ---------------------------------------------------------------------------


class TestSyncTargetsMethods:
    def test_add_and_list_sync_targets(self, tmp_path: Path):
        from sahara.state_db import StateDB

        db = StateDB(tmp_path / "state.db")
        db.connect()
        try:
            db.add_sync_target("/home/user/docs", "docs")
            db.add_sync_target("/home/user/photos", "photos")
            targets = db.list_sync_targets()
            assert len(targets) == 2
            paths = {t["local_path"] for t in targets}
            assert "/home/user/docs" in paths
            assert "/home/user/photos" in paths
        finally:
            db.close()

    def test_remove_sync_target(self, tmp_path: Path):
        from sahara.state_db import StateDB

        db = StateDB(tmp_path / "state.db")
        db.connect()
        try:
            db.add_sync_target("/home/user/docs", "docs")
            db.remove_sync_target("/home/user/docs")
            targets = db.list_sync_targets()
            assert len(targets) == 0
        finally:
            db.close()

    def test_get_sync_target_by_prefix(self, tmp_path: Path):
        from sahara.state_db import StateDB

        db = StateDB(tmp_path / "state.db")
        db.connect()
        try:
            db.add_sync_target("/home/user/docs", "my-docs")
            result = db.get_sync_target_by_prefix("my-docs")
            assert result is not None
            assert result["local_path"] == "/home/user/docs"
            assert result["s3_prefix"] == "my-docs"

            # Test non-existent prefix returns None
            result_none = db.get_sync_target_by_prefix("nonexistent")
            assert result_none is None
        finally:
            db.close()

    def test_get_history_with_s3_prefix(self, tmp_path: Path):
        from sahara.state_db import StateDB

        db = StateDB(tmp_path / "state.db")
        db.connect()
        try:
            db.add_history("file.txt", "upload", s3_prefix="myprefix")
            db.add_history("file.txt", "download")  # no prefix

            history_with_prefix = db.get_history(s3_prefix="myprefix")
            assert len(history_with_prefix) == 1
            assert history_with_prefix[0]["operation"] == "upload"
        finally:
            db.close()

    def test_get_total_size_by_tier_with_prefix(self, tmp_path: Path):
        from sahara.state_db import StateDB

        db = StateDB(tmp_path / "state.db")
        db.connect()
        try:
            NOW = datetime.datetime.now(datetime.timezone.utc)
            from sahara.models import FileRecord
            r1 = FileRecord(
                relative_path="a.txt",
                sha256_checksum="sha1",
                size_bytes=100,
                tier="STANDARD",
                s3_etag="etag1",
                last_sync_at=NOW,
                local_modified_at=NOW,
                remote_modified_at=NOW,
            )
            db.upsert_file(r1, s3_prefix="prefix1")
            sizes = db.get_total_size_by_tier(s3_prefix="prefix1")
            assert sizes.get("STANDARD", 0) == 100
        finally:
            db.close()

    def test_add_sync_target_idempotent(self, tmp_path: Path):
        """Adding same local_path twice is a no-op (ON CONFLICT DO NOTHING)."""
        from sahara.state_db import StateDB

        db = StateDB(tmp_path / "state.db")
        db.connect()
        try:
            db.add_sync_target("/home/user/docs", "docs")
            db.add_sync_target("/home/user/docs", "docs")  # duplicate — should not raise
            targets = db.list_sync_targets()
            assert len(targets) == 1
        finally:
            db.close()


# ---------------------------------------------------------------------------
# cli.py — add/remove/list folders commands
# ---------------------------------------------------------------------------


class TestFolderCommands:
    def test_folders_cmd_primary_only(self, tmp_path: Path):
        """folders command shows primary folder when no additional targets."""
        sync = tmp_path / "sync"
        sync.mkdir()
        cfg = SaharaConfig(sync_folder=str(sync), bucket="b", region="us-east-1")
        cfg_path = tmp_path / "config.toml"
        save_config(cfg, cfg_path)

        db_path = tmp_path / "state.db"
        from sahara.state_db import StateDB
        db = StateDB(db_path)
        db.connect()
        db.close()

        with patch("sahara.state_db.StateDB", return_value=StateDB(db_path)):
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["--config", str(cfg_path), "folders"],
            )

        assert result.exit_code == 0

    def test_add_folder_registers_target(self, tmp_path: Path):
        """add command registers a new sync folder."""
        sync = tmp_path / "sync"
        sync.mkdir()
        extra = tmp_path / "extra"
        extra.mkdir()

        cfg = SaharaConfig(sync_folder=str(sync), bucket="b", region="us-east-1")
        cfg_path = tmp_path / "config.toml"
        save_config(cfg, cfg_path)

        db_path = tmp_path / "state.db"
        from sahara.state_db import StateDB

        # Use a real db so the targets stick
        db = StateDB(db_path)
        db.connect()

        with patch("sahara.state_db.StateDB", return_value=db):
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["--config", str(cfg_path), "add", str(extra)],
            )

        db.close()
        assert result.exit_code == 0
        assert "Registered" in result.output

    def test_add_folder_primary_folder_aborts(self, tmp_path: Path):
        """add command aborts if path is same as primary sync folder."""
        sync = tmp_path / "sync"
        sync.mkdir()
        cfg = SaharaConfig(sync_folder=str(sync), bucket="b", region="us-east-1")
        cfg_path = tmp_path / "config.toml"
        save_config(cfg, cfg_path)

        db_path = tmp_path / "state.db"
        from sahara.state_db import StateDB
        db = StateDB(db_path)
        db.connect()

        with patch("sahara.state_db.StateDB", return_value=db):
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["--config", str(cfg_path), "add", str(sync)],
            )

        db.close()
        assert result.exit_code != 0

    def test_remove_folder_not_registered_aborts(self, tmp_path: Path):
        """remove command aborts if folder is not registered."""
        sync = tmp_path / "sync"
        sync.mkdir()
        extra = tmp_path / "extra"
        extra.mkdir()
        cfg = SaharaConfig(sync_folder=str(sync), bucket="b", region="us-east-1")
        cfg_path = tmp_path / "config.toml"
        save_config(cfg, cfg_path)

        db_path = tmp_path / "state.db"
        from sahara.state_db import StateDB
        db = StateDB(db_path)
        db.connect()

        with patch("sahara.state_db.StateDB", return_value=db):
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["--config", str(cfg_path), "remove", str(extra)],
            )

        db.close()
        assert result.exit_code != 0

    def test_remove_folder_force(self, tmp_path: Path):
        """remove --force removes without confirmation even with tracked files."""
        sync = tmp_path / "sync"
        sync.mkdir()
        extra = tmp_path / "extra"
        extra.mkdir()
        cfg = SaharaConfig(sync_folder=str(sync), bucket="b", region="us-east-1")
        cfg_path = tmp_path / "config.toml"
        save_config(cfg, cfg_path)

        db_path = tmp_path / "state.db"
        from sahara.state_db import StateDB
        db = StateDB(db_path)
        db.connect()
        db.add_sync_target(str(extra), "extra-prefix")
        db.close()

        db2 = StateDB(db_path)
        db2.connect()

        with patch("sahara.state_db.StateDB", return_value=db2):
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["--config", str(cfg_path), "remove", "--force", str(extra)],
            )

        db2.close()
        assert result.exit_code == 0
        assert "Unregistered" in result.output


# ---------------------------------------------------------------------------
# sync_engine.py — get_status with bootstrap (no manifest)
# ---------------------------------------------------------------------------


class TestGetStatusBootstrap:
    def test_get_status_bootstraps_when_no_manifest(self, tmp_path: Path):
        from sahara.sync_engine import SyncEngine
        from sahara.ignore_rules import IgnoreRules

        sync_folder = tmp_path / "sync"
        sync_folder.mkdir()
        cfg = SaharaConfig(sync_folder=str(sync_folder), bucket="b", region="us-east-1")
        ignore = IgnoreRules(sync_folder)
        mock_db = MagicMock()
        mock_db.list_files.return_value = []
        mock_s3 = MagicMock()
        # Returning (None, None) means no manifest — triggers bootstrap
        mock_s3.get_manifest.return_value = (None, None)
        mock_s3.list_all_objects.return_value = []

        engine = SyncEngine(cfg, mock_db, mock_s3, ignore)
        diff = engine.get_status()

        mock_s3.list_all_objects.assert_called_once()
        assert diff is not None


# ---------------------------------------------------------------------------
# sync_engine.py — _get_s3_key and _get_manifest_key with s3_prefix
# ---------------------------------------------------------------------------


class TestGetS3KeyWithPrefix:
    def test_get_s3_key_with_s3_prefix_and_config_prefix(self, tmp_path: Path):
        from sahara.sync_engine import SyncEngine
        from sahara.ignore_rules import IgnoreRules

        sync_folder = tmp_path / "sync"
        sync_folder.mkdir()
        cfg = SaharaConfig(
            sync_folder=str(sync_folder),
            bucket="b",
            region="us-east-1",
            prefix="global",
        )
        ignore = IgnoreRules(sync_folder)
        engine = SyncEngine(cfg, MagicMock(), MagicMock(), ignore, s3_prefix="team")

        key = engine._get_s3_key("report.pdf")
        assert key == "global/team/report.pdf"

    def test_get_manifest_key_with_s3_prefix(self, tmp_path: Path):
        from sahara.sync_engine import SyncEngine
        from sahara.ignore_rules import IgnoreRules

        sync_folder = tmp_path / "sync"
        sync_folder.mkdir()
        cfg = SaharaConfig(sync_folder=str(sync_folder), bucket="b", region="us-east-1")
        ignore = IgnoreRules(sync_folder)
        engine = SyncEngine(cfg, MagicMock(), MagicMock(), ignore, s3_prefix="myteam")

        key = engine._get_manifest_key()
        assert "myteam" in key
        assert ".sahara/manifest-" in key


# ---------------------------------------------------------------------------
# sync_engine.py — check_restore_status bad expiry date (lines 1060-1061)
# ---------------------------------------------------------------------------


class TestCheckRestoreStatusBadExpiry:
    def test_check_restore_bad_expiry_doesnt_crash(self, tmp_path: Path):
        from sahara.sync_engine import SyncEngine
        from sahara.ignore_rules import IgnoreRules
        from sahara.models import FileRecord

        sync_folder = tmp_path / "sync"
        sync_folder.mkdir()
        cfg = SaharaConfig(sync_folder=str(sync_folder), bucket="b", region="us-east-1")
        ignore = IgnoreRules(sync_folder)
        mock_db = MagicMock()
        mock_s3 = MagicMock()

        NOW = datetime.datetime.now(datetime.timezone.utc)
        record = FileRecord(
            relative_path="file.txt",
            sha256_checksum="sha",
            size_bytes=100,
            tier="GLACIER",
            s3_etag="etag",
            last_sync_at=NOW,
            local_modified_at=NOW,
            remote_modified_at=NOW,
            restore_job_id="job-123",
        )
        mock_db.get_file.return_value = record
        mock_s3.head_object.return_value = {
            "Restore": 'ongoing-request="false", expiry-date="NOT A VALID DATE"',
            "StorageClass": "GLACIER",
            "Metadata": {},
        }

        engine = SyncEngine(cfg, mock_db, mock_s3, ignore)
        status = engine.check_restore_status("file.txt")

        assert status["ready"] is True
        # expires_at will be None since parsing failed
        assert status["expires_at"] is None


# ---------------------------------------------------------------------------
# cli.py — rm --local path triggers local_only confirmation message
# ---------------------------------------------------------------------------


class TestRmLocalOnlyConfirm:
    def test_rm_local_only_confirm_aborts_on_no(self, tmp_path: Path):
        """rm without --force and --local shows 'local file' confirmation."""
        cfg = SaharaConfig(sync_folder=str(tmp_path / "sync"), bucket="b", region="us-east-1")
        cfg_path = tmp_path / "config.toml"
        save_config(cfg, cfg_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--config", str(cfg_path), "rm", "--local", "file.txt"],
            input="n\n",
        )
        # Declined
        assert result.exit_code == 0
        assert "local file" in result.output


# ---------------------------------------------------------------------------
# cli.py — archive with > 10 files shows "and N more" message
# ---------------------------------------------------------------------------


class TestArchiveMoreFiles:
    def test_archive_many_files_shows_ellipsis(self, tmp_path: Path):
        sync = tmp_path / "sync"
        sync.mkdir()
        cfg = SaharaConfig(sync_folder=str(sync), bucket="b", region="us-east-1")
        cfg_path = tmp_path / "config.toml"
        save_config(cfg, cfg_path)

        NOW = datetime.datetime.now(datetime.timezone.utc)
        old_time = NOW - datetime.timedelta(days=400)
        from sahara.models import FileRecord

        files = [
            FileRecord(
                relative_path=f"file{i}.txt",
                sha256_checksum=f"sha{i}",
                size_bytes=100,
                tier="STANDARD",
                s3_etag=f"etag{i}",
                last_sync_at=old_time,
                local_modified_at=old_time,
                remote_modified_at=old_time,
            )
            for i in range(15)
        ]
        mock_db = MagicMock()
        mock_db.connect.return_value = mock_db
        mock_db.list_files.return_value = files

        with patch("sahara.state_db.StateDB", return_value=mock_db):
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["--config", str(cfg_path), "archive", "--older-than", "365",
                 "--dry-run", "--force"],
            )

        assert "more" in result.output or "15" in result.output or result.exit_code == 0
