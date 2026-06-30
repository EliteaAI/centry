"""Unit tests for streams monitoring.

Validates that:
1. StreamsMonitor._get_registered_streams() reads from metrics registry
2. check_stuck_consumers() detects pending messages older than threshold
3. check_inactive_groups() detects groups with zero active consumers
4. check_dlq_depth() detects DLQ streams exceeding depth threshold
5. check_stream() combines all per-stream checks
6. check_all() iterates all registered streams
7. get_streams_status() returns correct status based on anomalies
8. _get_stream_detail() returns length and group info
9. _get_stale_pending() counts stale messages via XPENDING range
10. Helper functions decode bytes/dict/list response formats
11. /health/streams endpoint integration
12. Edge cases: empty registry, Redis errors, no groups, no pending

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_streams_monitor.py -v
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

_events_pkg = types.ModuleType("centry.pylon_main.plugins.elitea_core.events")
_events_pkg.__path__ = [str(_PLUGIN_ROOT / "events")]
_events_pkg.__package__ = "centry.pylon_main.plugins.elitea_core.events"
sys.modules.setdefault("centry.pylon_main.plugins.elitea_core.events", _events_pkg)

_plugin_pkg = types.ModuleType("centry.pylon_main.plugins.elitea_core")
_plugin_pkg.__path__ = [str(_PLUGIN_ROOT)]
_plugin_pkg.__package__ = "centry.pylon_main.plugins.elitea_core"
sys.modules.setdefault("centry.pylon_main.plugins.elitea_core", _plugin_pkg)

# Load redis_streams first (dependency)
_rs_path = _PLUGIN_ROOT / "utils" / "redis_streams.py"
_rs_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.utils.redis_streams",
    _rs_path, submodule_search_locations=[],
)
_rs_mod = importlib.util.module_from_spec(_rs_spec)
sys.modules[_rs_spec.name] = _rs_mod
_rs_spec.loader.exec_module(_rs_mod)

# Load event_classification (dependency)
_ec_path = _PLUGIN_ROOT / "events" / "event_classification.py"
_ec_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.events.event_classification",
    _ec_path, submodule_search_locations=[],
)
_ec_mod = importlib.util.module_from_spec(_ec_spec)
sys.modules[_ec_spec.name] = _ec_mod
_ec_spec.loader.exec_module(_ec_mod)

# Load dead_letter_queue (dependency)
_dlq_path = _PLUGIN_ROOT / "utils" / "dead_letter_queue.py"
_dlq_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.utils.dead_letter_queue",
    _dlq_path, submodule_search_locations=[],
)
_dlq_mod = importlib.util.module_from_spec(_dlq_spec)
sys.modules[_dlq_spec.name] = _dlq_mod
_dlq_spec.loader.exec_module(_dlq_mod)

# Load event_metrics (dependency)
_em_path = _PLUGIN_ROOT / "utils" / "event_metrics.py"
_em_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.utils.event_metrics",
    _em_path, submodule_search_locations=[],
)
_em_mod = importlib.util.module_from_spec(_em_spec)
sys.modules[_em_spec.name] = _em_mod
_em_spec.loader.exec_module(_em_mod)

# Load streams_monitor (the module under test)
_module_path = _PLUGIN_ROOT / "utils" / "streams_monitor.py"
_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.utils.streams_monitor",
    _module_path, submodule_search_locations=[],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

StreamsMonitor = _mod.StreamsMonitor
DEFAULT_PENDING_AGE_THRESHOLD_MS = _mod.DEFAULT_PENDING_AGE_THRESHOLD_MS
DEFAULT_DLQ_DEPTH_THRESHOLD = _mod.DEFAULT_DLQ_DEPTH_THRESHOLD
DEFAULT_IDLE_CONSUMER_THRESHOLD_MS = _mod.DEFAULT_IDLE_CONSUMER_THRESHOLD_MS
_decode_field = _mod._decode_field
_int_field = _mod._int_field
_get_idle_time = _mod._get_idle_time


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def redis_mock():
    """Create a mock Redis client."""
    mock = MagicMock()
    mock.smembers.return_value = set()
    mock.xinfo_groups.return_value = []
    mock.xlen.return_value = 0
    mock.xpending_range.return_value = []
    return mock


@pytest.fixture
def monitor(redis_mock):
    """Create a StreamsMonitor instance with mock Redis."""
    return StreamsMonitor(redis_mock)


# ---------------------------------------------------------------------------
# Tests: _get_registered_streams
# ---------------------------------------------------------------------------


class TestGetRegisteredStreams:
    def test_empty_registry(self, monitor, redis_mock):
        redis_mock.smembers.return_value = set()
        result = monitor._get_registered_streams()
        assert result == []

    def test_returns_decoded_sorted_streams(self, monitor, redis_mock):
        redis_mock.smembers.return_value = {
            b"work:task_distribution",
            b"notify:log_data",
            b"work:voice_events",
        }
        result = monitor._get_registered_streams()
        assert result == ["notify:log_data", "work:task_distribution", "work:voice_events"]

    def test_handles_string_members(self, monitor, redis_mock):
        redis_mock.smembers.return_value = {"stream_a", "stream_b"}
        result = monitor._get_registered_streams()
        assert result == ["stream_a", "stream_b"]

    def test_none_members_returns_empty(self, monitor, redis_mock):
        redis_mock.smembers.return_value = None
        result = monitor._get_registered_streams()
        assert result == []


# ---------------------------------------------------------------------------
# Tests: check_stuck_consumers
# ---------------------------------------------------------------------------


class TestCheckStuckConsumers:
    def test_no_groups_returns_empty(self, monitor, redis_mock):
        redis_mock.xinfo_groups.return_value = []
        result = monitor.check_stuck_consumers("work:tasks")
        assert result == []

    def test_no_pending_returns_empty(self, monitor, redis_mock):
        redis_mock.xinfo_groups.return_value = [
            {"name": "task_workers", "consumers": 2, "pending": 0,
             "last-delivered-id": "1234-0"}
        ]
        result = monitor.check_stuck_consumers("work:tasks")
        assert result == []

    def test_detects_stale_pending(self, monitor, redis_mock):
        redis_mock.xinfo_groups.return_value = [
            {"name": "task_workers", "consumers": 2, "pending": 5,
             "last-delivered-id": "1234-0"}
        ]
        # XPENDING range returns entries with idle > threshold
        redis_mock.xpending_range.return_value = [
            {"message_id": "1-0", "consumer": "pod-1",
             "time_since_delivered": 400_000, "times_delivered": 3},
            {"message_id": "2-0", "consumer": "pod-1",
             "time_since_delivered": 350_000, "times_delivered": 2},
            {"message_id": "3-0", "consumer": "pod-2",
             "time_since_delivered": 100_000, "times_delivered": 1},
        ]
        result = monitor.check_stuck_consumers("work:tasks")
        assert len(result) == 1
        assert result[0]["type"] == "stuck_consumers"
        assert result[0]["stream"] == "work:tasks"
        assert result[0]["group"] == "task_workers"
        assert result[0]["stale_pending_count"] == 2

    def test_xinfo_groups_exception_returns_empty(self, monitor, redis_mock):
        redis_mock.xinfo_groups.side_effect = Exception("connection lost")
        result = monitor.check_stuck_consumers("work:tasks")
        assert result == []

    def test_xpending_range_exception_returns_zero_stale(self, monitor, redis_mock):
        redis_mock.xinfo_groups.return_value = [
            {"name": "grp", "consumers": 1, "pending": 10,
             "last-delivered-id": "1-0"}
        ]
        redis_mock.xpending_range.side_effect = Exception("timeout")
        result = monitor.check_stuck_consumers("work:tasks")
        assert result == []

    def test_multiple_groups_checked(self, monitor, redis_mock):
        redis_mock.xinfo_groups.return_value = [
            {"name": "grp_a", "consumers": 1, "pending": 2,
             "last-delivered-id": "1-0"},
            {"name": "grp_b", "consumers": 1, "pending": 3,
             "last-delivered-id": "2-0"},
        ]
        redis_mock.xpending_range.side_effect = [
            [{"message_id": "1-0", "consumer": "p1",
              "time_since_delivered": 500_000, "times_delivered": 1}],
            [{"message_id": "2-0", "consumer": "p2",
              "time_since_delivered": 600_000, "times_delivered": 2}],
        ]
        result = monitor.check_stuck_consumers("work:tasks")
        assert len(result) == 2
        assert result[0]["group"] == "grp_a"
        assert result[1]["group"] == "grp_b"

    def test_custom_threshold(self, redis_mock):
        mon = StreamsMonitor(redis_mock, pending_age_threshold_ms=60_000)
        redis_mock.xinfo_groups.return_value = [
            {"name": "grp", "consumers": 1, "pending": 1,
             "last-delivered-id": "1-0"}
        ]
        redis_mock.xpending_range.return_value = [
            {"message_id": "1-0", "consumer": "p1",
             "time_since_delivered": 70_000, "times_delivered": 1},
        ]
        result = mon.check_stuck_consumers("work:tasks")
        assert len(result) == 1
        assert result[0]["threshold_ms"] == 60_000


# ---------------------------------------------------------------------------
# Tests: check_inactive_groups
# ---------------------------------------------------------------------------


class TestCheckInactiveGroups:
    def test_no_groups_returns_empty(self, monitor, redis_mock):
        redis_mock.xinfo_groups.return_value = []
        result = monitor.check_inactive_groups("work:tasks")
        assert result == []

    def test_active_group_returns_empty(self, monitor, redis_mock):
        redis_mock.xinfo_groups.return_value = [
            {"name": "workers", "consumers": 3, "pending": 0,
             "last-delivered-id": "5-0"}
        ]
        result = monitor.check_inactive_groups("work:tasks")
        assert result == []

    def test_detects_zero_consumers(self, monitor, redis_mock):
        redis_mock.xinfo_groups.return_value = [
            {"name": "orphaned_group", "consumers": 0, "pending": 5,
             "last-delivered-id": "10-0"}
        ]
        result = monitor.check_inactive_groups("work:tasks")
        assert len(result) == 1
        assert result[0]["type"] == "no_active_consumers"
        assert result[0]["group"] == "orphaned_group"
        assert result[0]["last_delivered_id"] == "10-0"

    def test_zero_consumers_no_delivered_id_skipped(self, monitor, redis_mock):
        redis_mock.xinfo_groups.return_value = [
            {"name": "new_group", "consumers": 0, "pending": 0,
             "last-delivered-id": ""}
        ]
        result = monitor.check_inactive_groups("work:tasks")
        assert result == []

    def test_xinfo_groups_exception(self, monitor, redis_mock):
        redis_mock.xinfo_groups.side_effect = Exception("err")
        result = monitor.check_inactive_groups("work:tasks")
        assert result == []

    def test_multiple_groups_mixed(self, monitor, redis_mock):
        redis_mock.xinfo_groups.return_value = [
            {"name": "active", "consumers": 2, "pending": 0,
             "last-delivered-id": "1-0"},
            {"name": "dead", "consumers": 0, "pending": 10,
             "last-delivered-id": "5-0"},
        ]
        result = monitor.check_inactive_groups("work:tasks")
        assert len(result) == 1
        assert result[0]["group"] == "dead"


# ---------------------------------------------------------------------------
# Tests: check_dlq_depth
# ---------------------------------------------------------------------------


class TestCheckDlqDepth:
    def test_dlq_below_threshold_returns_empty(self, monitor, redis_mock):
        redis_mock.xlen.return_value = 50
        result = monitor.check_dlq_depth("work:tasks")
        assert result == []

    def test_dlq_at_threshold_returns_empty(self, monitor, redis_mock):
        redis_mock.xlen.return_value = 100
        result = monitor.check_dlq_depth("work:tasks")
        assert result == []

    def test_dlq_above_threshold_detected(self, monitor, redis_mock):
        redis_mock.xlen.return_value = 150
        result = monitor.check_dlq_depth("work:tasks")
        assert len(result) == 1
        assert result[0]["type"] == "dlq_depth_exceeded"
        assert result[0]["dlq_depth"] == 150
        assert result[0]["threshold"] == DEFAULT_DLQ_DEPTH_THRESHOLD
        assert result[0]["dlq_stream"] == "dlq:work:tasks"

    def test_xlen_exception_returns_empty(self, monitor, redis_mock):
        redis_mock.xlen.side_effect = Exception("timeout")
        result = monitor.check_dlq_depth("work:tasks")
        assert result == []

    def test_custom_threshold(self, redis_mock):
        mon = StreamsMonitor(redis_mock, dlq_depth_threshold=50)
        redis_mock.xlen.return_value = 55
        result = mon.check_dlq_depth("work:tasks")
        assert len(result) == 1
        assert result[0]["threshold"] == 50

    def test_dlq_key_construction(self, monitor, redis_mock):
        redis_mock.xlen.return_value = 200
        monitor.check_dlq_depth("work:voice_events")
        redis_mock.xlen.assert_called_with("stream:dlq:work:voice_events")


# ---------------------------------------------------------------------------
# Tests: check_stream (combines all checks)
# ---------------------------------------------------------------------------


class TestCheckStream:
    def test_runs_all_checks(self, monitor, redis_mock):
        redis_mock.xinfo_groups.return_value = [
            {"name": "grp", "consumers": 0, "pending": 5,
             "last-delivered-id": "1-0"}
        ]
        redis_mock.xpending_range.return_value = [
            {"message_id": "1-0", "consumer": "p1",
             "time_since_delivered": 600_000, "times_delivered": 1},
        ]
        redis_mock.xlen.return_value = 200

        result = monitor.check_stream("work:tasks")
        types_found = {a["type"] for a in result}
        assert "stuck_consumers" in types_found
        assert "no_active_consumers" in types_found
        assert "dlq_depth_exceeded" in types_found

    def test_no_anomalies(self, monitor, redis_mock):
        redis_mock.xinfo_groups.return_value = [
            {"name": "grp", "consumers": 2, "pending": 0,
             "last-delivered-id": "1-0"}
        ]
        redis_mock.xlen.return_value = 10
        result = monitor.check_stream("work:tasks")
        assert result == []


# ---------------------------------------------------------------------------
# Tests: check_all
# ---------------------------------------------------------------------------


class TestCheckAll:
    def test_empty_registry(self, monitor, redis_mock):
        redis_mock.smembers.return_value = set()
        result = monitor.check_all()
        assert result == []

    def test_multiple_streams(self, monitor, redis_mock):
        redis_mock.smembers.return_value = {b"work:a", b"work:b"}
        redis_mock.xinfo_groups.return_value = [
            {"name": "grp", "consumers": 0, "pending": 1,
             "last-delivered-id": "1-0"}
        ]
        redis_mock.xpending_range.return_value = []
        redis_mock.xlen.return_value = 0
        result = monitor.check_all()
        # Both streams have inactive group
        assert len(result) == 2
        streams = {a["stream"] for a in result}
        assert "work:a" in streams
        assert "work:b" in streams

    def test_aggregates_anomalies_across_streams(self, monitor, redis_mock):
        redis_mock.smembers.return_value = {b"work:x"}
        redis_mock.xinfo_groups.return_value = [
            {"name": "grp", "consumers": 1, "pending": 3,
             "last-delivered-id": "1-0"}
        ]
        redis_mock.xpending_range.return_value = [
            {"message_id": "1-0", "consumer": "p1",
             "time_since_delivered": 400_000, "times_delivered": 1},
        ]
        redis_mock.xlen.return_value = 200
        result = monitor.check_all()
        types_found = {a["type"] for a in result}
        assert "stuck_consumers" in types_found
        assert "dlq_depth_exceeded" in types_found


# ---------------------------------------------------------------------------
# Tests: get_streams_status
# ---------------------------------------------------------------------------


class TestGetStreamsStatus:
    def test_healthy_when_no_anomalies(self, monitor, redis_mock):
        redis_mock.smembers.return_value = {b"work:tasks"}
        redis_mock.xinfo_groups.return_value = [
            {"name": "grp", "consumers": 2, "pending": 0,
             "last-delivered-id": "1-0"}
        ]
        redis_mock.xlen.return_value = 50
        status = monitor.get_streams_status()
        assert status["status"] == "healthy"
        assert status["total_streams"] == 1
        assert status["anomalies"] == []
        assert len(status["streams"]) == 1

    def test_unhealthy_on_stuck_consumers(self, monitor, redis_mock):
        redis_mock.smembers.return_value = {b"work:tasks"}
        redis_mock.xinfo_groups.return_value = [
            {"name": "grp", "consumers": 1, "pending": 5,
             "last-delivered-id": "1-0"}
        ]
        redis_mock.xpending_range.return_value = [
            {"message_id": "1-0", "consumer": "p1",
             "time_since_delivered": 500_000, "times_delivered": 2},
        ]
        redis_mock.xlen.return_value = 10
        status = monitor.get_streams_status()
        assert status["status"] == "unhealthy"

    def test_unhealthy_on_dlq_depth(self, monitor, redis_mock):
        redis_mock.smembers.return_value = {b"work:tasks"}
        redis_mock.xinfo_groups.return_value = [
            {"name": "grp", "consumers": 2, "pending": 0,
             "last-delivered-id": "1-0"}
        ]
        redis_mock.xlen.return_value = 200
        status = monitor.get_streams_status()
        assert status["status"] == "unhealthy"
        assert any(a["type"] == "dlq_depth_exceeded" for a in status["anomalies"])

    def test_degraded_on_inactive_group(self, monitor, redis_mock):
        redis_mock.smembers.return_value = {b"work:tasks"}
        redis_mock.xinfo_groups.return_value = [
            {"name": "orphan", "consumers": 0, "pending": 0,
             "last-delivered-id": "1-0"}
        ]
        redis_mock.xlen.return_value = 10
        status = monitor.get_streams_status()
        assert status["status"] == "degraded"

    def test_empty_registry_returns_healthy(self, monitor, redis_mock):
        redis_mock.smembers.return_value = set()
        status = monitor.get_streams_status()
        assert status["status"] == "healthy"
        assert status["total_streams"] == 0
        assert status["streams"] == []
        assert status["anomalies"] == []

    def test_checked_at_timestamp(self, monitor, redis_mock):
        redis_mock.smembers.return_value = set()
        before = time.time()
        status = monitor.get_streams_status()
        after = time.time()
        assert before <= status["checked_at"] <= after


# ---------------------------------------------------------------------------
# Tests: _get_stream_detail
# ---------------------------------------------------------------------------


class TestGetStreamDetail:
    def test_returns_length_and_groups(self, monitor, redis_mock):
        redis_mock.xlen.return_value = 42
        redis_mock.xinfo_groups.return_value = [
            {"name": "workers", "consumers": 3, "pending": 2,
             "last-delivered-id": "5-0"},
        ]
        detail = monitor._get_stream_detail("work:tasks")
        assert detail["name"] == "work:tasks"
        assert detail["length"] == 42
        assert len(detail["groups"]) == 1
        assert detail["groups"][0]["name"] == "workers"
        assert detail["groups"][0]["consumers"] == 3
        assert detail["groups"][0]["pending"] == 2

    def test_xlen_exception_returns_zero_length(self, monitor, redis_mock):
        redis_mock.xlen.side_effect = Exception("err")
        redis_mock.xinfo_groups.return_value = []
        detail = monitor._get_stream_detail("work:tasks")
        assert detail["length"] == 0

    def test_xinfo_groups_exception_returns_empty_groups(self, monitor, redis_mock):
        redis_mock.xlen.return_value = 10
        redis_mock.xinfo_groups.side_effect = Exception("err")
        detail = monitor._get_stream_detail("work:tasks")
        assert detail["groups"] == []

    def test_no_groups(self, monitor, redis_mock):
        redis_mock.xlen.return_value = 5
        redis_mock.xinfo_groups.return_value = None
        detail = monitor._get_stream_detail("work:tasks")
        assert detail["groups"] == []


# ---------------------------------------------------------------------------
# Tests: _get_stale_pending
# ---------------------------------------------------------------------------


class TestGetStalePending:
    def test_empty_pending_returns_zero(self, monitor, redis_mock):
        redis_mock.xpending_range.return_value = []
        result = monitor._get_stale_pending("stream:work:t", "grp", 300_000)
        assert result == 0

    def test_counts_stale_entries(self, monitor, redis_mock):
        redis_mock.xpending_range.return_value = [
            {"message_id": "1-0", "consumer": "p1",
             "time_since_delivered": 400_000, "times_delivered": 2},
            {"message_id": "2-0", "consumer": "p1",
             "time_since_delivered": 100_000, "times_delivered": 1},
            {"message_id": "3-0", "consumer": "p2",
             "time_since_delivered": 350_000, "times_delivered": 1},
        ]
        result = monitor._get_stale_pending("stream:work:t", "grp", 300_000)
        assert result == 2

    def test_exception_returns_zero(self, monitor, redis_mock):
        redis_mock.xpending_range.side_effect = Exception("timeout")
        result = monitor._get_stale_pending("stream:work:t", "grp", 300_000)
        assert result == 0

    def test_list_format_pending(self, monitor, redis_mock):
        redis_mock.xpending_range.return_value = [
            ["1-0", "consumer_a", 500_000, 3],
            ["2-0", "consumer_b", 200_000, 1],
        ]
        result = monitor._get_stale_pending("stream:work:t", "grp", 300_000)
        assert result == 1

    def test_none_pending_returns_zero(self, monitor, redis_mock):
        redis_mock.xpending_range.return_value = None
        result = monitor._get_stale_pending("stream:work:t", "grp", 300_000)
        assert result == 0


# ---------------------------------------------------------------------------
# Tests: Helper functions
# ---------------------------------------------------------------------------


class TestDecodeField:
    def test_dict_string_field(self):
        info = {"name": "my_group", "consumers": 3}
        assert _decode_field(info, "name") == "my_group"

    def test_dict_bytes_value(self):
        info = {"name": b"my_group"}
        assert _decode_field(info, "name") == "my_group"

    def test_dict_missing_field(self):
        info = {"other": "value"}
        assert _decode_field(info, "name") == ""

    def test_list_format(self):
        info = [b"name", b"group_a", b"consumers", b"2"]
        assert _decode_field(info, "name") == "group_a"

    def test_list_format_string_keys(self):
        info = ["name", "group_b", "consumers", "5"]
        assert _decode_field(info, "name") == "group_b"

    def test_list_format_missing_field(self):
        info = ["other", "val"]
        assert _decode_field(info, "name") == ""

    def test_non_dict_non_list_returns_empty(self):
        assert _decode_field(42, "name") == ""
        assert _decode_field(None, "name") == ""

    def test_none_value_returns_empty(self):
        info = {"name": None}
        assert _decode_field(info, "name") == ""


class TestIntField:
    def test_numeric_string(self):
        info = {"pending": "5"}
        assert _int_field(info, "pending") == 5

    def test_bytes_value(self):
        info = {"pending": b"10"}
        assert _int_field(info, "pending") == 10

    def test_missing_returns_zero(self):
        assert _int_field({}, "pending") == 0

    def test_non_numeric_returns_zero(self):
        info = {"pending": "abc"}
        assert _int_field(info, "pending") == 0

    def test_integer_value_via_decode(self):
        info = {"consumers": "3"}
        assert _int_field(info, "consumers") == 3


class TestGetIdleTime:
    def test_dict_format(self):
        entry = {"time_since_delivered": 5000, "message_id": "1-0"}
        assert _get_idle_time(entry) == 5000

    def test_dict_idle_key(self):
        entry = {"idle": 3000}
        assert _get_idle_time(entry) == 3000

    def test_list_format(self):
        entry = ["1-0", "consumer_a", 7000, 2]
        assert _get_idle_time(entry) == 7000

    def test_list_bytes(self):
        entry = [b"1-0", b"consumer_a", b"4000", 1]
        assert _get_idle_time(entry) == 4000

    def test_dict_bytes_idle(self):
        entry = {"time_since_delivered": b"9000"}
        assert _get_idle_time(entry) == 9000

    def test_empty_dict(self):
        assert _get_idle_time({}) == 0

    def test_short_list(self):
        assert _get_idle_time(["1-0", "c"]) == 0

    def test_none_returns_zero(self):
        assert _get_idle_time(None) == 0

    def test_non_container_returns_zero(self):
        assert _get_idle_time(42) == 0


# ---------------------------------------------------------------------------
# Tests: stream_key construction
# ---------------------------------------------------------------------------


class TestStreamKey:
    def test_adds_prefix(self, monitor):
        assert monitor._stream_key("work:tasks") == "stream:work:tasks"

    def test_no_double_prefix(self, monitor):
        assert monitor._stream_key("stream:work:tasks") == "stream:work:tasks"


# ---------------------------------------------------------------------------
# Tests: /health/streams endpoint (integration-style)
# ---------------------------------------------------------------------------


class TestHealthStreamsEndpoint:
    def test_healthy_response_structure(self, monitor, redis_mock):
        redis_mock.smembers.return_value = {b"work:tasks"}
        redis_mock.xinfo_groups.return_value = [
            {"name": "workers", "consumers": 2, "pending": 0,
             "last-delivered-id": "1-0"}
        ]
        redis_mock.xlen.return_value = 30
        status = monitor.get_streams_status()
        assert "status" in status
        assert "total_streams" in status
        assert "streams" in status
        assert "anomalies" in status
        assert "checked_at" in status

    def test_stream_detail_in_response(self, monitor, redis_mock):
        redis_mock.smembers.return_value = {b"work:tasks"}
        redis_mock.xinfo_groups.return_value = [
            {"name": "workers", "consumers": 2, "pending": 1,
             "last-delivered-id": "5-0"}
        ]
        redis_mock.xpending_range.return_value = []
        redis_mock.xlen.return_value = 100
        status = monitor.get_streams_status()
        stream = status["streams"][0]
        assert stream["name"] == "work:tasks"
        assert stream["length"] == 100
        assert stream["groups"][0]["name"] == "workers"


# ---------------------------------------------------------------------------
# Tests: Default values
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_pending_threshold(self):
        assert DEFAULT_PENDING_AGE_THRESHOLD_MS == 300_000

    def test_default_dlq_threshold(self):
        assert DEFAULT_DLQ_DEPTH_THRESHOLD == 100

    def test_default_idle_consumer_threshold(self):
        assert DEFAULT_IDLE_CONSUMER_THRESHOLD_MS == 600_000


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_bytes_group_info(self, monitor, redis_mock):
        redis_mock.xinfo_groups.return_value = [
            {b"name": b"grp", b"consumers": b"0", b"pending": b"3",
             b"last-delivered-id": b"1-0"}
        ]
        redis_mock.xpending_range.return_value = [
            [b"1-0", b"p1", 400_000, 2],
        ]
        redis_mock.xlen.return_value = 0
        result = monitor.check_stream("work:tasks")
        types_found = {a["type"] for a in result}
        assert "stuck_consumers" in types_found
        assert "no_active_consumers" in types_found

    def test_detected_at_is_timestamp(self, monitor, redis_mock):
        redis_mock.xinfo_groups.return_value = [
            {"name": "grp", "consumers": 0, "pending": 0,
             "last-delivered-id": "1-0"}
        ]
        redis_mock.xlen.return_value = 0
        before = time.time()
        result = monitor.check_inactive_groups("work:tasks")
        after = time.time()
        assert len(result) == 1
        assert before <= result[0]["detected_at"] <= after

    def test_check_all_with_redis_error_on_one_stream(self, monitor, redis_mock):
        redis_mock.smembers.return_value = {b"work:a", b"work:b"}

        call_count = [0]
        original_xinfo_groups = redis_mock.xinfo_groups

        def xinfo_side_effect(key):
            call_count[0] += 1
            if "work:a" in key:
                raise Exception("connection reset")
            return [{"name": "grp", "consumers": 0, "pending": 0,
                     "last-delivered-id": "1-0"}]

        redis_mock.xinfo_groups.side_effect = xinfo_side_effect
        redis_mock.xlen.return_value = 0
        result = monitor.check_all()
        # work:a fails gracefully, work:b produces inactive group anomaly
        assert any(a["stream"] == "work:b" for a in result)
