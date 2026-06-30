"""Unit tests for handler_timeout module.

Validates that:
1. HandlerTimeoutError is raised with correct attributes
2. @timeout decorator enforces time limits
3. Signal-based timeout works on main thread (Unix)
4. Thread-based timeout works as fallback
5. Redis timeout tracking increments counters correctly
6. TimeoutTracker.get_all_counts, reset_count, reset_all work
7. Decorator preserves function metadata (functools.wraps)
8. Invalid seconds raises ValueError
9. Handler completing within limit is not interrupted
10. redis_client=None disables metric tracking
11. handler_name override works
12. _supports_signal_timeout detection logic

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_handler_timeout.py -v
"""

import importlib
import importlib.util
import os
import pathlib
import platform
import signal
import sys
import threading
import time
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


handler_timeout_mod = _load_module("handler_timeout", "handler_timeout.py")

HandlerTimeoutError = handler_timeout_mod.HandlerTimeoutError
TimeoutTracker = handler_timeout_mod.TimeoutTracker
timeout = handler_timeout_mod.timeout
_supports_signal_timeout = handler_timeout_mod._supports_signal_timeout
_SignalTimeout = handler_timeout_mod._SignalTimeout
_ThreadTimeout = handler_timeout_mod._ThreadTimeout
DEFAULT_TIMEOUT_SECONDS = handler_timeout_mod.DEFAULT_TIMEOUT_SECONDS
METRICS_KEY_PREFIX = handler_timeout_mod.METRICS_KEY_PREFIX


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis():
    """Create a mock Redis client."""
    client = MagicMock()
    client.incr.return_value = 1
    client.get.return_value = None
    client.delete.return_value = 1
    client.expire.return_value = True
    client.scan.return_value = (0, [])
    client.pipeline.return_value = MagicMock(execute=MagicMock(return_value=[]))
    return client


@pytest.fixture
def tracker(mock_redis):
    """Create a TimeoutTracker instance."""
    return TimeoutTracker(mock_redis)


# ---------------------------------------------------------------------------
# HandlerTimeoutError Tests
# ---------------------------------------------------------------------------

class TestHandlerTimeoutError:
    """Tests for the HandlerTimeoutError exception class."""

    def test_basic_creation(self):
        err = HandlerTimeoutError("my_handler", 30)
        assert err.handler_name == "my_handler"
        assert err.timeout_seconds == 30
        assert err.elapsed == 30
        assert "my_handler" in str(err)
        assert "30" in str(err)

    def test_with_elapsed(self):
        err = HandlerTimeoutError("slow_handler", 10, elapsed=10.5)
        assert err.elapsed == 10.5
        assert "10.5" in str(err)

    def test_is_exception(self):
        err = HandlerTimeoutError("h", 5)
        assert isinstance(err, Exception)

    def test_can_be_raised_and_caught(self):
        with pytest.raises(HandlerTimeoutError) as exc_info:
            raise HandlerTimeoutError("test_handler", 15, 14.9)
        assert exc_info.value.handler_name == "test_handler"
        assert exc_info.value.timeout_seconds == 15

    def test_default_elapsed_matches_timeout(self):
        err = HandlerTimeoutError("h", 42)
        assert err.elapsed == 42

    def test_message_format(self):
        err = HandlerTimeoutError("process_task", 60, 59.8)
        msg = str(err)
        assert "process_task" in msg
        assert "59.8" in msg
        assert "60" in msg


# ---------------------------------------------------------------------------
# TimeoutTracker Tests
# ---------------------------------------------------------------------------

class TestTimeoutTracker:
    """Tests for Redis-based timeout metric tracking."""

    def test_record_timeout_first_time(self, tracker, mock_redis):
        mock_redis.incr.return_value = 1
        count = tracker.record_timeout("my_handler")
        assert count == 1
        mock_redis.incr.assert_called_once_with(f"{METRICS_KEY_PREFIX}:my_handler")
        mock_redis.expire.assert_called_once_with(f"{METRICS_KEY_PREFIX}:my_handler", 86400 * 7)

    def test_record_timeout_subsequent(self, tracker, mock_redis):
        mock_redis.incr.return_value = 5
        count = tracker.record_timeout("my_handler")
        assert count == 5
        mock_redis.expire.assert_not_called()

    def test_record_timeout_with_seconds(self, tracker, mock_redis):
        mock_redis.incr.return_value = 2
        count = tracker.record_timeout("slow", timeout_seconds=60)
        assert count == 2

    def test_get_timeout_count_exists(self, tracker, mock_redis):
        mock_redis.get.return_value = b"7"
        count = tracker.get_timeout_count("handler_a")
        assert count == 7

    def test_get_timeout_count_none(self, tracker, mock_redis):
        mock_redis.get.return_value = None
        count = tracker.get_timeout_count("handler_b")
        assert count == 0

    def test_get_timeout_count_str(self, tracker, mock_redis):
        mock_redis.get.return_value = "3"
        count = tracker.get_timeout_count("handler_c")
        assert count == 3

    def test_get_all_counts_empty(self, tracker, mock_redis):
        mock_redis.scan.return_value = (0, [])
        result = tracker.get_all_counts()
        assert result == {}

    def test_get_all_counts_with_data(self, tracker, mock_redis):
        keys = [
            f"{METRICS_KEY_PREFIX}:handler_a".encode(),
            f"{METRICS_KEY_PREFIX}:handler_b".encode(),
        ]
        mock_redis.scan.return_value = (0, keys)
        pipe = MagicMock()
        pipe.execute.return_value = [b"3", b"7"]
        mock_redis.pipeline.return_value = pipe

        result = tracker.get_all_counts()
        assert result == {"handler_a": 3, "handler_b": 7}

    def test_get_all_counts_string_keys(self, tracker, mock_redis):
        keys = [
            f"{METRICS_KEY_PREFIX}:handler_x",
        ]
        mock_redis.scan.return_value = (0, keys)
        pipe = MagicMock()
        pipe.execute.return_value = [b"2"]
        mock_redis.pipeline.return_value = pipe

        result = tracker.get_all_counts()
        assert result == {"handler_x": 2}

    def test_get_all_counts_none_value(self, tracker, mock_redis):
        keys = [f"{METRICS_KEY_PREFIX}:h".encode()]
        mock_redis.scan.return_value = (0, keys)
        pipe = MagicMock()
        pipe.execute.return_value = [None]
        mock_redis.pipeline.return_value = pipe

        result = tracker.get_all_counts()
        assert result == {"h": 0}

    def test_get_all_counts_paginated_scan(self, tracker, mock_redis):
        key1 = f"{METRICS_KEY_PREFIX}:a".encode()
        key2 = f"{METRICS_KEY_PREFIX}:b".encode()
        mock_redis.scan.side_effect = [
            (42, [key1]),
            (0, [key2]),
        ]
        pipe = MagicMock()
        pipe.execute.side_effect = [[b"1"], [b"2"]]
        mock_redis.pipeline.return_value = pipe

        result = tracker.get_all_counts()
        assert result == {"a": 1, "b": 2}

    def test_reset_count_exists(self, tracker, mock_redis):
        mock_redis.delete.return_value = 1
        assert tracker.reset_count("handler_a") is True

    def test_reset_count_not_exists(self, tracker, mock_redis):
        mock_redis.delete.return_value = 0
        assert tracker.reset_count("handler_z") is False

    def test_reset_all_empty(self, tracker, mock_redis):
        mock_redis.scan.return_value = (0, [])
        assert tracker.reset_all() == 0

    def test_reset_all_with_keys(self, tracker, mock_redis):
        keys = [b"metrics:handler_timeouts:a", b"metrics:handler_timeouts:b"]
        mock_redis.scan.return_value = (0, keys)
        mock_redis.delete.return_value = 2
        assert tracker.reset_all() == 2
        mock_redis.delete.assert_called_once_with(*keys)

    def test_custom_key_prefix(self, mock_redis):
        t = TimeoutTracker(mock_redis, key_prefix="custom:timeouts")
        mock_redis.incr.return_value = 1
        t.record_timeout("h")
        mock_redis.incr.assert_called_once_with("custom:timeouts:h")


# ---------------------------------------------------------------------------
# @timeout Decorator Tests
# ---------------------------------------------------------------------------

class TestTimeoutDecorator:
    """Tests for the @timeout decorator."""

    def test_handler_completes_within_limit(self):
        @timeout(seconds=5, use_signal=False)
        def fast_handler(data):
            return data["value"] * 2

        result = fast_handler({"value": 21})
        assert result == 42

    def test_handler_exceeds_limit_thread_timeout(self):
        @timeout(seconds=1, use_signal=False)
        def slow_handler(data):
            time.sleep(5)
            return "should not reach"

        with pytest.raises(HandlerTimeoutError) as exc_info:
            slow_handler({"key": "val"})
        assert exc_info.value.handler_name == "slow_handler"
        assert exc_info.value.timeout_seconds == 1

    def test_preserves_function_name(self):
        @timeout(seconds=10, use_signal=False)
        def my_special_handler():
            pass

        assert my_special_handler.__name__ == "my_special_handler"

    def test_preserves_docstring(self):
        @timeout(seconds=10, use_signal=False)
        def documented_handler():
            """This is my docstring."""
            pass

        assert documented_handler.__doc__ == "This is my docstring."

    def test_invalid_seconds_zero(self):
        with pytest.raises(ValueError) as exc_info:
            @timeout(seconds=0)
            def bad():
                pass
        assert "must be > 0" in str(exc_info.value)

    def test_invalid_seconds_negative(self):
        with pytest.raises(ValueError):
            @timeout(seconds=-5)
            def bad():
                pass

    def test_handler_name_override(self):
        @timeout(seconds=1, use_signal=False, handler_name="custom_name")
        def slow(data):
            time.sleep(3)

        with pytest.raises(HandlerTimeoutError) as exc_info:
            slow({})
        assert exc_info.value.handler_name == "custom_name"

    def test_timeout_metadata_on_wrapper(self):
        @timeout(seconds=42, use_signal=False)
        def handler():
            pass

        assert handler._timeout_seconds == 42
        assert handler._handler_name == "handler"

    def test_custom_handler_name_metadata(self):
        @timeout(seconds=10, use_signal=False, handler_name="overridden")
        def handler():
            pass

        assert handler._handler_name == "overridden"

    def test_no_redis_client_no_tracker(self):
        @timeout(seconds=10, use_signal=False)
        def handler():
            pass

        assert handler._timeout_tracker is None

    def test_with_redis_client_creates_tracker(self, mock_redis):
        @timeout(seconds=10, redis_client=mock_redis, use_signal=False)
        def handler():
            pass

        assert handler._timeout_tracker is not None

    def test_redis_metric_recorded_on_timeout(self, mock_redis):
        mock_redis.incr.return_value = 1

        @timeout(seconds=1, redis_client=mock_redis, use_signal=False)
        def slow_handler(data):
            time.sleep(3)

        with pytest.raises(HandlerTimeoutError):
            slow_handler({})

        mock_redis.incr.assert_called_once_with(f"{METRICS_KEY_PREFIX}:slow_handler")

    def test_redis_metric_not_recorded_on_success(self, mock_redis):
        @timeout(seconds=10, redis_client=mock_redis, use_signal=False)
        def fast_handler(data):
            return "ok"

        result = fast_handler({})
        assert result == "ok"
        mock_redis.incr.assert_not_called()

    def test_redis_metric_error_does_not_suppress_timeout(self, mock_redis):
        mock_redis.incr.side_effect = Exception("Redis down")

        @timeout(seconds=1, redis_client=mock_redis, use_signal=False)
        def slow(data):
            time.sleep(3)

        with pytest.raises(HandlerTimeoutError):
            slow({})

    def test_handler_returns_none(self):
        @timeout(seconds=5, use_signal=False)
        def returns_none():
            return None

        assert returns_none() is None

    def test_handler_raises_other_exception(self):
        @timeout(seconds=5, use_signal=False)
        def raises_value_error():
            raise ValueError("bad input")

        with pytest.raises(ValueError, match="bad input"):
            raises_value_error()

    def test_handler_with_args_and_kwargs(self):
        @timeout(seconds=5, use_signal=False)
        def handler(a, b, c=None):
            return (a, b, c)

        result = handler(1, 2, c=3)
        assert result == (1, 2, 3)

    def test_default_timeout_seconds(self):
        assert DEFAULT_TIMEOUT_SECONDS == 30


# ---------------------------------------------------------------------------
# Signal Timeout Tests (Unix-only, main thread)
# ---------------------------------------------------------------------------

class TestSignalTimeout:
    """Tests for signal-based timeout on Unix platforms."""

    @pytest.mark.skipif(
        platform.system() == "Windows",
        reason="signal.SIGALRM not available on Windows"
    )
    def test_signal_timeout_raises_on_slow(self):
        @timeout(seconds=1, use_signal=True)
        def slow():
            time.sleep(5)

        with pytest.raises(HandlerTimeoutError) as exc_info:
            slow()
        assert exc_info.value.timeout_seconds == 1

    @pytest.mark.skipif(
        platform.system() == "Windows",
        reason="signal.SIGALRM not available on Windows"
    )
    def test_signal_timeout_fast_handler_ok(self):
        @timeout(seconds=5, use_signal=True)
        def fast():
            return "done"

        assert fast() == "done"

    @pytest.mark.skipif(
        platform.system() == "Windows",
        reason="signal.SIGALRM not available on Windows"
    )
    def test_signal_timeout_restores_old_handler(self):
        old_handler = signal.getsignal(signal.SIGALRM)

        @timeout(seconds=5, use_signal=True)
        def fast():
            return "ok"

        fast()
        assert signal.getsignal(signal.SIGALRM) == old_handler

    @pytest.mark.skipif(
        platform.system() == "Windows",
        reason="signal.SIGALRM not available on Windows"
    )
    def test_signal_timeout_context_manager(self):
        with pytest.raises(HandlerTimeoutError):
            with _SignalTimeout(1, "test_handler"):
                time.sleep(5)


# ---------------------------------------------------------------------------
# Thread Timeout Tests
# ---------------------------------------------------------------------------

class TestThreadTimeout:
    """Tests for thread-based timeout fallback."""

    def test_thread_timeout_raises_on_slow(self):
        @timeout(seconds=1, use_signal=False)
        def slow():
            time.sleep(5)

        with pytest.raises(HandlerTimeoutError):
            slow()

    def test_thread_timeout_fast_ok(self):
        @timeout(seconds=5, use_signal=False)
        def fast():
            return 42

        assert fast() == 42

    def test_thread_timeout_in_non_main_thread(self):
        result = {"value": None, "error": None}

        @timeout(seconds=1, use_signal=False)
        def slow():
            # Use short sleeps in a loop so the injected async exception
            # can be delivered between Python bytecode instructions
            for _ in range(200):
                time.sleep(0.1)

        def run():
            try:
                slow()
            except HandlerTimeoutError as e:
                result["error"] = e

        t = threading.Thread(target=run)
        t.start()
        t.join(timeout=10)

        assert result["error"] is not None
        assert isinstance(result["error"], HandlerTimeoutError)

    def test_thread_timeout_context_manager_exit_cancels_timer(self):
        ctx = _ThreadTimeout(60, "test")
        ctx.__enter__()
        assert ctx._timer is not None
        assert ctx._timer.is_alive()
        ctx.__exit__(None, None, None)
        assert ctx._timer is None or not ctx._timer.is_alive()


# ---------------------------------------------------------------------------
# _supports_signal_timeout Tests
# ---------------------------------------------------------------------------

class TestSupportsSignalTimeout:
    """Tests for signal availability detection."""

    @pytest.mark.skipif(
        platform.system() == "Windows",
        reason="SIGALRM always unavailable on Windows"
    )
    def test_returns_true_on_main_thread_unix(self):
        assert _supports_signal_timeout() is True

    def test_returns_false_in_non_main_thread(self):
        result = {"value": None}

        def check():
            result["value"] = _supports_signal_timeout()

        t = threading.Thread(target=check)
        t.start()
        t.join()
        assert result["value"] is False

    @patch("platform.system", return_value="Windows")
    def test_returns_false_on_windows(self, mock_sys):
        # Reload module to pick up mocked platform
        # Instead just call the logic directly
        assert platform.system() == "Windows" or True  # patched
        # The function under test uses platform.system()
        from unittest.mock import patch as _p
        with _p.object(handler_timeout_mod.platform, "system", return_value="Windows"):
            assert handler_timeout_mod._supports_signal_timeout() is False


# ---------------------------------------------------------------------------
# Auto-detect Tests
# ---------------------------------------------------------------------------

class TestAutoDetect:
    """Tests for automatic timeout mechanism selection."""

    def test_auto_selects_working_mechanism(self):
        @timeout(seconds=5)
        def handler():
            return "auto"

        assert handler() == "auto"

    @pytest.mark.skipif(
        platform.system() == "Windows",
        reason="Only relevant on Unix"
    )
    def test_auto_picks_signal_on_main_thread(self):
        # Verify the decorator auto-detects correctly
        @timeout(seconds=1)
        def slow():
            time.sleep(5)

        with pytest.raises(HandlerTimeoutError):
            slow()

    def test_use_signal_true_forces_signal(self):
        # If we're on main thread Unix, this should work
        if platform.system() != "Windows":
            @timeout(seconds=5, use_signal=True)
            def fast():
                return "signal"
            assert fast() == "signal"

    def test_use_signal_false_forces_thread(self):
        @timeout(seconds=5, use_signal=False)
        def fast():
            return "thread"
        assert fast() == "thread"


# ---------------------------------------------------------------------------
# Integration / Edge Cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge case and integration tests."""

    def test_nested_timeouts_thread_based(self):
        @timeout(seconds=5, use_signal=False)
        def outer():
            @timeout(seconds=2, use_signal=False)
            def inner():
                time.sleep(4)
            return inner()

        with pytest.raises(HandlerTimeoutError) as exc_info:
            outer()
        assert exc_info.value.timeout_seconds == 2

    def test_handler_just_under_limit(self):
        @timeout(seconds=2, use_signal=False)
        def just_fast_enough():
            time.sleep(0.5)
            return "made it"

        assert just_fast_enough() == "made it"

    def test_multiple_calls_same_decorator(self):
        call_count = {"n": 0}

        @timeout(seconds=5, use_signal=False)
        def counter():
            call_count["n"] += 1
            return call_count["n"]

        assert counter() == 1
        assert counter() == 2
        assert counter() == 3

    def test_timeout_with_generator_func(self):
        @timeout(seconds=5, use_signal=False)
        def gen_handler():
            return list(range(10))

        assert gen_handler() == list(range(10))

    def test_handler_with_no_args(self):
        @timeout(seconds=5, use_signal=False)
        def no_args():
            return "no args"

        assert no_args() == "no args"

    def test_handler_with_only_kwargs(self):
        @timeout(seconds=5, use_signal=False)
        def kwargs_only(**kwargs):
            return kwargs

        result = kwargs_only(a=1, b=2)
        assert result == {"a": 1, "b": 2}

    def test_concurrent_handlers_thread_timeout(self):
        results = []
        errors = []

        @timeout(seconds=1, use_signal=False)
        def slow():
            time.sleep(5)

        @timeout(seconds=5, use_signal=False)
        def fast():
            return "fast_done"

        def run_slow():
            try:
                slow()
            except HandlerTimeoutError as e:
                errors.append(e)

        def run_fast():
            results.append(fast())

        t1 = threading.Thread(target=run_slow)
        t2 = threading.Thread(target=run_fast)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(errors) == 1
        assert results == ["fast_done"]

    def test_timeout_1_second_precision(self):
        @timeout(seconds=1, use_signal=False)
        def exactly_1s():
            # Use a loop with short sleeps so the injected exception
            # can be caught between iterations
            for _ in range(100):
                time.sleep(0.1)

        start = time.time()
        with pytest.raises(HandlerTimeoutError):
            exactly_1s()
        elapsed = time.time() - start
        assert elapsed < 5.0  # should timeout within a few seconds

    def test_tracker_key_format(self, mock_redis):
        t = TimeoutTracker(mock_redis)
        expected_key = f"{METRICS_KEY_PREFIX}:my_handler"
        mock_redis.incr.return_value = 1
        t.record_timeout("my_handler")
        mock_redis.incr.assert_called_with(expected_key)

    def test_large_timeout_value(self):
        @timeout(seconds=3600, use_signal=False)
        def handler():
            return "ok"

        assert handler() == "ok"
        assert handler._timeout_seconds == 3600
