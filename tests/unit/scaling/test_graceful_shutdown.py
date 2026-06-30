"""Unit tests for graceful_shutdown module."""

import sys
import time
import threading
from unittest.mock import MagicMock, patch, call
import importlib.util

import pytest


# Load module via importlib to avoid plugin import chain
def _load_module():
    spec = importlib.util.spec_from_file_location(
        "graceful_shutdown",
        "/Users/Alexander_Kharkevich/projects/eliteaai/centry/pylon_main/plugins/elitea_core/utils/graceful_shutdown.py",
    )
    # Mock pylon.core.tools.log
    mock_log_module = MagicMock()
    mock_log_module.info = MagicMock()
    mock_log_module.warning = MagicMock()
    mock_log_module.debug = MagicMock()
    sys.modules.setdefault("pylon", MagicMock())
    sys.modules.setdefault("pylon.core", MagicMock())
    sys.modules.setdefault("pylon.core.tools", MagicMock())
    sys.modules["pylon.core.tools"].log = mock_log_module
    sys.modules.setdefault("pylon.core.tools.log", mock_log_module)

    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()
GracefulShutdown = _mod.GracefulShutdown


@pytest.fixture
def mock_sio():
    sio = MagicMock()
    manager = MagicMock()
    sio.manager = manager
    return sio


@pytest.fixture
def mock_redis():
    client = MagicMock()
    client.ping.return_value = True
    return client


@pytest.fixture
def shutdown(mock_sio, mock_redis):
    return GracefulShutdown(sio=mock_sio, redis_client=mock_redis, drain_timeout=15)


class TestGracefulShutdownInit:
    def test_creates_with_defaults(self, mock_sio):
        gs = GracefulShutdown(sio=mock_sio)
        assert gs._sio is mock_sio
        assert gs._redis_client is None
        assert gs._drain_timeout == 15
        assert not gs.is_shutting_down

    def test_creates_with_custom_timeout(self, mock_sio, mock_redis):
        gs = GracefulShutdown(sio=mock_sio, redis_client=mock_redis, drain_timeout=30)
        assert gs._drain_timeout == 30

    def test_is_shutting_down_initially_false(self, shutdown):
        assert not shutdown.is_shutting_down


class TestGracefulShutdownExecute:
    def test_sets_shutting_down_flag(self, shutdown, mock_sio):
        mock_sio.manager.get_participants.return_value = []
        shutdown.execute()
        assert shutdown.is_shutting_down

    def test_disconnects_clients(self, shutdown, mock_sio):
        mock_sio.manager.get_participants.return_value = [
            ("sid1", "eio1"),
            ("sid2", "eio2"),
        ]
        shutdown.execute()
        mock_sio.emit.assert_any_call(
            "server_shutting_down", {"reason": "pod_terminating"}, to="sid1"
        )
        mock_sio.emit.assert_any_call(
            "server_shutting_down", {"reason": "pod_terminating"}, to="sid2"
        )
        mock_sio.disconnect.assert_any_call("sid1")
        mock_sio.disconnect.assert_any_call("sid2")

    def test_no_clients_connected(self, shutdown, mock_sio):
        mock_sio.manager.get_participants.return_value = []
        shutdown.execute()
        mock_sio.disconnect.assert_not_called()

    def test_flushes_redis(self, shutdown, mock_sio, mock_redis):
        mock_sio.manager.get_participants.return_value = []
        shutdown.execute()
        mock_redis.ping.assert_called_once()

    def test_no_redis_client(self, mock_sio):
        gs = GracefulShutdown(sio=mock_sio, redis_client=None)
        mock_sio.manager.get_participants.return_value = []
        gs.execute()
        # Should not crash

    def test_redis_failure_during_flush(self, shutdown, mock_sio, mock_redis):
        mock_sio.manager.get_participants.return_value = []
        mock_redis.ping.side_effect = ConnectionError("Redis down")
        shutdown.execute()
        # Should not raise


class TestDisconnectSIOClients:
    def test_emit_failure_does_not_prevent_disconnect(self, shutdown, mock_sio):
        mock_sio.manager.get_participants.return_value = [("sid1", "eio1")]
        mock_sio.emit.side_effect = Exception("emit failed")
        shutdown.execute()
        mock_sio.disconnect.assert_called_once_with("sid1")

    def test_disconnect_failure_logged(self, shutdown, mock_sio):
        mock_sio.manager.get_participants.return_value = [
            ("sid1", "eio1"),
            ("sid2", "eio2"),
        ]
        mock_sio.disconnect.side_effect = [Exception("fail"), None]
        shutdown.execute()
        # Should complete without raising, second still attempted
        assert mock_sio.disconnect.call_count == 2

    def test_get_participants_exception(self, shutdown, mock_sio):
        mock_sio.manager.get_participants.side_effect = Exception("no manager")
        shutdown.execute()
        mock_sio.disconnect.assert_not_called()

    def test_multiple_clients_all_disconnected(self, shutdown, mock_sio):
        sids = [(f"sid{i}", f"eio{i}") for i in range(5)]
        mock_sio.manager.get_participants.return_value = sids
        shutdown.execute()
        assert mock_sio.disconnect.call_count == 5


class TestGetConnectedSids:
    def test_returns_sids_from_default_namespace(self, shutdown, mock_sio):
        mock_sio.manager.get_participants.return_value = [
            ("sid_a", "eio_a"),
            ("sid_b", "eio_b"),
        ]
        result = shutdown._get_connected_sids()
        assert result == ["sid_a", "sid_b"]
        mock_sio.manager.get_participants.assert_called_with("/", None)

    def test_returns_empty_on_exception(self, shutdown, mock_sio):
        mock_sio.manager.get_participants.side_effect = Exception("broken")
        result = shutdown._get_connected_sids()
        assert result == []


class TestFlushRedis:
    def test_ping_success(self, shutdown, mock_redis):
        shutdown._flush_redis()
        mock_redis.ping.assert_called_once()

    def test_ping_failure_handled(self, shutdown, mock_redis):
        mock_redis.ping.side_effect = ConnectionError("timeout")
        shutdown._flush_redis()
        # No exception

    def test_no_redis_client_noop(self, mock_sio):
        gs = GracefulShutdown(sio=mock_sio, redis_client=None)
        gs._flush_redis()
        # No exception


class TestIsShuttingDown:
    def test_thread_safe_flag(self, mock_sio):
        gs = GracefulShutdown(sio=mock_sio)
        mock_sio.manager.get_participants.return_value = []
        assert not gs.is_shutting_down

        results = []

        def check_flag():
            time.sleep(0.01)
            results.append(gs.is_shutting_down)

        t = threading.Thread(target=check_flag)
        t.start()
        gs.execute()
        t.join()
        assert gs.is_shutting_down
        assert results[0] is True


class TestExecuteOrdering:
    def test_emit_before_disconnect(self, shutdown, mock_sio):
        mock_sio.manager.get_participants.return_value = [("sid1", "eio1")]
        call_order = []
        mock_sio.emit.side_effect = lambda *a, **kw: call_order.append("emit")
        mock_sio.disconnect.side_effect = lambda *a, **kw: call_order.append("disconnect")
        shutdown.execute()
        assert call_order == ["emit", "disconnect"]

    def test_shutdown_flag_set_before_disconnect(self, shutdown, mock_sio):
        flag_state_during_disconnect = []

        def capture_flag(*args, **kwargs):
            flag_state_during_disconnect.append(shutdown.is_shutting_down)

        mock_sio.manager.get_participants.return_value = [("sid1", "eio1")]
        mock_sio.disconnect.side_effect = capture_flag
        shutdown.execute()
        assert flag_state_during_disconnect == [True]


class TestEdgeCases:
    def test_execute_idempotent(self, shutdown, mock_sio):
        mock_sio.manager.get_participants.return_value = [("sid1", "eio1")]
        shutdown.execute()
        mock_sio.manager.get_participants.return_value = []
        shutdown.execute()
        # Second call should work fine even if no clients

    def test_large_number_of_clients(self, shutdown, mock_sio):
        sids = [(f"sid{i}", f"eio{i}") for i in range(100)]
        mock_sio.manager.get_participants.return_value = sids
        shutdown.execute()
        assert mock_sio.disconnect.call_count == 100

    def test_drain_timeout_zero(self, mock_sio, mock_redis):
        gs = GracefulShutdown(sio=mock_sio, redis_client=mock_redis, drain_timeout=0)
        mock_sio.manager.get_participants.return_value = [("sid1", "eio1")]
        gs.execute()
        mock_sio.disconnect.assert_called_once_with("sid1")
