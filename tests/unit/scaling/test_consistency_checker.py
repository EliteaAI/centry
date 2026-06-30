"""Unit tests for consistency_checker module.

Validates that:
1. CheckResult produces correct dict output
2. ConsistencyChecker initializes with correct check list
3. Session check compares Redis scan count vs DB count
4. Session check respects tolerance (±5)
5. Canvas version check detects mismatches
6. Canvas version check handles no canvases gracefully
7. Feature flag check validates JSON schema
8. Feature flag check accepts legacy "1"/"0" format
9. Feature flag check detects invalid/corrupt values
10. run_all_checks records results and updates last_run
11. Error in a check produces inconsistent result (not crash)
12. get_status returns correct overall status
13. get_metrics produces Prometheus-compatible format
14. reset_metrics clears counters
15. _validate_flag_value handles all edge cases
16. _record_result increments metric counter on inconsistency

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_consistency_checker.py -v
"""

import importlib
import importlib.util
import json
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
_plugin_pkg.utils = _utils_pkg
sys.modules.setdefault("centry.pylon_main.plugins.elitea_core", _plugin_pkg)


def _load_module(module_name, file_name):
    """Load a module from the plugin utils directory."""
    spec = importlib.util.spec_from_file_location(
        f"centry.pylon_main.plugins.elitea_core.utils.{module_name}",
        _PLUGIN_ROOT / "utils" / file_name,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_cc_mod = _load_module("consistency_checker", "consistency_checker.py")

ConsistencyChecker = _cc_mod.ConsistencyChecker
CheckResult = _cc_mod.CheckResult
CHECK_INTERVAL_S = _cc_mod.CHECK_INTERVAL_S
RESULT_TTL = _cc_mod.RESULT_TTL
KEY_PREFIX_RESULTS = _cc_mod.KEY_PREFIX_RESULTS
KEY_PREFIX_METRICS = _cc_mod.KEY_PREFIX_METRICS
LAST_RUN_KEY = _cc_mod.LAST_RUN_KEY
CANVAS_AUTOSAVE_PREFIX = _cc_mod.CANVAS_AUTOSAVE_PREFIX
FEATURE_FLAG_GLOBAL_PREFIX = _cc_mod.FEATURE_FLAG_GLOBAL_PREFIX


# --- Fixtures ---


@pytest.fixture
def mock_redis():
    """Create a mock Redis client with common operations."""
    client = MagicMock()
    pipe = MagicMock()
    pipe.execute.return_value = []
    client.pipeline.return_value = pipe
    client.scan.return_value = (0, [])
    client.get.return_value = None
    client.hgetall.return_value = {}
    client.smembers.return_value = set()
    return client


@pytest.fixture
def mock_db_engine():
    """Create a mock SQLAlchemy engine."""
    engine = MagicMock()
    conn = MagicMock()
    engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
    engine.connect.return_value.__exit__ = MagicMock(return_value=False)
    return engine


@pytest.fixture
def checker(mock_redis, mock_db_engine):
    """Create a ConsistencyChecker with both Redis and DB engine."""
    return ConsistencyChecker(
        redis_client=mock_redis,
        db_engine=mock_db_engine,
    )


@pytest.fixture
def checker_redis_only(mock_redis):
    """Create a ConsistencyChecker without DB engine (feature flags only)."""
    return ConsistencyChecker(redis_client=mock_redis)


# --- CheckResult Tests ---


class TestCheckResult:
    def test_init_defaults(self):
        r = CheckResult(name="test", consistent=True)
        assert r.name == "test"
        assert r.consistent is True
        assert r.details == ""
        assert r.redis_count == 0
        assert r.db_count == 0
        assert r.mismatches == 0
        assert r.timestamp > 0

    def test_init_with_all_params(self):
        ts = 1234567890.0
        r = CheckResult(
            name="sessions",
            consistent=False,
            details="drift detected",
            redis_count=10,
            db_count=5,
            mismatches=5,
            timestamp=ts,
        )
        assert r.name == "sessions"
        assert r.consistent is False
        assert r.details == "drift detected"
        assert r.redis_count == 10
        assert r.db_count == 5
        assert r.mismatches == 5
        assert r.timestamp == ts

    def test_to_dict_consistent(self):
        r = CheckResult(
            name="canvas_versions",
            consistent=True,
            redis_count=3,
            db_count=3,
            timestamp=1000.5,
        )
        d = r.to_dict()
        assert d["name"] == "canvas_versions"
        assert d["consistent"] == "1"
        assert d["redis_count"] == "3"
        assert d["db_count"] == "3"
        assert d["mismatches"] == "0"
        assert d["timestamp"] == "1000.5"

    def test_to_dict_inconsistent(self):
        r = CheckResult(name="x", consistent=False, mismatches=7)
        d = r.to_dict()
        assert d["consistent"] == "0"
        assert d["mismatches"] == "7"

    def test_to_dict_details(self):
        r = CheckResult(name="y", consistent=False, details="some error text")
        d = r.to_dict()
        assert d["details"] == "some error text"


# --- ConsistencyChecker Initialization Tests ---


class TestCheckerInit:
    def test_with_db_engine_has_all_checks(self, checker):
        names = checker.check_names
        assert "sessions" in names
        assert "canvas_versions" in names
        assert "feature_flags" in names

    def test_without_db_engine_only_feature_flags(self, checker_redis_only):
        names = checker_redis_only.check_names
        assert "feature_flags" in names
        assert "sessions" not in names
        assert "canvas_versions" not in names

    def test_custom_session_pattern(self, mock_redis, mock_db_engine):
        c = ConsistencyChecker(
            redis_client=mock_redis,
            db_engine=mock_db_engine,
            session_key_pattern="custom_session_*",
        )
        assert c._session_key_pattern == "custom_session_*"

    def test_custom_session_table(self, mock_redis, mock_db_engine):
        c = ConsistencyChecker(
            redis_client=mock_redis,
            db_engine=mock_db_engine,
            session_table="custom_sessions",
        )
        assert c._session_table == "custom_sessions"

    def test_check_order(self, checker):
        names = checker.check_names
        assert names.index("sessions") < names.index("feature_flags")
        assert names.index("canvas_versions") < names.index("feature_flags")


# --- Session Check Tests ---


class TestCheckSessions:
    def test_consistent_sessions(self, checker, mock_redis, mock_db_engine):
        mock_redis.scan.return_value = (0, [b"s1", b"s2", b"s3"])
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchone.return_value = (3,)
        conn.execute.return_value = result_mock
        mock_db_engine.connect.return_value.__enter__.return_value = conn

        result = checker._check_sessions()
        assert result.consistent is True
        assert result.redis_count == 3
        assert result.db_count == 3
        assert result.mismatches == 0

    def test_within_tolerance(self, checker, mock_redis, mock_db_engine):
        mock_redis.scan.return_value = (0, [b"s1", b"s2", b"s3", b"s4", b"s5", b"s6", b"s7"])
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchone.return_value = (5,)
        conn.execute.return_value = result_mock
        mock_db_engine.connect.return_value.__enter__.return_value = conn

        result = checker._check_sessions()
        assert result.consistent is True  # diff=2, tolerance=5

    def test_at_tolerance_boundary(self, checker, mock_redis, mock_db_engine):
        mock_redis.scan.return_value = (0, [f"s{i}".encode() for i in range(10)])
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchone.return_value = (5,)
        conn.execute.return_value = result_mock
        mock_db_engine.connect.return_value.__enter__.return_value = conn

        result = checker._check_sessions()
        assert result.consistent is True  # diff=5, tolerance=5

    def test_exceeds_tolerance(self, checker, mock_redis, mock_db_engine):
        mock_redis.scan.return_value = (0, [f"s{i}".encode() for i in range(15)])
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchone.return_value = (5,)
        conn.execute.return_value = result_mock
        mock_db_engine.connect.return_value.__enter__.return_value = conn

        result = checker._check_sessions()
        assert result.consistent is False
        assert result.redis_count == 15
        assert result.db_count == 5
        assert result.mismatches == 10
        assert "drift" in result.details.lower()

    def test_db_more_than_redis(self, checker, mock_redis, mock_db_engine):
        mock_redis.scan.return_value = (0, [b"s1"])
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchone.return_value = (20,)
        conn.execute.return_value = result_mock
        mock_db_engine.connect.return_value.__enter__.return_value = conn

        result = checker._check_sessions()
        assert result.consistent is False
        assert result.mismatches == 19

    def test_zero_sessions_both(self, checker, mock_redis, mock_db_engine):
        mock_redis.scan.return_value = (0, [])
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchone.return_value = (0,)
        conn.execute.return_value = result_mock
        mock_db_engine.connect.return_value.__enter__.return_value = conn

        result = checker._check_sessions()
        assert result.consistent is True
        assert result.redis_count == 0
        assert result.db_count == 0

    def test_paginated_scan(self, checker, mock_redis, mock_db_engine):
        mock_redis.scan.side_effect = [
            (42, [b"s1", b"s2"]),
            (0, [b"s3"]),
        ]
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchone.return_value = (3,)
        conn.execute.return_value = result_mock
        mock_db_engine.connect.return_value.__enter__.return_value = conn

        result = checker._check_sessions()
        assert result.consistent is True
        assert result.redis_count == 3

    def test_db_returns_none_row(self, checker, mock_redis, mock_db_engine):
        mock_redis.scan.return_value = (0, [b"s1"])
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchone.return_value = None
        conn.execute.return_value = result_mock
        mock_db_engine.connect.return_value.__enter__.return_value = conn

        result = checker._check_sessions()
        assert result.redis_count == 1
        assert result.db_count == 0


# --- Canvas Versions Check Tests ---


class TestCheckCanvasVersions:
    def test_no_canvases_in_redis(self, checker, mock_redis):
        mock_redis.scan.return_value = (0, [])
        result = checker._check_canvas_versions()
        assert result.consistent is True
        assert "No active canvases" in result.details

    def test_consistent_canvas_versions(self, checker, mock_redis, mock_db_engine):
        mock_redis.scan.side_effect = [
            (0, [b"canvas_autosave:proj1_uuid1", b"canvas_autosave:proj1_uuid2"]),
            (0, []),
        ]
        pipe = MagicMock()
        pipe.execute.return_value = [b"5", b"3"]
        mock_redis.pipeline.return_value = pipe

        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.__iter__ = MagicMock(return_value=iter([
            ("uuid1", 5),
            ("uuid2", 3),
        ]))
        conn.execute.return_value = result_mock
        mock_db_engine.connect.return_value.__enter__.return_value = conn

        result = checker._check_canvas_versions()
        assert result.consistent is True
        assert result.mismatches == 0

    def test_version_mismatch(self, checker, mock_redis, mock_db_engine):
        mock_redis.scan.side_effect = [
            (0, [b"canvas_autosave:proj1_uuid1"]),
            (0, []),
        ]
        pipe = MagicMock()
        pipe.execute.return_value = [b"10"]
        mock_redis.pipeline.return_value = pipe

        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.__iter__ = MagicMock(return_value=iter([
            ("uuid1", 7),
        ]))
        conn.execute.return_value = result_mock
        mock_db_engine.connect.return_value.__enter__.return_value = conn

        result = checker._check_canvas_versions()
        assert result.consistent is False
        assert result.mismatches == 1
        assert "mismatch" in result.details.lower()

    def test_canvas_not_in_db_is_not_mismatch(self, checker, mock_redis, mock_db_engine):
        mock_redis.scan.side_effect = [
            (0, [b"canvas_autosave:proj1_uuid1"]),
            (0, []),
        ]
        pipe = MagicMock()
        pipe.execute.return_value = [b"5"]
        mock_redis.pipeline.return_value = pipe

        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.__iter__ = MagicMock(return_value=iter([]))
        conn.execute.return_value = result_mock
        mock_db_engine.connect.return_value.__enter__.return_value = conn

        result = checker._check_canvas_versions()
        assert result.consistent is True

    def test_redis_version_zero_ignored(self, checker, mock_redis, mock_db_engine):
        mock_redis.scan.side_effect = [
            (0, [b"canvas_autosave:proj1_uuid1"]),
            (0, []),
        ]
        pipe = MagicMock()
        pipe.execute.return_value = [b"0"]
        mock_redis.pipeline.return_value = pipe

        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.__iter__ = MagicMock(return_value=iter([
            ("uuid1", 5),
        ]))
        conn.execute.return_value = result_mock
        mock_db_engine.connect.return_value.__enter__.return_value = conn

        result = checker._check_canvas_versions()
        assert result.consistent is True

    def test_multiple_mismatches_capped_details(self, checker, mock_redis, mock_db_engine):
        mock_redis.scan.side_effect = [
            (0, [
                b"canvas_autosave:p_u1",
                b"canvas_autosave:p_u2",
                b"canvas_autosave:p_u3",
                b"canvas_autosave:p_u4",
            ]),
            (0, []),
        ]
        pipe = MagicMock()
        pipe.execute.return_value = [b"10", b"10", b"10", b"10"]
        mock_redis.pipeline.return_value = pipe

        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.__iter__ = MagicMock(return_value=iter([
            ("u1", 1), ("u2", 2), ("u3", 3), ("u4", 4),
        ]))
        conn.execute.return_value = result_mock
        mock_db_engine.connect.return_value.__enter__.return_value = conn

        result = checker._check_canvas_versions()
        assert result.consistent is False
        assert result.mismatches == 4


# --- Feature Flags Check Tests ---


class TestCheckFeatureFlags:
    def test_no_flags_in_redis(self, checker_redis_only, mock_redis):
        mock_redis.scan.return_value = (0, [])
        result = checker_redis_only._check_feature_flags()
        assert result.consistent is True
        assert "No global feature flags" in result.details

    def test_valid_json_flags(self, checker_redis_only, mock_redis):
        mock_redis.scan.return_value = (
            0, [b"feature_flags:global:REDIS_STATE_ENABLED"]
        )
        mock_redis.get.return_value = json.dumps(
            {"enabled": True, "rollout_pct": 50}
        ).encode()

        result = checker_redis_only._check_feature_flags()
        assert result.consistent is True
        assert result.redis_count == 1
        assert result.mismatches == 0

    def test_valid_legacy_flags(self, checker_redis_only, mock_redis):
        mock_redis.scan.return_value = (
            0, [b"feature_flags:global:OLD_FLAG"]
        )
        mock_redis.get.return_value = b"1"

        result = checker_redis_only._check_feature_flags()
        assert result.consistent is True

    def test_valid_legacy_zero(self, checker_redis_only, mock_redis):
        mock_redis.scan.return_value = (
            0, [b"feature_flags:global:DISABLED"]
        )
        mock_redis.get.return_value = b"0"

        result = checker_redis_only._check_feature_flags()
        assert result.consistent is True

    def test_invalid_json_flag(self, checker_redis_only, mock_redis):
        mock_redis.scan.return_value = (
            0, [b"feature_flags:global:BAD_FLAG"]
        )
        mock_redis.get.return_value = b"not valid json"

        result = checker_redis_only._check_feature_flags()
        assert result.consistent is False
        assert result.mismatches == 1
        assert "BAD_FLAG" in result.details

    def test_invalid_schema_missing_enabled(self, checker_redis_only, mock_redis):
        mock_redis.scan.return_value = (
            0, [b"feature_flags:global:NO_ENABLED"]
        )
        mock_redis.get.return_value = json.dumps({"rollout_pct": 50}).encode()

        result = checker_redis_only._check_feature_flags()
        assert result.consistent is False
        assert result.mismatches == 1

    def test_invalid_rollout_pct_over_100(self, checker_redis_only, mock_redis):
        mock_redis.scan.return_value = (
            0, [b"feature_flags:global:OVER_PCT"]
        )
        mock_redis.get.return_value = json.dumps(
            {"enabled": True, "rollout_pct": 150}
        ).encode()

        result = checker_redis_only._check_feature_flags()
        assert result.consistent is False

    def test_invalid_rollout_pct_negative(self, checker_redis_only, mock_redis):
        mock_redis.scan.return_value = (
            0, [b"feature_flags:global:NEG_PCT"]
        )
        mock_redis.get.return_value = json.dumps(
            {"enabled": True, "rollout_pct": -5}
        ).encode()

        result = checker_redis_only._check_feature_flags()
        assert result.consistent is False

    def test_enabled_not_bool(self, checker_redis_only, mock_redis):
        mock_redis.scan.return_value = (
            0, [b"feature_flags:global:STR_ENABLED"]
        )
        mock_redis.get.return_value = json.dumps(
            {"enabled": "yes", "rollout_pct": 10}
        ).encode()

        result = checker_redis_only._check_feature_flags()
        assert result.consistent is False

    def test_multiple_flags_mix_valid_invalid(self, checker_redis_only, mock_redis):
        mock_redis.scan.return_value = (
            0, [
                b"feature_flags:global:GOOD",
                b"feature_flags:global:BAD",
                b"feature_flags:global:ALSO_GOOD",
            ]
        )

        def get_side_effect(key):
            key_str = key.decode() if isinstance(key, bytes) else str(key)
            if "GOOD" in key_str and "BAD" not in key_str:
                return json.dumps({"enabled": True, "rollout_pct": 100}).encode()
            if "ALSO_GOOD" in key_str:
                return b"1"
            return b"garbage_data"

        mock_redis.get.side_effect = get_side_effect

        result = checker_redis_only._check_feature_flags()
        assert result.consistent is False
        assert result.mismatches == 1
        assert result.redis_count == 3

    def test_flag_key_decoded_bytes(self, checker_redis_only, mock_redis):
        mock_redis.scan.return_value = (
            0, [b"feature_flags:global:MY_FEATURE"]
        )
        mock_redis.get.return_value = b"[]"

        result = checker_redis_only._check_feature_flags()
        assert result.consistent is False
        assert "MY_FEATURE" in result.details

    def test_none_value_skipped(self, checker_redis_only, mock_redis):
        mock_redis.scan.return_value = (
            0, [b"feature_flags:global:EXPIRED"]
        )
        mock_redis.get.return_value = None

        result = checker_redis_only._check_feature_flags()
        assert result.consistent is True

    def test_many_invalid_truncates_details(self, checker_redis_only, mock_redis):
        keys = [f"feature_flags:global:FLAG_{i}".encode() for i in range(10)]
        mock_redis.scan.return_value = (0, keys)
        mock_redis.get.return_value = b"invalid"

        result = checker_redis_only._check_feature_flags()
        assert result.mismatches == 10
        assert "+5 more" in result.details


# --- run_all_checks Tests ---


class TestRunAllChecks:
    def test_returns_all_results(self, checker, mock_redis, mock_db_engine):
        mock_redis.scan.return_value = (0, [])
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchone.return_value = (0,)
        conn.execute.return_value = result_mock
        mock_db_engine.connect.return_value.__enter__.return_value = conn

        pipe = MagicMock()
        pipe.execute.return_value = []
        mock_redis.pipeline.return_value = pipe

        results = checker.run_all_checks()
        assert len(results) == 3
        assert all(isinstance(r, CheckResult) for r in results)

    def test_sets_last_run(self, checker, mock_redis, mock_db_engine):
        mock_redis.scan.return_value = (0, [])
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchone.return_value = (0,)
        conn.execute.return_value = result_mock
        mock_db_engine.connect.return_value.__enter__.return_value = conn

        pipe = MagicMock()
        pipe.execute.return_value = []
        mock_redis.pipeline.return_value = pipe

        checker.run_all_checks()

        # Verify last_run is set with ex=RESULT_TTL
        set_calls = [c for c in mock_redis.set.call_args_list
                     if LAST_RUN_KEY in str(c)]
        assert len(set_calls) == 1

    def test_check_error_produces_result(self, checker, mock_redis, mock_db_engine):
        mock_redis.scan.side_effect = Exception("Redis connection refused")

        pipe = MagicMock()
        pipe.execute.return_value = []
        mock_redis.pipeline.return_value = pipe

        results = checker.run_all_checks()
        assert len(results) == 3
        assert results[0].consistent is False
        assert "error" in results[0].details.lower()

    def test_redis_only_checker_runs_single_check(self, checker_redis_only, mock_redis):
        mock_redis.scan.return_value = (0, [])
        pipe = MagicMock()
        pipe.execute.return_value = []
        mock_redis.pipeline.return_value = pipe

        results = checker_redis_only.run_all_checks()
        assert len(results) == 1
        assert results[0].name == "feature_flags"


# --- get_status Tests ---


class TestGetStatus:
    def test_no_data_status(self, checker, mock_redis):
        mock_redis.hgetall.return_value = {}
        mock_redis.get.return_value = None

        status = checker.get_status()
        assert status["status"] == "consistent"
        assert "sessions" in status["checks"]
        assert status["checks"]["sessions"] == {"status": "no_data"}
        assert status["last_run"] == ""

    def test_consistent_status(self, checker, mock_redis):
        mock_redis.hgetall.return_value = {
            b"name": b"sessions",
            b"consistent": b"1",
            b"mismatches": b"0",
        }

        def get_side_effect(key):
            key_str = key if isinstance(key, str) else key.decode() if isinstance(key, bytes) else str(key)
            if key_str == LAST_RUN_KEY:
                return b"1000.0"
            return b"0"

        mock_redis.get.side_effect = get_side_effect

        status = checker.get_status()
        assert status["status"] == "consistent"
        assert status["last_run"] == "1000.0"

    def test_inconsistent_status(self, checker, mock_redis):
        call_count = [0]

        def hgetall_side_effect(key):
            call_count[0] += 1
            if call_count[0] == 1:
                return {b"name": b"sessions", b"consistent": b"0", b"mismatches": b"3"}
            return {b"name": b"x", b"consistent": b"1", b"mismatches": b"0"}

        mock_redis.hgetall.side_effect = hgetall_side_effect
        mock_redis.get.return_value = None

        status = checker.get_status()
        assert status["status"] == "inconsistent"

    def test_metrics_in_status(self, checker, mock_redis):
        mock_redis.hgetall.return_value = {}

        call_count = [0]

        def get_side_effect(key):
            call_count[0] += 1
            key_str = key if isinstance(key, str) else str(key)
            if KEY_PREFIX_METRICS in key_str:
                return b"42"
            return None

        mock_redis.get.side_effect = get_side_effect

        status = checker.get_status()
        assert any(v == 42 for v in status["metrics"].values())


# --- get_metrics Tests ---


class TestGetMetrics:
    def test_no_results_returns_counters(self, checker, mock_redis):
        mock_redis.hgetall.return_value = {}
        mock_redis.get.return_value = None

        metrics = checker.get_metrics()
        counter_metrics = [m for m in metrics if m[0] == "consistency_check_total_inconsistencies"]
        assert len(counter_metrics) == 3

    def test_with_consistent_results(self, checker, mock_redis):
        mock_redis.hgetall.return_value = {
            b"consistent": b"1",
            b"mismatches": b"0",
        }
        mock_redis.get.return_value = b"5"

        metrics = checker.get_metrics()
        result_metrics = [m for m in metrics if m[0] == "consistency_check_result"]
        assert len(result_metrics) == 3
        assert all(m[2] == 1.0 for m in result_metrics)

    def test_with_inconsistent_results(self, checker, mock_redis):
        mock_redis.hgetall.return_value = {
            b"consistent": b"0",
            b"mismatches": b"7",
        }
        mock_redis.get.return_value = b"25"

        metrics = checker.get_metrics()
        result_metrics = [m for m in metrics if m[0] == "consistency_check_result"]
        mismatch_metrics = [m for m in metrics if m[0] == "consistency_check_mismatches"]
        total_metrics = [m for m in metrics if m[0] == "consistency_check_total_inconsistencies"]

        assert all(m[2] == 0.0 for m in result_metrics)
        assert all(m[2] == 7.0 for m in mismatch_metrics)
        assert all(m[2] == 25.0 for m in total_metrics)

    def test_labels_include_check_name(self, checker, mock_redis):
        mock_redis.hgetall.return_value = {b"consistent": b"1", b"mismatches": b"0"}
        mock_redis.get.return_value = b"0"

        metrics = checker.get_metrics()
        labels = [m[1] for m in metrics if "check" in m[1]]
        check_values = [l["check"] for l in labels]
        assert "sessions" in check_values
        assert "canvas_versions" in check_values
        assert "feature_flags" in check_values


# --- reset_metrics Tests ---


class TestResetMetrics:
    def test_reset_specific_check(self, checker, mock_redis):
        count = checker.reset_metrics("sessions")
        assert count == 1
        mock_redis.delete.assert_called_once_with(f"{KEY_PREFIX_METRICS}:sessions")

    def test_reset_all_checks(self, checker, mock_redis):
        count = checker.reset_metrics()
        assert count == 3
        assert mock_redis.delete.call_count == 3

    def test_reset_unknown_check(self, checker, mock_redis):
        count = checker.reset_metrics("nonexistent")
        assert count == 0
        mock_redis.delete.assert_not_called()

    def test_reset_empty_string_resets_all(self, checker, mock_redis):
        count = checker.reset_metrics("")
        assert count == 3


# --- _validate_flag_value Tests ---


class TestValidateFlagValue:
    @pytest.mark.parametrize("value,expected", [
        ("1", True),
        ("0", True),
        ('{"enabled": true, "rollout_pct": 50}', True),
        ('{"enabled": false, "rollout_pct": 0}', True),
        ('{"enabled": true, "rollout_pct": 100}', True),
        ('{"enabled": true}', True),
        ('{"enabled": false}', True),
        ('{"enabled": true, "rollout_pct": 101}', False),
        ('{"enabled": true, "rollout_pct": -1}', False),
        ('{"enabled": "yes"}', False),
        ('{"rollout_pct": 50}', False),
        ("not json", False),
        ("[]", False),
        ("null", False),
        ("", False),
        ('{"enabled": 1}', False),
        ('{"enabled": true, "rollout_pct": "50"}', False),
    ])
    def test_validate_flag_value(self, value, expected):
        assert ConsistencyChecker._validate_flag_value(value) is expected


# --- _record_result Tests ---


class TestRecordResult:
    def test_records_consistent_result(self, checker, mock_redis):
        pipe = MagicMock()
        pipe.execute.return_value = []
        mock_redis.pipeline.return_value = pipe

        result = CheckResult(name="test", consistent=True, mismatches=0)
        checker._record_result(result)

        pipe.hset.assert_called_once()
        pipe.expire.assert_called_once()
        pipe.incrby.assert_not_called()

    def test_records_inconsistent_result_increments(self, checker, mock_redis):
        pipe = MagicMock()
        pipe.execute.return_value = []
        mock_redis.pipeline.return_value = pipe

        result = CheckResult(name="test", consistent=False, mismatches=3)
        checker._record_result(result)

        pipe.hset.assert_called_once()
        pipe.incrby.assert_called_once_with(f"{KEY_PREFIX_METRICS}:test", 3)

    def test_negative_mismatches_not_incremented(self, checker, mock_redis):
        pipe = MagicMock()
        pipe.execute.return_value = []
        mock_redis.pipeline.return_value = pipe

        result = CheckResult(name="test", consistent=False, mismatches=-1)
        checker._record_result(result)

        pipe.incrby.assert_not_called()

    def test_zero_mismatches_inconsistent_not_incremented(self, checker, mock_redis):
        pipe = MagicMock()
        pipe.execute.return_value = []
        mock_redis.pipeline.return_value = pipe

        result = CheckResult(name="test", consistent=False, mismatches=0)
        checker._record_result(result)

        pipe.incrby.assert_not_called()


# --- _decode_hash Tests ---


class TestDecodeHash:
    def test_bytes_keys_and_values(self):
        raw = {b"key1": b"value1", b"key2": b"value2"}
        result = ConsistencyChecker._decode_hash(raw)
        assert result == {"key1": "value1", "key2": "value2"}

    def test_str_keys_and_values(self):
        raw = {"key1": "value1", "key2": "value2"}
        result = ConsistencyChecker._decode_hash(raw)
        assert result == {"key1": "value1", "key2": "value2"}

    def test_empty_hash(self):
        assert ConsistencyChecker._decode_hash({}) == {}

    def test_mixed_types(self):
        raw = {b"key1": "value1", "key2": b"value2"}
        result = ConsistencyChecker._decode_hash(raw)
        assert result == {"key1": "value1", "key2": "value2"}


# --- Edge Cases ---


class TestEdgeCases:
    def test_scan_multiple_pages(self, checker, mock_redis, mock_db_engine):
        mock_redis.scan.side_effect = [
            (100, [b"k1", b"k2"]),
            (200, [b"k3"]),
            (0, [b"k4"]),
        ]
        count = checker._count_redis_keys("pattern*")
        assert count == 4
        assert mock_redis.scan.call_count == 3

    def test_get_redis_canvas_versions_empty(self, checker, mock_redis):
        mock_redis.scan.return_value = (0, [])
        versions = checker._get_redis_canvas_versions()
        assert versions == {}

    def test_get_db_canvas_versions_empty_list(self, checker, mock_db_engine):
        versions = checker._get_db_canvas_versions([])
        assert versions == {}

    def test_get_db_canvas_versions_bad_format(self, checker, mock_db_engine):
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.__iter__ = MagicMock(return_value=iter([]))
        conn.execute.return_value = result_mock
        mock_db_engine.connect.return_value.__enter__.return_value = conn

        versions = checker._get_db_canvas_versions(["nounderscore"])
        assert versions == {}

    def test_check_interval_constant(self):
        assert CHECK_INTERVAL_S == 900

    def test_result_ttl_constant(self):
        assert RESULT_TTL == 1800
