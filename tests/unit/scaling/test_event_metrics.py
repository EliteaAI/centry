"""Unit tests for event metrics tracking.

Validates that:
1. EventMetrics.record_published() increments counters and sets timestamps
2. EventMetrics.record_consumed() increments consumed counter
3. EventMetrics.record_failed() increments failed counter
4. EventMetrics.update_pending() sets the pending gauge
5. EventMetrics.get_stream_health() computes lag, error_rate, status
6. EventMetrics.get_all_streams_health() iterates registered streams
7. EventMetrics.get_summary() aggregates across all streams
8. EventMetrics.reset_stream() removes metrics and deregisters
9. Status computation: healthy/degraded/unhealthy based on thresholds
10. Edge cases: empty data, zero division, bytes decoding
11. /health/events endpoint returns correct JSON structure

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_event_metrics.py -v
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

_module_path = _PLUGIN_ROOT / "utils" / "event_metrics.py"
_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.utils.event_metrics",
    _module_path,
    submodule_search_locations=[],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

EventMetrics = _mod.EventMetrics
METRICS_KEY_PREFIX = _mod.METRICS_KEY_PREFIX
STREAMS_REGISTRY_KEY = _mod.STREAMS_REGISTRY_KEY


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def redis_mock():
    """Create a mock Redis client with pipeline support."""
    mock = MagicMock()
    pipe_mock = MagicMock()
    mock.pipeline.return_value = pipe_mock
    pipe_mock.execute.return_value = [1, True]
    mock.hgetall.return_value = {}
    mock.smembers.return_value = set()
    mock.sadd.return_value = 1
    mock.srem.return_value = 1
    mock.delete.return_value = 1
    mock.hset.return_value = 1
    return mock


@pytest.fixture
def metrics(redis_mock):
    """Create an EventMetrics instance."""
    return EventMetrics(redis_mock)


# ---------------------------------------------------------------------------
# record_published Tests
# ---------------------------------------------------------------------------


class TestRecordPublished:
    """Tests for EventMetrics.record_published()."""

    def test_record_published_uses_pipeline(self, metrics, redis_mock):
        metrics.record_published("work:tasks")
        redis_mock.pipeline.assert_called_once_with(transaction=False)

    def test_record_published_increments_counter(self, metrics, redis_mock):
        pipe = redis_mock.pipeline.return_value
        metrics.record_published("work:tasks")
        pipe.hincrby.assert_called_once_with(
            f"{METRICS_KEY_PREFIX}:work:tasks", "messages_published", 1
        )

    def test_record_published_sets_timestamp(self, metrics, redis_mock):
        pipe = redis_mock.pipeline.return_value
        metrics.record_published("work:tasks")
        call_args = pipe.hset.call_args
        assert call_args[0][0] == f"{METRICS_KEY_PREFIX}:work:tasks"
        assert call_args[0][1] == "last_published_at"
        ts = float(call_args[0][2])
        assert ts > 0

    def test_record_published_executes_pipeline(self, metrics, redis_mock):
        pipe = redis_mock.pipeline.return_value
        metrics.record_published("work:tasks")
        pipe.execute.assert_called_once()

    def test_record_published_registers_stream(self, metrics, redis_mock):
        metrics.record_published("work:tasks")
        redis_mock.sadd.assert_called_once_with(STREAMS_REGISTRY_KEY, "work:tasks")

    def test_record_published_with_count(self, metrics, redis_mock):
        pipe = redis_mock.pipeline.return_value
        metrics.record_published("work:tasks", count=5)
        pipe.hincrby.assert_called_once_with(
            f"{METRICS_KEY_PREFIX}:work:tasks", "messages_published", 5
        )


# ---------------------------------------------------------------------------
# record_consumed Tests
# ---------------------------------------------------------------------------


class TestRecordConsumed:
    """Tests for EventMetrics.record_consumed()."""

    def test_record_consumed_uses_pipeline(self, metrics, redis_mock):
        metrics.record_consumed("work:tasks")
        redis_mock.pipeline.assert_called_once_with(transaction=False)

    def test_record_consumed_increments_counter(self, metrics, redis_mock):
        pipe = redis_mock.pipeline.return_value
        metrics.record_consumed("work:tasks")
        pipe.hincrby.assert_called_once_with(
            f"{METRICS_KEY_PREFIX}:work:tasks", "messages_consumed", 1
        )

    def test_record_consumed_sets_timestamp(self, metrics, redis_mock):
        pipe = redis_mock.pipeline.return_value
        metrics.record_consumed("work:tasks")
        call_args = pipe.hset.call_args
        assert call_args[0][1] == "last_consumed_at"

    def test_record_consumed_with_count(self, metrics, redis_mock):
        pipe = redis_mock.pipeline.return_value
        metrics.record_consumed("work:tasks", count=10)
        pipe.hincrby.assert_called_once_with(
            f"{METRICS_KEY_PREFIX}:work:tasks", "messages_consumed", 10
        )

    def test_record_consumed_executes_pipeline(self, metrics, redis_mock):
        pipe = redis_mock.pipeline.return_value
        metrics.record_consumed("work:tasks")
        pipe.execute.assert_called_once()


# ---------------------------------------------------------------------------
# record_failed Tests
# ---------------------------------------------------------------------------


class TestRecordFailed:
    """Tests for EventMetrics.record_failed()."""

    def test_record_failed_uses_pipeline(self, metrics, redis_mock):
        metrics.record_failed("work:tasks")
        redis_mock.pipeline.assert_called_once_with(transaction=False)

    def test_record_failed_increments_counter(self, metrics, redis_mock):
        pipe = redis_mock.pipeline.return_value
        metrics.record_failed("work:tasks")
        pipe.hincrby.assert_called_once_with(
            f"{METRICS_KEY_PREFIX}:work:tasks", "messages_failed", 1
        )

    def test_record_failed_sets_timestamp(self, metrics, redis_mock):
        pipe = redis_mock.pipeline.return_value
        metrics.record_failed("work:tasks")
        call_args = pipe.hset.call_args
        assert call_args[0][1] == "last_failed_at"

    def test_record_failed_with_count(self, metrics, redis_mock):
        pipe = redis_mock.pipeline.return_value
        metrics.record_failed("work:tasks", count=3)
        pipe.hincrby.assert_called_once_with(
            f"{METRICS_KEY_PREFIX}:work:tasks", "messages_failed", 3
        )

    def test_record_failed_executes_pipeline(self, metrics, redis_mock):
        pipe = redis_mock.pipeline.return_value
        metrics.record_failed("work:tasks")
        pipe.execute.assert_called_once()


# ---------------------------------------------------------------------------
# update_pending Tests
# ---------------------------------------------------------------------------


class TestUpdatePending:
    """Tests for EventMetrics.update_pending()."""

    def test_update_pending_sets_value(self, metrics, redis_mock):
        metrics.update_pending("work:tasks", 42)
        redis_mock.hset.assert_called_once_with(
            f"{METRICS_KEY_PREFIX}:work:tasks", "pending_count", "42"
        )

    def test_update_pending_zero(self, metrics, redis_mock):
        metrics.update_pending("work:tasks", 0)
        redis_mock.hset.assert_called_once_with(
            f"{METRICS_KEY_PREFIX}:work:tasks", "pending_count", "0"
        )

    def test_update_pending_large_value(self, metrics, redis_mock):
        metrics.update_pending("work:tasks", 99999)
        redis_mock.hset.assert_called_once_with(
            f"{METRICS_KEY_PREFIX}:work:tasks", "pending_count", "99999"
        )


# ---------------------------------------------------------------------------
# get_stream_health Tests
# ---------------------------------------------------------------------------


class TestGetStreamHealth:
    """Tests for EventMetrics.get_stream_health()."""

    def test_returns_empty_dict_for_unknown_stream(self, metrics, redis_mock):
        redis_mock.hgetall.return_value = {}
        result = metrics.get_stream_health("nonexistent")
        assert result == {}

    def test_returns_correct_counters(self, metrics, redis_mock):
        redis_mock.hgetall.return_value = {
            b"messages_published": b"100",
            b"messages_consumed": b"90",
            b"messages_failed": b"5",
            b"pending_count": b"5",
            b"last_published_at": b"1700000000.0",
            b"last_consumed_at": b"1700000000.0",
        }
        result = metrics.get_stream_health("work:tasks")
        assert result["messages_published"] == 100
        assert result["messages_consumed"] == 90
        assert result["messages_failed"] == 5
        assert result["pending_count"] == 5

    def test_computes_error_rate(self, metrics, redis_mock):
        redis_mock.hgetall.return_value = {
            b"messages_published": b"100",
            b"messages_consumed": b"80",
            b"messages_failed": b"20",
            b"pending_count": b"0",
            b"last_published_at": b"1700000000.0",
            b"last_consumed_at": b"1700000000.0",
        }
        result = metrics.get_stream_health("work:tasks")
        assert result["error_rate"] == 0.2

    def test_error_rate_zero_when_no_processed(self, metrics, redis_mock):
        redis_mock.hgetall.return_value = {
            b"messages_published": b"10",
            b"messages_consumed": b"0",
            b"messages_failed": b"0",
            b"pending_count": b"10",
        }
        result = metrics.get_stream_health("work:tasks")
        assert result["error_rate"] == 0.0

    def test_includes_stream_name(self, metrics, redis_mock):
        redis_mock.hgetall.return_value = {
            b"messages_published": b"1",
            b"messages_consumed": b"1",
            b"messages_failed": b"0",
            b"pending_count": b"0",
        }
        result = metrics.get_stream_health("work:my_stream")
        assert result["stream_name"] == "work:my_stream"

    def test_timestamp_fields(self, metrics, redis_mock):
        ts = str(time.time() - 10)
        redis_mock.hgetall.return_value = {
            b"messages_published": b"1",
            b"messages_consumed": b"1",
            b"messages_failed": b"0",
            b"pending_count": b"0",
            b"last_published_at": ts.encode(),
            b"last_consumed_at": ts.encode(),
        }
        result = metrics.get_stream_health("work:tasks")
        assert result["last_published_at"] is not None
        assert result["last_consumed_at"] is not None
        assert result["publish_age_seconds"] is not None
        assert result["publish_age_seconds"] >= 9.0

    def test_null_timestamps_when_never_published(self, metrics, redis_mock):
        redis_mock.hgetall.return_value = {
            b"messages_published": b"0",
            b"messages_consumed": b"0",
            b"messages_failed": b"0",
            b"pending_count": b"0",
        }
        result = metrics.get_stream_health("work:tasks")
        assert result["last_published_at"] is None
        assert result["last_consumed_at"] is None
        assert result["publish_age_seconds"] is None
        assert result["consume_age_seconds"] is None

    def test_handles_string_keys(self, metrics, redis_mock):
        redis_mock.hgetall.return_value = {
            "messages_published": "50",
            "messages_consumed": "45",
            "messages_failed": "2",
            "pending_count": "3",
        }
        result = metrics.get_stream_health("work:tasks")
        assert result["messages_published"] == 50
        assert result["messages_consumed"] == 45

    def test_status_healthy(self, metrics, redis_mock):
        redis_mock.hgetall.return_value = {
            b"messages_published": b"1000",
            b"messages_consumed": b"995",
            b"messages_failed": b"5",
            b"pending_count": b"0",
        }
        result = metrics.get_stream_health("work:tasks")
        assert result["status"] == "healthy"

    def test_status_degraded_high_pending(self, metrics, redis_mock):
        redis_mock.hgetall.return_value = {
            b"messages_published": b"1000",
            b"messages_consumed": b"900",
            b"messages_failed": b"0",
            b"pending_count": b"100",
        }
        result = metrics.get_stream_health("work:tasks")
        assert result["status"] == "degraded"

    def test_status_degraded_elevated_error_rate(self, metrics, redis_mock):
        redis_mock.hgetall.return_value = {
            b"messages_published": b"100",
            b"messages_consumed": b"80",
            b"messages_failed": b"12",
            b"pending_count": b"0",
        }
        result = metrics.get_stream_health("work:tasks")
        assert result["status"] == "degraded"

    def test_status_unhealthy_critical_pending(self, metrics, redis_mock):
        redis_mock.hgetall.return_value = {
            b"messages_published": b"5000",
            b"messages_consumed": b"3000",
            b"messages_failed": b"0",
            b"pending_count": b"2000",
        }
        result = metrics.get_stream_health("work:tasks")
        assert result["status"] == "unhealthy"

    def test_status_unhealthy_high_error_rate(self, metrics, redis_mock):
        redis_mock.hgetall.return_value = {
            b"messages_published": b"100",
            b"messages_consumed": b"30",
            b"messages_failed": b"60",
            b"pending_count": b"0",
        }
        result = metrics.get_stream_health("work:tasks")
        assert result["status"] == "unhealthy"

    def test_uses_correct_redis_key(self, metrics, redis_mock):
        redis_mock.hgetall.return_value = {}
        metrics.get_stream_health("work:voice_events")
        redis_mock.hgetall.assert_called_once_with(
            f"{METRICS_KEY_PREFIX}:work:voice_events"
        )


# ---------------------------------------------------------------------------
# get_all_streams_health Tests
# ---------------------------------------------------------------------------


class TestGetAllStreamsHealth:
    """Tests for EventMetrics.get_all_streams_health()."""

    def test_returns_empty_when_no_streams(self, metrics, redis_mock):
        redis_mock.smembers.return_value = set()
        result = metrics.get_all_streams_health()
        assert result == {}

    def test_returns_health_for_each_registered_stream(self, metrics, redis_mock):
        redis_mock.smembers.return_value = {b"work:tasks", b"work:voice"}
        redis_mock.hgetall.return_value = {
            b"messages_published": b"10",
            b"messages_consumed": b"8",
            b"messages_failed": b"1",
            b"pending_count": b"1",
        }
        result = metrics.get_all_streams_health()
        assert "work:tasks" in result
        assert "work:voice" in result

    def test_skips_streams_with_no_metrics(self, metrics, redis_mock):
        redis_mock.smembers.return_value = {b"work:tasks", b"work:empty"}

        def hgetall_side_effect(key):
            if "work:empty" in key:
                return {}
            return {
                b"messages_published": b"10",
                b"messages_consumed": b"10",
                b"messages_failed": b"0",
                b"pending_count": b"0",
            }

        redis_mock.hgetall.side_effect = hgetall_side_effect
        result = metrics.get_all_streams_health()
        assert "work:tasks" in result
        assert "work:empty" not in result

    def test_handles_string_set_members(self, metrics, redis_mock):
        redis_mock.smembers.return_value = {"work:tasks"}
        redis_mock.hgetall.return_value = {
            b"messages_published": b"5",
            b"messages_consumed": b"5",
            b"messages_failed": b"0",
            b"pending_count": b"0",
        }
        result = metrics.get_all_streams_health()
        assert "work:tasks" in result

    def test_queries_registry_key(self, metrics, redis_mock):
        redis_mock.smembers.return_value = set()
        metrics.get_all_streams_health()
        redis_mock.smembers.assert_called_once_with(STREAMS_REGISTRY_KEY)


# ---------------------------------------------------------------------------
# get_summary Tests
# ---------------------------------------------------------------------------


class TestGetSummary:
    """Tests for EventMetrics.get_summary()."""

    def test_returns_zeros_when_no_streams(self, metrics, redis_mock):
        redis_mock.smembers.return_value = set()
        result = metrics.get_summary()
        assert result["total_streams"] == 0
        assert result["total_published"] == 0
        assert result["total_consumed"] == 0
        assert result["total_failed"] == 0
        assert result["total_pending"] == 0
        assert result["overall_error_rate"] == 0.0
        assert result["streams_unhealthy"] == 0

    def test_aggregates_across_streams(self, metrics, redis_mock):
        redis_mock.smembers.return_value = {b"stream_a", b"stream_b"}

        call_count = [0]

        def hgetall_side_effect(key):
            call_count[0] += 1
            if call_count[0] <= 1:
                return {
                    b"messages_published": b"100",
                    b"messages_consumed": b"90",
                    b"messages_failed": b"5",
                    b"pending_count": b"5",
                }
            return {
                b"messages_published": b"200",
                b"messages_consumed": b"180",
                b"messages_failed": b"10",
                b"pending_count": b"10",
            }

        redis_mock.hgetall.side_effect = hgetall_side_effect
        result = metrics.get_summary()
        assert result["total_streams"] == 2
        assert result["total_published"] == 300
        assert result["total_consumed"] == 270
        assert result["total_failed"] == 15
        assert result["total_pending"] == 15

    def test_computes_overall_error_rate(self, metrics, redis_mock):
        redis_mock.smembers.return_value = {b"s1"}
        redis_mock.hgetall.return_value = {
            b"messages_published": b"100",
            b"messages_consumed": b"80",
            b"messages_failed": b"20",
            b"pending_count": b"0",
        }
        result = metrics.get_summary()
        assert result["overall_error_rate"] == 0.2

    def test_counts_unhealthy_streams(self, metrics, redis_mock):
        redis_mock.smembers.return_value = {b"healthy_stream", b"unhealthy_stream"}

        call_count = [0]

        def hgetall_side_effect(key):
            call_count[0] += 1
            if call_count[0] <= 1:
                return {
                    b"messages_published": b"100",
                    b"messages_consumed": b"100",
                    b"messages_failed": b"0",
                    b"pending_count": b"0",
                }
            return {
                b"messages_published": b"100",
                b"messages_consumed": b"10",
                b"messages_failed": b"80",
                b"pending_count": b"2000",
            }

        redis_mock.hgetall.side_effect = hgetall_side_effect
        result = metrics.get_summary()
        assert result["streams_unhealthy"] == 1

    def test_zero_error_rate_when_nothing_processed(self, metrics, redis_mock):
        redis_mock.smembers.return_value = {b"s1"}
        redis_mock.hgetall.return_value = {
            b"messages_published": b"10",
            b"messages_consumed": b"0",
            b"messages_failed": b"0",
            b"pending_count": b"10",
        }
        result = metrics.get_summary()
        assert result["overall_error_rate"] == 0.0


# ---------------------------------------------------------------------------
# reset_stream Tests
# ---------------------------------------------------------------------------


class TestResetStream:
    """Tests for EventMetrics.reset_stream()."""

    def test_deletes_metrics_key(self, metrics, redis_mock):
        metrics.reset_stream("work:tasks")
        redis_mock.delete.assert_called_once_with(
            f"{METRICS_KEY_PREFIX}:work:tasks"
        )

    def test_removes_from_registry(self, metrics, redis_mock):
        metrics.reset_stream("work:tasks")
        redis_mock.srem.assert_called_once_with(
            STREAMS_REGISTRY_KEY, "work:tasks"
        )


# ---------------------------------------------------------------------------
# _compute_status Tests
# ---------------------------------------------------------------------------


class TestComputeStatus:
    """Tests for EventMetrics._compute_status() thresholds."""

    def test_healthy_no_issues(self, metrics):
        assert metrics._compute_status(0, 0.0) == "healthy"

    def test_healthy_low_pending(self, metrics):
        assert metrics._compute_status(50, 0.01) == "healthy"

    def test_degraded_pending_100(self, metrics):
        assert metrics._compute_status(100, 0.0) == "degraded"

    def test_degraded_pending_500(self, metrics):
        assert metrics._compute_status(500, 0.0) == "degraded"

    def test_degraded_error_rate_10pct(self, metrics):
        assert metrics._compute_status(0, 0.1) == "degraded"

    def test_degraded_error_rate_30pct(self, metrics):
        assert metrics._compute_status(0, 0.3) == "degraded"

    def test_unhealthy_pending_1000(self, metrics):
        assert metrics._compute_status(1000, 0.0) == "unhealthy"

    def test_unhealthy_pending_5000(self, metrics):
        assert metrics._compute_status(5000, 0.0) == "unhealthy"

    def test_unhealthy_error_rate_50pct(self, metrics):
        assert metrics._compute_status(0, 0.5) == "unhealthy"

    def test_unhealthy_error_rate_90pct(self, metrics):
        assert metrics._compute_status(0, 0.9) == "unhealthy"

    def test_unhealthy_takes_precedence(self, metrics):
        assert metrics._compute_status(2000, 0.8) == "unhealthy"

    def test_boundary_99_pending_is_healthy(self, metrics):
        assert metrics._compute_status(99, 0.0) == "healthy"

    def test_boundary_999_pending_is_degraded(self, metrics):
        assert metrics._compute_status(999, 0.0) == "degraded"

    def test_boundary_error_rate_just_below_10pct(self, metrics):
        assert metrics._compute_status(0, 0.099) == "healthy"

    def test_boundary_error_rate_just_below_50pct(self, metrics):
        assert metrics._compute_status(0, 0.499) == "degraded"

    def test_publish_age_not_used_in_status(self, metrics):
        assert metrics._compute_status(0, 0.0, publish_age_s=9999.0) == "healthy"


# ---------------------------------------------------------------------------
# _metrics_key Tests
# ---------------------------------------------------------------------------


class TestMetricsKey:
    """Tests for key construction."""

    def test_metrics_key_format(self, metrics):
        assert metrics._metrics_key("work:tasks") == "metrics:streams:work:tasks"

    def test_metrics_key_with_special_chars(self, metrics):
        assert metrics._metrics_key("dlq:work:tasks") == "metrics:streams:dlq:work:tasks"


# ---------------------------------------------------------------------------
# Integration-style Tests
# ---------------------------------------------------------------------------


class TestIntegration:
    """Tests that simulate realistic usage patterns."""

    def test_full_lifecycle_publish_consume_health(self, metrics, redis_mock):
        metrics.record_published("work:tasks", count=100)
        metrics.record_consumed("work:tasks", count=95)
        metrics.record_failed("work:tasks", count=3)
        metrics.update_pending("work:tasks", 2)

        redis_mock.hgetall.return_value = {
            b"messages_published": b"100",
            b"messages_consumed": b"95",
            b"messages_failed": b"3",
            b"pending_count": b"2",
            b"last_published_at": str(time.time()).encode(),
            b"last_consumed_at": str(time.time()).encode(),
        }
        health = metrics.get_stream_health("work:tasks")
        assert health["status"] == "healthy"
        assert health["messages_published"] == 100

    def test_multiple_streams_summary(self, metrics, redis_mock):
        redis_mock.smembers.return_value = {b"work:a", b"work:b", b"work:c"}
        redis_mock.hgetall.return_value = {
            b"messages_published": b"50",
            b"messages_consumed": b"48",
            b"messages_failed": b"2",
            b"pending_count": b"0",
        }
        summary = metrics.get_summary()
        assert summary["total_streams"] == 3
        assert summary["total_published"] == 150

    def test_register_stream_called_on_publish(self, metrics, redis_mock):
        metrics.record_published("new:stream")
        redis_mock.sadd.assert_called_with(STREAMS_REGISTRY_KEY, "new:stream")

    def test_reset_then_health_returns_empty(self, metrics, redis_mock):
        metrics.reset_stream("work:tasks")
        redis_mock.hgetall.return_value = {}
        result = metrics.get_stream_health("work:tasks")
        assert result == {}
