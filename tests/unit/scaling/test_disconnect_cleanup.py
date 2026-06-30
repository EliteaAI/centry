"""Unit tests for disconnect_cleanup module.

Tests the DisconnectCleanup class which handles deferred session cleanup
after Socket.IO disconnects, with a grace period for reconnection.
"""

import importlib.util
import json
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Module loading (same pattern as other scaling tests)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _mock_pylon():
    """Mock pylon.core.tools before loading the module under test."""
    mock_log = MagicMock()
    mock_tools = MagicMock()
    mock_tools.log = mock_log

    mock_pylon = MagicMock()
    mock_pylon.core = MagicMock()
    mock_pylon.core.tools = mock_tools
    mock_pylon.core.tools.log = mock_log

    sys.modules.setdefault("pylon", mock_pylon)
    sys.modules.setdefault("pylon.core", mock_pylon.core)
    sys.modules.setdefault("pylon.core.tools", mock_tools)
    sys.modules.setdefault("pylon.core.tools.log", mock_log)

    yield mock_log


@pytest.fixture(scope="session")
def _mod():
    """Load the disconnect_cleanup module via importlib."""
    source_path = Path(__file__).resolve().parents[4] / "elitea_core" / "utils" / "disconnect_cleanup.py"
    spec = importlib.util.spec_from_file_location("disconnect_cleanup", source_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["disconnect_cleanup"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mock_redis():
    """Create a mock Redis client."""
    client = MagicMock()
    pipe = MagicMock()
    client.pipeline.return_value = pipe
    pipe.execute.return_value = [True, 1]
    return client


@pytest.fixture
def cleanup(_mod, mock_redis):
    """Create a DisconnectCleanup instance with mocked Redis."""
    instance = _mod.DisconnectCleanup(mock_redis, grace_period=60)
    yield instance
    if instance.is_running:
        instance.stop_subscriber()


# ---------------------------------------------------------------------------
# Tests: publish_disconnect
# ---------------------------------------------------------------------------

class TestPublishDisconnect:
    def test_publish_sets_pending_key_and_publishes(self, cleanup, mock_redis, _mod):
        """publish_disconnect should SET pending key with TTL and PUBLISH to channel."""
        pipe = mock_redis.pipeline.return_value

        cleanup.publish_disconnect("sid_123", metadata={"user_id": "u1"})

        mock_redis.pipeline.assert_called_once_with(transaction=False)
        assert pipe.set.call_count == 1
        assert pipe.publish.call_count == 1
        pipe.execute.assert_called_once()

        # Check pending key format and TTL
        set_call = pipe.set.call_args
        assert set_call[0][0] == "disconnect_pending:sid_123"
        set_call_data = json.loads(set_call[0][1])
        assert set_call_data["sid"] == "sid_123"
        assert set_call_data["metadata"] == {"user_id": "u1"}
        assert "timestamp" in set_call_data
        assert set_call[1]["ex"] == 60

        # Check publish channel
        publish_call = pipe.publish.call_args
        assert publish_call[0][0] == _mod.CHANNEL_NAME

    def test_publish_without_metadata(self, cleanup, mock_redis):
        """publish_disconnect with no metadata should use empty dict."""
        pipe = mock_redis.pipeline.return_value

        cleanup.publish_disconnect("sid_456")

        set_call = pipe.set.call_args
        data = json.loads(set_call[0][1])
        assert data["metadata"] == {}

    def test_publish_uses_configured_grace_period(self, _mod, mock_redis):
        """Pending key TTL should match the configured grace period."""
        instance = _mod.DisconnectCleanup(mock_redis, grace_period=30)
        pipe = mock_redis.pipeline.return_value

        instance.publish_disconnect("sid_789")

        set_call = pipe.set.call_args
        assert set_call[1]["ex"] == 30


# ---------------------------------------------------------------------------
# Tests: cancel_cleanup
# ---------------------------------------------------------------------------

class TestCancelCleanup:
    def test_cancel_deletes_pending_key(self, cleanup, mock_redis):
        """cancel_cleanup should delete the pending key from Redis."""
        mock_redis.delete.return_value = 1

        result = cleanup.cancel_cleanup("sid_abc")

        mock_redis.delete.assert_called_once_with("disconnect_pending:sid_abc")
        assert result is True

    def test_cancel_when_no_pending_returns_false(self, cleanup, mock_redis):
        """cancel_cleanup should return False if no pending key exists."""
        mock_redis.delete.return_value = 0

        result = cleanup.cancel_cleanup("sid_nonexistent")

        assert result is False

    def test_cancel_stops_pending_timer(self, cleanup, mock_redis):
        """cancel_cleanup should cancel any scheduled timer for the SID."""
        mock_redis.delete.return_value = 1

        mock_timer = MagicMock()
        with cleanup._timers_lock:
            cleanup._pending_timers["sid_timer"] = mock_timer

        cleanup.cancel_cleanup("sid_timer")

        mock_timer.cancel.assert_called_once()
        assert "sid_timer" not in cleanup._pending_timers

    def test_cancel_without_timer_still_works(self, cleanup, mock_redis):
        """cancel_cleanup should work even if there's no timer for the SID."""
        mock_redis.delete.return_value = 1

        result = cleanup.cancel_cleanup("sid_no_timer")
        assert result is True


# ---------------------------------------------------------------------------
# Tests: _execute_cleanup
# ---------------------------------------------------------------------------

class TestExecuteCleanup:
    def test_execute_runs_callbacks_when_pending(self, cleanup, mock_redis):
        """_execute_cleanup should run callbacks if pending key still exists."""
        mock_redis.get.return_value = b"1"
        callback1 = MagicMock()
        callback2 = MagicMock()
        cleanup._callbacks = [callback1, callback2]

        disconnect_info = {"sid": "sid_exec", "timestamp": 1000.0, "metadata": {}}
        cleanup._execute_cleanup("sid_exec", disconnect_info)

        mock_redis.get.assert_called_once_with("disconnect_pending:sid_exec")
        mock_redis.delete.assert_called_once_with("disconnect_pending:sid_exec")
        callback1.assert_called_once_with("sid_exec", disconnect_info)
        callback2.assert_called_once_with("sid_exec", disconnect_info)

    def test_execute_skips_if_key_missing(self, cleanup, mock_redis):
        """_execute_cleanup should NOT run callbacks if pending key was deleted (reconnected)."""
        mock_redis.get.return_value = None
        callback = MagicMock()
        cleanup._callbacks = [callback]

        cleanup._execute_cleanup("sid_gone", {"sid": "sid_gone"})

        callback.assert_not_called()
        # Should not try to delete a non-existent key
        mock_redis.delete.assert_not_called()

    def test_execute_handles_callback_exception(self, cleanup, mock_redis):
        """_execute_cleanup should continue to next callback even if one raises."""
        mock_redis.get.return_value = b"1"
        failing_cb = MagicMock(side_effect=RuntimeError("boom"))
        good_cb = MagicMock()
        cleanup._callbacks = [failing_cb, good_cb]

        cleanup._execute_cleanup("sid_err", {"sid": "sid_err"})

        failing_cb.assert_called_once()
        good_cb.assert_called_once()

    def test_execute_removes_timer_from_pending(self, cleanup, mock_redis):
        """_execute_cleanup should remove its timer from _pending_timers."""
        mock_redis.get.return_value = b"1"
        with cleanup._timers_lock:
            cleanup._pending_timers["sid_rm"] = MagicMock()

        cleanup._execute_cleanup("sid_rm", {"sid": "sid_rm"})

        assert "sid_rm" not in cleanup._pending_timers


# ---------------------------------------------------------------------------
# Tests: _schedule_deferred_cleanup
# ---------------------------------------------------------------------------

class TestScheduleDeferredCleanup:
    def test_schedules_timer_for_disconnect(self, cleanup, _mod):
        """_schedule_deferred_cleanup should create a timer with grace period."""
        disconnect_info = {"sid": "sid_sched", "timestamp": 1000.0}

        with patch.object(_mod.threading, "Timer") as mock_timer_cls:
            mock_timer = MagicMock()
            mock_timer_cls.return_value = mock_timer

            cleanup._schedule_deferred_cleanup(disconnect_info)

            mock_timer_cls.assert_called_once_with(
                60,  # grace period
                cleanup._execute_cleanup,
                args=("sid_sched", disconnect_info),
            )
            mock_timer.start.assert_called_once()
            assert cleanup._pending_timers["sid_sched"] is mock_timer

    def test_cancels_existing_timer_for_same_sid(self, cleanup, _mod):
        """If a timer already exists for a SID, it should be cancelled before scheduling new."""
        old_timer = MagicMock()
        with cleanup._timers_lock:
            cleanup._pending_timers["sid_dup"] = old_timer

        disconnect_info = {"sid": "sid_dup", "timestamp": 2000.0}

        with patch.object(_mod.threading, "Timer") as mock_timer_cls:
            new_timer = MagicMock()
            mock_timer_cls.return_value = new_timer

            cleanup._schedule_deferred_cleanup(disconnect_info)

            old_timer.cancel.assert_called_once()
            assert cleanup._pending_timers["sid_dup"] is new_timer

    def test_ignores_event_without_sid(self, cleanup, _mod):
        """_schedule_deferred_cleanup should do nothing if 'sid' is missing."""
        with patch.object(_mod.threading, "Timer") as mock_timer_cls:
            cleanup._schedule_deferred_cleanup({"timestamp": 1000.0})
            mock_timer_cls.assert_not_called()

    def test_ignores_empty_sid(self, cleanup, _mod):
        """_schedule_deferred_cleanup should do nothing if 'sid' is empty string."""
        with patch.object(_mod.threading, "Timer") as mock_timer_cls:
            cleanup._schedule_deferred_cleanup({"sid": "", "timestamp": 1000.0})
            mock_timer_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: subscriber lifecycle
# ---------------------------------------------------------------------------

class TestSubscriberLifecycle:
    def test_start_subscriber_sets_running(self, cleanup, mock_redis):
        """start_subscriber should set is_running and start thread."""
        # Make subscriber loop exit immediately
        pubsub = MagicMock()
        mock_redis.pubsub.return_value = pubsub
        def listen_exit():
            cleanup._running = False
            return iter([])
        pubsub.listen.return_value = listen_exit()

        cleanup.start_subscriber()
        assert cleanup.is_running is True
        assert cleanup._subscriber_thread is not None
        assert cleanup._subscriber_thread.daemon is True

        # Let thread finish
        cleanup._running = False
        cleanup._subscriber_thread.join(timeout=2)

    def test_start_subscriber_idempotent(self, cleanup, mock_redis):
        """Calling start_subscriber twice should not create a second thread."""
        pubsub = MagicMock()
        mock_redis.pubsub.return_value = pubsub
        def listen_exit():
            cleanup._running = False
            return iter([])
        pubsub.listen.return_value = listen_exit()

        cleanup.start_subscriber()
        thread1 = cleanup._subscriber_thread

        cleanup.start_subscriber()
        thread2 = cleanup._subscriber_thread

        assert thread1 is thread2
        cleanup._running = False
        cleanup._subscriber_thread.join(timeout=2)

    def test_stop_subscriber_clears_timers(self, cleanup, _mod):
        """stop_subscriber should cancel all pending timers and clear state."""
        timer1 = MagicMock()
        timer2 = MagicMock()
        with cleanup._timers_lock:
            cleanup._pending_timers["s1"] = timer1
            cleanup._pending_timers["s2"] = timer2

        cleanup._running = True
        cleanup._subscriber_thread = MagicMock()
        cleanup.stop_subscriber()

        timer1.cancel.assert_called_once()
        timer2.cancel.assert_called_once()
        assert len(cleanup._pending_timers) == 0
        assert cleanup._running is False


# ---------------------------------------------------------------------------
# Tests: _subscriber_loop (integration-style)
# ---------------------------------------------------------------------------

class TestSubscriberLoop:
    def test_processes_valid_message(self, cleanup, mock_redis, _mod):
        """_subscriber_loop should parse valid messages and schedule cleanup."""
        disconnect_info = {"sid": "sid_msg", "timestamp": 1000.0, "metadata": {}}
        message = {
            "type": "message",
            "data": json.dumps(disconnect_info).encode("utf-8"),
        }

        pubsub = MagicMock()
        mock_redis.pubsub.return_value = pubsub

        # Simulate: yield one message then break
        def listen_once():
            yield message
            cleanup._running = False

        pubsub.listen.return_value = listen_once()

        with patch.object(cleanup, "_schedule_deferred_cleanup") as mock_sched:
            cleanup._running = True
            cleanup._subscriber_loop()

            mock_sched.assert_called_once_with(disconnect_info)

    def test_handles_invalid_json_gracefully(self, cleanup, mock_redis):
        """_subscriber_loop should skip invalid JSON without crashing."""
        message = {"type": "message", "data": b"not valid json{{"}

        pubsub = MagicMock()
        mock_redis.pubsub.return_value = pubsub

        def listen_once():
            yield message
            cleanup._running = False

        pubsub.listen.return_value = listen_once()

        with patch.object(cleanup, "_schedule_deferred_cleanup") as mock_sched:
            cleanup._running = True
            cleanup._subscriber_loop()
            mock_sched.assert_not_called()

    def test_skips_non_message_types(self, cleanup, mock_redis):
        """_subscriber_loop should ignore subscribe/psubscribe messages."""
        message = {"type": "subscribe", "data": 1}

        pubsub = MagicMock()
        mock_redis.pubsub.return_value = pubsub

        def listen_once():
            yield message
            cleanup._running = False

        pubsub.listen.return_value = listen_once()

        with patch.object(cleanup, "_schedule_deferred_cleanup") as mock_sched:
            cleanup._running = True
            cleanup._subscriber_loop()
            mock_sched.assert_not_called()

    def test_handles_string_data(self, cleanup, mock_redis):
        """_subscriber_loop should handle string data (decode_responses=True)."""
        disconnect_info = {"sid": "sid_str", "timestamp": 2000.0, "metadata": {}}
        message = {"type": "message", "data": json.dumps(disconnect_info)}

        pubsub = MagicMock()
        mock_redis.pubsub.return_value = pubsub

        def listen_once():
            yield message
            cleanup._running = False

        pubsub.listen.return_value = listen_once()

        with patch.object(cleanup, "_schedule_deferred_cleanup") as mock_sched:
            cleanup._running = True
            cleanup._subscriber_loop()
            mock_sched.assert_called_once_with(disconnect_info)

    def test_retries_on_redis_error(self, cleanup, mock_redis, _mod):
        """_subscriber_loop should retry after Redis connection errors."""
        call_count = [0]

        def failing_pubsub(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("Redis down")
            # Second call: return working pubsub that stops the loop
            pubsub = MagicMock()
            def listen_gen():
                cleanup._running = False
                return iter([])
            pubsub.listen.side_effect = listen_gen
            return pubsub

        mock_redis.pubsub.side_effect = failing_pubsub

        # Patch time.sleep on the module to avoid real 3s wait
        fake_time = types.ModuleType("time")
        fake_time.sleep = MagicMock()
        fake_time.time = time.time

        original_time = _mod.time
        _mod.time = fake_time
        try:
            cleanup._running = True
            cleanup._subscriber_loop()
        finally:
            _mod.time = original_time

        assert call_count[0] == 2
        fake_time.sleep.assert_called_once_with(3)

    def test_stops_when_running_false(self, cleanup, mock_redis):
        """_subscriber_loop should exit cleanly when _running is set to False."""
        pubsub = MagicMock()
        mock_redis.pubsub.return_value = pubsub

        def listen_forever():
            yield {"type": "message", "data": b'{"sid":"x"}'}
            # After first message, mark as stopped
            cleanup._running = False
            yield None

        pubsub.listen.return_value = listen_forever()

        with patch.object(cleanup, "_schedule_deferred_cleanup"):
            cleanup._running = True
            cleanup._subscriber_loop()

        assert cleanup._running is False


# ---------------------------------------------------------------------------
# Tests: add_callback
# ---------------------------------------------------------------------------

class TestAddCallback:
    def test_add_callback_appends(self, cleanup):
        """add_callback should append to the callbacks list."""
        cb = MagicMock()
        cleanup.add_callback(cb)

        assert cb in cleanup._callbacks

    def test_multiple_callbacks_maintained(self, cleanup):
        """Multiple callbacks should all be stored."""
        cb1 = MagicMock()
        cb2 = MagicMock()
        cleanup.add_callback(cb1)
        cleanup.add_callback(cb2)

        assert len(cleanup._callbacks) == 2


# ---------------------------------------------------------------------------
# Tests: properties
# ---------------------------------------------------------------------------

class TestProperties:
    def test_grace_period_property(self, _mod, mock_redis):
        """grace_period property should return configured value."""
        instance = _mod.DisconnectCleanup(mock_redis, grace_period=45)
        assert instance.grace_period == 45

    def test_is_running_property(self, cleanup):
        """is_running property should reflect internal state."""
        assert cleanup.is_running is False
        cleanup._running = True
        assert cleanup.is_running is True


# ---------------------------------------------------------------------------
# Tests: pending key format
# ---------------------------------------------------------------------------

class TestKeyFormat:
    def test_pending_key_format(self, cleanup):
        """Pending keys should follow the expected format."""
        assert cleanup._pending_key("abc123") == "disconnect_pending:abc123"
        assert cleanup._pending_key("sid-with-dashes") == "disconnect_pending:sid-with-dashes"


# ---------------------------------------------------------------------------
# Tests: end-to-end flow (unit-level)
# ---------------------------------------------------------------------------

class TestEndToEndFlow:
    def test_disconnect_then_cleanup_executes(self, cleanup, mock_redis):
        """Full flow: publish disconnect → wait → execute cleanup."""
        callback = MagicMock()
        cleanup._callbacks = [callback]
        mock_redis.get.return_value = b'{"sid":"sid_e2e","timestamp":1}'

        # Simulate: publish sets the key
        pipe = mock_redis.pipeline.return_value
        cleanup.publish_disconnect("sid_e2e", {"user_id": "u99"})

        # Simulate: timer fires, key still exists
        mock_redis.get.return_value = b"1"
        cleanup._execute_cleanup("sid_e2e", {"sid": "sid_e2e", "metadata": {"user_id": "u99"}})

        callback.assert_called_once_with("sid_e2e", {"sid": "sid_e2e", "metadata": {"user_id": "u99"}})

    def test_disconnect_then_reconnect_cancels(self, cleanup, mock_redis):
        """Full flow: publish disconnect → reconnect → cleanup NOT executed."""
        callback = MagicMock()
        cleanup._callbacks = [callback]

        pipe = mock_redis.pipeline.return_value
        cleanup.publish_disconnect("sid_recon")

        # Reconnect cancels
        mock_redis.delete.return_value = 1
        cleanup.cancel_cleanup("sid_recon")

        # After grace: key is gone
        mock_redis.get.return_value = None
        cleanup._execute_cleanup("sid_recon", {"sid": "sid_recon"})

        callback.assert_not_called()

    def test_cleanup_with_no_callbacks_doesnt_crash(self, cleanup, mock_redis):
        """Executing cleanup with empty callback list should not error."""
        mock_redis.get.return_value = b"1"
        cleanup._callbacks = []

        cleanup._execute_cleanup("sid_empty", {"sid": "sid_empty"})
        # No exception = pass


# ---------------------------------------------------------------------------
# Tests: constructor
# ---------------------------------------------------------------------------

class TestConstructor:
    def test_defaults(self, _mod, mock_redis):
        """Constructor should use defaults for optional parameters."""
        instance = _mod.DisconnectCleanup(mock_redis)
        assert instance.grace_period == 60
        assert instance._callbacks == []
        assert instance.is_running is False

    def test_custom_callbacks(self, _mod, mock_redis):
        """Constructor should accept initial callbacks list."""
        cb = MagicMock()
        instance = _mod.DisconnectCleanup(mock_redis, cleanup_callbacks=[cb])
        assert cb in instance._callbacks

    def test_custom_grace_period(self, _mod, mock_redis):
        """Constructor should accept custom grace period."""
        instance = _mod.DisconnectCleanup(mock_redis, grace_period=120)
        assert instance.grace_period == 120
