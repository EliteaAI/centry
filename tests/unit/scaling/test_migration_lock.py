"""Unit tests for migration_lock module.

Validates that:
1. Advisory lock acquisition succeeds on first try
2. Advisory lock acquisition retries on failure until timeout
3. Lock release is always called on success and failure
4. MigrationLockTimeout raised when timeout exceeded
5. MigrationLockError handling for unexpected failures
6. run_migrations_with_lock integrates lock + migration call
7. Logging occurs at each stage (acquire, retry, release)
8. Engine/connection lifecycle managed correctly
9. Default parameters have correct values
10. Poll interval is respected during retries

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_migration_lock.py -v
"""

import importlib
import importlib.util
import pathlib
import sys
import time
import types
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Module loading setup
# ---------------------------------------------------------------------------

_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[3] / "pylon_main" / "plugins" / "elitea_core"

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
sys.modules.setdefault("centry.pylon_main.plugins.elitea_core", _plugin_pkg)

_module_path = _PLUGIN_ROOT / "utils" / "migration_lock.py"
_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.utils.migration_lock",
    _module_path,
    submodule_search_locations=[],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

migration_lock_ctx = _mod.migration_lock
MigrationLockTimeout = _mod.MigrationLockTimeout
MigrationLockError = _mod.MigrationLockError
run_migrations_with_lock = _mod.run_migrations_with_lock
_acquire_lock = _mod._acquire_lock
_release_lock = _mod._release_lock
DEFAULT_LOCK_ID = _mod.DEFAULT_LOCK_ID
DEFAULT_TIMEOUT_SECONDS = _mod.DEFAULT_TIMEOUT_SECONDS
DEFAULT_POLL_INTERVAL = _mod.DEFAULT_POLL_INTERVAL


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_engine():
    """Create a mock SQLAlchemy engine."""
    engine = MagicMock()
    connection = MagicMock()
    engine.connect.return_value = connection
    return engine, connection


@pytest.fixture
def mock_scalar_result():
    """Create a mock execute().scalar() chain."""
    result = MagicMock()
    result.scalar.return_value = True
    return result


# ---------------------------------------------------------------------------
# Tests: Default constants
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_lock_id(self):
        assert DEFAULT_LOCK_ID == 900100

    def test_default_timeout(self):
        assert DEFAULT_TIMEOUT_SECONDS == 600

    def test_default_poll_interval(self):
        assert DEFAULT_POLL_INTERVAL == 2.0


# ---------------------------------------------------------------------------
# Tests: _acquire_lock
# ---------------------------------------------------------------------------


class TestAcquireLock:
    def test_acquire_first_attempt_success(self, mock_engine):
        _, connection = mock_engine
        result_mock = MagicMock()
        result_mock.scalar.return_value = True
        connection.execute.return_value = result_mock

        acquired = _acquire_lock(connection, 900100, 10, 1.0)

        assert acquired is True
        connection.execute.assert_called_once()

    def test_acquire_after_retries(self, mock_engine):
        _, connection = mock_engine
        fail_result = MagicMock()
        fail_result.scalar.return_value = False
        success_result = MagicMock()
        success_result.scalar.return_value = True

        connection.execute.side_effect = [fail_result, fail_result, success_result]

        with patch.object(_mod, "time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.0, 2.0, 2.0, 4.0, 4.0]
            mock_time.sleep = MagicMock()

            acquired = _acquire_lock(connection, 900100, 10, 2.0)

        assert acquired is True
        assert connection.execute.call_count == 3
        assert mock_time.sleep.call_count == 2

    def test_acquire_timeout_raises(self, mock_engine):
        _, connection = mock_engine
        fail_result = MagicMock()
        fail_result.scalar.return_value = False
        connection.execute.return_value = fail_result

        with patch.object(_mod, "time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.0, 5.0, 5.0]
            mock_time.sleep = MagicMock()

            with pytest.raises(MigrationLockTimeout) as exc_info:
                _acquire_lock(connection, 900100, 5, 2.0)

        assert "900100" in str(exc_info.value)
        assert "5s" in str(exc_info.value)

    def test_acquire_timeout_at_boundary(self, mock_engine):
        """When elapsed + poll_interval > timeout, should raise immediately."""
        _, connection = mock_engine
        fail_result = MagicMock()
        fail_result.scalar.return_value = False
        connection.execute.return_value = fail_result

        with patch.object(_mod, "time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.0, 9.5, 9.5]
            mock_time.sleep = MagicMock()

            with pytest.raises(MigrationLockTimeout):
                _acquire_lock(connection, 900100, 10, 2.0)

    def test_acquire_uses_correct_sql(self, mock_engine):
        _, connection = mock_engine
        result_mock = MagicMock()
        result_mock.scalar.return_value = True
        connection.execute.return_value = result_mock

        _acquire_lock(connection, 12345, 10, 1.0)

        call_args = connection.execute.call_args
        sql_text = str(call_args[0][0].text)
        assert "pg_try_advisory_lock" in sql_text
        params = call_args[0][1]
        assert params["lock_id"] == 12345

    def test_acquire_logs_on_retry(self, mock_engine):
        _, connection = mock_engine
        fail_result = MagicMock()
        fail_result.scalar.return_value = False
        success_result = MagicMock()
        success_result.scalar.return_value = True
        connection.execute.side_effect = [fail_result, success_result]

        with patch.object(_mod, "time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.0, 1.0, 1.0]
            mock_time.sleep = MagicMock()

            _acquire_lock(connection, 900100, 10, 1.0)

        _mock_log.info.assert_any_call(
            "Migration lock %d held by another process, retrying in %.1fs "
            "(attempt %d, %.1fs elapsed)",
            900100, 1.0, 1, 0.0,
        )

    def test_acquire_logs_on_success(self, mock_engine):
        _, connection = mock_engine
        result_mock = MagicMock()
        result_mock.scalar.return_value = True
        connection.execute.return_value = result_mock

        with patch.object(_mod, "time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.5]

            _acquire_lock(connection, 900100, 10, 1.0)

        _mock_log.info.assert_any_call(
            "Migration lock %d acquired after %.1fs (%d attempts)",
            900100, 0.5, 1,
        )


# ---------------------------------------------------------------------------
# Tests: _release_lock
# ---------------------------------------------------------------------------


class TestReleaseLock:
    def test_release_success(self, mock_engine):
        _, connection = mock_engine
        result_mock = MagicMock()
        result_mock.scalar.return_value = True
        connection.execute.return_value = result_mock

        _release_lock(connection, 900100)

        call_args = connection.execute.call_args
        sql_text = str(call_args[0][0].text)
        assert "pg_advisory_unlock" in sql_text

    def test_release_returns_false_logs_warning(self, mock_engine):
        _, connection = mock_engine
        result_mock = MagicMock()
        result_mock.scalar.return_value = False
        connection.execute.return_value = result_mock

        _release_lock(connection, 900100)

        _mock_log.warning.assert_called_once_with(
            "Migration lock %d release returned False (lock not held?)", 900100
        )

    def test_release_exception_logs_error(self, mock_engine):
        _, connection = mock_engine
        connection.execute.side_effect = RuntimeError("connection lost")

        _release_lock(connection, 900100)

        _mock_log.error.assert_called_once()
        assert "900100" in str(_mock_log.error.call_args)

    def test_release_uses_correct_lock_id(self, mock_engine):
        _, connection = mock_engine
        result_mock = MagicMock()
        result_mock.scalar.return_value = True
        connection.execute.return_value = result_mock

        _release_lock(connection, 99999)

        call_args = connection.execute.call_args
        params = call_args[0][1]
        assert params["lock_id"] == 99999


# ---------------------------------------------------------------------------
# Tests: migration_lock context manager
# ---------------------------------------------------------------------------


class TestMigrationLockContextManager:
    @patch("sqlalchemy.create_engine")
    def test_context_manager_acquires_and_releases(self, mock_create_engine):
        engine = MagicMock()
        connection = MagicMock()
        engine.connect.return_value = connection
        mock_create_engine.return_value = engine

        result_mock = MagicMock()
        result_mock.scalar.return_value = True
        connection.execute.return_value = result_mock

        with patch.object(_mod, "time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.1]

            with migration_lock_ctx("postgresql://test/db") as conn:
                assert conn is connection

        assert connection.execute.call_count == 2
        connection.close.assert_called_once()
        engine.dispose.assert_called_once()

    @patch("sqlalchemy.create_engine")
    def test_context_manager_releases_on_exception(self, mock_create_engine):
        engine = MagicMock()
        connection = MagicMock()
        engine.connect.return_value = connection
        mock_create_engine.return_value = engine

        acquire_result = MagicMock()
        acquire_result.scalar.return_value = True
        release_result = MagicMock()
        release_result.scalar.return_value = True
        connection.execute.side_effect = [acquire_result, release_result]

        with patch.object(_mod, "time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.1]

            with pytest.raises(ValueError):
                with migration_lock_ctx("postgresql://test/db") as conn:
                    raise ValueError("something went wrong")

        assert connection.execute.call_count == 2
        connection.close.assert_called_once()
        engine.dispose.assert_called_once()

    @patch("sqlalchemy.create_engine")
    def test_context_manager_no_release_if_not_acquired(self, mock_create_engine):
        engine = MagicMock()
        connection = MagicMock()
        engine.connect.return_value = connection
        mock_create_engine.return_value = engine

        fail_result = MagicMock()
        fail_result.scalar.return_value = False
        connection.execute.return_value = fail_result

        with patch.object(_mod, "time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.0, 11.0, 11.0]
            mock_time.sleep = MagicMock()

            with pytest.raises(MigrationLockTimeout):
                with migration_lock_ctx("postgresql://test/db", timeout_seconds=10, poll_interval=2.0):
                    pass

        connection.close.assert_called_once()
        engine.dispose.assert_called_once()

    @patch("sqlalchemy.create_engine")
    def test_context_manager_uses_null_pool(self, mock_create_engine):
        engine = MagicMock()
        connection = MagicMock()
        engine.connect.return_value = connection
        mock_create_engine.return_value = engine

        result_mock = MagicMock()
        result_mock.scalar.return_value = True
        connection.execute.return_value = result_mock

        with patch.object(_mod, "time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.1]

            with migration_lock_ctx("postgresql://test/db"):
                pass

        mock_create_engine.assert_called_once_with(
            "postgresql://test/db",
            poolclass=_mod.sqlalchemy.pool.NullPool,
        )

    @patch("sqlalchemy.create_engine")
    def test_context_manager_custom_parameters(self, mock_create_engine):
        engine = MagicMock()
        connection = MagicMock()
        engine.connect.return_value = connection
        mock_create_engine.return_value = engine

        result_mock = MagicMock()
        result_mock.scalar.return_value = True
        connection.execute.return_value = result_mock

        with patch.object(_mod, "time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.05]

            with migration_lock_ctx(
                "postgresql://host/db",
                lock_id=55555,
                timeout_seconds=30,
                poll_interval=0.5,
            ):
                pass

        call_args = connection.execute.call_args_list[0]
        params = call_args[0][1]
        assert params["lock_id"] == 55555


# ---------------------------------------------------------------------------
# Tests: run_migrations_with_lock
# ---------------------------------------------------------------------------


class TestRunMigrationsWithLock:
    @patch("sqlalchemy.create_engine")
    def test_runs_migrations_with_lock(self, mock_create_engine):
        engine = MagicMock()
        connection = MagicMock()
        engine.connect.return_value = connection
        mock_create_engine.return_value = engine

        acquire_result = MagicMock()
        acquire_result.scalar.return_value = True
        release_result = MagicMock()
        release_result.scalar.return_value = True
        connection.execute.side_effect = [acquire_result, release_result]

        mock_module = MagicMock()
        mock_module.descriptor.name = "test_plugin"

        mock_db_migrations = MagicMock()

        with patch.object(_mod, "time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.1]

            with patch.dict(sys.modules, {"tools": MagicMock(db_migrations=mock_db_migrations)}):
                result = run_migrations_with_lock(
                    mock_module,
                    "postgresql://test/db",
                    lock_id=900100,
                    timeout_seconds=60,
                    payload={"key": "value"},
                )

        assert result is True
        mock_db_migrations.run_db_migrations.assert_called_once_with(
            mock_module, "postgresql://test/db", payload={"key": "value"}
        )

    @patch("sqlalchemy.create_engine")
    def test_raises_timeout_without_running_migrations(self, mock_create_engine):
        engine = MagicMock()
        connection = MagicMock()
        engine.connect.return_value = connection
        mock_create_engine.return_value = engine

        fail_result = MagicMock()
        fail_result.scalar.return_value = False
        connection.execute.return_value = fail_result

        mock_module = MagicMock()
        mock_module.descriptor.name = "test_plugin"

        mock_db_migrations = MagicMock()

        with patch.object(_mod, "time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.0, 6.0, 6.0]
            mock_time.sleep = MagicMock()

            with patch.dict(sys.modules, {"tools": MagicMock(db_migrations=mock_db_migrations)}):
                with pytest.raises(MigrationLockTimeout):
                    run_migrations_with_lock(
                        mock_module,
                        "postgresql://test/db",
                        timeout_seconds=5,
                        poll_interval=2.0,
                    )

        mock_db_migrations.run_db_migrations.assert_not_called()

    @patch("sqlalchemy.create_engine")
    def test_passes_additional_kwargs(self, mock_create_engine):
        engine = MagicMock()
        connection = MagicMock()
        engine.connect.return_value = connection
        mock_create_engine.return_value = engine

        acquire_result = MagicMock()
        acquire_result.scalar.return_value = True
        release_result = MagicMock()
        release_result.scalar.return_value = True
        connection.execute.side_effect = [acquire_result, release_result]

        mock_module = MagicMock()
        mock_module.descriptor.name = "auth_core"

        mock_db_migrations = MagicMock()

        with patch.object(_mod, "time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.1]

            with patch.dict(sys.modules, {"tools": MagicMock(db_migrations=mock_db_migrations)}):
                run_migrations_with_lock(
                    mock_module,
                    "postgresql://test/db",
                    migrations_path="plugins.auth_core:db/migrations",
                    version_table="db_version__auth_core",
                    revision="abc123",
                )

        mock_db_migrations.run_db_migrations.assert_called_once_with(
            mock_module,
            "postgresql://test/db",
            migrations_path="plugins.auth_core:db/migrations",
            version_table="db_version__auth_core",
            revision="abc123",
        )

    @patch("sqlalchemy.create_engine")
    def test_logs_module_name(self, mock_create_engine):
        engine = MagicMock()
        connection = MagicMock()
        engine.connect.return_value = connection
        mock_create_engine.return_value = engine

        acquire_result = MagicMock()
        acquire_result.scalar.return_value = True
        release_result = MagicMock()
        release_result.scalar.return_value = True
        connection.execute.side_effect = [acquire_result, release_result]

        mock_module = MagicMock()
        mock_module.descriptor.name = "my_plugin"

        mock_db_migrations = MagicMock()

        with patch.object(_mod, "time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.1]

            with patch.dict(sys.modules, {"tools": MagicMock(db_migrations=mock_db_migrations)}):
                run_migrations_with_lock(mock_module, "postgresql://test/db")

        _mock_log.info.assert_any_call(
            "Attempting to acquire migration lock %d for %s",
            900100, "my_plugin",
        )

    @patch("sqlalchemy.create_engine")
    def test_lock_released_on_migration_failure(self, mock_create_engine):
        engine = MagicMock()
        connection = MagicMock()
        engine.connect.return_value = connection
        mock_create_engine.return_value = engine

        acquire_result = MagicMock()
        acquire_result.scalar.return_value = True
        release_result = MagicMock()
        release_result.scalar.return_value = True
        connection.execute.side_effect = [acquire_result, release_result]

        mock_module = MagicMock()
        mock_module.descriptor.name = "bad_plugin"

        mock_db_migrations = MagicMock()
        mock_db_migrations.run_db_migrations.side_effect = RuntimeError("migration failed")

        with patch.object(_mod, "time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.1]

            with patch.dict(sys.modules, {"tools": MagicMock(db_migrations=mock_db_migrations)}):
                with pytest.raises(RuntimeError, match="migration failed"):
                    run_migrations_with_lock(mock_module, "postgresql://test/db")

        assert connection.execute.call_count == 2
        connection.close.assert_called_once()
        engine.dispose.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: Exception classes
# ---------------------------------------------------------------------------


class TestExceptions:
    def test_migration_lock_timeout_is_exception(self):
        assert issubclass(MigrationLockTimeout, Exception)

    def test_migration_lock_timeout_message(self):
        exc = MigrationLockTimeout("test message")
        assert str(exc) == "test message"

    def test_migration_lock_error_is_exception(self):
        assert issubclass(MigrationLockError, Exception)

    def test_migration_lock_error_message(self):
        exc = MigrationLockError("test error")
        assert str(exc) == "test error"


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_acquire_with_zero_timeout_tries_once(self, mock_engine):
        _, connection = mock_engine
        fail_result = MagicMock()
        fail_result.scalar.return_value = False
        connection.execute.return_value = fail_result

        with patch.object(_mod, "time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.0]

            with pytest.raises(MigrationLockTimeout):
                _acquire_lock(connection, 900100, 0, 1.0)

        assert connection.execute.call_count == 1

    @patch("sqlalchemy.create_engine")
    def test_connection_close_called_even_on_dispose_error(self, mock_create_engine):
        engine = MagicMock()
        connection = MagicMock()
        engine.connect.return_value = connection
        engine.dispose.side_effect = RuntimeError("dispose error")
        mock_create_engine.return_value = engine

        result_mock = MagicMock()
        result_mock.scalar.return_value = True
        connection.execute.return_value = result_mock

        with patch.object(_mod, "time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.1]

            with pytest.raises(RuntimeError, match="dispose error"):
                with migration_lock_ctx("postgresql://test/db"):
                    pass

        connection.close.assert_called_once()

    def test_module_without_descriptor_uses_str(self):
        """run_migrations_with_lock handles modules without descriptor gracefully."""
        mock_module = "plain_string_module"

        with patch("sqlalchemy.create_engine") as mock_create_engine:
            engine = MagicMock()
            connection = MagicMock()
            engine.connect.return_value = connection
            mock_create_engine.return_value = engine

            acquire_result = MagicMock()
            acquire_result.scalar.return_value = True
            release_result = MagicMock()
            release_result.scalar.return_value = True
            connection.execute.side_effect = [acquire_result, release_result]

            mock_db_migrations = MagicMock()

            with patch.object(_mod, "time") as mock_time:
                mock_time.time.side_effect = [0.0, 0.1]

                with patch.dict(sys.modules, {"tools": MagicMock(db_migrations=mock_db_migrations)}):
                    run_migrations_with_lock(mock_module, "postgresql://test/db")

            _mock_log.info.assert_any_call(
                "Attempting to acquire migration lock %d for %s",
                900100, "plain_string_module",
            )
