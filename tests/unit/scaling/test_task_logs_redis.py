"""Unit tests for TaskLogsRedis.

Validates that:
1. append stores log record in Redis sorted set with correct key and TTL
2. append_batch stores multiple records atomically
3. get_latest returns the N most recent entries in chronological order
4. get_all returns all entries in chronological order
5. get_since returns entries after a given timestamp
6. clear removes all entries for a task
7. count returns the number of entries
8. exists checks presence correctly
9. set_ttl resets the TTL on a task's log entries
10. max_entries trimming works (zremrangebyrank)
11. Malformed JSON entries are skipped with a warning

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_task_logs_redis.py -v
"""

import importlib
import importlib.util
import json
import pathlib
import sys
import types
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Module loading setup: mock pylon.core.tools so the module can be loaded
# without the full pylon framework installed.
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

# Load the task_logs_redis module
_module_path = _PLUGIN_ROOT / "utils" / "task_logs_redis.py"
_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.utils.task_logs_redis",
    _module_path,
    submodule_search_locations=[],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

TaskLogsRedis = _mod.TaskLogsRedis
DEFAULT_TTL = _mod.DEFAULT_TTL
DEFAULT_MAX_ENTRIES = _mod.DEFAULT_MAX_ENTRIES


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis():
    """Create a mock Redis client with pipeline support."""
    client = MagicMock()
    pipe = MagicMock()
    pipe.execute = MagicMock(return_value=[True, True, True])
    client.pipeline.return_value = pipe
    return client


@pytest.fixture
def store(mock_redis):
    """Create a TaskLogsRedis instance with mock Redis."""
    return TaskLogsRedis(mock_redis)


@pytest.fixture
def store_custom(mock_redis):
    """Create a TaskLogsRedis instance with custom TTL and max entries."""
    return TaskLogsRedis(mock_redis, ttl=3600, max_entries=50)


# ---------------------------------------------------------------------------
# Tests: __init__ and _key
# ---------------------------------------------------------------------------

class TestInit:
    def test_default_ttl(self, store):
        assert store._ttl == DEFAULT_TTL

    def test_default_max_entries(self, store):
        assert store._max_entries == DEFAULT_MAX_ENTRIES

    def test_custom_ttl(self, store_custom):
        assert store_custom._ttl == 3600

    def test_custom_max_entries(self, store_custom):
        assert store_custom._max_entries == 50

    def test_key_format(self, store):
        assert store._key("abc123") == "task_logs:abc123"

    def test_key_format_with_special_chars(self, store):
        assert store._key("task-id_v2.1") == "task_logs:task-id_v2.1"


# ---------------------------------------------------------------------------
# Tests: append
# ---------------------------------------------------------------------------

class TestAppend:
    def test_append_stores_record_as_json(self, store, mock_redis):
        record = {"level": "info", "message": "hello", "time": 1000.0}
        store.append("task1", record)

        pipe = mock_redis.pipeline.return_value
        pipe.zadd.assert_called_once_with(
            "task_logs:task1",
            {json.dumps(record, default=str): 1000.0}
        )

    def test_append_uses_record_time_as_score(self, store, mock_redis):
        record = {"level": "info", "message": "test", "time": 1234567890.123}
        store.append("task1", record)

        pipe = mock_redis.pipeline.return_value
        args = pipe.zadd.call_args
        mapping = args[0][1]
        assert list(mapping.values())[0] == 1234567890.123

    def test_append_generates_time_when_missing(self, store, mock_redis):
        record = {"level": "info", "message": "no time field"}
        with patch("time.time", return_value=9999.0):
            # time.time() is called inline in the module, need to patch at module level
            import time as _time
            original_time = _time.time
            _time.time = lambda: 9999.0
            try:
                store.append("task1", record)
            finally:
                _time.time = original_time

        pipe = mock_redis.pipeline.return_value
        args = pipe.zadd.call_args
        mapping = args[0][1]
        assert list(mapping.values())[0] == 9999.0

    def test_append_trims_to_max_entries(self, store, mock_redis):
        record = {"level": "info", "message": "test", "time": 100.0}
        store.append("task1", record)

        pipe = mock_redis.pipeline.return_value
        pipe.zremrangebyrank.assert_called_once_with(
            "task_logs:task1", 0, -(DEFAULT_MAX_ENTRIES + 1)
        )

    def test_append_sets_ttl(self, store, mock_redis):
        record = {"level": "info", "message": "test", "time": 100.0}
        store.append("task1", record)

        pipe = mock_redis.pipeline.return_value
        pipe.expire.assert_called_once_with("task_logs:task1", DEFAULT_TTL)

    def test_append_custom_ttl(self, store_custom, mock_redis):
        record = {"level": "info", "message": "test", "time": 100.0}
        store_custom.append("task1", record)

        pipe = mock_redis.pipeline.return_value
        pipe.expire.assert_called_once_with("task_logs:task1", 3600)

    def test_append_executes_pipeline(self, store, mock_redis):
        record = {"level": "info", "message": "test", "time": 100.0}
        store.append("task1", record)

        pipe = mock_redis.pipeline.return_value
        pipe.execute.assert_called_once()

    def test_append_serializes_non_json_types(self, store, mock_redis):
        from datetime import datetime
        record = {"level": "info", "timestamp": datetime(2026, 1, 1), "time": 100.0}
        store.append("task1", record)

        pipe = mock_redis.pipeline.return_value
        args = pipe.zadd.call_args
        member = list(args[0][1].keys())[0]
        parsed = json.loads(member)
        assert "2026" in parsed["timestamp"]


# ---------------------------------------------------------------------------
# Tests: append_batch
# ---------------------------------------------------------------------------

class TestAppendBatch:
    def test_append_batch_empty_list(self, store, mock_redis):
        store.append_batch("task1", [])
        mock_redis.pipeline.assert_not_called()

    def test_append_batch_stores_multiple_records(self, store, mock_redis):
        records = [
            {"level": "info", "message": "first", "time": 100.0},
            {"level": "warn", "message": "second", "time": 200.0},
        ]
        store.append_batch("task1", records)

        pipe = mock_redis.pipeline.return_value
        args = pipe.zadd.call_args
        mapping = args[0][1]
        assert len(mapping) == 2

    def test_append_batch_uses_individual_timestamps(self, store, mock_redis):
        records = [
            {"level": "info", "message": "first", "time": 100.0},
            {"level": "info", "message": "second", "time": 200.0},
        ]
        store.append_batch("task1", records)

        pipe = mock_redis.pipeline.return_value
        args = pipe.zadd.call_args
        mapping = args[0][1]
        scores = list(mapping.values())
        assert 100.0 in scores
        assert 200.0 in scores

    def test_append_batch_trims_and_sets_ttl(self, store, mock_redis):
        records = [{"level": "info", "message": "test", "time": 100.0}]
        store.append_batch("task1", records)

        pipe = mock_redis.pipeline.return_value
        pipe.zremrangebyrank.assert_called_once_with(
            "task_logs:task1", 0, -(DEFAULT_MAX_ENTRIES + 1)
        )
        pipe.expire.assert_called_once_with("task_logs:task1", DEFAULT_TTL)

    def test_append_batch_executes_pipeline(self, store, mock_redis):
        records = [{"level": "info", "message": "test", "time": 100.0}]
        store.append_batch("task1", records)

        pipe = mock_redis.pipeline.return_value
        pipe.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: get_latest
# ---------------------------------------------------------------------------

class TestGetLatest:
    def test_get_latest_returns_parsed_records(self, store, mock_redis):
        record1 = {"level": "info", "message": "first", "time": 100.0}
        record2 = {"level": "info", "message": "second", "time": 200.0}
        mock_redis.zrange.return_value = [
            json.dumps(record1),
            json.dumps(record2),
        ]

        result = store.get_latest("task1")
        assert result == [record1, record2]

    def test_get_latest_default_count(self, store, mock_redis):
        mock_redis.zrange.return_value = []
        store.get_latest("task1")
        mock_redis.zrange.assert_called_once_with("task_logs:task1", -100, -1)

    def test_get_latest_custom_count(self, store, mock_redis):
        mock_redis.zrange.return_value = []
        store.get_latest("task1", count=50)
        mock_redis.zrange.assert_called_once_with("task_logs:task1", -50, -1)

    def test_get_latest_empty_set(self, store, mock_redis):
        mock_redis.zrange.return_value = []
        result = store.get_latest("task1")
        assert result == []

    def test_get_latest_handles_bytes(self, store, mock_redis):
        record = {"level": "info", "message": "test", "time": 100.0}
        mock_redis.zrange.return_value = [json.dumps(record).encode()]

        result = store.get_latest("task1")
        assert result == [record]

    def test_get_latest_skips_malformed_json(self, store, mock_redis):
        mock_redis.zrange.return_value = [
            json.dumps({"level": "info", "message": "good", "time": 100.0}),
            "not valid json {{{",
            json.dumps({"level": "warn", "message": "also good", "time": 200.0}),
        ]

        result = store.get_latest("task1")
        assert len(result) == 2
        assert result[0]["message"] == "good"
        assert result[1]["message"] == "also good"


# ---------------------------------------------------------------------------
# Tests: get_all
# ---------------------------------------------------------------------------

class TestGetAll:
    def test_get_all_returns_all_records(self, store, mock_redis):
        records = [
            {"level": "info", "message": f"msg{i}", "time": float(i)}
            for i in range(5)
        ]
        mock_redis.zrange.return_value = [json.dumps(r) for r in records]

        result = store.get_all("task1")
        assert result == records

    def test_get_all_uses_full_range(self, store, mock_redis):
        mock_redis.zrange.return_value = []
        store.get_all("task1")
        mock_redis.zrange.assert_called_once_with("task_logs:task1", 0, -1)

    def test_get_all_empty_set(self, store, mock_redis):
        mock_redis.zrange.return_value = []
        result = store.get_all("task1")
        assert result == []

    def test_get_all_handles_bytes(self, store, mock_redis):
        record = {"level": "info", "message": "test", "time": 100.0}
        mock_redis.zrange.return_value = [json.dumps(record).encode()]
        result = store.get_all("task1")
        assert result == [record]

    def test_get_all_skips_malformed_json(self, store, mock_redis):
        mock_redis.zrange.return_value = ["not json", json.dumps({"ok": True})]
        result = store.get_all("task1")
        assert len(result) == 1
        assert result[0] == {"ok": True}


# ---------------------------------------------------------------------------
# Tests: get_since
# ---------------------------------------------------------------------------

class TestGetSince:
    def test_get_since_returns_entries_after_timestamp(self, store, mock_redis):
        record = {"level": "info", "message": "new", "time": 500.0}
        mock_redis.zrangebyscore.return_value = [json.dumps(record)]

        result = store.get_since("task1", 400.0)
        assert result == [record]

    def test_get_since_uses_exclusive_lower_bound(self, store, mock_redis):
        mock_redis.zrangebyscore.return_value = []
        store.get_since("task1", 123.456)
        mock_redis.zrangebyscore.assert_called_once_with(
            "task_logs:task1", "(123.456", "+inf"
        )

    def test_get_since_empty_result(self, store, mock_redis):
        mock_redis.zrangebyscore.return_value = []
        result = store.get_since("task1", 999.0)
        assert result == []

    def test_get_since_handles_bytes(self, store, mock_redis):
        record = {"level": "info", "message": "test", "time": 100.0}
        mock_redis.zrangebyscore.return_value = [json.dumps(record).encode()]
        result = store.get_since("task1", 50.0)
        assert result == [record]

    def test_get_since_skips_malformed_json(self, store, mock_redis):
        mock_redis.zrangebyscore.return_value = [
            "broken",
            json.dumps({"valid": True}),
        ]
        result = store.get_since("task1", 0.0)
        assert len(result) == 1
        assert result[0] == {"valid": True}


# ---------------------------------------------------------------------------
# Tests: clear
# ---------------------------------------------------------------------------

class TestClear:
    def test_clear_deletes_key(self, store, mock_redis):
        mock_redis.delete.return_value = 1
        result = store.clear("task1")
        mock_redis.delete.assert_called_once_with("task_logs:task1")
        assert result is True

    def test_clear_returns_false_when_key_missing(self, store, mock_redis):
        mock_redis.delete.return_value = 0
        result = store.clear("nonexistent")
        assert result is False


# ---------------------------------------------------------------------------
# Tests: count
# ---------------------------------------------------------------------------

class TestCount:
    def test_count_returns_cardinality(self, store, mock_redis):
        mock_redis.zcard.return_value = 42
        result = store.count("task1")
        mock_redis.zcard.assert_called_once_with("task_logs:task1")
        assert result == 42

    def test_count_returns_zero_for_missing_key(self, store, mock_redis):
        mock_redis.zcard.return_value = 0
        result = store.count("missing")
        assert result == 0


# ---------------------------------------------------------------------------
# Tests: exists
# ---------------------------------------------------------------------------

class TestExists:
    def test_exists_true(self, store, mock_redis):
        mock_redis.exists.return_value = 1
        assert store.exists("task1") is True
        mock_redis.exists.assert_called_once_with("task_logs:task1")

    def test_exists_false(self, store, mock_redis):
        mock_redis.exists.return_value = 0
        assert store.exists("task1") is False


# ---------------------------------------------------------------------------
# Tests: set_ttl
# ---------------------------------------------------------------------------

class TestSetTtl:
    def test_set_ttl_default(self, store, mock_redis):
        mock_redis.expire.return_value = True
        result = store.set_ttl("task1")
        mock_redis.expire.assert_called_once_with("task_logs:task1", DEFAULT_TTL)
        assert result is True

    def test_set_ttl_custom(self, store, mock_redis):
        mock_redis.expire.return_value = True
        result = store.set_ttl("task1", ttl=7200)
        mock_redis.expire.assert_called_once_with("task_logs:task1", 7200)
        assert result is True

    def test_set_ttl_returns_false_when_key_missing(self, store, mock_redis):
        mock_redis.expire.return_value = False
        result = store.set_ttl("nonexistent")
        assert result is False


# ---------------------------------------------------------------------------
# Tests: edge cases and integration patterns
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_different_task_ids_use_different_keys(self, store, mock_redis):
        mock_redis.zcard.return_value = 0
        store.count("task_a")
        store.count("task_b")
        calls = mock_redis.zcard.call_args_list
        assert calls[0] == call("task_logs:task_a")
        assert calls[1] == call("task_logs:task_b")

    def test_append_with_nested_dict(self, store, mock_redis):
        record = {
            "level": "info",
            "message": "complex",
            "metadata": {"key": "value", "nested": [1, 2, 3]},
            "time": 100.0,
        }
        store.append("task1", record)

        pipe = mock_redis.pipeline.return_value
        args = pipe.zadd.call_args
        member = list(args[0][1].keys())[0]
        parsed = json.loads(member)
        assert parsed["metadata"]["nested"] == [1, 2, 3]

    def test_custom_max_entries_trimming(self, store_custom, mock_redis):
        record = {"level": "info", "message": "test", "time": 100.0}
        store_custom.append("task1", record)

        pipe = mock_redis.pipeline.return_value
        pipe.zremrangebyrank.assert_called_once_with(
            "task_logs:task1", 0, -51  # -(50 + 1)
        )

    def test_constants_have_expected_values(self):
        assert DEFAULT_TTL == 604800  # 7 days
        assert DEFAULT_MAX_ENTRIES == 500
