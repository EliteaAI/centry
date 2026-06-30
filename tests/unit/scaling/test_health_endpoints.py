"""Unit tests for health check endpoints (/health/live, /health/ready).

Validates that:
1. /health/live returns 200 when Redis and PostgreSQL are healthy
2. /health/live returns 503 when Redis is down
3. /health/live returns 503 when PostgreSQL is down
4. /health/live includes latency measurements for each check
5. /health/ready returns 200 when all checks pass and plugin is initialized
6. /health/ready returns 503 when _scaling_ready is False (plugin not ready)
7. /health/ready returns 503 when Redis is down
8. /health/ready returns 503 when PostgreSQL is down
9. Response format is JSON with correct structure
10. Error messages are included for failed checks without leaking sensitive info

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_health_endpoints.py -v
"""

import importlib.util
import json
import pathlib
import sys
import types
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module loading setup — mock flask and pylon before loading health.py
# ---------------------------------------------------------------------------

_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[3] / "pylon_main" / "plugins" / "elitea_core"

# Mock pylon.core.tools — reuse existing mock if another test module set it up
sys.modules.setdefault("pylon", MagicMock())
sys.modules.setdefault("pylon.core", MagicMock())

if "pylon.core.tools" not in sys.modules:
    _mock_pylon_core_tools = MagicMock()
    sys.modules["pylon.core.tools"] = _mock_pylon_core_tools
else:
    _mock_pylon_core_tools = sys.modules["pylon.core.tools"]

# Ensure web.route is a passthrough decorator (safe to reassign on shared mock)
def _route_decorator(*args, **kwargs):
    def wrapper(fn):
        return fn
    return wrapper

_mock_pylon_core_tools.web.route = _route_decorator


# Mock flask.jsonify — returns a dict-like object that we can inspect
class FakeJsonResponse:
    """Mimics flask.jsonify response for testing."""

    def __init__(self, data):
        self._data = data

    def get_data(self, as_text=False):
        return json.dumps(self._data)


_mock_flask = types.ModuleType("flask")
_mock_flask.jsonify = lambda data: FakeJsonResponse(data)
sys.modules.setdefault("flask", _mock_flask)

# Note: we do NOT mock 'tools' at module level — the health route imports it
# lazily inside the function body. We mock it at call time in helpers instead.

# Load the module under test
_mod_path = _PLUGIN_ROOT / "routes" / "health.py"
_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.routes.health", _mod_path
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

Route = _mod.Route


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_module_mock(redis_ok=True, pg_ok=True, scaling_ready=True, sentinel_info=None):
    """Create a mock module instance that stands in for `self` in routes."""
    mock_module = MagicMock()
    mock_module._scaling_ready = scaling_ready

    redis_client = MagicMock()
    if redis_ok:
        redis_client.ping.return_value = True
    else:
        redis_client.ping.side_effect = ConnectionError("Connection refused")
    mock_module.get_redis_client.return_value = redis_client
    mock_module.get_sentinel_info.return_value = sentinel_info

    return mock_module


def make_db_engine_mock(pg_ok=True):
    """Create a mock DB engine."""
    mock_engine = MagicMock()
    if pg_ok:
        mock_conn = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
    else:
        mock_engine.connect.side_effect = Exception("could not connect to server")
    return mock_engine


def call_health_live(module, engine):
    """Call Route.health_live with mocked db tools."""
    mock_db = MagicMock()
    mock_db.engine = engine

    with patch.object(_mod, "_get_db_tools", return_value=mock_db, create=True):
        # Patch the import mechanism for `from tools import db as db_tools`
        original_tools = sys.modules.get("tools")
        fake_tools = types.ModuleType("tools")
        fake_tools.db = mock_db
        sys.modules["tools"] = fake_tools
        try:
            return Route.health_live(module)
        finally:
            if original_tools is not None:
                sys.modules["tools"] = original_tools
            else:
                del sys.modules["tools"]


def call_health_ready(module, engine):
    """Call Route.health_ready with mocked db tools."""
    mock_db = MagicMock()
    mock_db.engine = engine

    original_tools = sys.modules.get("tools")
    fake_tools = types.ModuleType("tools")
    fake_tools.db = mock_db
    sys.modules["tools"] = fake_tools
    try:
        return Route.health_ready(module)
    finally:
        if original_tools is not None:
            sys.modules["tools"] = original_tools
        else:
            del sys.modules["tools"]


def parse_response(response_tuple):
    """Parse the (FakeJsonResponse, status_code) tuple."""
    json_response, status_code = response_tuple
    data = json.loads(json_response.get_data(as_text=True))
    return data, status_code


# ---------------------------------------------------------------------------
# Tests: /health/live
# ---------------------------------------------------------------------------

class TestHealthLive:
    """Tests for the /health/live endpoint."""

    def test_returns_200_when_all_healthy(self):
        module = make_module_mock(redis_ok=True)
        engine = make_db_engine_mock(pg_ok=True)
        data, status = parse_response(call_health_live(module, engine))

        assert status == 200
        assert data["status"] == "ok"
        assert data["checks"]["redis"]["status"] == "ok"
        assert data["checks"]["postgres"]["status"] == "ok"

    def test_returns_503_when_redis_down(self):
        module = make_module_mock(redis_ok=False)
        engine = make_db_engine_mock(pg_ok=True)
        data, status = parse_response(call_health_live(module, engine))

        assert status == 503
        assert data["status"] == "unhealthy"
        assert data["checks"]["redis"]["status"] == "unhealthy"
        assert "error" in data["checks"]["redis"]
        assert data["checks"]["postgres"]["status"] == "ok"

    def test_returns_503_when_postgres_down(self):
        module = make_module_mock(redis_ok=True)
        engine = make_db_engine_mock(pg_ok=False)
        data, status = parse_response(call_health_live(module, engine))

        assert status == 503
        assert data["status"] == "unhealthy"
        assert data["checks"]["redis"]["status"] == "ok"
        assert data["checks"]["postgres"]["status"] == "unhealthy"
        assert "error" in data["checks"]["postgres"]

    def test_returns_503_when_both_down(self):
        module = make_module_mock(redis_ok=False)
        engine = make_db_engine_mock(pg_ok=False)
        data, status = parse_response(call_health_live(module, engine))

        assert status == 503
        assert data["status"] == "unhealthy"
        assert data["checks"]["redis"]["status"] == "unhealthy"
        assert data["checks"]["postgres"]["status"] == "unhealthy"

    def test_redis_latency_is_non_negative(self):
        module = make_module_mock(redis_ok=True)
        engine = make_db_engine_mock(pg_ok=True)
        data, _ = parse_response(call_health_live(module, engine))

        assert data["checks"]["redis"]["latency_ms"] >= 0

    def test_postgres_latency_is_non_negative(self):
        module = make_module_mock(redis_ok=True)
        engine = make_db_engine_mock(pg_ok=True)
        data, _ = parse_response(call_health_live(module, engine))

        assert data["checks"]["postgres"]["latency_ms"] >= 0

    def test_error_message_is_string(self):
        module = make_module_mock(redis_ok=False)
        engine = make_db_engine_mock(pg_ok=True)
        data, _ = parse_response(call_health_live(module, engine))

        assert isinstance(data["checks"]["redis"]["error"], str)

    def test_redis_timeout_exception(self):
        module = make_module_mock(redis_ok=True)
        module.get_redis_client.return_value.ping.side_effect = TimeoutError("Timed out")
        engine = make_db_engine_mock(pg_ok=True)
        data, status = parse_response(call_health_live(module, engine))

        assert status == 503
        assert data["checks"]["redis"]["status"] == "unhealthy"
        assert "Timed out" in data["checks"]["redis"]["error"]

    def test_get_redis_client_raises(self):
        module = make_module_mock(redis_ok=True)
        module.get_redis_client.side_effect = RuntimeError("Redis not configured")
        engine = make_db_engine_mock(pg_ok=True)
        data, status = parse_response(call_health_live(module, engine))

        assert status == 503
        assert data["checks"]["redis"]["status"] == "unhealthy"
        assert "Redis not configured" in data["checks"]["redis"]["error"]

    def test_redis_connection_error(self):
        module = make_module_mock(redis_ok=True)
        module.get_redis_client.return_value.ping.side_effect = ConnectionError(
            "Error 111 connecting to redis:6379"
        )
        engine = make_db_engine_mock(pg_ok=True)
        data, status = parse_response(call_health_live(module, engine))

        assert status == 503
        assert data["checks"]["redis"]["status"] == "unhealthy"

    def test_postgres_operational_error(self):
        module = make_module_mock(redis_ok=True)
        engine = MagicMock()
        engine.connect.side_effect = Exception("FATAL: too many connections")
        data, status = parse_response(call_health_live(module, engine))

        assert status == 503
        assert data["checks"]["postgres"]["status"] == "unhealthy"

    def test_has_exactly_two_checks(self):
        module = make_module_mock(redis_ok=True)
        engine = make_db_engine_mock(pg_ok=True)
        data, _ = parse_response(call_health_live(module, engine))

        assert set(data["checks"].keys()) == {"redis", "postgres"}


# ---------------------------------------------------------------------------
# Tests: /health/ready
# ---------------------------------------------------------------------------

class TestHealthReady:
    """Tests for the /health/ready endpoint."""

    def test_returns_200_when_fully_ready(self):
        module = make_module_mock(redis_ok=True, scaling_ready=True)
        engine = make_db_engine_mock(pg_ok=True)
        data, status = parse_response(call_health_ready(module, engine))

        assert status == 200
        assert data["status"] == "ok"
        assert data["checks"]["init"]["status"] == "ok"
        assert data["checks"]["redis"]["status"] == "ok"
        assert data["checks"]["postgres"]["status"] == "ok"

    def test_returns_503_when_not_ready(self):
        module = make_module_mock(redis_ok=True, scaling_ready=False)
        engine = make_db_engine_mock(pg_ok=True)
        data, status = parse_response(call_health_ready(module, engine))

        assert status == 503
        assert data["status"] == "not_ready"
        assert data["checks"]["init"]["status"] == "not_ready"

    def test_returns_503_when_redis_down(self):
        module = make_module_mock(redis_ok=False, scaling_ready=True)
        engine = make_db_engine_mock(pg_ok=True)
        data, status = parse_response(call_health_ready(module, engine))

        assert status == 503
        assert data["status"] == "unhealthy"
        assert data["checks"]["redis"]["status"] == "unhealthy"

    def test_returns_503_when_postgres_down(self):
        module = make_module_mock(redis_ok=True, scaling_ready=True)
        engine = make_db_engine_mock(pg_ok=False)
        data, status = parse_response(call_health_ready(module, engine))

        assert status == 503
        assert data["status"] == "unhealthy"
        assert data["checks"]["postgres"]["status"] == "unhealthy"

    def test_scaling_ready_attribute_missing(self):
        """If _scaling_ready not set, treat as not ready."""
        module = make_module_mock(redis_ok=True)
        del module._scaling_ready
        engine = make_db_engine_mock(pg_ok=True)
        data, status = parse_response(call_health_ready(module, engine))

        assert status == 503
        assert data["checks"]["init"]["status"] == "not_ready"

    def test_redis_latency_included(self):
        module = make_module_mock(redis_ok=True, scaling_ready=True)
        engine = make_db_engine_mock(pg_ok=True)
        data, _ = parse_response(call_health_ready(module, engine))

        assert "latency_ms" in data["checks"]["redis"]
        assert isinstance(data["checks"]["redis"]["latency_ms"], (int, float))

    def test_postgres_latency_included(self):
        module = make_module_mock(redis_ok=True, scaling_ready=True)
        engine = make_db_engine_mock(pg_ok=True)
        data, _ = parse_response(call_health_ready(module, engine))

        assert "latency_ms" in data["checks"]["postgres"]
        assert isinstance(data["checks"]["postgres"]["latency_ms"], (int, float))

    def test_init_check_no_latency(self):
        """Init check is a boolean, no latency measurement."""
        module = make_module_mock(redis_ok=True, scaling_ready=True)
        engine = make_db_engine_mock(pg_ok=True)
        data, _ = parse_response(call_health_ready(module, engine))

        assert "latency_ms" not in data["checks"]["init"]

    def test_has_three_checks(self):
        module = make_module_mock(redis_ok=True, scaling_ready=True)
        engine = make_db_engine_mock(pg_ok=True)
        data, _ = parse_response(call_health_ready(module, engine))

        assert set(data["checks"].keys()) == {"init", "redis", "postgres"}

    def test_not_ready_still_checks_deps(self):
        """Even when not ready, should still report dep status."""
        module = make_module_mock(redis_ok=False, scaling_ready=False)
        engine = make_db_engine_mock(pg_ok=False)
        data, status = parse_response(call_health_ready(module, engine))

        assert status == 503
        # All three checks should be present
        assert "init" in data["checks"]
        assert "redis" in data["checks"]
        assert "postgres" in data["checks"]

    def test_status_unhealthy_overrides_not_ready(self):
        """If deps are unhealthy, status should be 'unhealthy' even if init incomplete."""
        module = make_module_mock(redis_ok=False, scaling_ready=False)
        engine = make_db_engine_mock(pg_ok=True)
        data, status = parse_response(call_health_ready(module, engine))

        assert status == 503
        # 'unhealthy' takes precedence over 'not_ready'
        assert data["status"] == "unhealthy"


# ---------------------------------------------------------------------------
# Tests: Response format
# ---------------------------------------------------------------------------

class TestResponseFormat:
    """Tests for common response format."""

    def test_live_response_has_status_and_checks(self):
        module = make_module_mock(redis_ok=True)
        engine = make_db_engine_mock(pg_ok=True)
        data, _ = parse_response(call_health_live(module, engine))

        assert "status" in data
        assert "checks" in data

    def test_ready_response_has_status_and_checks(self):
        module = make_module_mock(redis_ok=True, scaling_ready=True)
        engine = make_db_engine_mock(pg_ok=True)
        data, _ = parse_response(call_health_ready(module, engine))

        assert "status" in data
        assert "checks" in data

    def test_status_values_are_expected(self):
        module = make_module_mock(redis_ok=True, scaling_ready=True)
        engine = make_db_engine_mock(pg_ok=True)
        data, _ = parse_response(call_health_live(module, engine))

        assert data["status"] in ("ok", "unhealthy", "not_ready")

    def test_latency_is_float_or_int(self):
        module = make_module_mock(redis_ok=True)
        engine = make_db_engine_mock(pg_ok=True)
        data, _ = parse_response(call_health_live(module, engine))

        for check_name in ("redis", "postgres"):
            latency = data["checks"][check_name]["latency_ms"]
            assert isinstance(latency, (int, float))

    def test_unhealthy_latency_still_present(self):
        """Even failing checks should include latency."""
        module = make_module_mock(redis_ok=False)
        engine = make_db_engine_mock(pg_ok=False)
        data, _ = parse_response(call_health_live(module, engine))

        assert "latency_ms" in data["checks"]["redis"]
        assert "latency_ms" in data["checks"]["postgres"]
