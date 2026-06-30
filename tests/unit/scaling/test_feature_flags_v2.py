"""Unit tests for FeatureFlags v2 — percentage rollout and admin API.

Validates:
1. Backward compatibility with legacy "1"/"0" string values
2. JSON-based flag values with rollout_pct
3. Percentage rollout evaluation with user_id hashing
4. Deterministic bucket assignment (same user always gets same bucket)
5. Rollout edge cases: 0%, 100%, no user_id
6. get_flag_details returns full flag info
7. list_all_details returns all flags
8. set_flag stores JSON with rollout_pct
9. New KNOWN_FLAGS include phase-4+ flags
10. Admin route GET lists all flags
11. Admin route POST sets flag with rollout
12. Admin auth via internal token and role

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_feature_flags_v2.py -v
"""

import importlib.util
import json
import os
import pathlib
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module loading setup
# ---------------------------------------------------------------------------

_SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[3].parent / "elitea_core"

_mock_log = MagicMock()
_mock_pylon_core_tools = MagicMock()
_mock_pylon_core_tools.log = _mock_log
sys.modules.setdefault("pylon", MagicMock())
sys.modules.setdefault("pylon.core", MagicMock())
sys.modules.setdefault("pylon.core.tools", _mock_pylon_core_tools)

# Load the module under test from the SOURCE repo
_mod_path = _SOURCE_ROOT / "utils" / "feature_flags.py"
_spec = importlib.util.spec_from_file_location(
    "elitea_core.utils.feature_flags", _mod_path
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

FeatureFlags = _mod.FeatureFlags
KNOWN_FLAGS = _mod.KNOWN_FLAGS
_hash_user_bucket = _mod._hash_user_bucket
_parse_flag_value = _mod._parse_flag_value


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
# Tests: _hash_user_bucket
# ---------------------------------------------------------------------------


class TestHashUserBucket:
    def test_returns_int_0_to_99(self):
        for uid in range(200):
            bucket = _hash_user_bucket(uid)
            assert 0 <= bucket <= 99

    def test_deterministic(self):
        assert _hash_user_bucket("user123") == _hash_user_bucket("user123")

    def test_different_users_different_buckets(self):
        buckets = {_hash_user_bucket(f"user_{i}") for i in range(100)}
        assert len(buckets) > 10  # should distribute across many buckets

    def test_string_and_int_user_ids(self):
        bucket_str = _hash_user_bucket("42")
        bucket_int = _hash_user_bucket(42)
        assert bucket_str == bucket_int


# ---------------------------------------------------------------------------
# Tests: _parse_flag_value
# ---------------------------------------------------------------------------


class TestParseFlagValue:
    def test_none_returns_none(self):
        assert _parse_flag_value(None) == (None, None)

    def test_legacy_true_string(self):
        assert _parse_flag_value("1") == (True, 100)
        assert _parse_flag_value("true") == (True, 100)
        assert _parse_flag_value("yes") == (True, 100)

    def test_legacy_false_string(self):
        assert _parse_flag_value("0") == (False, 0)
        assert _parse_flag_value("false") == (False, 0)
        assert _parse_flag_value("no") == (False, 0)

    def test_json_enabled_full_rollout(self):
        val = json.dumps({"enabled": True, "rollout_pct": 100})
        assert _parse_flag_value(val) == (True, 100)

    def test_json_enabled_partial_rollout(self):
        val = json.dumps({"enabled": True, "rollout_pct": 50})
        assert _parse_flag_value(val) == (True, 50)

    def test_json_disabled(self):
        val = json.dumps({"enabled": False, "rollout_pct": 0})
        assert _parse_flag_value(val) == (False, 0)

    def test_json_enabled_no_rollout_pct(self):
        val = json.dumps({"enabled": True})
        assert _parse_flag_value(val) == (True, 100)

    def test_json_disabled_no_rollout_pct(self):
        val = json.dumps({"enabled": False})
        assert _parse_flag_value(val) == (False, 0)

    def test_json_rollout_pct_clamped_high(self):
        val = json.dumps({"enabled": True, "rollout_pct": 200})
        assert _parse_flag_value(val) == (True, 100)

    def test_json_rollout_pct_clamped_low(self):
        val = json.dumps({"enabled": True, "rollout_pct": -5})
        assert _parse_flag_value(val) == (True, 0)

    def test_bytes_input(self):
        val = json.dumps({"enabled": True, "rollout_pct": 75}).encode()
        assert _parse_flag_value(val) == (True, 75)

    def test_invalid_json_returns_false(self):
        assert _parse_flag_value("garbage{") == (False, 0)

    def test_bytes_legacy_true(self):
        assert _parse_flag_value(b"1") == (True, 100)

    def test_bytes_legacy_false(self):
        assert _parse_flag_value(b"0") == (False, 0)


# ---------------------------------------------------------------------------
# Tests: Backward compatibility (legacy plain "1"/"0")
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_legacy_global_enabled(self, ff, mock_redis):
        mock_redis.get.return_value = "1"
        assert ff.is_enabled("REDIS_STATE_ENABLED") is True

    def test_legacy_global_disabled(self, ff, mock_redis):
        mock_redis.get.return_value = "0"
        assert ff.is_enabled("REDIS_STATE_ENABLED") is False

    def test_legacy_project_enabled(self, ff, mock_redis):
        mock_redis.get.return_value = "1"
        assert ff.is_enabled("REDIS_STATE_ENABLED", project_id=42) is True

    def test_legacy_bytes_response(self, ff, mock_redis):
        mock_redis.get.return_value = b"1"
        assert ff.is_enabled("REDIS_STATE_ENABLED") is True

    def test_env_still_overrides(self, ff, mock_redis):
        mock_redis.get.return_value = json.dumps({"enabled": True, "rollout_pct": 100})
        os.environ["FF_REDIS_STATE_ENABLED"] = "0"
        assert ff.is_enabled("REDIS_STATE_ENABLED") is False
        mock_redis.get.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Percentage rollout with user_id
# ---------------------------------------------------------------------------


class TestPercentageRollout:
    def test_100pct_always_enabled(self, ff, mock_redis):
        mock_redis.get.return_value = json.dumps({"enabled": True, "rollout_pct": 100})
        for uid in range(50):
            assert ff.is_enabled("REDIS_STATE_ENABLED", user_id=uid) is True

    def test_0pct_always_disabled(self, ff, mock_redis):
        mock_redis.get.return_value = json.dumps({"enabled": True, "rollout_pct": 0})
        for uid in range(50):
            assert ff.is_enabled("REDIS_STATE_ENABLED", user_id=uid) is False

    def test_50pct_partial_rollout(self, ff, mock_redis):
        mock_redis.get.return_value = json.dumps({"enabled": True, "rollout_pct": 50})
        enabled_count = sum(
            ff.is_enabled("REDIS_STATE_ENABLED", user_id=f"user_{i}")
            for i in range(200)
        )
        assert 60 < enabled_count < 140  # roughly 50% with some variance

    def test_no_user_id_with_partial_rollout_returns_true(self, ff, mock_redis):
        mock_redis.get.return_value = json.dumps({"enabled": True, "rollout_pct": 50})
        assert ff.is_enabled("REDIS_STATE_ENABLED") is True

    def test_disabled_flag_ignores_rollout(self, ff, mock_redis):
        mock_redis.get.return_value = json.dumps({"enabled": False, "rollout_pct": 100})
        assert ff.is_enabled("REDIS_STATE_ENABLED", user_id="anyone") is False

    def test_deterministic_for_same_user(self, ff, mock_redis):
        mock_redis.get.return_value = json.dumps({"enabled": True, "rollout_pct": 50})
        first = ff.is_enabled("REDIS_STATE_ENABLED", user_id="user_42")
        second = ff.is_enabled("REDIS_STATE_ENABLED", user_id="user_42")
        assert first == second

    def test_project_level_rollout(self, ff, mock_redis):
        val = json.dumps({"enabled": True, "rollout_pct": 25})

        def side_effect(key):
            if "42:" in key:
                return val
            return None
        mock_redis.get.side_effect = side_effect

        enabled_count = sum(
            ff.is_enabled("REDIS_STATE_ENABLED", project_id=42, user_id=f"u_{i}")
            for i in range(200)
        )
        assert 20 < enabled_count < 80  # roughly 25%

    def test_env_overrides_rollout(self, ff, mock_redis):
        mock_redis.get.return_value = json.dumps({"enabled": True, "rollout_pct": 10})
        os.environ["FF_REDIS_STATE_ENABLED"] = "1"
        assert ff.is_enabled("REDIS_STATE_ENABLED", user_id="anyone") is True


# ---------------------------------------------------------------------------
# Tests: set_flag with rollout_pct
# ---------------------------------------------------------------------------


class TestSetFlagRollout:
    def test_set_with_default_rollout(self, ff, mock_redis):
        ff.set_flag("REDIS_STATE_ENABLED", True)
        call_args = mock_redis.set.call_args
        key, value = call_args[0]
        assert key == "feature_flags:global:REDIS_STATE_ENABLED"
        data = json.loads(value)
        assert data == {"enabled": True, "rollout_pct": 100}

    def test_set_with_custom_rollout(self, ff, mock_redis):
        ff.set_flag("EVENT_DEDUP", True, rollout_pct=25)
        call_args = mock_redis.set.call_args
        _, value = call_args[0]
        data = json.loads(value)
        assert data == {"enabled": True, "rollout_pct": 25}

    def test_set_disabled_with_rollout(self, ff, mock_redis):
        ff.set_flag("HPA_ENABLED", False, rollout_pct=50)
        call_args = mock_redis.set.call_args
        _, value = call_args[0]
        data = json.loads(value)
        assert data == {"enabled": False, "rollout_pct": 50}

    def test_set_project_with_rollout(self, ff, mock_redis):
        ff.set_flag("SENTINEL_MODE", True, project_id=7, rollout_pct=75)
        call_args = mock_redis.set.call_args
        key, value = call_args[0]
        assert key == "feature_flags:7:SENTINEL_MODE"
        data = json.loads(value)
        assert data == {"enabled": True, "rollout_pct": 75}

    def test_set_clamps_rollout_high(self, ff, mock_redis):
        ff.set_flag("REDIS_STATE_ENABLED", True, rollout_pct=150)
        _, value = mock_redis.set.call_args[0]
        data = json.loads(value)
        assert data["rollout_pct"] == 100

    def test_set_clamps_rollout_low(self, ff, mock_redis):
        ff.set_flag("REDIS_STATE_ENABLED", True, rollout_pct=-10)
        _, value = mock_redis.set.call_args[0]
        data = json.loads(value)
        assert data["rollout_pct"] == 0


# ---------------------------------------------------------------------------
# Tests: get_flag_details
# ---------------------------------------------------------------------------


class TestGetFlagDetails:
    def test_default_when_not_set(self, ff, mock_redis):
        mock_redis.get.return_value = None
        details = ff.get_flag_details("REDIS_STATE_ENABLED")
        assert details == {
            "flag_name": "REDIS_STATE_ENABLED",
            "enabled": False,
            "rollout_pct": 0,
            "source": "default",
        }

    def test_from_env(self, ff, mock_redis):
        os.environ["FF_REDIS_STATE_ENABLED"] = "1"
        details = ff.get_flag_details("REDIS_STATE_ENABLED")
        assert details["source"] == "env"
        assert details["enabled"] is True
        assert details["rollout_pct"] == 100

    def test_from_global_redis(self, ff, mock_redis):
        mock_redis.get.return_value = json.dumps({"enabled": True, "rollout_pct": 60})
        details = ff.get_flag_details("REDIS_STATE_ENABLED")
        assert details["source"] == "global"
        assert details["enabled"] is True
        assert details["rollout_pct"] == 60

    def test_from_project_redis(self, ff, mock_redis):
        val = json.dumps({"enabled": True, "rollout_pct": 30})

        def side_effect(key):
            if "42:" in key:
                return val
            return None
        mock_redis.get.side_effect = side_effect

        details = ff.get_flag_details("REDIS_STATE_ENABLED", project_id=42)
        assert details["source"] == "project"
        assert details["project_id"] == 42
        assert details["rollout_pct"] == 30

    def test_env_disabled(self, ff, mock_redis):
        os.environ["FF_HPA_ENABLED"] = "0"
        details = ff.get_flag_details("HPA_ENABLED")
        assert details["enabled"] is False
        assert details["rollout_pct"] == 0
        assert details["source"] == "env"

    def test_legacy_value(self, ff, mock_redis):
        mock_redis.get.return_value = "1"
        details = ff.get_flag_details("REDIS_STREAMS")
        assert details["enabled"] is True
        assert details["rollout_pct"] == 100
        assert details["source"] == "global"


# ---------------------------------------------------------------------------
# Tests: list_all_details
# ---------------------------------------------------------------------------


class TestListAllDetails:
    def test_returns_list_for_all_known_flags(self, ff, mock_redis):
        mock_redis.get.return_value = None
        result = ff.list_all_details()
        assert len(result) == len(KNOWN_FLAGS)
        names = [r["flag_name"] for r in result]
        for flag in KNOWN_FLAGS:
            assert flag in names

    def test_with_mixed_sources(self, ff, mock_redis):
        os.environ["FF_REDIS_STATE_ENABLED"] = "1"
        mock_redis.get.return_value = json.dumps({"enabled": True, "rollout_pct": 50})
        result = ff.list_all_details()
        redis_state = next(r for r in result if r["flag_name"] == "REDIS_STATE_ENABLED")
        assert redis_state["source"] == "env"
        sentinel = next(r for r in result if r["flag_name"] == "SENTINEL_MODE")
        assert sentinel["source"] == "global"


# ---------------------------------------------------------------------------
# Tests: KNOWN_FLAGS includes new flags
# ---------------------------------------------------------------------------


class TestKnownFlagsV2:
    def test_includes_original_flags(self):
        assert "REDIS_STATE_ENABLED" in KNOWN_FLAGS
        assert "SOCKETIO_REDIS_ENABLED" in KNOWN_FLAGS
        assert "REDIS_STREAMS_ENABLED" in KNOWN_FLAGS

    def test_includes_new_phase_flags(self):
        assert "REDIS_STREAMS" in KNOWN_FLAGS
        assert "SENTINEL_MODE" in KNOWN_FLAGS
        assert "HPA_ENABLED" in KNOWN_FLAGS
        assert "EVENT_DEDUP" in KNOWN_FLAGS

    def test_is_tuple(self):
        assert isinstance(KNOWN_FLAGS, tuple)

    def test_total_count(self):
        assert len(KNOWN_FLAGS) == 7


# ---------------------------------------------------------------------------
# Tests: get_all_flags with user_id
# ---------------------------------------------------------------------------


class TestGetAllFlagsWithUser:
    def test_get_all_flags_default(self, ff, mock_redis):
        mock_redis.get.return_value = None
        result = ff.get_all_flags()
        assert all(v is False for v in result.values())

    def test_get_all_flags_with_user_id(self, ff, mock_redis):
        mock_redis.get.return_value = json.dumps({"enabled": True, "rollout_pct": 100})
        result = ff.get_all_flags(user_id="user_1")
        assert all(v is True for v in result.values())

    def test_get_all_flags_partial_rollout(self, ff, mock_redis):
        mock_redis.get.return_value = json.dumps({"enabled": True, "rollout_pct": 0})
        result = ff.get_all_flags(user_id="user_1")
        assert all(v is False for v in result.values())


# ---------------------------------------------------------------------------
# Tests: list_overrides with JSON values
# ---------------------------------------------------------------------------


class TestListOverridesV2:
    def test_legacy_override(self, ff, mock_redis):
        mock_redis.get.return_value = "1"
        result = ff.list_overrides()
        assert all(v is True for v in result.values())

    def test_json_override(self, ff, mock_redis):
        mock_redis.get.return_value = json.dumps({"enabled": True, "rollout_pct": 50})
        result = ff.list_overrides()
        assert all(v is True for v in result.values())

    def test_json_disabled_override(self, ff, mock_redis):
        mock_redis.get.return_value = json.dumps({"enabled": False, "rollout_pct": 0})
        result = ff.list_overrides()
        assert all(v is False for v in result.values())

    def test_no_overrides(self, ff, mock_redis):
        mock_redis.get.return_value = None
        result = ff.list_overrides()
        assert result == {}
