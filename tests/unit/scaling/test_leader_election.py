"""Unit tests for leader_election module.

Validates that:
1. LeaderElection acquires leadership via SET NX EX
2. Refresh extends TTL only if token matches (Lua script)
3. Leadership lost when refresh fails (token mismatch)
4. Release only deletes if token matches (Lua script)
5. Background loop acquires and refreshes correctly
6. stop() releases leadership and joins thread
7. on_acquired/on_lost callbacks fire correctly
8. leader_only decorator skips non-leaders
9. leader_only decorator runs on leader
10. get_current_leader returns current token
11. Invalid config raises ValueError
12. Election tolerates Redis errors without crashing
13. Multiple instances: only one becomes leader
14. Token uniqueness per instance
15. Callback errors don't crash the election loop

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_leader_election.py -v
"""

import importlib
import importlib.util
import pathlib
import sys
import threading
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
_plugin_pkg.utils = _utils_pkg
sys.modules.setdefault("centry.pylon_main.plugins.elitea_core", _plugin_pkg)


def _load_module(module_name, file_name):
    """Load a module from the plugin utils directory."""
    spec = importlib.util.spec_from_file_location(
        f"centry.pylon_main.plugins.elitea_core.utils.{module_name}",
        _PLUGIN_ROOT / "utils" / file_name,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


leader_election_mod = _load_module("leader_election", "leader_election.py")

LeaderElection = leader_election_mod.LeaderElection
leader_only = leader_election_mod.leader_only
DEFAULT_TTL = leader_election_mod.DEFAULT_TTL
DEFAULT_REFRESH_INTERVAL = leader_election_mod.DEFAULT_REFRESH_INTERVAL
LEADER_KEY_PREFIX = leader_election_mod.LEADER_KEY_PREFIX


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class FakeScript:
    """Simulates a Redis Lua script registered via register_script."""

    def __init__(self, behavior=None):
        self._behavior = behavior or (lambda keys, args: 1)

    def __call__(self, keys=None, args=None):
        return self._behavior(keys, args)


@pytest.fixture
def mock_redis():
    """Create a mock Redis client with script registration support."""
    client = MagicMock()
    client.register_script = MagicMock(side_effect=lambda script: FakeScript())
    client.set = MagicMock(return_value=True)
    client.get = MagicMock(return_value=None)
    return client


@pytest.fixture
def election(mock_redis):
    """Create a LeaderElection instance (not started)."""
    return LeaderElection(mock_redis, service_name="test_service", ttl=30, refresh_interval=10)


# ---------------------------------------------------------------------------
# Tests: Initialization
# ---------------------------------------------------------------------------

class TestInitialization:
    def test_valid_init(self, mock_redis):
        e = LeaderElection(mock_redis, "svc", ttl=30, refresh_interval=10)
        assert e.service_name == "svc"
        assert e.is_leader is False
        assert e._key == f"{LEADER_KEY_PREFIX}:svc"

    def test_empty_service_name_raises(self, mock_redis):
        with pytest.raises(ValueError, match="service_name must be non-empty"):
            LeaderElection(mock_redis, "", ttl=30, refresh_interval=10)

    def test_ttl_zero_raises(self, mock_redis):
        with pytest.raises(ValueError, match="ttl must be >= 1"):
            LeaderElection(mock_redis, "svc", ttl=0, refresh_interval=0)

    def test_refresh_ge_ttl_raises(self, mock_redis):
        with pytest.raises(ValueError, match="refresh_interval must be less than ttl"):
            LeaderElection(mock_redis, "svc", ttl=10, refresh_interval=10)

    def test_refresh_gt_ttl_raises(self, mock_redis):
        with pytest.raises(ValueError, match="refresh_interval must be less than ttl"):
            LeaderElection(mock_redis, "svc", ttl=10, refresh_interval=15)

    def test_custom_key_prefix(self, mock_redis):
        e = LeaderElection(mock_redis, "svc", key_prefix="custom_leader")
        assert e._key == "custom_leader:svc"

    def test_token_is_uuid(self, mock_redis):
        e = LeaderElection(mock_redis, "svc")
        assert len(e.token) == 36  # UUID format
        assert "-" in e.token

    def test_two_instances_have_different_tokens(self, mock_redis):
        e1 = LeaderElection(mock_redis, "svc")
        e2 = LeaderElection(mock_redis, "svc")
        assert e1.token != e2.token

    def test_register_script_called_twice(self, mock_redis):
        LeaderElection(mock_redis, "svc")
        assert mock_redis.register_script.call_count == 2


# ---------------------------------------------------------------------------
# Tests: try_acquire
# ---------------------------------------------------------------------------

class TestTryAcquire:
    def test_acquire_success(self, election, mock_redis):
        mock_redis.set.return_value = True
        result = election.try_acquire()
        assert result is True
        assert election.is_leader is True
        mock_redis.set.assert_called_once_with(
            election._key, election.token, nx=True, ex=30
        )

    def test_acquire_failure(self, election, mock_redis):
        mock_redis.set.return_value = False
        result = election.try_acquire()
        assert result is False
        assert election.is_leader is False

    def test_acquire_idempotent_callbacks(self, election, mock_redis):
        """Acquiring when already leader doesn't fire on_acquired again."""
        cb = MagicMock()
        election.on_acquired(cb)

        mock_redis.set.return_value = True
        election.try_acquire()
        election.try_acquire()

        assert cb.call_count == 1

    def test_acquire_fires_callback(self, election, mock_redis):
        cb = MagicMock()
        election.on_acquired(cb)
        mock_redis.set.return_value = True
        election.try_acquire()
        cb.assert_called_once()

    def test_acquire_does_not_fire_lost_callback(self, election, mock_redis):
        cb = MagicMock()
        election.on_lost(cb)
        mock_redis.set.return_value = True
        election.try_acquire()
        cb.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: refresh
# ---------------------------------------------------------------------------

class TestRefresh:
    def test_refresh_success(self, election, mock_redis):
        # Simulate being leader
        mock_redis.set.return_value = True
        election.try_acquire()

        # Refresh succeeds
        election._extend_script = FakeScript(lambda k, a: 1)
        result = election.refresh()
        assert result is True
        assert election.is_leader is True

    def test_refresh_failure_loses_leadership(self, election, mock_redis):
        mock_redis.set.return_value = True
        election.try_acquire()

        lost_cb = MagicMock()
        election.on_lost(lost_cb)

        # Refresh fails (token mismatch)
        election._extend_script = FakeScript(lambda k, a: 0)
        result = election.refresh()
        assert result is False
        assert election.is_leader is False
        lost_cb.assert_called_once()

    def test_refresh_when_not_leader(self, election):
        """Refresh when not leader returns False but doesn't fire on_lost."""
        lost_cb = MagicMock()
        election.on_lost(lost_cb)
        election._extend_script = FakeScript(lambda k, a: 0)
        result = election.refresh()
        assert result is False
        lost_cb.assert_not_called()

    def test_refresh_passes_correct_ttl_ms(self, election, mock_redis):
        mock_redis.set.return_value = True
        election.try_acquire()

        called_args = {}

        def capture_script(keys, args):
            called_args["keys"] = keys
            called_args["args"] = args
            return 1

        election._extend_script = FakeScript(capture_script)
        election.refresh()

        assert called_args["keys"] == [election._key]
        assert called_args["args"] == [election.token, "30000"]


# ---------------------------------------------------------------------------
# Tests: release
# ---------------------------------------------------------------------------

class TestRelease:
    def test_release_when_leader(self, election, mock_redis):
        mock_redis.set.return_value = True
        election.try_acquire()

        lost_cb = MagicMock()
        election.on_lost(lost_cb)

        election._release_script = FakeScript(lambda k, a: 1)
        election._release_leadership()
        assert election.is_leader is False
        lost_cb.assert_called_once()

    def test_release_when_not_leader(self, election):
        """Release when not leader is a no-op for callbacks."""
        lost_cb = MagicMock()
        election.on_lost(lost_cb)
        election._release_script = FakeScript(lambda k, a: 0)
        election._release_leadership()
        lost_cb.assert_not_called()

    def test_release_script_token_mismatch(self, election, mock_redis):
        """If token doesn't match (expired), release returns 0 but still clears state."""
        mock_redis.set.return_value = True
        election.try_acquire()

        election._release_script = FakeScript(lambda k, a: 0)
        election._release_leadership()
        assert election.is_leader is False


# ---------------------------------------------------------------------------
# Tests: get_current_leader
# ---------------------------------------------------------------------------

class TestGetCurrentLeader:
    def test_no_leader(self, election, mock_redis):
        mock_redis.get.return_value = None
        assert election.get_current_leader() == ""

    def test_leader_exists_bytes(self, election, mock_redis):
        mock_redis.get.return_value = b"some-token-123"
        assert election.get_current_leader() == "some-token-123"

    def test_leader_exists_str(self, election, mock_redis):
        mock_redis.get.return_value = "token-str"
        assert election.get_current_leader() == "token-str"


# ---------------------------------------------------------------------------
# Tests: Background loop (start/stop)
# ---------------------------------------------------------------------------

class TestBackgroundLoop:
    def test_start_creates_daemon_thread(self, election, mock_redis):
        mock_redis.set.return_value = False
        election._refresh_interval = 0.05
        election.start()
        try:
            assert election._thread is not None
            assert election._thread.daemon is True
            assert election._thread.is_alive()
            assert election._running is True
        finally:
            election.stop()

    def test_start_idempotent(self, election, mock_redis):
        mock_redis.set.return_value = False
        election._refresh_interval = 0.05
        election.start()
        t1 = election._thread
        election.start()
        assert election._thread is t1
        election.stop()

    def test_stop_joins_thread(self, election, mock_redis):
        mock_redis.set.return_value = False
        election._refresh_interval = 0.05
        election.start()
        election.stop()
        assert election._running is False
        assert not election._thread.is_alive()

    def test_stop_idempotent(self, election, mock_redis):
        mock_redis.set.return_value = False
        election._refresh_interval = 0.05
        election.start()
        election.stop()
        election.stop()  # no error

    def test_loop_acquires_leadership(self, election, mock_redis):
        mock_redis.set.return_value = True
        election._refresh_interval = 0.02
        election.start()
        time.sleep(0.1)
        assert election.is_leader is True
        election.stop()

    def test_loop_refreshes_leadership(self, election, mock_redis):
        mock_redis.set.return_value = True
        extend_calls = {"count": 0}

        def track_extend(keys, args):
            extend_calls["count"] += 1
            return 1

        election._extend_script = FakeScript(track_extend)
        election._refresh_interval = 0.02
        election.start()
        time.sleep(0.15)
        election.stop()
        assert extend_calls["count"] >= 2

    def test_loop_tolerates_redis_error(self, election, mock_redis):
        """Redis exception in loop doesn't crash the thread."""
        mock_redis.set.side_effect = ConnectionError("Redis down")
        election._refresh_interval = 0.02
        election.start()
        time.sleep(0.1)
        assert election._thread.is_alive()
        election.stop()

    def test_loop_loses_leadership_on_error(self, election, mock_redis):
        """If leader and Redis errors, leadership is marked lost."""
        mock_redis.set.return_value = True
        election._refresh_interval = 0.02

        lost_cb = MagicMock()
        election.on_lost(lost_cb)

        election.start()
        time.sleep(0.05)
        assert election.is_leader is True

        # Simulate Redis failure during refresh and prevent re-acquire
        def raise_error(keys, args):
            raise ConnectionError("gone")

        election._extend_script = FakeScript(raise_error)
        mock_redis.set.return_value = False  # Prevent re-acquisition
        time.sleep(0.15)
        assert election.is_leader is False
        lost_cb.assert_called()
        election.stop()

    def test_stop_releases_leadership(self, election, mock_redis):
        mock_redis.set.return_value = True
        released = {"called": False}

        def track_release(keys, args):
            released["called"] = True
            return 1

        election._release_script = FakeScript(track_release)
        election._refresh_interval = 0.02
        election.start()
        time.sleep(0.05)
        assert election.is_leader is True
        election.stop()
        assert released["called"] is True
        assert election.is_leader is False


# ---------------------------------------------------------------------------
# Tests: Callbacks
# ---------------------------------------------------------------------------

class TestCallbacks:
    def test_on_acquired_multiple_callbacks(self, election, mock_redis):
        cb1 = MagicMock()
        cb2 = MagicMock()
        election.on_acquired(cb1)
        election.on_acquired(cb2)
        mock_redis.set.return_value = True
        election.try_acquire()
        cb1.assert_called_once()
        cb2.assert_called_once()

    def test_on_lost_multiple_callbacks(self, election, mock_redis):
        cb1 = MagicMock()
        cb2 = MagicMock()
        election.on_lost(cb1)
        election.on_lost(cb2)
        mock_redis.set.return_value = True
        election.try_acquire()
        election._extend_script = FakeScript(lambda k, a: 0)
        election.refresh()
        cb1.assert_called_once()
        cb2.assert_called_once()

    def test_on_acquired_callback_error_doesnt_crash(self, election, mock_redis):
        def bad_callback():
            raise RuntimeError("callback broke")

        election.on_acquired(bad_callback)
        mock_redis.set.return_value = True
        election.try_acquire()  # should not raise
        assert election.is_leader is True

    def test_on_lost_callback_error_doesnt_crash(self, election, mock_redis):
        mock_redis.set.return_value = True
        election.try_acquire()

        def bad_callback():
            raise RuntimeError("callback broke")

        election.on_lost(bad_callback)
        election._extend_script = FakeScript(lambda k, a: 0)
        election.refresh()  # should not raise
        assert election.is_leader is False


# ---------------------------------------------------------------------------
# Tests: leader_only decorator
# ---------------------------------------------------------------------------

class TestLeaderOnlyDecorator:
    def test_skips_when_not_leader(self, election):
        @leader_only(election)
        def my_task():
            return "executed"

        result = my_task()
        assert result is None

    def test_runs_when_leader(self, election, mock_redis):
        mock_redis.set.return_value = True
        election.try_acquire()

        @leader_only(election)
        def my_task():
            return "executed"

        result = my_task()
        assert result == "executed"

    def test_preserves_function_name(self, election):
        @leader_only(election)
        def my_task():
            """Docstring."""
            pass

        assert my_task.__name__ == "my_task"
        assert my_task.__doc__ == "Docstring."

    def test_passes_args_and_kwargs(self, election, mock_redis):
        mock_redis.set.return_value = True
        election.try_acquire()

        @leader_only(election)
        def add(a, b, extra=0):
            return a + b + extra

        result = add(1, 2, extra=3)
        assert result == 6

    def test_leader_lost_between_check(self, election, mock_redis):
        """If leadership lost before execution, decorator returns None."""
        # Not leader — should skip
        @leader_only(election)
        def my_task():
            return "done"

        assert my_task() is None


# ---------------------------------------------------------------------------
# Tests: Concurrency scenarios
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_two_elections_one_leader(self):
        """Only one of two election instances becomes leader."""
        client = MagicMock()
        # Simulate: first SET NX succeeds, second fails
        lock_held = {"token": None}

        def fake_set(key, value, nx=False, ex=None):
            if nx and lock_held["token"] is None:
                lock_held["token"] = value
                return True
            return False

        client.set = MagicMock(side_effect=fake_set)
        client.register_script = MagicMock(return_value=FakeScript())

        e1 = LeaderElection(client, "svc", ttl=30, refresh_interval=10)
        e2 = LeaderElection(client, "svc", ttl=30, refresh_interval=10)

        e1.try_acquire()
        e2.try_acquire()

        assert e1.is_leader is True
        assert e2.is_leader is False

    def test_second_acquires_after_release(self):
        """Second instance acquires after first releases."""
        client = MagicMock()
        lock_held = {"token": None}

        def fake_set(key, value, nx=False, ex=None):
            if nx and lock_held["token"] is None:
                lock_held["token"] = value
                return True
            return False

        client.set = MagicMock(side_effect=fake_set)

        def make_release_script(script_text):
            def release_fn(keys, args):
                if lock_held["token"] == args[0]:
                    lock_held["token"] = None
                    return 1
                return 0
            return FakeScript(release_fn)

        def make_extend_script(script_text):
            return FakeScript(lambda k, a: 1 if lock_held["token"] == a[0] else 0)

        # First call -> extend script, second call -> release script
        scripts = []
        def register_side_effect(script_text):
            if "pexpire" in script_text:
                s = make_extend_script(script_text)
            else:
                s = make_release_script(script_text)
            scripts.append(s)
            return s

        client.register_script = MagicMock(side_effect=register_side_effect)

        e1 = LeaderElection(client, "svc", ttl=30, refresh_interval=10)
        e2 = LeaderElection(client, "svc", ttl=30, refresh_interval=10)

        e1.try_acquire()
        assert e1.is_leader is True

        e2.try_acquire()
        assert e2.is_leader is False

        # Release first
        e1._release_leadership()
        assert e1.is_leader is False

        # Now second can acquire
        e2.try_acquire()
        assert e2.is_leader is True

    def test_background_loop_failover(self):
        """After leader stops, another instance acquires in its loop."""
        client = MagicMock()
        lock_held = {"token": None}

        def fake_set(key, value, nx=False, ex=None):
            if nx and lock_held["token"] is None:
                lock_held["token"] = value
                return True
            return False

        client.set = MagicMock(side_effect=fake_set)
        client.get = MagicMock(return_value=None)

        def register_side_effect(script_text):
            if "pexpire" in script_text:
                return FakeScript(lambda k, a: 1 if lock_held["token"] == a[0] else 0)
            else:
                def release_fn(keys, args):
                    if lock_held["token"] == args[0]:
                        lock_held["token"] = None
                        return 1
                    return 0
                return FakeScript(release_fn)

        client.register_script = MagicMock(side_effect=register_side_effect)

        e1 = LeaderElection(client, "svc", ttl=30, refresh_interval=0.02)
        e2 = LeaderElection(client, "svc", ttl=30, refresh_interval=0.02)

        e1.start()
        time.sleep(0.05)
        assert e1.is_leader is True

        e2.start()
        time.sleep(0.05)
        assert e2.is_leader is False

        # Stop e1 (releases lock)
        e1.stop()
        time.sleep(0.1)  # Wait for e2's loop to acquire

        assert e2.is_leader is True
        e2.stop()


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_default_ttl_and_refresh(self):
        assert DEFAULT_TTL == 30
        assert DEFAULT_REFRESH_INTERVAL == 10

    def test_stop_without_start(self, election):
        election.stop()  # should not raise

    def test_start_after_stop(self, election, mock_redis):
        mock_redis.set.return_value = False
        election._refresh_interval = 0.02
        election.start()
        election.stop()
        election._stop_event.clear()
        election._running = False
        election.start()
        assert election._thread.is_alive()
        election.stop()

    def test_is_leader_property_readonly(self, election):
        with pytest.raises(AttributeError):
            election.is_leader = True

    def test_service_name_property(self, election):
        assert election.service_name == "test_service"

    def test_token_property(self, election):
        assert len(election.token) > 0

    def test_key_format(self, election):
        assert election._key == "leader_lock:test_service"

    def test_running_flag_before_start(self, election):
        assert election._running is False

    def test_thread_name(self, election, mock_redis):
        mock_redis.set.return_value = False
        election._refresh_interval = 0.02
        election.start()
        assert election._thread.name == "leader-election-test_service"
        election.stop()


# ---------------------------------------------------------------------------
# Tests: Re-election after loss
# ---------------------------------------------------------------------------

class TestReElection:
    def test_reacquire_after_loss(self, election, mock_redis):
        """Instance can re-acquire leadership after losing it."""
        mock_redis.set.return_value = True
        election.try_acquire()
        assert election.is_leader is True

        # Lose leadership
        election._extend_script = FakeScript(lambda k, a: 0)
        election.refresh()
        assert election.is_leader is False

        # Re-acquire
        election.try_acquire()
        assert election.is_leader is True

    def test_callbacks_fire_on_reacquisition(self, election, mock_redis):
        acquired_cb = MagicMock()
        lost_cb = MagicMock()
        election.on_acquired(acquired_cb)
        election.on_lost(lost_cb)

        mock_redis.set.return_value = True
        election.try_acquire()
        assert acquired_cb.call_count == 1

        # Lose
        election._extend_script = FakeScript(lambda k, a: 0)
        election.refresh()
        assert lost_cb.call_count == 1

        # Re-acquire
        election.try_acquire()
        assert acquired_cb.call_count == 2

    def test_leader_only_reflects_current_state(self, election, mock_redis):
        """leader_only decorator responds to real-time leadership changes."""
        results = []

        @leader_only(election)
        def task():
            results.append("ran")
            return "ok"

        # Not leader — skip
        task()
        assert len(results) == 0

        # Become leader
        mock_redis.set.return_value = True
        election.try_acquire()
        task()
        assert len(results) == 1

        # Lose leadership
        election._extend_script = FakeScript(lambda k, a: 0)
        election.refresh()
        task()
        assert len(results) == 1  # Didn't run again


# ---------------------------------------------------------------------------
# Tests: Integration patterns
# ---------------------------------------------------------------------------

class TestIntegrationPatterns:
    def test_leader_only_with_exception(self, election, mock_redis):
        """leader_only doesn't swallow exceptions from the wrapped function."""
        mock_redis.set.return_value = True
        election.try_acquire()

        @leader_only(election)
        def failing_task():
            raise ValueError("task error")

        with pytest.raises(ValueError, match="task error"):
            failing_task()

    def test_multiple_decorated_functions(self, election, mock_redis):
        """Multiple functions can use the same election."""
        mock_redis.set.return_value = True
        election.try_acquire()

        @leader_only(election)
        def task_a():
            return "a"

        @leader_only(election)
        def task_b():
            return "b"

        assert task_a() == "a"
        assert task_b() == "b"

    def test_leader_key_prefix_isolation(self, mock_redis):
        """Different services don't interfere with each other."""
        e1 = LeaderElection(mock_redis, "svc_a")
        e2 = LeaderElection(mock_redis, "svc_b")
        assert e1._key != e2._key
        assert "svc_a" in e1._key
        assert "svc_b" in e2._key

    def test_get_current_leader_during_election(self, election, mock_redis):
        """get_current_leader returns our token when we are leader."""
        mock_redis.set.return_value = True
        election.try_acquire()
        mock_redis.get.return_value = election.token.encode()
        assert election.get_current_leader() == election.token
