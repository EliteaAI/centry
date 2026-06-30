"""Unit tests for synthetic monitoring.

Validates that:
1. SyntheticMonitor initializes with correct probe list
2. _probe_redis() calls ping and raises on failure
3. _probe_postgres() executes SELECT 1 and raises on failure
4. _probe_http_health() makes HTTP GET and validates status
5. _probe_socketio() verifies Socket.IO handshake endpoint
6. _execute_probe() wraps probe with timing and error handling
7. _record_result() stores results in Redis, manages failure counters
8. run_probes() executes all probes and returns results
9. get_status() aggregates probe results and alerts
10. Alert lifecycle: set after threshold, clear on recovery
11. get_prometheus_metrics() returns correctly formatted metrics
12. clear_alerts() removes all active alerts
13. reset_probe() resets failure state for a specific probe
14. ProbeResult serialization and deserialization
15. Edge cases: Redis errors during recording, missing data

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_synthetic_monitor.py -v
"""

import importlib
import importlib.util
import pathlib
import sys
import time
import types
from unittest.mock import MagicMock, patch, call, PropertyMock

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

# Load the synthetic_monitor module
_sm_path = _PLUGIN_ROOT / "utils" / "synthetic_monitor.py"
_sm_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.utils.synthetic_monitor",
    _sm_path, submodule_search_locations=[],
)
_sm_mod = importlib.util.module_from_spec(_sm_spec)
sys.modules[_sm_spec.name] = _sm_mod
_sm_spec.loader.exec_module(_sm_mod)

SyntheticMonitor = _sm_mod.SyntheticMonitor
ProbeResult = _sm_mod.ProbeResult
KEY_PREFIX_RESULTS = _sm_mod.KEY_PREFIX_RESULTS
KEY_PREFIX_FAILURES = _sm_mod.KEY_PREFIX_FAILURES
KEY_PREFIX_ALERT = _sm_mod.KEY_PREFIX_ALERT
ALERTS_SET_KEY = _sm_mod.ALERTS_SET_KEY
PROBE_RESULT_TTL = _sm_mod.PROBE_RESULT_TTL
DEFAULT_FAILURE_THRESHOLD = _sm_mod.DEFAULT_FAILURE_THRESHOLD


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def redis_client():
    """Create a mock Redis client."""
    client = MagicMock()
    client.ping.return_value = True
    client.pipeline.return_value = MagicMock()
    client.pipeline.return_value.execute.return_value = []
    client.get.return_value = None
    client.exists.return_value = False
    client.smembers.return_value = set()
    client.hgetall.return_value = {}
    return client


@pytest.fixture
def db_engine():
    """Create a mock SQLAlchemy engine."""
    engine = MagicMock()
    conn = MagicMock()
    row = MagicMock()
    row.fetchone.return_value = (1,)
    conn.execute.return_value = row
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    engine.connect.return_value = conn
    return engine


@pytest.fixture
def monitor(redis_client, db_engine):
    """Create a SyntheticMonitor with all probes configured."""
    return SyntheticMonitor(
        redis_client=redis_client,
        db_engine=db_engine,
        health_url="http://localhost:8080/health/live",
        sio_url="http://localhost:8080",
        failure_threshold=3,
    )


@pytest.fixture
def minimal_monitor(redis_client):
    """Create a SyntheticMonitor with only Redis probe."""
    return SyntheticMonitor(
        redis_client=redis_client,
        failure_threshold=3,
    )


# ---------------------------------------------------------------------------
# ProbeResult tests
# ---------------------------------------------------------------------------

class TestProbeResult:
    def test_create_success(self):
        r = ProbeResult(name="redis", success=True, latency_ms=1.5, timestamp=100.0)
        assert r.name == "redis"
        assert r.success is True
        assert r.latency_ms == 1.5
        assert r.error == ""
        assert r.timestamp == 100.0

    def test_create_failure(self):
        r = ProbeResult(name="pg", success=False, latency_ms=50.0, error="timeout")
        assert r.success is False
        assert r.error == "timeout"

    def test_to_dict_success(self):
        r = ProbeResult(name="redis", success=True, latency_ms=2.345, timestamp=1000.5)
        d = r.to_dict()
        assert d["name"] == "redis"
        assert d["success"] == "1"
        assert d["latency_ms"] == "2.35"
        assert d["error"] == ""
        assert d["timestamp"] == "1000.5"

    def test_to_dict_failure(self):
        r = ProbeResult(name="pg", success=False, latency_ms=100.1, error="conn refused")
        d = r.to_dict()
        assert d["success"] == "0"
        assert d["error"] == "conn refused"

    def test_default_timestamp(self):
        before = time.time()
        r = ProbeResult(name="test", success=True, latency_ms=0.0)
        after = time.time()
        assert before <= r.timestamp <= after


# ---------------------------------------------------------------------------
# Initialization tests
# ---------------------------------------------------------------------------

class TestInitialization:
    def test_all_probes_configured(self, monitor):
        names = monitor.probe_names
        assert "redis" in names
        assert "postgres" in names
        assert "http_health" in names
        assert "socketio" in names

    def test_redis_only(self, minimal_monitor):
        names = minimal_monitor.probe_names
        assert names == ["redis"]

    def test_no_sio_when_empty_url(self, redis_client, db_engine):
        m = SyntheticMonitor(redis_client=redis_client, db_engine=db_engine)
        assert "socketio" not in m.probe_names
        assert "redis" in m.probe_names
        assert "postgres" in m.probe_names

    def test_no_postgres_when_no_engine(self, redis_client):
        m = SyntheticMonitor(redis_client=redis_client, health_url="http://x")
        assert "postgres" not in m.probe_names
        assert "http_health" in m.probe_names

    def test_failure_threshold_validation(self, redis_client):
        with pytest.raises(ValueError, match="failure_threshold must be >= 1"):
            SyntheticMonitor(redis_client=redis_client, failure_threshold=0)

    def test_failure_threshold_negative(self, redis_client):
        with pytest.raises(ValueError, match="failure_threshold must be >= 1"):
            SyntheticMonitor(redis_client=redis_client, failure_threshold=-1)

    def test_custom_failure_threshold(self, redis_client):
        m = SyntheticMonitor(redis_client=redis_client, failure_threshold=5)
        assert m._failure_threshold == 5


# ---------------------------------------------------------------------------
# Probe execution tests
# ---------------------------------------------------------------------------

class TestProbeRedis:
    def test_success(self, monitor, redis_client):
        redis_client.ping.return_value = True
        monitor._probe_redis()

    def test_failure_raises(self, monitor, redis_client):
        redis_client.ping.side_effect = ConnectionError("Connection refused")
        with pytest.raises(ConnectionError):
            monitor._probe_redis()

    def test_falsy_response_raises(self, monitor, redis_client):
        redis_client.ping.return_value = False
        with pytest.raises(RuntimeError, match="PING returned falsy"):
            monitor._probe_redis()


class TestProbePostgres:
    def test_success(self, monitor, db_engine):
        monitor._probe_postgres()
        db_engine.connect.assert_called_once()

    def test_connection_error(self, monitor, db_engine):
        db_engine.connect.side_effect = Exception("pg down")
        with pytest.raises(Exception, match="pg down"):
            monitor._probe_postgres()

    def test_no_rows_raises(self, monitor, db_engine):
        conn = MagicMock()
        row = MagicMock()
        row.fetchone.return_value = None
        conn.execute.return_value = row
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        db_engine.connect.return_value = conn
        with pytest.raises(RuntimeError, match="no rows"):
            monitor._probe_postgres()


class TestProbeHttpHealth:
    def test_success(self, monitor):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            monitor._probe_http_health()

    def test_non_200_raises(self, monitor):
        mock_resp = MagicMock()
        mock_resp.status = 503
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="returned 503"):
                monitor._probe_http_health()

    def test_http_error(self, monitor):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError(None, 500, "err", {}, None)):
            with pytest.raises(RuntimeError, match="returned 500"):
                monitor._probe_http_health()

    def test_url_error(self, monitor):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("timeout")):
            with pytest.raises(RuntimeError, match="unreachable"):
                monitor._probe_http_health()


class TestProbeSocketIO:
    def test_success(self, monitor):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'0{"sid":"abc123","upgrades":["websocket"]}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            monitor._probe_socketio()

    def test_missing_sid(self, monitor):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"error": "invalid"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="missing sid"):
                monitor._probe_socketio()

    def test_non_200(self, monitor):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError(None, 404, "not found", {}, None)):
            with pytest.raises(RuntimeError, match="returned 404"):
                monitor._probe_socketio()

    def test_connection_refused(self, monitor):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("Connection refused")):
            with pytest.raises(RuntimeError, match="unreachable"):
                monitor._probe_socketio()


# ---------------------------------------------------------------------------
# Execute probe tests
# ---------------------------------------------------------------------------

class TestExecuteProbe:
    def test_success_result(self, monitor):
        def ok_probe():
            pass

        result = monitor._execute_probe("test_probe", ok_probe)
        assert result.name == "test_probe"
        assert result.success is True
        assert result.latency_ms >= 0
        assert result.error == ""

    def test_failure_result(self, monitor):
        def bad_probe():
            raise RuntimeError("boom")

        result = monitor._execute_probe("fail_probe", bad_probe)
        assert result.name == "fail_probe"
        assert result.success is False
        assert "boom" in result.error

    def test_latency_measured(self, monitor):
        def slow_probe():
            time.sleep(0.01)

        result = monitor._execute_probe("slow", slow_probe)
        assert result.latency_ms >= 10

    def test_error_truncated(self, monitor):
        def long_error():
            raise RuntimeError("x" * 500)

        result = monitor._execute_probe("trunc", long_error)
        assert len(result.error) <= 200


# ---------------------------------------------------------------------------
# Record result tests
# ---------------------------------------------------------------------------

class TestRecordResult:
    def test_success_resets_counter(self, monitor, redis_client):
        result = ProbeResult(name="redis", success=True, latency_ms=1.0)
        pipe = redis_client.pipeline.return_value

        monitor._record_result(result)

        pipe.hset.assert_called()
        pipe.expire.assert_called()
        pipe.set.assert_called()
        pipe.execute.assert_called()

    def test_failure_increments_counter(self, monitor, redis_client):
        result = ProbeResult(name="redis", success=False, latency_ms=1.0, error="down")
        pipe = redis_client.pipeline.return_value
        redis_client.get.return_value = b"1"

        monitor._record_result(result)

        pipe.incr.assert_called()
        pipe.execute.assert_called()

    def test_failure_threshold_triggers_alert(self, monitor, redis_client):
        result = ProbeResult(name="redis", success=False, latency_ms=1.0, error="down")
        redis_client.get.return_value = b"3"
        redis_client.exists.return_value = False

        monitor._record_result(result)

        redis_client.hset.assert_called()
        redis_client.sadd.assert_called_with(ALERTS_SET_KEY, "redis")

    def test_below_threshold_no_alert(self, monitor, redis_client):
        result = ProbeResult(name="redis", success=False, latency_ms=1.0, error="flap")
        redis_client.get.return_value = b"1"

        monitor._record_result(result)

        redis_client.sadd.assert_not_called()

    def test_success_clears_existing_alert(self, monitor, redis_client):
        result = ProbeResult(name="redis", success=True, latency_ms=1.0)
        redis_client.exists.return_value = True

        monitor._record_result(result)

        redis_client.delete.assert_called()
        redis_client.srem.assert_called_with(ALERTS_SET_KEY, "redis")


# ---------------------------------------------------------------------------
# Run probes tests
# ---------------------------------------------------------------------------

class TestRunProbes:
    def test_runs_all_probes(self, minimal_monitor, redis_client):
        redis_client.ping.return_value = True
        redis_client.get.return_value = b"0"

        results = minimal_monitor.run_probes()
        assert len(results) == 1
        assert results[0].name == "redis"
        assert results[0].success is True

    def test_multiple_probes(self, monitor, redis_client, db_engine):
        redis_client.get.return_value = b"0"

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'0{"sid":"x"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            results = monitor.run_probes()

        assert len(results) == 4
        names = [r.name for r in results]
        assert "redis" in names
        assert "postgres" in names
        assert "http_health" in names
        assert "socketio" in names

    def test_one_failure_doesnt_stop_others(self, redis_client, db_engine):
        redis_client.ping.side_effect = ConnectionError("down")
        redis_client.get.return_value = b"0"

        m = SyntheticMonitor(redis_client=redis_client, db_engine=db_engine)
        results = m.run_probes()
        assert len(results) == 2
        redis_result = next(r for r in results if r.name == "redis")
        pg_result = next(r for r in results if r.name == "postgres")
        assert redis_result.success is False
        assert pg_result.success is True


# ---------------------------------------------------------------------------
# Get status tests
# ---------------------------------------------------------------------------

class TestGetStatus:
    def test_healthy_status(self, minimal_monitor, redis_client):
        redis_client.hgetall.return_value = {
            b"name": b"redis", b"success": b"1",
            b"latency_ms": b"0.5", b"error": b"", b"timestamp": b"1000",
        }
        redis_client.smembers.return_value = set()

        status = minimal_monitor.get_status()
        assert status["status"] == "healthy"
        assert "redis" in status["probes"]
        assert status["alerts"] == []
        assert status["failure_threshold"] == 3

    def test_degraded_status(self, minimal_monitor, redis_client):
        redis_client.hgetall.return_value = {
            b"name": b"redis", b"success": b"0",
            b"latency_ms": b"5.0", b"error": b"timeout", b"timestamp": b"1000",
        }
        redis_client.smembers.return_value = set()

        status = minimal_monitor.get_status()
        assert status["status"] == "degraded"

    def test_alerting_status(self, minimal_monitor, redis_client):
        redis_client.hgetall.side_effect = [
            {b"name": b"redis", b"success": b"0", b"latency_ms": b"1.0",
             b"error": b"down", b"timestamp": b"1000"},
            {b"probe": b"redis", b"consecutive_failures": b"5",
             b"last_error": b"down", b"alerted_at": b"1000"},
        ]
        redis_client.smembers.return_value = {b"redis"}

        status = minimal_monitor.get_status()
        assert status["status"] == "alerting"
        assert len(status["alerts"]) == 1

    def test_no_data_probe(self, minimal_monitor, redis_client):
        redis_client.hgetall.return_value = {}
        redis_client.smembers.return_value = set()

        status = minimal_monitor.get_status()
        assert status["probes"]["redis"] == {"status": "no_data"}

    def test_stale_alert_removed(self, minimal_monitor, redis_client):
        redis_client.hgetall.side_effect = [
            {b"name": b"redis", b"success": b"1", b"latency_ms": b"1.0",
             b"error": b"", b"timestamp": b"1000"},
            {},  # alert key expired
        ]
        redis_client.smembers.return_value = {b"redis"}

        status = minimal_monitor.get_status()
        assert status["alerts"] == []
        redis_client.srem.assert_called_with(ALERTS_SET_KEY, "redis")


# ---------------------------------------------------------------------------
# Prometheus metrics tests
# ---------------------------------------------------------------------------

class TestPrometheusMetrics:
    def test_returns_probe_metrics(self, minimal_monitor, redis_client):
        redis_client.hgetall.return_value = {
            b"name": b"redis", b"success": b"1",
            b"latency_ms": b"1.23", b"error": b"", b"timestamp": b"1000",
        }
        redis_client.get.return_value = b"0"
        redis_client.smembers.return_value = set()

        metrics = minimal_monitor.get_prometheus_metrics()
        names = [m[0] for m in metrics]
        assert "synthetic_probe_success" in names
        assert "synthetic_probe_latency_ms" in names
        assert "synthetic_probe_consecutive_failures" in names
        assert "synthetic_probe_alerts_active" in names

    def test_success_metric_value(self, minimal_monitor, redis_client):
        redis_client.hgetall.return_value = {
            b"name": b"redis", b"success": b"1",
            b"latency_ms": b"2.0", b"error": b"", b"timestamp": b"1000",
        }
        redis_client.get.return_value = b"0"
        redis_client.smembers.return_value = set()

        metrics = minimal_monitor.get_prometheus_metrics()
        success_metric = next(m for m in metrics if m[0] == "synthetic_probe_success")
        assert success_metric[1] == {"probe": "redis"}
        assert success_metric[2] == 1.0

    def test_failure_metric_value(self, minimal_monitor, redis_client):
        redis_client.hgetall.return_value = {
            b"name": b"redis", b"success": b"0",
            b"latency_ms": b"100.0", b"error": b"err", b"timestamp": b"1000",
        }
        redis_client.get.return_value = b"2"
        redis_client.smembers.return_value = set()

        metrics = minimal_monitor.get_prometheus_metrics()
        success_metric = next(m for m in metrics if m[0] == "synthetic_probe_success")
        assert success_metric[2] == 0.0
        failures_metric = next(m for m in metrics
                               if m[0] == "synthetic_probe_consecutive_failures")
        assert failures_metric[2] == 2.0

    def test_no_data_skips_probe(self, minimal_monitor, redis_client):
        redis_client.hgetall.return_value = {}
        redis_client.get.return_value = b"0"
        redis_client.smembers.return_value = set()

        metrics = minimal_monitor.get_prometheus_metrics()
        success_metrics = [m for m in metrics if m[0] == "synthetic_probe_success"]
        assert len(success_metrics) == 0

    def test_alert_count(self, minimal_monitor, redis_client):
        redis_client.hgetall.return_value = {}
        redis_client.get.return_value = b"0"
        redis_client.smembers.return_value = {b"redis", b"postgres"}

        metrics = minimal_monitor.get_prometheus_metrics()
        alert_metric = next(m for m in metrics if m[0] == "synthetic_probe_alerts_active")
        assert alert_metric[2] == 2.0


# ---------------------------------------------------------------------------
# Clear alerts tests
# ---------------------------------------------------------------------------

class TestClearAlerts:
    def test_clears_all(self, minimal_monitor, redis_client):
        redis_client.smembers.return_value = {b"redis", b"postgres"}

        count = minimal_monitor.clear_alerts()
        assert count == 2
        redis_client.delete.assert_any_call(f"{KEY_PREFIX_ALERT}:redis")
        redis_client.delete.assert_any_call(f"{KEY_PREFIX_ALERT}:postgres")
        redis_client.delete.assert_any_call(ALERTS_SET_KEY)

    def test_no_alerts(self, minimal_monitor, redis_client):
        redis_client.smembers.return_value = set()

        count = minimal_monitor.clear_alerts()
        assert count == 0


# ---------------------------------------------------------------------------
# Reset probe tests
# ---------------------------------------------------------------------------

class TestResetProbe:
    def test_reset_existing(self, minimal_monitor, redis_client):
        result = minimal_monitor.reset_probe("redis")
        assert result is True
        pipe = redis_client.pipeline.return_value
        pipe.delete.assert_called()
        pipe.execute.assert_called()

    def test_reset_unknown_probe(self, minimal_monitor):
        result = minimal_monitor.reset_probe("nonexistent")
        assert result is False


# ---------------------------------------------------------------------------
# Get failure count tests
# ---------------------------------------------------------------------------

class TestGetFailureCount:
    def test_returns_zero_when_none(self, minimal_monitor, redis_client):
        redis_client.get.return_value = None
        assert minimal_monitor._get_failure_count("redis") == 0

    def test_returns_count_bytes(self, minimal_monitor, redis_client):
        redis_client.get.return_value = b"5"
        assert minimal_monitor._get_failure_count("redis") == 5

    def test_returns_count_str(self, minimal_monitor, redis_client):
        redis_client.get.return_value = "3"
        assert minimal_monitor._get_failure_count("redis") == 3


# ---------------------------------------------------------------------------
# Decode hash tests
# ---------------------------------------------------------------------------

class TestDecodeHash:
    def test_decode_bytes(self):
        raw = {b"key1": b"val1", b"key2": b"val2"}
        result = SyntheticMonitor._decode_hash(raw)
        assert result == {"key1": "val1", "key2": "val2"}

    def test_decode_str(self):
        raw = {"key1": "val1", "key2": "val2"}
        result = SyntheticMonitor._decode_hash(raw)
        assert result == {"key1": "val1", "key2": "val2"}

    def test_decode_mixed(self):
        raw = {b"bkey": "sval", "skey": b"bval"}
        result = SyntheticMonitor._decode_hash(raw)
        assert result == {"bkey": "sval", "skey": "bval"}


# ---------------------------------------------------------------------------
# Alert lifecycle integration tests
# ---------------------------------------------------------------------------

class TestAlertLifecycle:
    def test_three_failures_triggers_alert(self, minimal_monitor, redis_client):
        redis_client.ping.side_effect = ConnectionError("down")
        redis_client.get.side_effect = [b"1", b"2", b"3"]
        redis_client.exists.return_value = False

        for _ in range(3):
            minimal_monitor.run_probes()

        redis_client.sadd.assert_called_with(ALERTS_SET_KEY, "redis")

    def test_recovery_after_alert(self, minimal_monitor, redis_client):
        redis_client.ping.return_value = True
        redis_client.get.return_value = b"0"
        redis_client.exists.return_value = True

        result = ProbeResult(name="redis", success=True, latency_ms=1.0)
        minimal_monitor._record_result(result)

        redis_client.delete.assert_called()
        redis_client.srem.assert_called_with(ALERTS_SET_KEY, "redis")

    def test_set_alert_stores_details(self, minimal_monitor, redis_client):
        minimal_monitor._set_alert("redis", 5, "connection refused")

        alert_key = f"{KEY_PREFIX_ALERT}:redis"
        call_args = redis_client.hset.call_args
        assert call_args[0][0] == alert_key
        mapping = call_args[1]["mapping"]
        assert mapping["probe"] == "redis"
        assert mapping["consecutive_failures"] == "5"
        assert mapping["last_error"] == "connection refused"
        redis_client.sadd.assert_called_with(ALERTS_SET_KEY, "redis")
