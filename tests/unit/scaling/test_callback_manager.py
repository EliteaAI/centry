"""Unit tests for CallbackManager.

Validates that:
1. register_callback stores data in Redis with correct key structure
2. get_callback retrieves without removing
3. pop_callback retrieves AND removes atomically (GETDEL)
4. remove_callback deletes without returning data
5. exists checks presence correctly
6. TTL is applied on registration
7. None handling for missing keys
8. JSON serialization/deserialization of callback data

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_callback_manager.py -v
"""

import importlib
import importlib.util
import json
import pathlib
import sys
import types
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Module loading setup: mock pylon.core.tools so the module can be loaded
# without the full pylon framework installed.
# ---------------------------------------------------------------------------

_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[3] / "pylon_main" / "plugins" / "elitea_core"

# Mock pylon.core.tools (for log import)
_mock_log = MagicMock()
_mock_pylon_core_tools = MagicMock()
_mock_pylon_core_tools.log = _mock_log
sys.modules.setdefault("pylon", MagicMock())
sys.modules.setdefault("pylon.core", MagicMock())
sys.modules.setdefault("pylon.core.tools", _mock_pylon_core_tools)

# Create package hierarchy so relative imports work (module has no relative imports,
# but set it up for consistency with the test suite pattern)
_utils_pkg = types.ModuleType("centry.pylon_main.plugins.elitea_core.utils")
_utils_pkg.__path__ = [str(_PLUGIN_ROOT / "utils")]
_utils_pkg.__package__ = "centry.pylon_main.plugins.elitea_core.utils"
sys.modules.setdefault("centry.pylon_main.plugins.elitea_core.utils", _utils_pkg)

_plugin_pkg = types.ModuleType("centry.pylon_main.plugins.elitea_core")
_plugin_pkg.__path__ = [str(_PLUGIN_ROOT)]
_plugin_pkg.__package__ = "centry.pylon_main.plugins.elitea_core"
sys.modules.setdefault("centry.pylon_main.plugins.elitea_core", _plugin_pkg)

# Load the callback_manager module
_module_path = _PLUGIN_ROOT / "utils" / "callback_manager.py"
_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.utils.callback_manager",
    _module_path,
    submodule_search_locations=[],
)
_mod = importlib.util.module_from_spec(_spec)
_mod.__package__ = "centry.pylon_main.plugins.elitea_core.utils"
sys.modules["centry.pylon_main.plugins.elitea_core.utils.callback_manager"] = _mod
_spec.loader.exec_module(_mod)

CallbackManager = _mod.CallbackManager
DEFAULT_TTL = _mod.DEFAULT_TTL


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis():
    """Create a mock Redis client."""
    client = MagicMock()
    client.get.return_value = None
    client.getdel.return_value = None
    client.delete.return_value = 0
    client.exists.return_value = 0
    return client


@pytest.fixture
def manager(mock_redis):
    """Create a CallbackManager with mocked Redis client."""
    return CallbackManager(redis_client=mock_redis, ttl=DEFAULT_TTL)


# ---------------------------------------------------------------------------
# Tests: Key generation
# ---------------------------------------------------------------------------

class TestKeyGeneration:
    def test_key_format(self, manager):
        assert manager._key("task-abc-123") == "callback_tasks:task-abc-123"

    def test_key_with_special_chars(self, manager):
        assert manager._key("task/with:special") == "callback_tasks:task/with:special"

    def test_key_empty_string(self, manager):
        assert manager._key("") == "callback_tasks:"


# ---------------------------------------------------------------------------
# Tests: register_callback
# ---------------------------------------------------------------------------

class TestRegisterCallback:
    def test_register_stores_json_with_ttl(self, manager, mock_redis):
        manager.register_callback("task-1", "https://example.com/hook", {"X-Token": "abc"})

        expected_data = json.dumps({
            "callback_url": "https://example.com/hook",
            "callback_headers": {"X-Token": "abc"},
        })
        mock_redis.set.assert_called_once_with(
            "callback_tasks:task-1", expected_data, ex=DEFAULT_TTL
        )

    def test_register_without_headers(self, manager, mock_redis):
        manager.register_callback("task-2", "https://example.com/hook")

        expected_data = json.dumps({
            "callback_url": "https://example.com/hook",
            "callback_headers": None,
        })
        mock_redis.set.assert_called_once_with(
            "callback_tasks:task-2", expected_data, ex=DEFAULT_TTL
        )

    def test_register_with_empty_headers(self, manager, mock_redis):
        manager.register_callback("task-3", "https://example.com/hook", {})

        expected_data = json.dumps({
            "callback_url": "https://example.com/hook",
            "callback_headers": {},
        })
        mock_redis.set.assert_called_once_with(
            "callback_tasks:task-3", expected_data, ex=DEFAULT_TTL
        )

    def test_register_with_multiple_headers(self, manager, mock_redis):
        headers = {"Authorization": "Bearer xyz", "X-Custom": "value", "Content-Type": "application/json"}
        manager.register_callback("task-4", "https://api.example.com/callback", headers)

        call_args = mock_redis.set.call_args
        stored_data = json.loads(call_args[0][1])
        assert stored_data["callback_url"] == "https://api.example.com/callback"
        assert stored_data["callback_headers"] == headers

    def test_register_uses_custom_ttl(self, mock_redis):
        custom_ttl = 7200
        mgr = CallbackManager(redis_client=mock_redis, ttl=custom_ttl)
        mgr.register_callback("task-5", "https://example.com/hook")

        call_args = mock_redis.set.call_args
        assert call_args[1]["ex"] == custom_ttl or call_args[0][2] == custom_ttl

    def test_register_overwrites_existing(self, manager, mock_redis):
        """Registering same task_id twice overwrites (SET is idempotent)."""
        manager.register_callback("task-dup", "https://first.com/hook")
        manager.register_callback("task-dup", "https://second.com/hook")

        assert mock_redis.set.call_count == 2
        last_call = mock_redis.set.call_args
        stored_data = json.loads(last_call[0][1])
        assert stored_data["callback_url"] == "https://second.com/hook"


# ---------------------------------------------------------------------------
# Tests: get_callback
# ---------------------------------------------------------------------------

class TestGetCallback:
    def test_get_existing_callback(self, manager, mock_redis):
        stored = json.dumps({"callback_url": "https://example.com/hook", "callback_headers": {"X-Token": "abc"}})
        mock_redis.get.return_value = stored

        result = manager.get_callback("task-1")

        mock_redis.get.assert_called_once_with("callback_tasks:task-1")
        assert result == {"callback_url": "https://example.com/hook", "callback_headers": {"X-Token": "abc"}}

    def test_get_nonexistent_callback(self, manager, mock_redis):
        mock_redis.get.return_value = None

        result = manager.get_callback("task-missing")

        assert result is None

    def test_get_does_not_delete(self, manager, mock_redis):
        stored = json.dumps({"callback_url": "https://example.com/hook", "callback_headers": None})
        mock_redis.get.return_value = stored

        manager.get_callback("task-1")

        mock_redis.delete.assert_not_called()
        mock_redis.getdel.assert_not_called()

    def test_get_with_null_headers(self, manager, mock_redis):
        stored = json.dumps({"callback_url": "https://example.com/hook", "callback_headers": None})
        mock_redis.get.return_value = stored

        result = manager.get_callback("task-1")

        assert result["callback_headers"] is None


# ---------------------------------------------------------------------------
# Tests: pop_callback
# ---------------------------------------------------------------------------

class TestPopCallback:
    def test_pop_existing_callback(self, manager, mock_redis):
        stored = json.dumps({"callback_url": "https://example.com/hook", "callback_headers": {"X-Key": "val"}})
        mock_redis.getdel.return_value = stored

        result = manager.pop_callback("task-1")

        mock_redis.getdel.assert_called_once_with("callback_tasks:task-1")
        assert result == {"callback_url": "https://example.com/hook", "callback_headers": {"X-Key": "val"}}

    def test_pop_nonexistent_callback(self, manager, mock_redis):
        mock_redis.getdel.return_value = None

        result = manager.pop_callback("task-missing")

        assert result is None

    def test_pop_is_atomic(self, manager, mock_redis):
        """pop_callback uses GETDEL which is a single atomic operation."""
        stored = json.dumps({"callback_url": "https://example.com/hook", "callback_headers": None})
        mock_redis.getdel.return_value = stored

        manager.pop_callback("task-1")

        # Only getdel should be called, no separate get + delete
        mock_redis.getdel.assert_called_once()
        mock_redis.get.assert_not_called()
        mock_redis.delete.assert_not_called()

    def test_pop_with_complex_headers(self, manager, mock_redis):
        headers = {
            "Authorization": "Bearer very-long-token-value",
            "X-Request-ID": "req-12345",
            "X-Forwarded-For": "192.168.1.1",
        }
        stored = json.dumps({"callback_url": "https://api.example.com/webhook", "callback_headers": headers})
        mock_redis.getdel.return_value = stored

        result = manager.pop_callback("task-complex")

        assert result["callback_headers"] == headers


# ---------------------------------------------------------------------------
# Tests: remove_callback
# ---------------------------------------------------------------------------

class TestRemoveCallback:
    def test_remove_existing(self, manager, mock_redis):
        mock_redis.delete.return_value = 1

        result = manager.remove_callback("task-1")

        mock_redis.delete.assert_called_once_with("callback_tasks:task-1")
        assert result is True

    def test_remove_nonexistent(self, manager, mock_redis):
        mock_redis.delete.return_value = 0

        result = manager.remove_callback("task-missing")

        assert result is False


# ---------------------------------------------------------------------------
# Tests: exists
# ---------------------------------------------------------------------------

class TestExists:
    def test_exists_true(self, manager, mock_redis):
        mock_redis.exists.return_value = 1

        assert manager.exists("task-1") is True
        mock_redis.exists.assert_called_once_with("callback_tasks:task-1")

    def test_exists_false(self, manager, mock_redis):
        mock_redis.exists.return_value = 0

        assert manager.exists("task-missing") is False


# ---------------------------------------------------------------------------
# Tests: Integration scenarios
# ---------------------------------------------------------------------------

class TestIntegrationScenarios:
    def test_register_then_pop_flow(self, manager, mock_redis):
        """Simulates the typical predict -> task_status_changed flow."""
        # Register (predict API handler)
        manager.register_callback("task-abc", "https://client.com/webhook", {"Auth": "secret"})

        # Simulate pop (task_status_changed on potentially different pod)
        stored = json.dumps({"callback_url": "https://client.com/webhook", "callback_headers": {"Auth": "secret"}})
        mock_redis.getdel.return_value = stored
        result = manager.pop_callback("task-abc")

        assert result["callback_url"] == "https://client.com/webhook"
        assert result["callback_headers"] == {"Auth": "secret"}

    def test_pop_returns_none_for_race_loser(self, manager, mock_redis):
        """If two pods race on pop, only one gets the data."""
        mock_redis.getdel.return_value = None  # Second pod loses the race

        result = manager.pop_callback("task-abc")
        assert result is None

    def test_custom_ttl_constructor(self, mock_redis):
        """Verify custom TTL is passed through."""
        mgr = CallbackManager(redis_client=mock_redis, ttl=3600)
        mgr.register_callback("task-x", "https://example.com/hook")

        call_args = mock_redis.set.call_args
        assert call_args == (
            ("callback_tasks:task-x", json.dumps({"callback_url": "https://example.com/hook", "callback_headers": None})),
            {"ex": 3600},
        )

    def test_default_ttl_is_24_hours(self):
        assert DEFAULT_TTL == 86400

    def test_json_roundtrip_preserves_data(self, manager, mock_redis):
        """Ensure JSON serialization preserves all callback data types."""
        headers = {"X-Int": "123", "X-Empty": "", "X-Unicode": "héllo"}
        data = {"callback_url": "https://example.com/hook", "callback_headers": headers}

        # Capture what register stores
        manager.register_callback("task-json", "https://example.com/hook", headers)
        stored_json = mock_redis.set.call_args[0][1]

        # Simulate get returning the same data
        mock_redis.get.return_value = stored_json
        result = manager.get_callback("task-json")

        assert result == data
