"""Unit tests for FeatureFlags.

Validates that:
1. Environment variable override takes highest priority
2. Per-project Redis flag overrides global
3. Global Redis flag is used when no project override
4. Default is False when no env/Redis value exists
5. set_flag correctly writes to Redis (global and per-project)
6. delete_flag removes Redis keys
7. get_all_flags returns state for all known flags
8. list_overrides returns only flags with explicit Redis values
9. Byte vs string responses handled (decode_responses=True/False)
10. Invalid/empty env values handled gracefully

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_feature_flags.py -v
"""

import importlib.util
import os
import pathlib
import sys
import types
from unittest.mock import MagicMock, patch

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

# Load the module under test
_mod_path = _PLUGIN_ROOT / "utils" / "feature_flags.py"
_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.utils.feature_flags", _mod_path
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

FeatureFlags = _mod.FeatureFlags
KNOWN_FLAGS = _mod.KNOWN_FLAGS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_redis():
    client = MagicMock()
    client.get.return_value = None
    client.set.return_value = True
    client.delete.return_value = 0
    return client


@pytest.fixture
def ff(mock_redis):
    return FeatureFlags(mock_redis)


@pytest.fixture(autouse=True)
def clean_env():
    """Remove any FF_ env vars before each test."""
    env_keys = [k for k in os.environ if k.startswith("FF_")]
    for k in env_keys:
        del os.environ[k]
    yield
    env_keys = [k for k in os.environ if k.startswith("FF_")]
    for k in env_keys:
        del os.environ[k]


# ---------------------------------------------------------------------------
# Tests: Environment variable priority
# ---------------------------------------------------------------------------


class TestEnvVarPriority:
    def test_env_true_values(self, ff):
        for val in ("1", "true", "yes", "True", "YES", "TRUE"):
            os.environ["FF_REDIS_STATE_ENABLED"] = val
            assert ff.is_enabled("REDIS_STATE_ENABLED") is True

    def test_env_false_values(self, ff):
        for val in ("0", "false", "no", "False", "NO", "FALSE"):
            os.environ["FF_REDIS_STATE_ENABLED"] = val
            assert ff.is_enabled("REDIS_STATE_ENABLED") is False

    def test_env_unrecognized_value_is_false(self, ff):
        os.environ["FF_REDIS_STATE_ENABLED"] = "maybe"
        assert ff.is_enabled("REDIS_STATE_ENABLED") is False

    def test_env_empty_string_is_false(self, ff):
        os.environ["FF_REDIS_STATE_ENABLED"] = ""
        assert ff.is_enabled("REDIS_STATE_ENABLED") is False

    def test_env_overrides_redis_global(self, ff, mock_redis):
        mock_redis.get.return_value = "1"
        os.environ["FF_REDIS_STATE_ENABLED"] = "0"
        assert ff.is_enabled("REDIS_STATE_ENABLED") is False
        mock_redis.get.assert_not_called()

    def test_env_overrides_redis_project(self, ff, mock_redis):
        mock_redis.get.return_value = "1"
        os.environ["FF_REDIS_STATE_ENABLED"] = "no"
        assert ff.is_enabled("REDIS_STATE_ENABLED", project_id=42) is False
        mock_redis.get.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Per-project Redis override
# ---------------------------------------------------------------------------


class TestProjectOverride:
    def test_project_enabled(self, ff, mock_redis):
        mock_redis.get.return_value = "1"
        assert ff.is_enabled("REDIS_STATE_ENABLED", project_id=42) is True
        mock_redis.get.assert_called_once_with("feature_flags:42:REDIS_STATE_ENABLED")

    def test_project_disabled(self, ff, mock_redis):
        mock_redis.get.return_value = "0"
        assert ff.is_enabled("REDIS_STATE_ENABLED", project_id=42) is False

    def test_project_none_falls_through_to_global(self, ff, mock_redis):
        def side_effect(key):
            if "42:" in key:
                return None
            return "1"
        mock_redis.get.side_effect = side_effect
        assert ff.is_enabled("REDIS_STATE_ENABLED", project_id=42) is True
        assert mock_redis.get.call_count == 2

    def test_project_bytes_response(self, ff, mock_redis):
        mock_redis.get.return_value = b"1"
        assert ff.is_enabled("REDIS_STATE_ENABLED", project_id=7) is True

    def test_project_bytes_disabled(self, ff, mock_redis):
        mock_redis.get.return_value = b"0"
        assert ff.is_enabled("REDIS_STATE_ENABLED", project_id=7) is False


# ---------------------------------------------------------------------------
# Tests: Global Redis flag
# ---------------------------------------------------------------------------


class TestGlobalFlag:
    def test_global_enabled(self, ff, mock_redis):
        mock_redis.get.return_value = "1"
        assert ff.is_enabled("SOCKETIO_REDIS_ENABLED") is True
        mock_redis.get.assert_called_once_with("feature_flags:global:SOCKETIO_REDIS_ENABLED")

    def test_global_disabled(self, ff, mock_redis):
        mock_redis.get.return_value = "0"
        assert ff.is_enabled("SOCKETIO_REDIS_ENABLED") is False

    def test_global_bytes_response(self, ff, mock_redis):
        mock_redis.get.return_value = b"1"
        assert ff.is_enabled("SOCKETIO_REDIS_ENABLED") is True

    def test_global_none_returns_false(self, ff, mock_redis):
        mock_redis.get.return_value = None
        assert ff.is_enabled("SOCKETIO_REDIS_ENABLED") is False


# ---------------------------------------------------------------------------
# Tests: Default behavior
# ---------------------------------------------------------------------------


class TestDefault:
    def test_no_env_no_redis_returns_false(self, ff, mock_redis):
        mock_redis.get.return_value = None
        assert ff.is_enabled("REDIS_STREAMS_ENABLED") is False

    def test_unknown_flag_returns_false(self, ff, mock_redis):
        mock_redis.get.return_value = None
        assert ff.is_enabled("TOTALLY_UNKNOWN_FLAG") is False

    def test_no_project_id_skips_project_lookup(self, ff, mock_redis):
        mock_redis.get.return_value = None
        ff.is_enabled("REDIS_STATE_ENABLED")
        mock_redis.get.assert_called_once_with("feature_flags:global:REDIS_STATE_ENABLED")


# ---------------------------------------------------------------------------
# Tests: set_flag
# ---------------------------------------------------------------------------


class TestSetFlag:
    def test_set_global_enabled(self, ff, mock_redis):
        import json
        ff.set_flag("REDIS_STATE_ENABLED", True)
        key, value = mock_redis.set.call_args[0]
        assert key == "feature_flags:global:REDIS_STATE_ENABLED"
        data = json.loads(value)
        assert data["enabled"] is True
        assert data["rollout_pct"] == 100

    def test_set_global_disabled(self, ff, mock_redis):
        import json
        ff.set_flag("REDIS_STATE_ENABLED", False)
        key, value = mock_redis.set.call_args[0]
        assert key == "feature_flags:global:REDIS_STATE_ENABLED"
        data = json.loads(value)
        assert data["enabled"] is False

    def test_set_project_enabled(self, ff, mock_redis):
        import json
        ff.set_flag("SOCKETIO_REDIS_ENABLED", True, project_id=99)
        key, value = mock_redis.set.call_args[0]
        assert key == "feature_flags:99:SOCKETIO_REDIS_ENABLED"
        data = json.loads(value)
        assert data["enabled"] is True

    def test_set_project_disabled(self, ff, mock_redis):
        import json
        ff.set_flag("REDIS_STREAMS_ENABLED", False, project_id=5)
        key, value = mock_redis.set.call_args[0]
        assert key == "feature_flags:5:REDIS_STREAMS_ENABLED"
        data = json.loads(value)
        assert data["enabled"] is False


# ---------------------------------------------------------------------------
# Tests: delete_flag
# ---------------------------------------------------------------------------


class TestDeleteFlag:
    def test_delete_global_exists(self, ff, mock_redis):
        mock_redis.delete.return_value = 1
        assert ff.delete_flag("REDIS_STATE_ENABLED") is True
        mock_redis.delete.assert_called_once_with("feature_flags:global:REDIS_STATE_ENABLED")

    def test_delete_global_not_exists(self, ff, mock_redis):
        mock_redis.delete.return_value = 0
        assert ff.delete_flag("REDIS_STATE_ENABLED") is False

    def test_delete_project(self, ff, mock_redis):
        mock_redis.delete.return_value = 1
        assert ff.delete_flag("SOCKETIO_REDIS_ENABLED", project_id=42) is True
        mock_redis.delete.assert_called_once_with("feature_flags:42:SOCKETIO_REDIS_ENABLED")


# ---------------------------------------------------------------------------
# Tests: get_all_flags
# ---------------------------------------------------------------------------


class TestGetAllFlags:
    def test_all_disabled_by_default(self, ff, mock_redis):
        mock_redis.get.return_value = None
        result = ff.get_all_flags()
        assert result == {flag: False for flag in KNOWN_FLAGS}

    def test_all_enabled_via_redis(self, ff, mock_redis):
        mock_redis.get.return_value = "1"
        result = ff.get_all_flags()
        assert result == {flag: True for flag in KNOWN_FLAGS}

    def test_mixed_flags(self, ff, mock_redis):
        def side_effect(key):
            if "REDIS_STATE_ENABLED" in key:
                return "1"
            return None
        mock_redis.get.side_effect = side_effect
        result = ff.get_all_flags()
        assert result["REDIS_STATE_ENABLED"] is True
        assert result["SOCKETIO_REDIS_ENABLED"] is False
        assert result["REDIS_STREAMS_ENABLED"] is False

    def test_with_project_id(self, ff, mock_redis):
        def side_effect(key):
            if "42:" in key and "SOCKETIO" in key:
                return "1"
            return None
        mock_redis.get.side_effect = side_effect
        result = ff.get_all_flags(project_id=42)
        assert result["SOCKETIO_REDIS_ENABLED"] is True
        assert result["REDIS_STATE_ENABLED"] is False

    def test_env_takes_priority_in_get_all(self, ff, mock_redis):
        mock_redis.get.return_value = None
        os.environ["FF_REDIS_STREAMS_ENABLED"] = "1"
        result = ff.get_all_flags()
        assert result["REDIS_STREAMS_ENABLED"] is True
        assert result["REDIS_STATE_ENABLED"] is False


# ---------------------------------------------------------------------------
# Tests: list_overrides
# ---------------------------------------------------------------------------


class TestListOverrides:
    def test_no_overrides(self, ff, mock_redis):
        mock_redis.get.return_value = None
        result = ff.list_overrides()
        assert result == {}

    def test_global_override(self, ff, mock_redis):
        def side_effect(key):
            if "REDIS_STATE_ENABLED" in key:
                return "1"
            return None
        mock_redis.get.side_effect = side_effect
        result = ff.list_overrides()
        assert result == {"REDIS_STATE_ENABLED": True}

    def test_project_override(self, ff, mock_redis):
        def side_effect(key):
            if "SOCKETIO_REDIS_ENABLED" in key:
                return "0"
            return None
        mock_redis.get.side_effect = side_effect
        result = ff.list_overrides(project_id=10)
        assert result == {"SOCKETIO_REDIS_ENABLED": False}

    def test_bytes_in_list_overrides(self, ff, mock_redis):
        mock_redis.get.return_value = b"1"
        result = ff.list_overrides()
        assert all(v is True for v in result.values())
        assert len(result) == len(KNOWN_FLAGS)

    def test_multiple_overrides(self, ff, mock_redis):
        def side_effect(key):
            if "REDIS_STATE_ENABLED" in key:
                return "1"
            if "REDIS_STREAMS_ENABLED" in key:
                return "0"
            return None
        mock_redis.get.side_effect = side_effect
        result = ff.list_overrides()
        assert result == {"REDIS_STATE_ENABLED": True, "REDIS_STREAMS_ENABLED": False}


# ---------------------------------------------------------------------------
# Tests: KNOWN_FLAGS constant
# ---------------------------------------------------------------------------


class TestKnownFlags:
    def test_known_flags_is_tuple(self):
        assert isinstance(KNOWN_FLAGS, tuple)

    def test_all_required_flags_present(self):
        assert "REDIS_STATE_ENABLED" in KNOWN_FLAGS
        assert "SOCKETIO_REDIS_ENABLED" in KNOWN_FLAGS
        assert "REDIS_STREAMS_ENABLED" in KNOWN_FLAGS

    def test_known_flags_count(self):
        assert len(KNOWN_FLAGS) == 7
