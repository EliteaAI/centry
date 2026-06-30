"""Unit tests for EventDeduplicator and @deduplicate decorator.

Validates that:
1. is_duplicate returns False on first call (event not seen)
2. is_duplicate returns True on subsequent calls (event already seen)
3. TTL is applied to dedup entries
4. mark_processed/is_processed work independently
5. clear removes entries
6. bulk_check and bulk_mark operate atomically via pipeline
7. generate_event_id produces deterministic hashes
8. @deduplicate decorator skips duplicate events
9. @deduplicate with custom event_id_func extracts ID correctly
10. Empty/None event IDs are handled gracefully

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_event_dedup.py -v
"""

import importlib
import importlib.util
import json
import pathlib
import sys
import types
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Module loading setup: mock pylon.core.tools so the module can be loaded
# without the full pylon framework installed.
# ---------------------------------------------------------------------------

_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[3] / "pylon_main" / "plugins" / "elitea_core"

# Mock pylon.core.tools (for log import)
_mock_log = MagicMock()
_mock_pylon_core_tools = MagicMock()
_mock_pylon_core_tools.log = _mock_log
sys.modules.setdefault("pylon", MagicMock())
sys.modules.setdefault("pylon.core", MagicMock())
sys.modules.setdefault("pylon.core.tools", _mock_pylon_core_tools)

# Create package hierarchy
_utils_pkg = types.ModuleType("centry.pylon_main.plugins.elitea_core.utils")
_utils_pkg.__path__ = [str(_PLUGIN_ROOT / "utils")]
_utils_pkg.__package__ = "centry.pylon_main.plugins.elitea_core.utils"
sys.modules.setdefault("centry.pylon_main.plugins.elitea_core.utils", _utils_pkg)

_plugin_pkg = types.ModuleType("centry.pylon_main.plugins.elitea_core")
_plugin_pkg.__path__ = [str(_PLUGIN_ROOT)]
_plugin_pkg.__package__ = "centry.pylon_main.plugins.elitea_core"
sys.modules.setdefault("centry.pylon_main.plugins.elitea_core", _plugin_pkg)

# Load the event_dedup module
_module_path = _PLUGIN_ROOT / "utils" / "event_dedup.py"
_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.utils.event_dedup",
    _module_path,
    submodule_search_locations=[],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

EventDeduplicator = _mod.EventDeduplicator
generate_event_id = _mod.generate_event_id
deduplicate = _mod.deduplicate
DEFAULT_TTL = _mod.DEFAULT_TTL
KEY_PREFIX = _mod.KEY_PREFIX


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_redis():
    """Create a mock Redis client with pipeline support."""
    client = MagicMock()
    # Pipeline mock that collects calls
    pipeline = MagicMock()
    pipeline.__enter__ = MagicMock(return_value=pipeline)
    pipeline.__exit__ = MagicMock(return_value=False)
    client.pipeline.return_value = pipeline
    return client


@pytest.fixture
def dedup(mock_redis):
    """Create an EventDeduplicator instance with mock Redis."""
    return EventDeduplicator(mock_redis)


# ---------------------------------------------------------------------------
# EventDeduplicator Tests
# ---------------------------------------------------------------------------


class TestEventDeduplicatorInit:
    """Test initialization and configuration."""

    def test_default_prefix(self, mock_redis):
        d = EventDeduplicator(mock_redis)
        assert d._prefix == KEY_PREFIX

    def test_custom_prefix(self, mock_redis):
        d = EventDeduplicator(mock_redis, key_prefix="custom_dedup")
        assert d._prefix == "custom_dedup"

    def test_default_ttl(self, mock_redis):
        d = EventDeduplicator(mock_redis)
        assert d._default_ttl == DEFAULT_TTL

    def test_custom_ttl(self, mock_redis):
        d = EventDeduplicator(mock_redis, default_ttl=600)
        assert d._default_ttl == 600

    def test_key_generation(self, dedup):
        assert dedup._key("evt-123") == f"{KEY_PREFIX}:evt-123"

    def test_custom_prefix_key_generation(self, mock_redis):
        d = EventDeduplicator(mock_redis, key_prefix="my_prefix")
        assert d._key("abc") == "my_prefix:abc"


class TestIsDuplicate:
    """Test is_duplicate method (core SETNX logic)."""

    def test_first_event_not_duplicate(self, dedup, mock_redis):
        mock_redis.set.return_value = True  # SET NX succeeded (key was new)
        result = dedup.is_duplicate("evt-001")
        assert result is False
        mock_redis.set.assert_called_once_with(
            f"{KEY_PREFIX}:evt-001", "1", nx=True, ex=DEFAULT_TTL
        )

    def test_duplicate_event_detected(self, dedup, mock_redis):
        mock_redis.set.return_value = None  # SET NX failed (key exists)
        result = dedup.is_duplicate("evt-001")
        assert result is True

    def test_custom_ttl_passed_to_redis(self, dedup, mock_redis):
        mock_redis.set.return_value = True
        dedup.is_duplicate("evt-002", ttl_seconds=60)
        mock_redis.set.assert_called_once_with(
            f"{KEY_PREFIX}:evt-002", "1", nx=True, ex=60
        )

    def test_empty_event_id_returns_false(self, dedup, mock_redis):
        result = dedup.is_duplicate("")
        assert result is False
        mock_redis.set.assert_not_called()

    def test_none_event_id_returns_false(self, dedup, mock_redis):
        result = dedup.is_duplicate(None)
        assert result is False
        mock_redis.set.assert_not_called()

    def test_different_events_independent(self, dedup, mock_redis):
        mock_redis.set.return_value = True  # Both are new
        assert dedup.is_duplicate("evt-a") is False
        assert dedup.is_duplicate("evt-b") is False
        assert mock_redis.set.call_count == 2


class TestMarkProcessed:
    """Test mark_processed method."""

    def test_mark_new_event_returns_true(self, dedup, mock_redis):
        mock_redis.set.return_value = True
        result = dedup.mark_processed("evt-100")
        assert result is True

    def test_mark_existing_event_returns_false(self, dedup, mock_redis):
        mock_redis.set.return_value = None
        result = dedup.mark_processed("evt-100")
        assert result is False

    def test_mark_with_custom_ttl(self, dedup, mock_redis):
        mock_redis.set.return_value = True
        dedup.mark_processed("evt-200", ttl_seconds=120)
        mock_redis.set.assert_called_once_with(
            f"{KEY_PREFIX}:evt-200", "1", nx=True, ex=120
        )

    def test_mark_empty_id_returns_false(self, dedup, mock_redis):
        result = dedup.mark_processed("")
        assert result is False
        mock_redis.set.assert_not_called()

    def test_mark_none_id_returns_false(self, dedup, mock_redis):
        result = dedup.mark_processed(None)
        assert result is False


class TestIsProcessed:
    """Test is_processed method (read-only check)."""

    def test_processed_event_returns_true(self, dedup, mock_redis):
        mock_redis.exists.return_value = 1
        assert dedup.is_processed("evt-300") is True
        mock_redis.exists.assert_called_once_with(f"{KEY_PREFIX}:evt-300")

    def test_unprocessed_event_returns_false(self, dedup, mock_redis):
        mock_redis.exists.return_value = 0
        assert dedup.is_processed("evt-301") is False

    def test_empty_id_returns_false(self, dedup, mock_redis):
        assert dedup.is_processed("") is False
        mock_redis.exists.assert_not_called()

    def test_none_id_returns_false(self, dedup, mock_redis):
        assert dedup.is_processed(None) is False


class TestClear:
    """Test clear method."""

    def test_clear_existing_returns_true(self, dedup, mock_redis):
        mock_redis.delete.return_value = 1
        result = dedup.clear("evt-400")
        assert result is True
        mock_redis.delete.assert_called_once_with(f"{KEY_PREFIX}:evt-400")

    def test_clear_nonexistent_returns_false(self, dedup, mock_redis):
        mock_redis.delete.return_value = 0
        result = dedup.clear("evt-401")
        assert result is False


class TestGetTTL:
    """Test get_ttl method."""

    def test_existing_key_with_ttl(self, dedup, mock_redis):
        mock_redis.ttl.return_value = 250
        result = dedup.get_ttl("evt-500")
        assert result == 250
        mock_redis.ttl.assert_called_once_with(f"{KEY_PREFIX}:evt-500")

    def test_nonexistent_key(self, dedup, mock_redis):
        mock_redis.ttl.return_value = -2
        result = dedup.get_ttl("evt-501")
        assert result == -2

    def test_key_without_expiry(self, dedup, mock_redis):
        mock_redis.ttl.return_value = -1
        result = dedup.get_ttl("evt-502")
        assert result == -1


class TestBulkCheck:
    """Test bulk_check method (pipeline-based)."""

    def test_empty_list_returns_empty_dict(self, dedup, mock_redis):
        result = dedup.bulk_check([])
        assert result == {}
        mock_redis.pipeline.assert_not_called()

    def test_all_new_events(self, dedup, mock_redis):
        pipeline = mock_redis.pipeline.return_value
        pipeline.execute.return_value = [0, 0, 0]

        result = dedup.bulk_check(["a", "b", "c"])
        assert result == {"a": False, "b": False, "c": False}

    def test_all_duplicate_events(self, dedup, mock_redis):
        pipeline = mock_redis.pipeline.return_value
        pipeline.execute.return_value = [1, 1]

        result = dedup.bulk_check(["x", "y"])
        assert result == {"x": True, "y": True}

    def test_mixed_results(self, dedup, mock_redis):
        pipeline = mock_redis.pipeline.return_value
        pipeline.execute.return_value = [1, 0, 1]

        result = dedup.bulk_check(["evt-1", "evt-2", "evt-3"])
        assert result == {"evt-1": True, "evt-2": False, "evt-3": True}

    def test_skips_empty_event_ids(self, dedup, mock_redis):
        pipeline = mock_redis.pipeline.return_value
        pipeline.execute.return_value = [0]

        result = dedup.bulk_check(["", "valid", ""])
        assert result == {"valid": False}

    def test_uses_non_transactional_pipeline(self, dedup, mock_redis):
        pipeline = mock_redis.pipeline.return_value
        pipeline.execute.return_value = [0]

        dedup.bulk_check(["a"])
        mock_redis.pipeline.assert_called_once_with(transaction=False)


class TestBulkMark:
    """Test bulk_mark method."""

    def test_empty_list_returns_empty_dict(self, dedup, mock_redis):
        result = dedup.bulk_mark([])
        assert result == {}

    def test_all_newly_marked(self, dedup, mock_redis):
        pipeline = mock_redis.pipeline.return_value
        pipeline.execute.return_value = [True, True, True]

        result = dedup.bulk_mark(["a", "b", "c"])
        assert result == {"a": True, "b": True, "c": True}

    def test_some_already_marked(self, dedup, mock_redis):
        pipeline = mock_redis.pipeline.return_value
        pipeline.execute.return_value = [True, None, True]

        result = dedup.bulk_mark(["a", "b", "c"])
        assert result == {"a": True, "b": False, "c": True}

    def test_custom_ttl_in_bulk(self, dedup, mock_redis):
        pipeline = mock_redis.pipeline.return_value
        pipeline.execute.return_value = [True]

        dedup.bulk_mark(["evt-1"], ttl_seconds=60)
        pipeline.set.assert_called_once_with(
            f"{KEY_PREFIX}:evt-1", "1", nx=True, ex=60
        )

    def test_skips_empty_ids(self, dedup, mock_redis):
        pipeline = mock_redis.pipeline.return_value
        pipeline.execute.return_value = [True]

        result = dedup.bulk_mark(["", "valid", None])
        assert "valid" in result
        assert "" not in result


# ---------------------------------------------------------------------------
# generate_event_id Tests
# ---------------------------------------------------------------------------


class TestGenerateEventId:
    """Test generate_event_id utility function."""

    def test_deterministic_output(self):
        id1 = generate_event_id("task", "123", "create")
        id2 = generate_event_id("task", "123", "create")
        assert id1 == id2

    def test_different_inputs_different_ids(self):
        id1 = generate_event_id("task", "123")
        id2 = generate_event_id("task", "456")
        assert id1 != id2

    def test_returns_32_char_hex(self):
        result = generate_event_id("some", "data")
        assert len(result) == 32
        assert all(c in "0123456789abcdef" for c in result)

    def test_dict_input(self):
        id1 = generate_event_id({"type": "task", "id": 1})
        id2 = generate_event_id({"type": "task", "id": 1})
        assert id1 == id2

    def test_order_independent_for_dicts(self):
        id1 = generate_event_id({"b": 2, "a": 1})
        id2 = generate_event_id({"a": 1, "b": 2})
        assert id1 == id2

    def test_handles_non_serializable_types(self):
        from datetime import datetime
        result = generate_event_id(datetime(2026, 1, 1))
        assert len(result) == 32

    def test_single_arg(self):
        result = generate_event_id("single")
        assert len(result) == 32

    def test_no_args(self):
        result = generate_event_id()
        assert len(result) == 32


# ---------------------------------------------------------------------------
# @deduplicate Decorator Tests
# ---------------------------------------------------------------------------


class TestDeduplicateDecorator:
    """Test the @deduplicate decorator."""

    def test_first_call_executes_handler(self, mock_redis):
        mock_redis.set.return_value = True  # Not duplicate

        @deduplicate(mock_redis, ttl=300)
        def handler(event_data):
            return "processed"

        result = handler({"event_id": "evt-001", "payload": "data"})
        assert result == "processed"

    def test_duplicate_call_returns_none(self, mock_redis):
        mock_redis.set.return_value = None  # Duplicate

        call_count = []

        @deduplicate(mock_redis, ttl=300)
        def handler(event_data):
            call_count.append(1)
            return "processed"

        result = handler({"event_id": "evt-001"})
        assert result is None
        assert len(call_count) == 0

    def test_uses_event_id_from_data(self, mock_redis):
        mock_redis.set.return_value = True

        @deduplicate(mock_redis, ttl=300)
        def handler(event_data):
            return "ok"

        handler({"event_id": "my-unique-id"})
        mock_redis.set.assert_called_once_with(
            f"{KEY_PREFIX}:my-unique-id", "1", nx=True, ex=300
        )

    def test_custom_event_id_func(self, mock_redis):
        mock_redis.set.return_value = True

        @deduplicate(mock_redis, ttl=60, event_id_func=lambda d: d["task_id"])
        def handler(event_data):
            return "processed"

        handler({"task_id": "task-xyz", "other": "field"})
        mock_redis.set.assert_called_once_with(
            f"{KEY_PREFIX}:task-xyz", "1", nx=True, ex=60
        )

    def test_generated_id_when_no_event_id_field(self, mock_redis):
        mock_redis.set.return_value = True

        @deduplicate(mock_redis, ttl=300)
        def handler(event_data):
            return "ok"

        handler({"type": "task", "data": "value"})
        # Should call set with a generated hash key
        call_args = mock_redis.set.call_args
        key = call_args[0][0]
        assert key.startswith(f"{KEY_PREFIX}:")
        # Generated ID should be 32 chars hex
        event_id_part = key.split(":", 1)[1]
        assert len(event_id_part) == 32

    def test_preserves_function_name(self, mock_redis):
        @deduplicate(mock_redis, ttl=300)
        def my_handler(event_data):
            """My docstring."""
            return "ok"

        assert my_handler.__name__ == "my_handler"
        assert my_handler.__doc__ == "My docstring."

    def test_custom_key_prefix(self, mock_redis):
        mock_redis.set.return_value = True

        @deduplicate(mock_redis, ttl=300, key_prefix="custom")
        def handler(event_data):
            return "ok"

        handler({"event_id": "evt-1"})
        call_args = mock_redis.set.call_args
        key = call_args[0][0]
        assert key.startswith("custom:")

    def test_deduplicator_accessible_on_wrapper(self, mock_redis):
        @deduplicate(mock_redis, ttl=300)
        def handler(event_data):
            return "ok"

        assert hasattr(handler, '_deduplicator')
        assert isinstance(handler._deduplicator, EventDeduplicator)

    def test_kwargs_event_data(self, mock_redis):
        mock_redis.set.return_value = True

        @deduplicate(mock_redis, ttl=300)
        def handler(event_data):
            return "handled"

        result = handler(event_data={"event_id": "kw-evt"})
        assert result == "handled"
        call_args = mock_redis.set.call_args
        assert f"{KEY_PREFIX}:kw-evt" == call_args[0][0]

    def test_handler_with_multiple_args(self, mock_redis):
        mock_redis.set.return_value = True

        @deduplicate(mock_redis, ttl=300)
        def handler(event_data, extra_context):
            return f"processed-{extra_context}"

        result = handler({"event_id": "multi-arg"}, "ctx")
        assert result == "processed-ctx"

    def test_ttl_passed_correctly_in_decorator(self, mock_redis):
        mock_redis.set.return_value = True

        @deduplicate(mock_redis, ttl=120)
        def handler(event_data):
            return "ok"

        handler({"event_id": "ttl-test"})
        mock_redis.set.assert_called_once_with(
            f"{KEY_PREFIX}:ttl-test", "1", nx=True, ex=120
        )


# ---------------------------------------------------------------------------
# Integration-style Tests (logical flow)
# ---------------------------------------------------------------------------


class TestDeduplicationFlow:
    """Test dedup behavior across multiple calls."""

    def test_sequence_first_new_then_duplicate(self, mock_redis):
        dedup = EventDeduplicator(mock_redis)

        # First call: event is new
        mock_redis.set.return_value = True
        assert dedup.is_duplicate("flow-1") is False

        # Second call: event is duplicate
        mock_redis.set.return_value = None
        assert dedup.is_duplicate("flow-1") is True

    def test_clear_allows_reprocessing(self, mock_redis):
        dedup = EventDeduplicator(mock_redis)

        # Mark as processed
        mock_redis.set.return_value = True
        dedup.mark_processed("clear-test")

        # Clear it
        mock_redis.delete.return_value = 1
        dedup.clear("clear-test")

        # Now it's no longer a duplicate
        mock_redis.set.return_value = True
        assert dedup.is_duplicate("clear-test") is False

    def test_mark_then_check_consistency(self, mock_redis):
        dedup = EventDeduplicator(mock_redis)

        mock_redis.set.return_value = True
        dedup.mark_processed("consistency-1")

        mock_redis.exists.return_value = 1
        assert dedup.is_processed("consistency-1") is True

    def test_different_prefix_isolates_events(self, mock_redis):
        dedup_a = EventDeduplicator(mock_redis, key_prefix="prefix_a")
        dedup_b = EventDeduplicator(mock_redis, key_prefix="prefix_b")

        mock_redis.set.return_value = True
        dedup_a.is_duplicate("shared-id")
        dedup_b.is_duplicate("shared-id")

        calls = mock_redis.set.call_args_list
        assert calls[0][0][0] == "prefix_a:shared-id"
        assert calls[1][0][0] == "prefix_b:shared-id"
