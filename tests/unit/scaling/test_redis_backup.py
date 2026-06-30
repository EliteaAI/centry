"""Unit tests for redis_backup module.

Validates that:
1. RedisBackupManager initializes with valid arguments
2. Invalid arguments raise ValueError
3. backup() triggers BGSAVE and waits for completion
4. backup() uploads RDB to S3 with correct key format
5. backup() handles "already in progress" BGSAVE gracefully
6. backup() raises BackupTimeoutError on slow BGSAVE
7. backup() raises RedisBackupError on lastsave failure
8. backup() raises RedisBackupError on upload failure
9. backup() reads RDB from correct path (CONFIG GET dir)
10. backup() handles FileNotFoundError for RDB
11. backup() handles PermissionError for RDB
12. restore() downloads RDB from S3
13. restore() raises RestoreError on download failure
14. restore() resolves timestamp to S3 key correctly
15. list_backups() returns sorted list from S3
16. list_backups() handles empty bucket
17. list_backups() filters non-RDB files
18. list_backups() handles S3 errors gracefully
19. delete_old_backups() removes expired backups
20. delete_old_backups() keeps recent backups
21. delete_old_backups() handles deletion errors gracefully
22. _lastsave_to_epoch handles datetime objects
23. _lastsave_to_epoch handles integer values
24. _resolve_backup_key handles various input formats
25. Properties return correct values
26. backup() uses default data dir on config_get failure
27. BGSAVE poll retries on transient Redis errors
28. backup() logs progress correctly
29. restore() with full S3 key (prefix included)
30. Integration: backup + list + restore cycle

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_redis_backup.py -v
"""

import importlib
import importlib.util
import pathlib
import sys
import time
import types
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call, mock_open

import pytest

# ---------------------------------------------------------------------------
# Module loading setup
# ---------------------------------------------------------------------------

_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[3] / "pylon_main" / "plugins" / "elitea_core"
_SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[4] / "elitea_core"

_mock_log = MagicMock()
_mock_pylon_core_tools = MagicMock()
_mock_pylon_core_tools.log = _mock_log
sys.modules.setdefault("pylon", MagicMock())
sys.modules.setdefault("pylon.core", MagicMock())
sys.modules.setdefault("pylon.core.tools", _mock_pylon_core_tools)

_utils_pkg = types.ModuleType("centry.pylon_main.plugins.elitea_core.utils")
_utils_pkg.__path__ = [str(_PLUGIN_ROOT / "utils")]
_utils_pkg.__package__ = "centry.pylon_main.plugins.elitea_core.utils"
sys.modules.setdefault("centry.pylon_main.plugins.elitea_core.utils", _utils_pkg)

_plugin_pkg = types.ModuleType("centry.pylon_main.plugins.elitea_core")
_plugin_pkg.__path__ = [str(_PLUGIN_ROOT)]
_plugin_pkg.__package__ = "centry.pylon_main.plugins.elitea_core"
_plugin_pkg.utils = _utils_pkg
sys.modules.setdefault("centry.pylon_main.plugins.elitea_core", _plugin_pkg)


def _load_module(module_name, file_name):
    """Load a module from the source elitea_core/utils directory."""
    spec = importlib.util.spec_from_file_location(
        f"centry.pylon_main.plugins.elitea_core.utils.{module_name}",
        _SOURCE_ROOT / "utils" / file_name,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


redis_backup_mod = _load_module("redis_backup", "redis_backup.py")

RedisBackupManager = redis_backup_mod.RedisBackupManager
RedisBackupError = redis_backup_mod.RedisBackupError
BackupTimeoutError = redis_backup_mod.BackupTimeoutError
RestoreError = redis_backup_mod.RestoreError
DEFAULT_BUCKET = redis_backup_mod.DEFAULT_BUCKET
DEFAULT_PREFIX = redis_backup_mod.DEFAULT_PREFIX
BGSAVE_MAX_WAIT = redis_backup_mod.BGSAVE_MAX_WAIT
BACKUP_RETENTION_DAYS = redis_backup_mod.BACKUP_RETENTION_DAYS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis():
    """Create a mock Redis client."""
    client = MagicMock()
    client.lastsave.return_value = 1719741600  # epoch
    client.bgsave.return_value = True
    client.config_get.side_effect = lambda key: {
        "dir": {"dir": "/data"},
        "dbfilename": {"dbfilename": "dump.rdb"},
    }.get(key, {})
    return client


@pytest.fixture
def mock_s3():
    """Create a mock S3 client."""
    client = MagicMock()
    client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}
    client.get_object.return_value = {
        "Body": MagicMock(read=MagicMock(return_value=b"REDIS0011...")),
    }
    client.list_objects_v2.return_value = {"Contents": []}
    client.delete_object.return_value = {}
    return client


@pytest.fixture
def rdb_data():
    """Sample RDB file content."""
    return b"REDIS0011\xfa\x09redis-ver\x067.0.0" + b"\x00" * 100


@pytest.fixture
def manager(mock_redis, mock_s3):
    """Create a RedisBackupManager instance."""
    return RedisBackupManager(
        redis_client=mock_redis,
        s3_client=mock_s3,
        bucket="test-backups",
        prefix="redis/",
        bgsave_timeout=5.0,
        poll_interval=0.01,
    )


# ---------------------------------------------------------------------------
# Initialization tests
# ---------------------------------------------------------------------------

class TestInit:
    """Tests for RedisBackupManager initialization."""

    def test_valid_init(self, mock_redis, mock_s3):
        mgr = RedisBackupManager(mock_redis, mock_s3)
        assert mgr.bucket == DEFAULT_BUCKET
        assert mgr.prefix == DEFAULT_PREFIX

    def test_custom_bucket_and_prefix(self, mock_redis, mock_s3):
        mgr = RedisBackupManager(mock_redis, mock_s3, bucket="my-bucket", prefix="backups/redis")
        assert mgr.bucket == "my-bucket"
        assert mgr.prefix == "backups/redis/"

    def test_prefix_trailing_slash_normalization(self, mock_redis, mock_s3):
        mgr = RedisBackupManager(mock_redis, mock_s3, prefix="foo/bar/")
        assert mgr.prefix == "foo/bar/"

    def test_empty_prefix(self, mock_redis, mock_s3):
        mgr = RedisBackupManager(mock_redis, mock_s3, prefix="")
        assert mgr.prefix == ""

    def test_none_redis_client_raises(self, mock_s3):
        with pytest.raises(ValueError, match="redis_client must not be None"):
            RedisBackupManager(None, mock_s3)

    def test_none_s3_client_raises(self, mock_redis):
        with pytest.raises(ValueError, match="s3_client must not be None"):
            RedisBackupManager(mock_redis, None)

    def test_empty_bucket_raises(self, mock_redis, mock_s3):
        with pytest.raises(ValueError, match="bucket must be non-empty"):
            RedisBackupManager(mock_redis, mock_s3, bucket="")

    def test_custom_timeouts(self, mock_redis, mock_s3):
        mgr = RedisBackupManager(
            mock_redis, mock_s3,
            bgsave_timeout=600.0,
            poll_interval=2.0,
        )
        assert mgr._bgsave_timeout == 600.0
        assert mgr._poll_interval == 2.0


# ---------------------------------------------------------------------------
# Backup tests
# ---------------------------------------------------------------------------

class TestBackup:
    """Tests for the backup() method."""

    def test_successful_backup(self, manager, mock_redis, mock_s3, rdb_data):
        mock_redis.lastsave.side_effect = [1719741600, 1719741700]

        with patch("builtins.open", mock_open(read_data=rdb_data)):
            result = manager.backup()

        assert "timestamp" in result
        assert "key" in result
        assert "size_bytes" in result
        assert result["key"].startswith("redis/")
        assert result["key"].endswith(".rdb")
        assert result["size_bytes"] == len(rdb_data)
        mock_redis.bgsave.assert_called_once()
        mock_s3.put_object.assert_called_once()

    def test_backup_key_format(self, manager, mock_redis, mock_s3, rdb_data):
        mock_redis.lastsave.side_effect = [1719741600, 1719741700]

        with patch("builtins.open", mock_open(read_data=rdb_data)):
            result = manager.backup()

        key = result["key"]
        assert key.startswith("redis/")
        filename = key.split("/")[-1]
        assert filename.endswith(".rdb")
        assert "T" in filename
        assert filename.endswith("Z.rdb")

    def test_backup_bgsave_already_in_progress(self, manager, mock_redis, mock_s3, rdb_data):
        mock_redis.bgsave.side_effect = Exception("Background save already in progress")
        mock_redis.lastsave.side_effect = [1719741600, 1719741700]

        with patch("builtins.open", mock_open(read_data=rdb_data)):
            result = manager.backup()

        assert result["size_bytes"] == len(rdb_data)

    def test_backup_bgsave_failure(self, manager, mock_redis):
        mock_redis.bgsave.side_effect = Exception("Redis OOM")

        with pytest.raises(RedisBackupError, match="BGSAVE command failed"):
            manager.backup()

    def test_backup_initial_lastsave_failure(self, manager, mock_redis):
        mock_redis.lastsave.side_effect = Exception("Connection refused")

        with pytest.raises(RedisBackupError, match="Failed to get initial lastsave"):
            manager.backup()

    def test_backup_timeout(self, mock_redis, mock_s3):
        mgr = RedisBackupManager(
            mock_redis, mock_s3,
            bucket="test",
            bgsave_timeout=0.05,
            poll_interval=0.01,
        )
        mock_redis.lastsave.return_value = 1719741600

        with pytest.raises(BackupTimeoutError, match="did not complete within"):
            mgr.backup()

    def test_backup_upload_failure(self, manager, mock_redis, mock_s3, rdb_data):
        mock_redis.lastsave.side_effect = [1719741600, 1719741700]
        mock_s3.put_object.side_effect = Exception("S3 connection timeout")

        with patch("builtins.open", mock_open(read_data=rdb_data)):
            with pytest.raises(RedisBackupError, match="Failed to upload RDB to S3"):
                manager.backup()

    def test_backup_rdb_file_not_found(self, manager, mock_redis):
        mock_redis.lastsave.side_effect = [1719741600, 1719741700]

        with patch("builtins.open", side_effect=FileNotFoundError("No such file")):
            with pytest.raises(RedisBackupError, match="RDB file not found"):
                manager.backup()

    def test_backup_rdb_permission_error(self, manager, mock_redis):
        mock_redis.lastsave.side_effect = [1719741600, 1719741700]

        with patch("builtins.open", side_effect=PermissionError("Access denied")):
            with pytest.raises(RedisBackupError, match="Permission denied"):
                manager.backup()

    def test_backup_rdb_generic_error(self, manager, mock_redis):
        mock_redis.lastsave.side_effect = [1719741600, 1719741700]

        with patch("builtins.open", side_effect=IOError("Disk error")):
            with pytest.raises(RedisBackupError, match="Failed to read RDB file"):
                manager.backup()

    def test_backup_config_get_dir_failure_uses_default(self, manager, mock_redis, mock_s3, rdb_data):
        mock_redis.config_get.side_effect = Exception("CONFIG disabled")
        mock_redis.lastsave.side_effect = [1719741600, 1719741700]

        with patch("builtins.open", mock_open(read_data=rdb_data)) as mocked_open:
            result = manager.backup()

        mocked_open.assert_called_once_with("/data/dump.rdb", "rb")
        assert result["size_bytes"] == len(rdb_data)

    def test_backup_with_datetime_lastsave(self, manager, mock_redis, mock_s3, rdb_data):
        dt1 = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
        dt2 = datetime(2026, 6, 30, 12, 5, 0, tzinfo=timezone.utc)
        mock_redis.lastsave.side_effect = [dt1, dt2]

        with patch("builtins.open", mock_open(read_data=rdb_data)):
            result = manager.backup()

        assert "20260630T120500Z" in result["key"]

    def test_backup_polls_multiple_times(self, mock_redis, mock_s3, rdb_data):
        mgr = RedisBackupManager(
            mock_redis, mock_s3,
            bucket="test",
            bgsave_timeout=5.0,
            poll_interval=0.01,
        )
        mock_redis.lastsave.side_effect = [
            1719741600,  # initial
            1719741600,  # poll 1 - not advanced
            1719741600,  # poll 2 - not advanced
            1719741700,  # poll 3 - advanced
        ]

        with patch("builtins.open", mock_open(read_data=rdb_data)):
            result = mgr.backup()

        assert result["size_bytes"] == len(rdb_data)
        assert mock_redis.lastsave.call_count == 4

    def test_backup_poll_transient_redis_error(self, mock_redis, mock_s3, rdb_data):
        mgr = RedisBackupManager(
            mock_redis, mock_s3,
            bucket="test",
            bgsave_timeout=5.0,
            poll_interval=0.01,
        )
        mock_redis.lastsave.side_effect = [
            1719741600,  # initial
            Exception("Temporary connection issue"),  # poll 1 - error
            1719741700,  # poll 2 - advanced
        ]

        with patch("builtins.open", mock_open(read_data=rdb_data)):
            result = mgr.backup()

        assert result["size_bytes"] == len(rdb_data)

    def test_backup_s3_put_object_params(self, manager, mock_redis, mock_s3, rdb_data):
        mock_redis.lastsave.side_effect = [1719741600, 1719741700]

        with patch("builtins.open", mock_open(read_data=rdb_data)):
            manager.backup()

        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "test-backups"
        assert call_kwargs["Key"].startswith("redis/")
        assert call_kwargs["Body"] == rdb_data
        assert call_kwargs["ContentType"] == "application/octet-stream"


# ---------------------------------------------------------------------------
# Restore tests
# ---------------------------------------------------------------------------

class TestRestore:
    """Tests for the restore() method."""

    def test_successful_restore(self, manager, mock_s3):
        rdb_content = b"REDIS0011..."
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=rdb_content)),
        }

        result = manager.restore("20260630T120000Z")

        assert result["key"] == "redis/20260630T120000Z.rdb"
        assert result["size_bytes"] == len(rdb_content)
        assert result["data"] == rdb_content

    def test_restore_with_full_key(self, manager, mock_s3):
        rdb_content = b"data"
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=rdb_content)),
        }

        result = manager.restore("redis/20260630T120000Z.rdb")

        assert result["key"] == "redis/20260630T120000Z.rdb"
        mock_s3.get_object.assert_called_with(
            Bucket="test-backups", Key="redis/20260630T120000Z.rdb"
        )

    def test_restore_with_iso_timestamp(self, manager, mock_s3):
        rdb_content = b"data"
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=rdb_content)),
        }

        result = manager.restore("2026-06-30T12:00:00Z")

        assert result["key"] == "redis/20260630T120000Z.rdb"

    def test_restore_download_failure(self, manager, mock_s3):
        mock_s3.get_object.side_effect = Exception("NoSuchKey")

        with pytest.raises(RestoreError, match="Failed to download backup"):
            manager.restore("20260630T120000Z")

    def test_restore_with_filename_only(self, manager, mock_s3):
        rdb_content = b"data"
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=rdb_content)),
        }

        result = manager.restore("20260630T120000Z.rdb")

        assert result["key"] == "redis/20260630T120000Z.rdb"


# ---------------------------------------------------------------------------
# List backups tests
# ---------------------------------------------------------------------------

class TestListBackups:
    """Tests for the list_backups() method."""

    def test_list_empty_bucket(self, manager, mock_s3):
        mock_s3.list_objects_v2.return_value = {"Contents": []}
        result = manager.list_backups()
        assert result == []

    def test_list_no_contents_key(self, manager, mock_s3):
        mock_s3.list_objects_v2.return_value = {}
        result = manager.list_backups()
        assert result == []

    def test_list_multiple_backups_sorted(self, manager, mock_s3):
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "redis/20260628T060000Z.rdb", "Size": 1000, "LastModified": datetime(2026, 6, 28)},
                {"Key": "redis/20260630T120000Z.rdb", "Size": 2000, "LastModified": datetime(2026, 6, 30)},
                {"Key": "redis/20260629T090000Z.rdb", "Size": 1500, "LastModified": datetime(2026, 6, 29)},
            ]
        }

        result = manager.list_backups()

        assert len(result) == 3
        assert result[0]["key"] == "redis/20260630T120000Z.rdb"
        assert result[1]["key"] == "redis/20260629T090000Z.rdb"
        assert result[2]["key"] == "redis/20260628T060000Z.rdb"

    def test_list_filters_non_rdb_files(self, manager, mock_s3):
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "redis/20260630T120000Z.rdb", "Size": 2000, "LastModified": datetime(2026, 6, 30)},
                {"Key": "redis/README.md", "Size": 100, "LastModified": datetime(2026, 6, 30)},
                {"Key": "redis/.metadata.json", "Size": 50, "LastModified": datetime(2026, 6, 30)},
            ]
        }

        result = manager.list_backups()

        assert len(result) == 1
        assert result[0]["key"] == "redis/20260630T120000Z.rdb"

    def test_list_respects_limit(self, manager, mock_s3):
        contents = [
            {"Key": f"redis/2026063{i}T120000Z.rdb", "Size": 1000, "LastModified": datetime(2026, 6, 20 + i)}
            for i in range(5)
        ]
        mock_s3.list_objects_v2.return_value = {"Contents": contents}

        result = manager.list_backups(limit=2)

        assert len(result) == 2

    def test_list_s3_error_returns_empty(self, manager, mock_s3):
        mock_s3.list_objects_v2.side_effect = Exception("Access denied")

        result = manager.list_backups()

        assert result == []

    def test_list_calls_s3_with_correct_params(self, manager, mock_s3):
        mock_s3.list_objects_v2.return_value = {"Contents": []}
        manager.list_backups(limit=50)

        mock_s3.list_objects_v2.assert_called_with(
            Bucket="test-backups",
            Prefix="redis/",
            MaxKeys=50,
        )


# ---------------------------------------------------------------------------
# Delete old backups tests
# ---------------------------------------------------------------------------

class TestDeleteOldBackups:
    """Tests for the delete_old_backups() method."""

    def test_delete_old_backups_removes_expired(self, manager, mock_s3):
        old_date = datetime.now(tz=timezone.utc) - timedelta(days=10)
        recent_date = datetime.now(tz=timezone.utc) - timedelta(days=1)

        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "redis/old.rdb", "Size": 1000, "LastModified": old_date},
                {"Key": "redis/recent.rdb", "Size": 2000, "LastModified": recent_date},
            ]
        }

        deleted = manager.delete_old_backups(retention_days=7)

        assert deleted == 1
        mock_s3.delete_object.assert_called_once_with(
            Bucket="test-backups", Key="redis/old.rdb"
        )

    def test_delete_old_backups_keeps_recent(self, manager, mock_s3):
        recent_date = datetime.now(tz=timezone.utc) - timedelta(days=1)

        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "redis/recent1.rdb", "Size": 1000, "LastModified": recent_date},
                {"Key": "redis/recent2.rdb", "Size": 2000, "LastModified": recent_date},
            ]
        }

        deleted = manager.delete_old_backups(retention_days=7)

        assert deleted == 0
        mock_s3.delete_object.assert_not_called()

    def test_delete_old_backups_handles_delete_error(self, manager, mock_s3):
        old_date = datetime.now(tz=timezone.utc) - timedelta(days=10)

        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "redis/old1.rdb", "Size": 1000, "LastModified": old_date},
                {"Key": "redis/old2.rdb", "Size": 1000, "LastModified": old_date},
            ]
        }
        mock_s3.delete_object.side_effect = [Exception("Access denied"), None]

        deleted = manager.delete_old_backups(retention_days=7)

        assert deleted == 1
        assert mock_s3.delete_object.call_count == 2

    def test_delete_old_backups_no_last_modified(self, manager, mock_s3):
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "redis/unknown.rdb", "Size": 1000, "LastModified": None},
            ]
        }

        deleted = manager.delete_old_backups(retention_days=7)

        assert deleted == 0
        mock_s3.delete_object.assert_not_called()

    def test_delete_old_backups_empty_list(self, manager, mock_s3):
        mock_s3.list_objects_v2.return_value = {"Contents": []}

        deleted = manager.delete_old_backups()

        assert deleted == 0

    def test_delete_old_backups_with_epoch_last_modified(self, manager, mock_s3):
        old_epoch = (datetime.now(tz=timezone.utc) - timedelta(days=10)).timestamp()

        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "redis/old.rdb", "Size": 1000, "LastModified": old_epoch},
            ]
        }

        deleted = manager.delete_old_backups(retention_days=7)

        assert deleted == 1


# ---------------------------------------------------------------------------
# Internal method tests
# ---------------------------------------------------------------------------

class TestInternalMethods:
    """Tests for internal helper methods."""

    def test_lastsave_to_epoch_with_datetime(self, manager):
        dt = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
        result = manager._lastsave_to_epoch(dt)
        assert result == int(dt.timestamp())

    def test_lastsave_to_epoch_with_int(self, manager):
        result = manager._lastsave_to_epoch(1719741600)
        assert result == 1719741600

    def test_lastsave_to_epoch_with_float(self, manager):
        result = manager._lastsave_to_epoch(1719741600.5)
        assert result == 1719741600

    def test_lastsave_advanced_true(self, manager):
        assert manager._lastsave_advanced(1719741600, 1719741700)

    def test_lastsave_advanced_false_same(self, manager):
        assert not manager._lastsave_advanced(1719741600, 1719741600)

    def test_lastsave_advanced_false_earlier(self, manager):
        assert not manager._lastsave_advanced(1719741700, 1719741600)

    def test_resolve_backup_key_timestamp_only(self, manager):
        result = manager._resolve_backup_key("20260630T120000Z")
        assert result == "redis/20260630T120000Z.rdb"

    def test_resolve_backup_key_with_prefix(self, manager):
        result = manager._resolve_backup_key("redis/20260630T120000Z.rdb")
        assert result == "redis/20260630T120000Z.rdb"

    def test_resolve_backup_key_iso_format(self, manager):
        result = manager._resolve_backup_key("2026-06-30T12:00:00")
        assert result == "redis/20260630T120000Z.rdb"

    def test_resolve_backup_key_with_rdb_extension(self, manager):
        result = manager._resolve_backup_key("20260630T120000Z.rdb")
        assert result == "redis/20260630T120000Z.rdb"

    def test_resolve_backup_key_iso_with_z(self, manager):
        result = manager._resolve_backup_key("2026-06-30T12:00:00Z")
        assert result == "redis/20260630T120000Z.rdb"


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

class TestIntegration:
    """Integration-style tests combining multiple operations."""

    def test_backup_list_restore_cycle(self, manager, mock_redis, mock_s3, rdb_data):
        mock_redis.lastsave.side_effect = [1719741600, 1719741700]

        with patch("builtins.open", mock_open(read_data=rdb_data)):
            backup_result = manager.backup()

        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {
                    "Key": backup_result["key"],
                    "Size": backup_result["size_bytes"],
                    "LastModified": datetime.now(tz=timezone.utc),
                }
            ]
        }
        listed = manager.list_backups()
        assert len(listed) == 1
        assert listed[0]["key"] == backup_result["key"]

        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=rdb_data)),
        }
        restore_result = manager.restore(backup_result["key"])
        assert restore_result["data"] == rdb_data

    def test_backup_and_cleanup(self, manager, mock_redis, mock_s3, rdb_data):
        mock_redis.lastsave.side_effect = [1719741600, 1719741700]

        with patch("builtins.open", mock_open(read_data=rdb_data)):
            manager.backup()

        old_date = datetime.now(tz=timezone.utc) - timedelta(days=10)
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "redis/20260620T000000Z.rdb", "Size": 500, "LastModified": old_date},
                {"Key": "redis/20260630T120000Z.rdb", "Size": 2000,
                 "LastModified": datetime.now(tz=timezone.utc)},
            ]
        }

        deleted = manager.delete_old_backups(retention_days=7)
        assert deleted == 1

    def test_multiple_backups_different_timestamps(self, mock_redis, mock_s3, rdb_data):
        mgr = RedisBackupManager(
            mock_redis, mock_s3,
            bucket="test-backups",
            prefix="redis/",
            bgsave_timeout=5.0,
            poll_interval=0.01,
        )

        mock_redis.lastsave.side_effect = [1719741600, 1719741700]
        with patch("builtins.open", mock_open(read_data=rdb_data)):
            result1 = mgr.backup()

        mock_redis.lastsave.side_effect = [1719741700, 1719741800]
        with patch("builtins.open", mock_open(read_data=rdb_data)):
            result2 = mgr.backup()

        assert result1["key"] != result2["key"]
        assert result1["timestamp"] != result2["timestamp"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge case tests."""

    def test_empty_rdb_file(self, manager, mock_redis, mock_s3):
        mock_redis.lastsave.side_effect = [1719741600, 1719741700]

        with patch("builtins.open", mock_open(read_data=b"")):
            result = manager.backup()

        assert result["size_bytes"] == 0

    def test_large_rdb_file(self, manager, mock_redis, mock_s3):
        large_data = b"X" * (100 * 1024 * 1024)  # 100MB
        mock_redis.lastsave.side_effect = [1719741600, 1719741700]

        with patch("builtins.open", mock_open(read_data=large_data)):
            result = manager.backup()

        assert result["size_bytes"] == 100 * 1024 * 1024

    def test_backup_with_non_standard_dir(self, manager, mock_redis, mock_s3, rdb_data):
        mock_redis.config_get.side_effect = lambda key: {
            "dir": {"dir": "/var/lib/redis"},
            "dbfilename": {"dbfilename": "my_dump.rdb"},
        }.get(key, {})
        mock_redis.lastsave.side_effect = [1719741600, 1719741700]

        with patch("builtins.open", mock_open(read_data=rdb_data)) as mocked_open:
            manager.backup()

        mocked_open.assert_called_once_with("/var/lib/redis/my_dump.rdb", "rb")

    def test_restore_empty_data(self, manager, mock_s3):
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=b"")),
        }

        result = manager.restore("20260630T120000Z")

        assert result["size_bytes"] == 0
        assert result["data"] == b""

    def test_delete_old_backups_custom_retention(self, manager, mock_s3):
        old_date = datetime.now(tz=timezone.utc) - timedelta(days=3)

        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "redis/old.rdb", "Size": 1000, "LastModified": old_date},
            ]
        }

        deleted = manager.delete_old_backups(retention_days=2)
        assert deleted == 1

        mock_s3.delete_object.reset_mock()
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "redis/old.rdb", "Size": 1000, "LastModified": old_date},
            ]
        }
        deleted = manager.delete_old_backups(retention_days=5)
        assert deleted == 0

    def test_backup_returns_iso_timestamp(self, manager, mock_redis, mock_s3, rdb_data):
        dt = datetime(2026, 6, 30, 14, 30, 0, tzinfo=timezone.utc)
        mock_redis.lastsave.side_effect = [
            1719741600,
            dt,
        ]

        with patch("builtins.open", mock_open(read_data=rdb_data)):
            result = manager.backup()

        assert "2026-06-30" in result["timestamp"]
        assert "14:30:00" in result["timestamp"]
