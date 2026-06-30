"""Unit tests for StateReconstruction.

Validates that:
1. Registries (toolkit_schemas, index_types, mcp_prebuilt_configs) are checked via HLEN
2. Sessions (mcp_servers, asr_sessions) are counted via SCAN
3. Callbacks are counted via SCAN
4. Missing registries trigger re-request via event_node.emit
5. event_node=None skips re-request gracefully
6. Redis errors are caught and logged (no crash)
7. Summary dict contains correct structure and counts
8. Log output differentiates warm (all populated) vs cold (missing data) startup
9. SCAN pagination works (multi-cursor iteration)
10. Individual check failures don't block other checks

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_state_reconstruction.py -v
"""

import importlib.util
import pathlib
import sys
import types
from unittest.mock import MagicMock, call, patch

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
_mod_path = _PLUGIN_ROOT / "utils" / "state_reconstruction.py"
_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.utils.state_reconstruction", _mod_path
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

StateReconstruction = _mod.StateReconstruction


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_redis():
    """Create a mock Redis client."""
    client = MagicMock()
    client.hlen.return_value = 0
    client.scan.return_value = (0, [])
    return client


@pytest.fixture
def mock_event_node():
    """Create a mock event_node."""
    return MagicMock()


@pytest.fixture
def reconstruction(mock_redis, mock_event_node):
    """Create a StateReconstruction instance with mocks."""
    return StateReconstruction(redis_client=mock_redis, event_node=mock_event_node)


@pytest.fixture
def reconstruction_no_event_node(mock_redis):
    """Create a StateReconstruction instance without event_node."""
    return StateReconstruction(redis_client=mock_redis, event_node=None)


# ---------------------------------------------------------------------------
# Tests: Basic Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_creates_with_redis_and_event_node(self, mock_redis, mock_event_node):
        sr = StateReconstruction(redis_client=mock_redis, event_node=mock_event_node)
        assert sr._client is mock_redis
        assert sr._event_node is mock_event_node

    def test_creates_with_redis_only(self, mock_redis):
        sr = StateReconstruction(redis_client=mock_redis)
        assert sr._client is mock_redis
        assert sr._event_node is None

    def test_creates_with_explicit_none_event_node(self, mock_redis):
        sr = StateReconstruction(redis_client=mock_redis, event_node=None)
        assert sr._event_node is None


# ---------------------------------------------------------------------------
# Tests: Registry Checks
# ---------------------------------------------------------------------------


class TestRegistryChecks:
    def test_populated_registries_counted(self, reconstruction, mock_redis):
        mock_redis.hlen.side_effect = [10, 3, 5]  # toolkit, index, prebuilt
        result = reconstruction.run()
        assert result["registries"]["toolkit_schemas"] == 10
        assert result["registries"]["index_types"] == 3
        assert result["registries"]["mcp_prebuilt_configs"] == 5
        assert result["missing_registries"] == []

    def test_empty_registry_marked_missing(self, reconstruction, mock_redis):
        mock_redis.hlen.side_effect = [10, 0, 5]
        result = reconstruction.run()
        assert result["registries"]["index_types"] == 0
        assert "index_types" in result["missing_registries"]

    def test_all_registries_empty(self, reconstruction, mock_redis):
        mock_redis.hlen.return_value = 0
        result = reconstruction.run()
        assert len(result["missing_registries"]) == 3
        assert "toolkit_schemas" in result["missing_registries"]
        assert "index_types" in result["missing_registries"]
        assert "mcp_prebuilt_configs" in result["missing_registries"]

    def test_registry_error_marked_negative(self, reconstruction, mock_redis):
        mock_redis.hlen.side_effect = [Exception("connection lost"), 5, 3]
        result = reconstruction.run()
        assert result["registries"]["toolkit_schemas"] == -1
        assert "toolkit_schemas" in result["missing_registries"]
        assert result["registries"]["index_types"] == 5

    def test_total_keys_includes_registries(self, reconstruction, mock_redis):
        mock_redis.hlen.side_effect = [10, 3, 5]
        result = reconstruction.run()
        assert result["total_keys_found"] >= 18  # 10 + 3 + 5

    def test_hlen_called_with_correct_keys(self, reconstruction, mock_redis):
        mock_redis.hlen.return_value = 1
        reconstruction.run()
        calls = mock_redis.hlen.call_args_list
        keys_checked = [c[0][0] for c in calls]
        assert "toolkit_schemas:global" in keys_checked
        assert "index_types:global" in keys_checked
        assert "mcp_prebuilt_configs:global" in keys_checked


# ---------------------------------------------------------------------------
# Tests: Re-request via event_node
# ---------------------------------------------------------------------------


class TestReRequest:
    def test_missing_registry_triggers_event_emit(self, reconstruction, mock_redis, mock_event_node):
        mock_redis.hlen.return_value = 0
        result = reconstruction.run()
        assert "toolkit_schemas" in result["re_requested"]
        assert "index_types" in result["re_requested"]
        assert "mcp_prebuilt_configs" in result["re_requested"]
        assert mock_event_node.emit.call_count == 3

    def test_emits_correct_event_names(self, reconstruction, mock_redis, mock_event_node):
        mock_redis.hlen.return_value = 0
        reconstruction.run()
        event_names = [c[0][0] for c in mock_event_node.emit.call_args_list]
        assert "application_toolkits_request" in event_names
        assert "application_file_loaders_request" in event_names
        assert "application_mcp_prebuilt_config_request" in event_names

    def test_emit_passes_empty_dict(self, reconstruction, mock_redis, mock_event_node):
        mock_redis.hlen.return_value = 0
        reconstruction.run()
        for c in mock_event_node.emit.call_args_list:
            assert c[0][1] == dict()

    def test_no_event_node_skips_re_request(self, reconstruction_no_event_node, mock_redis):
        mock_redis.hlen.return_value = 0
        result = reconstruction_no_event_node.run()
        assert result["re_requested"] == []
        assert len(result["missing_registries"]) == 3

    def test_populated_registry_not_re_requested(self, reconstruction, mock_redis, mock_event_node):
        mock_redis.hlen.side_effect = [10, 3, 5]
        result = reconstruction.run()
        assert result["re_requested"] == []
        mock_event_node.emit.assert_not_called()

    def test_emit_failure_caught_gracefully(self, reconstruction, mock_redis, mock_event_node):
        mock_redis.hlen.return_value = 0
        mock_event_node.emit.side_effect = Exception("event_node down")
        result = reconstruction.run()
        assert result["re_requested"] == []


# ---------------------------------------------------------------------------
# Tests: Session Checks
# ---------------------------------------------------------------------------


class TestSessionChecks:
    def test_counts_mcp_server_sessions(self, reconstruction, mock_redis):
        mock_redis.hlen.return_value = 1
        mock_redis.scan.side_effect = [
            (1, ["mcp_servers:proj1", "mcp_servers:proj2"]),
            (0, ["mcp_servers:proj3"]),
            (0, []),  # callback scan
        ]
        result = reconstruction.run()
        assert result["sessions"]["mcp_servers"] == 3

    def test_counts_asr_sessions(self, reconstruction, mock_redis):
        mock_redis.hlen.return_value = 1
        mock_redis.scan.side_effect = [
            (0, []),  # mcp scan
            (0, ["asr_session:sid1", "asr_session:sid2"]),  # asr scan
            (0, []),  # callback scan
        ]
        result = reconstruction.run()
        assert result["sessions"]["asr_sessions"] == 2

    def test_session_error_marked_negative(self, reconstruction, mock_redis):
        mock_redis.hlen.return_value = 1
        mock_redis.scan.side_effect = Exception("timeout")
        result = reconstruction.run()
        assert result["sessions"]["mcp_servers"] == -1
        assert result["sessions"]["asr_sessions"] == -1

    def test_session_counts_in_total(self, reconstruction, mock_redis):
        mock_redis.hlen.return_value = 5
        mock_redis.scan.side_effect = [
            (0, ["mcp_servers:a", "mcp_servers:b"]),  # mcp
            (0, ["asr_session:x"]),  # asr
            (0, []),  # callbacks
        ]
        result = reconstruction.run()
        assert result["total_keys_found"] == 5 + 5 + 5 + 2 + 1  # 3 registries + mcp + asr


# ---------------------------------------------------------------------------
# Tests: Callback Checks
# ---------------------------------------------------------------------------


class TestCallbackChecks:
    def test_counts_pending_callbacks(self, reconstruction, mock_redis):
        mock_redis.hlen.return_value = 1
        mock_redis.scan.side_effect = [
            (0, []),  # mcp
            (0, []),  # asr
            (0, ["callback_tasks:t1", "callback_tasks:t2", "callback_tasks:t3"]),
        ]
        result = reconstruction.run()
        assert result["callbacks"] == 3

    def test_callback_error_marked_negative(self, reconstruction, mock_redis):
        mock_redis.hlen.return_value = 1
        # First two scans succeed (sessions), third fails
        mock_redis.scan.side_effect = [
            (0, []),  # mcp
            (0, []),  # asr
            Exception("connection refused"),
        ]
        result = reconstruction.run()
        assert result["callbacks"] == -1

    def test_callbacks_in_total_keys(self, reconstruction, mock_redis):
        mock_redis.hlen.return_value = 0
        mock_redis.scan.side_effect = [
            (0, []),  # mcp
            (0, []),  # asr
            (0, ["callback_tasks:t1"]),
        ]
        result = reconstruction.run()
        assert result["total_keys_found"] >= 1


# ---------------------------------------------------------------------------
# Tests: SCAN Pagination
# ---------------------------------------------------------------------------


class TestScanPagination:
    def test_multi_page_scan(self, reconstruction, mock_redis):
        mock_redis.hlen.return_value = 1
        mock_redis.scan.side_effect = [
            # mcp_servers scan: 3 pages
            (42, ["mcp_servers:a"]),
            (99, ["mcp_servers:b", "mcp_servers:c"]),
            (0, ["mcp_servers:d"]),
            # asr scan: 1 page
            (0, []),
            # callbacks scan: 1 page
            (0, []),
        ]
        result = reconstruction.run()
        assert result["sessions"]["mcp_servers"] == 4

    def test_scan_called_with_correct_patterns(self, reconstruction, mock_redis):
        mock_redis.hlen.return_value = 1
        mock_redis.scan.return_value = (0, [])
        reconstruction.run()
        scan_patterns = [c[1]["match"] for c in mock_redis.scan.call_args_list]
        assert "mcp_servers:*" in scan_patterns
        assert "asr_session:*" in scan_patterns
        assert "callback_tasks:*" in scan_patterns

    def test_scan_uses_count_100(self, reconstruction, mock_redis):
        mock_redis.hlen.return_value = 1
        mock_redis.scan.return_value = (0, [])
        reconstruction.run()
        for c in mock_redis.scan.call_args_list:
            assert c[1]["count"] == 100


# ---------------------------------------------------------------------------
# Tests: Summary and Logging
# ---------------------------------------------------------------------------


class TestSummaryAndLogging:
    def test_warm_startup_logged(self, reconstruction, mock_redis):
        mock_redis.hlen.return_value = 5
        mock_redis.scan.return_value = (0, [])
        reconstruction.run()
        log_calls = _mock_log.info.call_args_list
        # Find the summary log call
        summary_call = [c for c in log_calls if "State reconstruction complete" in str(c)]
        assert len(summary_call) >= 1
        assert "warm" in str(summary_call[-1])

    def test_cold_startup_logged(self, reconstruction, mock_redis):
        _mock_log.reset_mock()
        mock_redis.hlen.return_value = 0
        mock_redis.scan.return_value = (0, [])
        reconstruction.run()
        log_calls = _mock_log.info.call_args_list
        summary_call = [c for c in log_calls if "State reconstruction complete" in str(c)]
        assert len(summary_call) >= 1
        assert "cold" in str(summary_call[-1])

    def test_summary_dict_structure(self, reconstruction, mock_redis):
        mock_redis.hlen.return_value = 0
        mock_redis.scan.return_value = (0, [])
        result = reconstruction.run()
        assert "registries" in result
        assert "sessions" in result
        assert "callbacks" in result
        assert "total_keys_found" in result
        assert "missing_registries" in result
        assert "re_requested" in result

    def test_run_returns_dict(self, reconstruction, mock_redis):
        mock_redis.hlen.return_value = 0
        mock_redis.scan.return_value = (0, [])
        result = reconstruction.run()
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Tests: Error Isolation
# ---------------------------------------------------------------------------


class TestErrorIsolation:
    def test_registry_error_doesnt_block_sessions(self, reconstruction, mock_redis):
        mock_redis.hlen.side_effect = Exception("redis error")
        mock_redis.scan.return_value = (0, ["mcp_servers:a"])
        result = reconstruction.run()
        # Registries all failed
        for name in ["toolkit_schemas", "index_types", "mcp_prebuilt_configs"]:
            assert result["registries"][name] == -1
        # But sessions still counted
        assert result["sessions"]["mcp_servers"] == 1

    def test_session_error_doesnt_block_callbacks(self, reconstruction, mock_redis):
        mock_redis.hlen.return_value = 5
        call_count = [0]
        original_scan = mock_redis.scan

        def scan_side_effect(cursor, match=None, count=100):
            call_count[0] += 1
            if "mcp_servers" in (match or "") or "asr_session" in (match or ""):
                raise Exception("session scan failed")
            return (0, ["callback_tasks:t1"])

        mock_redis.scan.side_effect = scan_side_effect
        result = reconstruction.run()
        assert result["sessions"]["mcp_servers"] == -1
        assert result["sessions"]["asr_sessions"] == -1
        assert result["callbacks"] == 1

    def test_all_checks_fail_gracefully(self, reconstruction, mock_redis):
        mock_redis.hlen.side_effect = Exception("dead")
        mock_redis.scan.side_effect = Exception("dead")
        result = reconstruction.run()
        assert isinstance(result, dict)
        assert result["total_keys_found"] == 0


# ---------------------------------------------------------------------------
# Tests: Integration with module.py (import check)
# ---------------------------------------------------------------------------


class TestModuleIntegration:
    def test_can_import_state_reconstruction(self):
        """Verify the module can be imported without errors."""
        assert hasattr(_mod, "StateReconstruction")
        assert callable(StateReconstruction)

    def test_run_method_exists(self):
        assert hasattr(StateReconstruction, "run")

    def test_all_private_methods_exist(self):
        sr = StateReconstruction(redis_client=MagicMock())
        assert hasattr(sr, "_check_registries")
        assert hasattr(sr, "_check_sessions")
        assert hasattr(sr, "_check_callbacks")
        assert hasattr(sr, "_re_request")
        assert hasattr(sr, "_scan_count")
        assert hasattr(sr, "_log_summary")
