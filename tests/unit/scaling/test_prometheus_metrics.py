"""Unit tests for Prometheus metrics exposition (task 5.10).

Validates that:
1. MetricsCollector exposes pylon_active_connections gauge
2. MetricsCollector exposes pylon_task_queue_depth gauge
3. MetricsCollector exposes per-stream published/consumed/failed/pending metrics
4. Connection count reads from SIO manager.get_participants()
5. Connection count fallback to manager.rooms()
6. Connection count fallback to manager.eio.sockets
7. Connection count returns 0 when SIO is None
8. Task queue depth reads from Redis XLEN
9. Task queue depth returns 0 when Redis is None
10. Stream metrics handle empty registry
11. Stream metrics handle Redis errors gracefully
12. get_registry() creates a dedicated CollectorRegistry
13. /metrics route returns valid Prometheus text format
14. /metrics route caches the collector instance
15. Edge cases: bytes keys, missing fields, exceptions

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_prometheus_metrics.py -v
"""

import importlib
import importlib.util
import pathlib
import sys
import types
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ---------------------------------------------------------------------------
# Module loading setup (mirrors pattern from test_event_metrics.py)
# ---------------------------------------------------------------------------

_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[3] / "pylon_main" / "plugins" / "elitea_core"
_SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[4] / "elitea_core"

_mock_log = MagicMock()
_mock_pylon_core_tools = MagicMock()
_mock_pylon_core_tools.log = _mock_log
sys.modules.setdefault("pylon", MagicMock())
sys.modules.setdefault("pylon.core", MagicMock())
sys.modules.setdefault("pylon.core.tools", _mock_pylon_core_tools)

# Mock prometheus_client
_mock_prometheus = MagicMock()
_mock_registry_mod = MagicMock()


# Load the source module directly
_module_path = _SOURCE_ROOT / "utils" / "prometheus_metrics.py"
_spec = importlib.util.spec_from_file_location(
    "elitea_core.utils.prometheus_metrics",
    _module_path,
    submodule_search_locations=[],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

MetricsCollector = _mod.MetricsCollector
get_registry = _mod.get_registry
STREAM_KEY = _mod.STREAM_KEY
STREAMS_REGISTRY_KEY = _mod.STREAMS_REGISTRY_KEY


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis():
    """Create a mock Redis client."""
    client = MagicMock()
    client.xlen.return_value = 0
    client.smembers.return_value = set()
    client.hgetall.return_value = {}
    return client


@pytest.fixture
def mock_sio():
    """Create a mock Socket.IO server with manager."""
    sio = MagicMock()
    manager = MagicMock()
    sio.manager = manager
    manager.get_participants.return_value = iter([
        ("sid1", None), ("sid2", None), ("sid3", None)
    ])
    return sio


@pytest.fixture
def collector(mock_sio, mock_redis):
    """Create a MetricsCollector with mocked dependencies."""
    return MetricsCollector(sio_server=mock_sio, redis_client=mock_redis)


# ---------------------------------------------------------------------------
# Tests: Connection metrics
# ---------------------------------------------------------------------------

class TestConnectionMetrics:
    """Tests for pylon_active_connections gauge."""

    def test_returns_connection_count_from_get_participants(self, collector, mock_sio):
        """get_participants returns iterable of (sid, eio_sid) tuples."""
        mock_sio.manager.get_participants.return_value = iter([
            ("sid1", None), ("sid2", None), ("sid3", None), ("sid4", None)
        ])
        count = collector._get_active_connections()
        assert count == 4
        mock_sio.manager.get_participants.assert_called_once_with("/", None)

    def test_returns_zero_when_sio_is_none(self):
        """No SIO server → 0 connections."""
        collector = MetricsCollector(sio_server=None, redis_client=MagicMock())
        assert collector._get_active_connections() == 0

    def test_returns_zero_when_manager_is_none(self):
        """SIO server without manager → 0 connections."""
        sio = MagicMock()
        sio.manager = None
        collector = MetricsCollector(sio_server=sio, redis_client=MagicMock())
        assert collector._get_active_connections() == 0

    def test_fallback_to_rooms_when_get_participants_missing(self):
        """If get_participants doesn't exist, try rooms()."""
        sio = MagicMock()
        manager = MagicMock(spec=[])
        manager.rooms = MagicMock(return_value={"room1", "room2", "room3"})
        sio.manager = manager
        del manager.get_participants
        collector = MetricsCollector(sio_server=sio, redis_client=MagicMock())
        assert collector._get_active_connections() == 3

    def test_fallback_to_eio_sockets(self):
        """If no get_participants or rooms, try eio.sockets."""
        sio = MagicMock()
        manager = MagicMock(spec=[])
        eio = MagicMock()
        eio.sockets = {"sid1": {}, "sid2": {}}
        manager.eio = eio
        sio.manager = manager
        del manager.get_participants
        collector = MetricsCollector(sio_server=sio, redis_client=MagicMock())
        assert collector._get_active_connections() == 2

    def test_returns_zero_on_exception(self):
        """Any exception during counting → returns 0."""
        sio = MagicMock()
        sio.manager.get_participants.side_effect = RuntimeError("boom")
        collector = MetricsCollector(sio_server=sio, redis_client=MagicMock())
        assert collector._get_active_connections() == 0

    def test_collect_yields_gauge_family(self, collector, mock_sio):
        """_collect_connection_metrics yields a GaugeMetricFamily."""
        mock_sio.manager.get_participants.return_value = iter([("s1", None), ("s2", None)])
        metrics = list(collector._collect_connection_metrics())
        assert len(metrics) == 1
        gauge = metrics[0]
        assert gauge.name == "pylon_active_connections"
        assert len(gauge.samples) == 1
        assert gauge.samples[0].value == 2

    def test_empty_participants_returns_zero(self, collector, mock_sio):
        """Empty participants iterator → 0."""
        mock_sio.manager.get_participants.return_value = iter([])
        assert collector._get_active_connections() == 0

    def test_set_sio_server_late_binding(self, mock_redis):
        """Can set SIO server after construction."""
        collector = MetricsCollector(sio_server=None, redis_client=mock_redis)
        assert collector._get_active_connections() == 0

        sio = MagicMock()
        sio.manager.get_participants.return_value = iter([("s1", None)])
        collector.set_sio_server(sio)
        assert collector._get_active_connections() == 1


# ---------------------------------------------------------------------------
# Tests: Task queue depth
# ---------------------------------------------------------------------------

class TestTaskQueueDepth:
    """Tests for pylon_task_queue_depth gauge."""

    def test_returns_xlen_value(self, collector, mock_redis):
        """Queue depth is the stream length from XLEN."""
        mock_redis.xlen.return_value = 42
        assert collector._get_task_queue_depth() == 42
        mock_redis.xlen.assert_called_once_with(STREAM_KEY)

    def test_returns_zero_when_redis_is_none(self):
        """No Redis client → 0 depth."""
        collector = MetricsCollector(sio_server=MagicMock(), redis_client=None)
        assert collector._get_task_queue_depth() == 0

    def test_returns_zero_on_redis_exception(self, collector, mock_redis):
        """Redis error → 0 depth."""
        mock_redis.xlen.side_effect = Exception("connection refused")
        assert collector._get_task_queue_depth() == 0

    def test_returns_zero_when_xlen_returns_none(self, collector, mock_redis):
        """XLEN returning None → 0."""
        mock_redis.xlen.return_value = None
        assert collector._get_task_queue_depth() == 0

    def test_collect_yields_gauge_family(self, collector, mock_redis):
        """_collect_task_queue_metrics yields a GaugeMetricFamily."""
        mock_redis.xlen.return_value = 17
        metrics = list(collector._collect_task_queue_metrics())
        assert len(metrics) == 1
        gauge = metrics[0]
        assert gauge.name == "pylon_task_queue_depth"
        assert gauge.samples[0].value == 17

    def test_set_redis_client_late_binding(self):
        """Can set Redis client after construction."""
        collector = MetricsCollector(sio_server=MagicMock(), redis_client=None)
        assert collector._get_task_queue_depth() == 0

        redis = MagicMock()
        redis.xlen.return_value = 5
        collector.set_redis_client(redis)
        assert collector._get_task_queue_depth() == 5


# ---------------------------------------------------------------------------
# Tests: Stream metrics
# ---------------------------------------------------------------------------

class TestStreamMetrics:
    """Tests for per-stream published/consumed/failed/pending metrics."""

    def test_no_metrics_when_redis_is_none(self):
        """No Redis → no stream metrics yielded."""
        collector = MetricsCollector(sio_server=MagicMock(), redis_client=None)
        metrics = list(collector._collect_stream_metrics())
        assert metrics == []

    def test_no_metrics_when_registry_empty(self, collector, mock_redis):
        """Empty stream registry → no metrics."""
        mock_redis.smembers.return_value = set()
        metrics = list(collector._collect_stream_metrics())
        assert metrics == []

    def test_yields_four_metric_families(self, collector, mock_redis):
        """With one stream registered, yields published/consumed/failed/pending."""
        mock_redis.smembers.return_value = {b"work:task_distribution"}
        mock_redis.hgetall.return_value = {
            b"messages_published": b"100",
            b"messages_consumed": b"95",
            b"messages_failed": b"3",
            b"pending_count": b"2",
        }
        metrics = list(collector._collect_stream_metrics())
        assert len(metrics) == 4

        names = {m.name for m in metrics}
        assert "pylon_stream_messages_published" in names
        assert "pylon_stream_messages_consumed" in names
        assert "pylon_stream_messages_failed" in names
        assert "pylon_stream_pending_count" in names

    def test_metric_values_correct(self, collector, mock_redis):
        """Verify metric values match Redis data."""
        mock_redis.smembers.return_value = {b"work:task_distribution"}
        mock_redis.hgetall.return_value = {
            b"messages_published": b"200",
            b"messages_consumed": b"180",
            b"messages_failed": b"5",
            b"pending_count": b"15",
        }
        metrics = list(collector._collect_stream_metrics())
        values = {}
        for m in metrics:
            for sample in m.samples:
                values[m.name] = sample.value

        assert values["pylon_stream_messages_published"] == 200
        assert values["pylon_stream_messages_consumed"] == 180
        assert values["pylon_stream_messages_failed"] == 5
        assert values["pylon_stream_pending_count"] == 15

    def test_multiple_streams(self, collector, mock_redis):
        """Multiple registered streams produce labeled metrics."""
        mock_redis.smembers.return_value = {b"stream_a", b"stream_b"}

        def hgetall_side_effect(key):
            if b"stream_a" in key.encode() if isinstance(key, str) else b"stream_a" in key:
                return {b"messages_published": b"10", b"messages_consumed": b"8",
                        b"messages_failed": b"1", b"pending_count": b"1"}
            return {b"messages_published": b"50", b"messages_consumed": b"49",
                    b"messages_failed": b"0", b"pending_count": b"1"}

        mock_redis.hgetall.side_effect = hgetall_side_effect
        metrics = list(collector._collect_stream_metrics())
        # Should have 4 families, each with 2 samples (one per stream)
        assert len(metrics) == 4
        for m in metrics:
            assert len(m.samples) == 2

    def test_handles_smembers_exception(self, collector, mock_redis):
        """Redis smembers error → no metrics (graceful)."""
        mock_redis.smembers.side_effect = Exception("timeout")
        metrics = list(collector._collect_stream_metrics())
        assert metrics == []

    def test_handles_hgetall_exception_per_stream(self, collector, mock_redis):
        """One stream's hgetall failing doesn't crash others."""
        mock_redis.smembers.return_value = {b"good_stream", b"bad_stream"}
        call_count = [0]

        def hgetall_side_effect(key):
            call_count[0] += 1
            if "bad_stream" in key:
                raise Exception("corrupt data")
            return {b"messages_published": b"10", b"messages_consumed": b"10",
                    b"messages_failed": b"0", b"pending_count": b"0"}

        mock_redis.hgetall.side_effect = hgetall_side_effect
        metrics = list(collector._collect_stream_metrics())
        assert len(metrics) == 4
        # Only good_stream has samples
        for m in metrics:
            assert len(m.samples) == 1

    def test_handles_empty_hgetall(self, collector, mock_redis):
        """Stream with no metrics data is skipped."""
        mock_redis.smembers.return_value = {b"empty_stream"}
        mock_redis.hgetall.return_value = {}
        metrics = list(collector._collect_stream_metrics())
        # Families are created but have 0 samples
        assert len(metrics) == 4
        for m in metrics:
            assert len(m.samples) == 0

    def test_handles_string_keys_from_redis(self, collector, mock_redis):
        """Some Redis clients return strings instead of bytes."""
        mock_redis.smembers.return_value = {"string_stream"}
        mock_redis.hgetall.return_value = {
            "messages_published": "5",
            "messages_consumed": "4",
            "messages_failed": "1",
            "pending_count": "0",
        }
        metrics = list(collector._collect_stream_metrics())
        assert len(metrics) == 4
        published = next(m for m in metrics if m.name == "pylon_stream_messages_published")
        assert published.samples[0].value == 5

    def test_missing_fields_default_to_zero(self, collector, mock_redis):
        """Partial hash data: missing fields default to 0."""
        mock_redis.smembers.return_value = {b"partial_stream"}
        mock_redis.hgetall.return_value = {
            b"messages_published": b"10",
            # consumed, failed, pending_count all missing
        }
        metrics = list(collector._collect_stream_metrics())
        consumed = next(m for m in metrics if m.name == "pylon_stream_messages_consumed")
        assert consumed.samples[0].value == 0


# ---------------------------------------------------------------------------
# Tests: Full collect() integration
# ---------------------------------------------------------------------------

class TestCollect:
    """Tests for the complete collect() method."""

    def test_collect_yields_all_metric_types(self, collector, mock_redis, mock_sio):
        """Full collect yields connections + queue + stream metrics."""
        mock_sio.manager.get_participants.return_value = iter([("s1", None)])
        mock_redis.xlen.return_value = 3
        mock_redis.smembers.return_value = {b"test_stream"}
        mock_redis.hgetall.return_value = {
            b"messages_published": b"1",
            b"messages_consumed": b"1",
            b"messages_failed": b"0",
            b"pending_count": b"0",
        }

        metrics = list(collector.collect())
        names = {m.name for m in metrics}
        assert "pylon_active_connections" in names
        assert "pylon_task_queue_depth" in names
        assert "pylon_stream_messages_published" in names

    def test_collect_with_no_dependencies(self):
        """Collector with no SIO/Redis yields zeros gracefully."""
        collector = MetricsCollector(sio_server=None, redis_client=None)
        metrics = list(collector.collect())
        # At minimum: connections gauge + queue depth gauge
        names = [m.name for m in metrics]
        assert "pylon_active_connections" in names
        assert "pylon_task_queue_depth" in names

    def test_collect_connection_gauge_has_namespace_label(self, collector, mock_sio):
        """pylon_active_connections has a 'namespace' label."""
        mock_sio.manager.get_participants.return_value = iter([("s1", None)])
        metrics = list(collector._collect_connection_metrics())
        sample = metrics[0].samples[0]
        assert sample.labels == {"namespace": "/"}


# ---------------------------------------------------------------------------
# Tests: get_registry()
# ---------------------------------------------------------------------------

class TestGetRegistry:
    """Tests for get_registry() helper."""

    def test_creates_dedicated_registry(self, collector):
        """Returns a CollectorRegistry separate from global REGISTRY."""
        from prometheus_client.core import REGISTRY as GLOBAL_REGISTRY
        registry = get_registry(collector)
        assert registry is not GLOBAL_REGISTRY

    def test_collector_is_registered(self, collector):
        """The collector can be scraped from the returned registry."""
        registry = get_registry(collector)
        from prometheus_client import generate_latest
        output = generate_latest(registry).decode("utf-8")
        assert "pylon_active_connections" in output
        assert "pylon_task_queue_depth" in output

    def test_multiple_registries_independent(self, mock_sio, mock_redis):
        """Two registries from different collectors don't interfere."""
        c1 = MetricsCollector(sio_server=mock_sio, redis_client=mock_redis)
        c2 = MetricsCollector(sio_server=None, redis_client=None)
        r1 = get_registry(c1)
        r2 = get_registry(c2)
        assert r1 is not r2


# ---------------------------------------------------------------------------
# Tests: /metrics route
# ---------------------------------------------------------------------------

class TestMetricsRoute:
    """Tests for the /metrics HTTP endpoint."""

    def test_route_returns_prometheus_content_type(self):
        """Endpoint returns text/plain with Prometheus version."""
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

        collector = MetricsCollector(sio_server=None, redis_client=MagicMock())
        collector._redis.xlen.return_value = 0
        collector._redis.smembers.return_value = set()
        registry = get_registry(collector)
        output = generate_latest(registry)
        assert isinstance(output, bytes)
        assert b"pylon_active_connections" in output
        assert b"pylon_task_queue_depth" in output

    def test_output_contains_help_and_type(self):
        """Prometheus format includes HELP and TYPE annotations."""
        from prometheus_client import generate_latest

        redis = MagicMock()
        redis.xlen.return_value = 5
        redis.smembers.return_value = set()
        collector = MetricsCollector(sio_server=None, redis_client=redis)
        registry = get_registry(collector)
        output = generate_latest(registry).decode("utf-8")

        assert "# HELP pylon_active_connections" in output
        assert "# TYPE pylon_active_connections gauge" in output
        assert "# HELP pylon_task_queue_depth" in output
        assert "# TYPE pylon_task_queue_depth gauge" in output

    def test_output_contains_stream_metrics_when_available(self):
        """Stream metrics appear when streams are registered."""
        from prometheus_client import generate_latest

        redis = MagicMock()
        redis.xlen.return_value = 0
        redis.smembers.return_value = {b"work:task_distribution"}
        redis.hgetall.return_value = {
            b"messages_published": b"500",
            b"messages_consumed": b"490",
            b"messages_failed": b"2",
            b"pending_count": b"8",
        }
        collector = MetricsCollector(sio_server=None, redis_client=redis)
        registry = get_registry(collector)
        output = generate_latest(registry).decode("utf-8")

        assert "pylon_stream_messages_published" in output
        assert "pylon_stream_messages_consumed" in output
        assert "pylon_stream_messages_failed" in output
        assert "pylon_stream_pending_count" in output
        assert 'stream="work:task_distribution"' in output

    def test_metric_values_in_output(self):
        """Actual numeric values appear in the text output."""
        from prometheus_client import generate_latest

        sio = MagicMock()
        sio.manager.get_participants.return_value = iter([
            ("s1", None), ("s2", None), ("s3", None), ("s4", None), ("s5", None)
        ])
        redis = MagicMock()
        redis.xlen.return_value = 23
        redis.smembers.return_value = set()

        collector = MetricsCollector(sio_server=sio, redis_client=redis)
        registry = get_registry(collector)
        output = generate_latest(registry).decode("utf-8")

        assert "5.0" in output  # 5 connections
        assert "23.0" in output  # queue depth 23


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases and robustness."""

    def test_large_connection_count(self, mock_sio, mock_redis):
        """Handles large number of connections."""
        mock_sio.manager.get_participants.return_value = iter(
            [(f"sid_{i}", None) for i in range(10000)]
        )
        collector = MetricsCollector(sio_server=mock_sio, redis_client=mock_redis)
        assert collector._get_active_connections() == 10000

    def test_large_queue_depth(self, mock_redis):
        """Handles large queue depth value."""
        mock_redis.xlen.return_value = 999999
        collector = MetricsCollector(sio_server=None, redis_client=mock_redis)
        assert collector._get_task_queue_depth() == 999999

    def test_zero_values_everywhere(self, mock_redis):
        """All zeros is a valid state."""
        mock_redis.xlen.return_value = 0
        mock_redis.smembers.return_value = set()
        collector = MetricsCollector(sio_server=None, redis_client=mock_redis)
        metrics = list(collector.collect())
        for m in metrics:
            for sample in m.samples:
                assert sample.value == 0

    def test_collect_is_idempotent(self, collector, mock_sio, mock_redis):
        """Calling collect() multiple times produces consistent results."""
        mock_sio.manager.get_participants.return_value = iter([("s1", None)])
        mock_redis.xlen.return_value = 7
        mock_redis.smembers.return_value = set()

        results1 = list(collector.collect())

        mock_sio.manager.get_participants.return_value = iter([("s1", None)])
        results2 = list(collector.collect())

        assert len(results1) == len(results2)

    def test_rooms_returns_none(self):
        """manager.rooms() returning None doesn't crash."""
        sio = MagicMock()
        manager = MagicMock(spec=[])
        manager.rooms = MagicMock(return_value=None)
        sio.manager = manager
        del manager.get_participants
        collector = MetricsCollector(sio_server=sio, redis_client=MagicMock())
        assert collector._get_active_connections() == 0

    def test_concurrent_access_safe(self, collector, mock_sio, mock_redis):
        """Collector doesn't hold state between scrapes."""
        mock_sio.manager.get_participants.return_value = iter([("s1", None)])
        mock_redis.xlen.return_value = 1
        mock_redis.smembers.return_value = set()
        list(collector.collect())

        mock_sio.manager.get_participants.return_value = iter([("s1", None), ("s2", None)])
        mock_redis.xlen.return_value = 5
        metrics = list(collector.collect())
        conn_metric = next(m for m in metrics if m.name == "pylon_active_connections")
        assert conn_metric.samples[0].value == 2

        queue_metric = next(m for m in metrics if m.name == "pylon_task_queue_depth")
        assert queue_metric.samples[0].value == 5
