"""Unit tests for DistributedLock.

Validates that:
1. acquire() uses SET NX EX for atomic lock creation
2. release() uses Lua script to safely release only if token matches
3. extend() extends TTL only if we hold the lock
4. is_held() reflects local token state
5. acquire_blocking() retries and times out correctly
6. lock() context manager handles both wait=True and wait=False
7. TTL is always applied (prevents deadlocks)
8. Multiple locks can coexist independently
9. Edge cases: release without acquire, double acquire, token mismatch

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_distributed_lock.py -v
"""

import importlib
import importlib.util
import pathlib
import sys
import types
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Module loading setup: mock pylon.core.tools so the module can be loaded
# without the full pylon framework installed.
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

# Load the module under test
_spec = importlib.util.spec_from_file_location(
    "distributed_lock",
    _PLUGIN_ROOT / "utils" / "distributed_lock.py",
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["distributed_lock"] = _mod
_spec.loader.exec_module(_mod)

DistributedLock = _mod.DistributedLock
LockNotAcquired = _mod.LockNotAcquired
DEFAULT_TTL = _mod.DEFAULT_TTL
DEFAULT_WAIT_TIMEOUT = _mod.DEFAULT_WAIT_TIMEOUT
DEFAULT_POLL_INTERVAL = _mod.DEFAULT_POLL_INTERVAL


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis():
    """Create a mock Redis client with register_script support."""
    client = MagicMock()
    # register_script returns a callable script object
    client.register_script.return_value = MagicMock(return_value=1)
    return client


@pytest.fixture
def lock(mock_redis):
    """Create a DistributedLock instance with mocked Redis."""
    return DistributedLock(mock_redis)


@pytest.fixture
def lock_custom_prefix(mock_redis):
    """Create a DistributedLock with custom prefix."""
    return DistributedLock(mock_redis, key_prefix="myapp_lock")


# ---------------------------------------------------------------------------
# Tests: __init__
# ---------------------------------------------------------------------------

class TestInit:
    def test_registers_lua_scripts(self, mock_redis):
        DistributedLock(mock_redis)
        assert mock_redis.register_script.call_count == 2

    def test_default_prefix(self, lock):
        assert lock._prefix == "lock"

    def test_custom_prefix(self, lock_custom_prefix):
        assert lock_custom_prefix._prefix == "myapp_lock"

    def test_empty_tokens_on_init(self, lock):
        assert lock._tokens == {}


# ---------------------------------------------------------------------------
# Tests: _key
# ---------------------------------------------------------------------------

class TestKey:
    def test_key_format_default_prefix(self, lock):
        assert lock._key("my_resource") == "lock:my_resource"

    def test_key_format_custom_prefix(self, lock_custom_prefix):
        assert lock_custom_prefix._key("res") == "myapp_lock:res"

    def test_key_with_colons(self, lock):
        assert lock._key("user:123:create") == "lock:user:123:create"


# ---------------------------------------------------------------------------
# Tests: acquire
# ---------------------------------------------------------------------------

class TestAcquire:
    def test_acquire_success(self, lock, mock_redis):
        mock_redis.set.return_value = True
        result = lock.acquire("resource_a")
        assert result is True
        mock_redis.set.assert_called_once()
        args, kwargs = mock_redis.set.call_args
        assert args[0] == "lock:resource_a"
        assert kwargs == {"nx": True, "ex": DEFAULT_TTL}

    def test_acquire_stores_token(self, lock, mock_redis):
        mock_redis.set.return_value = True
        lock.acquire("resource_a")
        assert "resource_a" in lock._tokens
        # Token should be a UUID string
        token = lock._tokens["resource_a"]
        assert len(token) == 36  # UUID4 format
        assert "-" in token

    def test_acquire_failure_returns_false(self, lock, mock_redis):
        mock_redis.set.return_value = None  # Redis returns None when NX fails
        result = lock.acquire("resource_a")
        assert result is False

    def test_acquire_failure_no_token_stored(self, lock, mock_redis):
        mock_redis.set.return_value = None
        lock.acquire("resource_a")
        assert "resource_a" not in lock._tokens

    def test_acquire_custom_ttl(self, lock, mock_redis):
        mock_redis.set.return_value = True
        lock.acquire("resource_a", ttl=60)
        _, kwargs = mock_redis.set.call_args
        assert kwargs["ex"] == 60

    def test_acquire_generates_unique_tokens(self, lock, mock_redis):
        mock_redis.set.return_value = True
        lock.acquire("res_1")
        lock.acquire("res_2")
        assert lock._tokens["res_1"] != lock._tokens["res_2"]

    def test_acquire_passes_token_as_value(self, lock, mock_redis):
        mock_redis.set.return_value = True
        lock.acquire("resource_a")
        args, _ = mock_redis.set.call_args
        assert args[1] == lock._tokens["resource_a"]


# ---------------------------------------------------------------------------
# Tests: release
# ---------------------------------------------------------------------------

class TestRelease:
    def test_release_success(self, lock, mock_redis):
        mock_redis.set.return_value = True
        lock.acquire("resource_a")
        token = lock._tokens["resource_a"]

        release_script = mock_redis.register_script.return_value
        release_script.return_value = 1

        result = lock.release("resource_a")
        assert result is True
        release_script.assert_called_with(keys=["lock:resource_a"], args=[token])

    def test_release_removes_local_token(self, lock, mock_redis):
        mock_redis.set.return_value = True
        lock.acquire("resource_a")
        lock.release("resource_a")
        assert "resource_a" not in lock._tokens

    def test_release_without_acquire_returns_false(self, lock, mock_redis):
        result = lock.release("never_acquired")
        assert result is False

    def test_release_token_mismatch_returns_false(self, lock, mock_redis):
        mock_redis.set.return_value = True
        lock.acquire("resource_a")

        # Simulate Lua script returning 0 (token didn't match)
        release_script = mock_redis.register_script.return_value
        release_script.return_value = 0

        result = lock.release("resource_a")
        assert result is False

    def test_release_clears_token_even_on_failure(self, lock, mock_redis):
        mock_redis.set.return_value = True
        lock.acquire("resource_a")

        release_script = mock_redis.register_script.return_value
        release_script.return_value = 0

        lock.release("resource_a")
        assert "resource_a" not in lock._tokens


# ---------------------------------------------------------------------------
# Tests: extend
# ---------------------------------------------------------------------------

class TestExtend:
    def test_extend_success(self, lock, mock_redis):
        mock_redis.set.return_value = True
        lock.acquire("resource_a")
        token = lock._tokens["resource_a"]

        # The extend script is the second registered script
        scripts = [c[0][0] for c in mock_redis.register_script.call_args_list]
        extend_script = mock_redis.register_script.return_value
        extend_script.return_value = 1

        result = lock.extend("resource_a", 5000)
        assert result is True
        extend_script.assert_called_with(keys=["lock:resource_a"], args=[token, "5000"])

    def test_extend_without_token_returns_false(self, lock, mock_redis):
        result = lock.extend("not_held", 5000)
        assert result is False

    def test_extend_lua_returns_zero(self, lock, mock_redis):
        mock_redis.set.return_value = True
        lock.acquire("resource_a")

        extend_script = mock_redis.register_script.return_value
        extend_script.return_value = 0

        result = lock.extend("resource_a", 5000)
        assert result is False


# ---------------------------------------------------------------------------
# Tests: is_held
# ---------------------------------------------------------------------------

class TestIsHeld:
    def test_not_held_initially(self, lock):
        assert lock.is_held("resource_a") is False

    def test_held_after_acquire(self, lock, mock_redis):
        mock_redis.set.return_value = True
        lock.acquire("resource_a")
        assert lock.is_held("resource_a") is True

    def test_not_held_after_release(self, lock, mock_redis):
        mock_redis.set.return_value = True
        lock.acquire("resource_a")
        lock.release("resource_a")
        assert lock.is_held("resource_a") is False

    def test_independent_lock_names(self, lock, mock_redis):
        mock_redis.set.return_value = True
        lock.acquire("resource_a")
        assert lock.is_held("resource_a") is True
        assert lock.is_held("resource_b") is False


# ---------------------------------------------------------------------------
# Tests: acquire_blocking
# ---------------------------------------------------------------------------

class TestAcquireBlocking:
    def test_immediate_acquisition(self, lock, mock_redis):
        mock_redis.set.return_value = True
        result = lock.acquire_blocking("resource_a")
        assert result is True

    @patch.object(_mod, "time")
    def test_retries_and_succeeds(self, mock_time, lock, mock_redis):
        mock_time.time.side_effect = [0.0, 0.0, 0.1, 0.1, 0.2]
        mock_time.sleep = MagicMock()
        # First two attempts fail, third succeeds
        mock_redis.set.side_effect = [None, None, True]

        result = lock.acquire_blocking("resource_a", ttl=30, wait_timeout=5)
        assert result is True
        assert mock_redis.set.call_count == 3
        assert mock_time.sleep.call_count == 2

    @patch.object(_mod, "time")
    def test_timeout_raises_exception(self, mock_time, lock, mock_redis):
        mock_time.time.side_effect = [0.0, 0.0, 5.0, 5.0, 10.1]
        mock_time.sleep = MagicMock()
        mock_redis.set.return_value = None

        with pytest.raises(LockNotAcquired, match="Could not acquire lock 'res'"):
            lock.acquire_blocking("res", wait_timeout=10, poll_interval=5.0)

    @patch.object(_mod, "time")
    def test_custom_poll_interval(self, mock_time, lock, mock_redis):
        mock_time.time.side_effect = [0.0, 0.0, 0.5]
        mock_time.sleep = MagicMock()
        mock_redis.set.side_effect = [None, True]

        lock.acquire_blocking("res", poll_interval=0.5)
        mock_time.sleep.assert_called_with(0.5)


# ---------------------------------------------------------------------------
# Tests: lock (context manager, wait=False)
# ---------------------------------------------------------------------------

class TestLockContextManagerNoWait:
    def test_acquired_yields_true(self, lock, mock_redis):
        mock_redis.set.return_value = True
        with lock.lock("resource_a") as acquired:
            assert acquired is True

    def test_not_acquired_yields_false(self, lock, mock_redis):
        mock_redis.set.return_value = None
        with lock.lock("resource_a") as acquired:
            assert acquired is False

    def test_releases_on_exit_when_acquired(self, lock, mock_redis):
        mock_redis.set.return_value = True
        with lock.lock("resource_a"):
            assert lock.is_held("resource_a") is True
        assert lock.is_held("resource_a") is False

    def test_no_release_when_not_acquired(self, lock, mock_redis):
        mock_redis.set.return_value = None
        release_script = mock_redis.register_script.return_value
        with lock.lock("resource_a"):
            pass
        # release script should not be called since we didn't acquire
        # (release() was never called because no token)
        assert "resource_a" not in lock._tokens

    def test_releases_on_exception(self, lock, mock_redis):
        mock_redis.set.return_value = True
        try:
            with lock.lock("resource_a"):
                raise ValueError("boom")
        except ValueError:
            pass
        assert lock.is_held("resource_a") is False

    def test_custom_ttl(self, lock, mock_redis):
        mock_redis.set.return_value = True
        with lock.lock("resource_a", ttl=120):
            pass
        _, kwargs = mock_redis.set.call_args
        assert kwargs["ex"] == 120


# ---------------------------------------------------------------------------
# Tests: lock (context manager, wait=True)
# ---------------------------------------------------------------------------

class TestLockContextManagerWait:
    def test_acquired_yields_true(self, lock, mock_redis):
        mock_redis.set.return_value = True
        with lock.lock("resource_a", wait=True) as acquired:
            assert acquired is True

    @patch.object(_mod, "time")
    def test_timeout_raises_in_context(self, mock_time, lock, mock_redis):
        mock_time.time.side_effect = [0.0, 0.0, 11.0]
        mock_time.sleep = MagicMock()
        mock_redis.set.return_value = None

        with pytest.raises(LockNotAcquired):
            with lock.lock("resource_a", wait=True, wait_timeout=10):
                pass  # pragma: no cover

    def test_releases_on_exit_when_wait_acquired(self, lock, mock_redis):
        mock_redis.set.return_value = True
        with lock.lock("resource_a", wait=True):
            assert lock.is_held("resource_a") is True
        assert lock.is_held("resource_a") is False

    def test_releases_on_exception_in_wait_mode(self, lock, mock_redis):
        mock_redis.set.return_value = True
        try:
            with lock.lock("resource_a", wait=True):
                raise RuntimeError("error")
        except RuntimeError:
            pass
        assert lock.is_held("resource_a") is False


# ---------------------------------------------------------------------------
# Tests: Multiple locks
# ---------------------------------------------------------------------------

class TestMultipleLocks:
    def test_independent_locks(self, lock, mock_redis):
        mock_redis.set.return_value = True
        lock.acquire("lock_a")
        lock.acquire("lock_b")
        assert lock.is_held("lock_a") is True
        assert lock.is_held("lock_b") is True

    def test_release_one_keeps_other(self, lock, mock_redis):
        mock_redis.set.return_value = True
        lock.acquire("lock_a")
        lock.acquire("lock_b")
        lock.release("lock_a")
        assert lock.is_held("lock_a") is False
        assert lock.is_held("lock_b") is True

    def test_different_tokens_per_lock(self, lock, mock_redis):
        mock_redis.set.return_value = True
        lock.acquire("lock_a")
        lock.acquire("lock_b")
        assert lock._tokens["lock_a"] != lock._tokens["lock_b"]


# ---------------------------------------------------------------------------
# Tests: Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_default_ttl(self):
        assert DEFAULT_TTL == 30

    def test_default_wait_timeout(self):
        assert DEFAULT_WAIT_TIMEOUT == 10

    def test_default_poll_interval(self):
        assert DEFAULT_POLL_INTERVAL == 0.1

    def test_lock_not_acquired_is_exception(self):
        assert issubclass(LockNotAcquired, Exception)


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_acquire_same_name_overwrites_token(self, lock, mock_redis):
        mock_redis.set.return_value = True
        lock.acquire("resource_a")
        token1 = lock._tokens["resource_a"]
        lock.acquire("resource_a")
        token2 = lock._tokens["resource_a"]
        assert token1 != token2

    def test_release_nonexistent_is_safe(self, lock):
        result = lock.release("does_not_exist")
        assert result is False

    def test_lock_name_with_special_chars(self, lock, mock_redis):
        mock_redis.set.return_value = True
        name = "conversation_create:user_42:chat_abc-123"
        lock.acquire(name)
        args, _ = mock_redis.set.call_args
        assert args[0] == f"lock:{name}"
