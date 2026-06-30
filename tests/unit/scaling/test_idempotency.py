"""Unit tests for IdempotencyStore and @idempotent decorator.

Validates that:
1. IdempotencyStore.get returns None for absent keys
2. IdempotencyStore.set stores result with NX semantics
3. IdempotencyStore.set returns False if key already exists
4. IdempotencyStore.has checks existence without retrieval
5. IdempotencyStore.force_set overwrites existing values
6. IdempotencyStore.invalidate removes cached results
7. IdempotencyStore.get_ttl returns remaining TTL
8. IdempotencyStore.get_with_metadata returns result + TTL
9. IdempotencyStore.check_and_set handles both cases
10. IdempotencyStore.bulk_check retrieves multiple results
11. compute_params_hash produces deterministic hashes
12. @idempotent decorator caches function results
13. @idempotent with key_func uses custom key extraction
14. @idempotent handles None results correctly
15. Edge cases: empty strings, special characters, large payloads

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_idempotency.py -v
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

_module_path = _PLUGIN_ROOT / "utils" / "idempotency.py"
_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.utils.idempotency",
    _module_path,
    submodule_search_locations=[],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

IdempotencyStore = _mod.IdempotencyStore
compute_params_hash = _mod.compute_params_hash
idempotent = _mod.idempotent
DEFAULT_TTL = _mod.DEFAULT_TTL
KEY_PREFIX = _mod.KEY_PREFIX


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis():
    """Create a mock Redis client with common method mocking."""
    client = MagicMock()
    client.get.return_value = None
    client.set.return_value = True
    client.exists.return_value = 0
    client.delete.return_value = 1
    client.ttl.return_value = -2
    pipe = MagicMock()
    pipe.execute.return_value = []
    pipe.__enter__ = MagicMock(return_value=pipe)
    pipe.__exit__ = MagicMock(return_value=False)
    client.pipeline.return_value = pipe
    return client


@pytest.fixture
def store(mock_redis):
    """Create an IdempotencyStore with mock Redis."""
    return IdempotencyStore(mock_redis)


# ---------------------------------------------------------------------------
# Tests: IdempotencyStore.get
# ---------------------------------------------------------------------------

class TestGet:
    def test_returns_none_for_absent_key(self, store, mock_redis):
        mock_redis.get.return_value = None
        result = store.get("create_agent", "abc123")
        assert result is None
        mock_redis.get.assert_called_once_with("idempotency:create_agent:abc123")

    def test_returns_deserialized_json_for_existing_key(self, store, mock_redis):
        mock_redis.get.return_value = b'{"status": "created", "id": 42}'
        result = store.get("create_agent", "abc123")
        assert result == {"status": "created", "id": 42}

    def test_returns_string_for_non_json_value(self, store, mock_redis):
        mock_redis.get.return_value = b"raw_string_value"
        result = store.get("op", "hash1")
        assert result == "raw_string_value"

    def test_handles_integer_json(self, store, mock_redis):
        mock_redis.get.return_value = b"42"
        result = store.get("op", "hash1")
        assert result == 42

    def test_handles_list_json(self, store, mock_redis):
        mock_redis.get.return_value = b'[1, 2, 3]'
        result = store.get("op", "hash1")
        assert result == [1, 2, 3]

    def test_handles_null_json(self, store, mock_redis):
        mock_redis.get.return_value = b"null"
        result = store.get("op", "hash1")
        assert result is None

    def test_handles_boolean_json(self, store, mock_redis):
        mock_redis.get.return_value = b"true"
        result = store.get("op", "hash1")
        assert result is True


# ---------------------------------------------------------------------------
# Tests: IdempotencyStore.has
# ---------------------------------------------------------------------------

class TestHas:
    def test_returns_false_for_absent_key(self, store, mock_redis):
        mock_redis.exists.return_value = 0
        assert store.has("op", "hash1") is False
        mock_redis.exists.assert_called_once_with("idempotency:op:hash1")

    def test_returns_true_for_existing_key(self, store, mock_redis):
        mock_redis.exists.return_value = 1
        assert store.has("op", "hash1") is True


# ---------------------------------------------------------------------------
# Tests: IdempotencyStore.set
# ---------------------------------------------------------------------------

class TestSet:
    def test_stores_with_nx_and_ttl(self, store, mock_redis):
        mock_redis.set.return_value = True
        result = store.set("create_agent", "abc123", {"id": 1}, ttl=600)
        assert result is True
        mock_redis.set.assert_called_once_with(
            "idempotency:create_agent:abc123",
            '{"id": 1}',
            nx=True,
            ex=600,
        )

    def test_returns_false_when_key_exists(self, store, mock_redis):
        mock_redis.set.return_value = None  # Redis returns None on NX failure
        result = store.set("create_agent", "abc123", {"id": 1})
        assert result is False

    def test_uses_default_ttl_when_not_specified(self, store, mock_redis):
        mock_redis.set.return_value = True
        store.set("op", "hash1", "value")
        _, kwargs = mock_redis.set.call_args
        assert kwargs["ex"] == DEFAULT_TTL

    def test_serializes_complex_objects(self, store, mock_redis):
        mock_redis.set.return_value = True
        payload = {"nested": {"list": [1, 2], "str": "hello"}}
        store.set("op", "hash1", payload)
        serialized = mock_redis.set.call_args[0][1]
        assert json.loads(serialized) == payload

    def test_serializes_none_result(self, store, mock_redis):
        mock_redis.set.return_value = True
        store.set("op", "hash1", None)
        serialized = mock_redis.set.call_args[0][1]
        assert serialized == "null"


# ---------------------------------------------------------------------------
# Tests: IdempotencyStore.force_set
# ---------------------------------------------------------------------------

class TestForceSet:
    def test_overwrites_existing_value(self, store, mock_redis):
        store.force_set("op", "hash1", {"new": True}, ttl=300)
        mock_redis.set.assert_called_once_with(
            "idempotency:op:hash1",
            '{"new": true}',
            ex=300,
        )

    def test_uses_default_ttl(self, store, mock_redis):
        store.force_set("op", "hash1", "val")
        _, kwargs = mock_redis.set.call_args
        assert kwargs["ex"] == DEFAULT_TTL


# ---------------------------------------------------------------------------
# Tests: IdempotencyStore.invalidate
# ---------------------------------------------------------------------------

class TestInvalidate:
    def test_removes_key(self, store, mock_redis):
        mock_redis.delete.return_value = 1
        assert store.invalidate("op", "hash1") is True
        mock_redis.delete.assert_called_once_with("idempotency:op:hash1")

    def test_returns_false_for_nonexistent_key(self, store, mock_redis):
        mock_redis.delete.return_value = 0
        assert store.invalidate("op", "hash1") is False


# ---------------------------------------------------------------------------
# Tests: IdempotencyStore.get_ttl
# ---------------------------------------------------------------------------

class TestGetTTL:
    def test_returns_ttl_for_existing_key(self, store, mock_redis):
        mock_redis.ttl.return_value = 300
        assert store.get_ttl("op", "hash1") == 300

    def test_returns_minus_2_for_absent_key(self, store, mock_redis):
        mock_redis.ttl.return_value = -2
        assert store.get_ttl("op", "hash1") == -2

    def test_returns_minus_1_for_no_expiry(self, store, mock_redis):
        mock_redis.ttl.return_value = -1
        assert store.get_ttl("op", "hash1") == -1


# ---------------------------------------------------------------------------
# Tests: IdempotencyStore.get_with_metadata
# ---------------------------------------------------------------------------

class TestGetWithMetadata:
    def test_returns_result_and_ttl(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [b'{"id": 5}', 250]
        meta = store.get_with_metadata("op", "hash1")
        assert meta == {"exists": True, "result": {"id": 5}, "ttl_remaining": 250}

    def test_returns_not_found_metadata(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [None, -2]
        meta = store.get_with_metadata("op", "hash1")
        assert meta == {"exists": False, "result": None, "ttl_remaining": -2}

    def test_handles_non_json_value(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [b"plain_text", 100]
        meta = store.get_with_metadata("op", "hash1")
        assert meta["exists"] is True
        assert meta["result"] == "plain_text"
        assert meta["ttl_remaining"] == 100


# ---------------------------------------------------------------------------
# Tests: IdempotencyStore.check_and_set
# ---------------------------------------------------------------------------

class TestCheckAndSet:
    def test_returns_cached_when_exists(self, store, mock_redis):
        mock_redis.get.return_value = b'{"cached": true}'
        was_cached, result = store.check_and_set("op:hash1", {"new": True})
        assert was_cached is True
        assert result == {"cached": True}
        mock_redis.set.assert_not_called()

    def test_stores_and_returns_when_absent(self, store, mock_redis):
        mock_redis.get.return_value = None
        was_cached, result = store.check_and_set("op:hash1", {"new": True}, ttl=600)
        assert was_cached is False
        assert result == {"new": True}
        mock_redis.set.assert_called_once()

    def test_uses_full_prefix_when_key_already_prefixed(self, store, mock_redis):
        mock_redis.get.return_value = None
        store.check_and_set("idempotency:op:hash1", {"x": 1})
        mock_redis.get.assert_called_once_with("idempotency:op:hash1")

    def test_adds_prefix_when_key_not_prefixed(self, store, mock_redis):
        mock_redis.get.return_value = None
        store.check_and_set("my_op:abc", {"x": 1})
        mock_redis.get.assert_called_once_with("idempotency:my_op:abc")

    def test_handles_non_json_cached_value(self, store, mock_redis):
        mock_redis.get.return_value = b"not{valid}json"
        was_cached, result = store.check_and_set("op:h", {"x": 1})
        assert was_cached is True
        assert result == "not{valid}json"


# ---------------------------------------------------------------------------
# Tests: IdempotencyStore.bulk_check
# ---------------------------------------------------------------------------

class TestBulkCheck:
    def test_returns_empty_dict_for_empty_input(self, store, mock_redis):
        assert store.bulk_check("op", []) == {}

    def test_retrieves_multiple_results(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [b'{"a": 1}', None, b'"hello"']
        result = store.bulk_check("op", ["h1", "h2", "h3"])
        assert result == {"h1": {"a": 1}, "h2": None, "h3": "hello"}

    def test_skips_empty_hashes(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [b'"val"']
        result = store.bulk_check("op", ["", "h1", None])
        assert "h1" in result
        assert "" not in result

    def test_handles_non_json_values(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [b"raw_bytes"]
        result = store.bulk_check("op", ["h1"])
        assert result["h1"] == "raw_bytes"


# ---------------------------------------------------------------------------
# Tests: compute_params_hash
# ---------------------------------------------------------------------------

class TestComputeParamsHash:
    def test_returns_32_char_hex_string(self):
        h = compute_params_hash("arg1", key="val")
        assert len(h) == 32
        assert all(c in "0123456789abcdef" for c in h)

    def test_deterministic_for_same_inputs(self):
        h1 = compute_params_hash(1, "two", key="three")
        h2 = compute_params_hash(1, "two", key="three")
        assert h1 == h2

    def test_different_for_different_inputs(self):
        h1 = compute_params_hash("a", "b")
        h2 = compute_params_hash("a", "c")
        assert h1 != h2

    def test_kwargs_order_independent(self):
        h1 = compute_params_hash(x=1, y=2)
        h2 = compute_params_hash(y=2, x=1)
        assert h1 == h2

    def test_handles_complex_objects(self):
        h = compute_params_hash({"nested": [1, 2]}, set_val={3, 4})
        assert len(h) == 32

    def test_handles_none_values(self):
        h = compute_params_hash(None, key=None)
        assert len(h) == 32

    def test_empty_args_produces_hash(self):
        h = compute_params_hash()
        assert len(h) == 32


# ---------------------------------------------------------------------------
# Tests: @idempotent decorator
# ---------------------------------------------------------------------------

class TestIdempotentDecorator:
    def test_executes_function_on_first_call(self, mock_redis):
        mock_redis.get.return_value = None
        mock_redis.set.return_value = True

        @idempotent(mock_redis, ttl=60)
        def my_func(x, y):
            return x + y

        result = my_func(2, 3)
        assert result == 5

    def test_returns_cached_on_second_call(self, mock_redis):
        mock_redis.get.return_value = b"5"

        @idempotent(mock_redis, ttl=60)
        def my_func(x, y):
            raise AssertionError("Should not be called")

        result = my_func(2, 3)
        assert result == 5

    def test_uses_key_func_for_cache_key(self, mock_redis):
        mock_redis.get.return_value = None
        mock_redis.set.return_value = True

        @idempotent(mock_redis, key_func=lambda req: f"{req['id']}", ttl=60)
        def create(req):
            return {"created": req["id"]}

        result = create({"id": "abc"})
        assert result == {"created": "abc"}
        get_key = mock_redis.get.call_args[0][0]
        assert "abc" in get_key

    def test_caches_dict_result(self, mock_redis):
        mock_redis.get.return_value = None
        mock_redis.set.return_value = True

        @idempotent(mock_redis, ttl=120)
        def get_data():
            return {"items": [1, 2, 3]}

        result = get_data()
        assert result == {"items": [1, 2, 3]}
        set_value = mock_redis.set.call_args[0][1]
        assert json.loads(set_value) == {"items": [1, 2, 3]}

    def test_caches_none_result_with_force_set(self, mock_redis):
        mock_redis.get.return_value = None
        call_count = 0

        @idempotent(mock_redis, ttl=60)
        def returns_none():
            nonlocal call_count
            call_count += 1
            return None

        returns_none()
        assert call_count == 1
        # force_set is called (without nx)
        assert mock_redis.set.called

    def test_custom_operation_name(self, mock_redis):
        mock_redis.get.return_value = None
        mock_redis.set.return_value = True

        @idempotent(mock_redis, operation="custom_op", ttl=60)
        def my_func():
            return "result"

        my_func()
        get_key = mock_redis.get.call_args[0][0]
        assert "custom_op" in get_key

    def test_custom_key_prefix(self, mock_redis):
        mock_redis.get.return_value = None
        mock_redis.set.return_value = True

        @idempotent(mock_redis, key_prefix="my_prefix", ttl=60)
        def my_func():
            return "result"

        my_func()
        get_key = mock_redis.get.call_args[0][0]
        assert get_key.startswith("my_prefix:")

    def test_preserves_function_name(self, mock_redis):
        @idempotent(mock_redis, ttl=60)
        def original_name():
            """Original docstring."""
            return 1

        assert original_name.__name__ == "original_name"
        assert original_name.__doc__ == "Original docstring."

    def test_exposes_idempotency_store(self, mock_redis):
        @idempotent(mock_redis, ttl=60)
        def my_func():
            return 1

        assert hasattr(my_func, "_idempotency_store")
        assert isinstance(my_func._idempotency_store, IdempotencyStore)

    def test_exposes_operation_name(self, mock_redis):
        @idempotent(mock_redis, operation="my_op", ttl=60)
        def my_func():
            return 1

        assert my_func._operation == "my_op"

    def test_different_args_different_keys(self, mock_redis):
        mock_redis.get.return_value = None
        mock_redis.set.return_value = True
        keys_seen = []

        original_get = mock_redis.get

        def track_get(key):
            keys_seen.append(key)
            return None

        mock_redis.get.side_effect = track_get

        @idempotent(mock_redis, ttl=60)
        def my_func(x):
            return x * 2

        my_func(1)
        my_func(2)
        assert len(keys_seen) == 2
        assert keys_seen[0] != keys_seen[1]

    def test_returns_cached_dict_result(self, mock_redis):
        mock_redis.get.return_value = b'{"status": "ok", "id": 99}'

        @idempotent(mock_redis, ttl=60)
        def create_item(name):
            raise AssertionError("Should not execute")

        result = create_item("test")
        assert result == {"status": "ok", "id": 99}

    def test_key_func_receives_all_args(self, mock_redis):
        mock_redis.get.return_value = None
        mock_redis.set.return_value = True
        received_args = []

        def capture_key(*args, **kwargs):
            received_args.append((args, kwargs))
            return "fixed_key"

        @idempotent(mock_redis, key_func=capture_key, ttl=60)
        def my_func(a, b, c=3):
            return a + b + c

        my_func(1, 2, c=10)
        assert received_args[0] == ((1, 2), {"c": 10})


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_store_with_custom_prefix(self, mock_redis):
        store = IdempotencyStore(mock_redis, key_prefix="custom")
        mock_redis.get.return_value = b'"val"'
        result = store.get("op", "h")
        mock_redis.get.assert_called_once_with("custom:op:h")
        assert result == "val"

    def test_store_with_custom_ttl(self, mock_redis):
        store = IdempotencyStore(mock_redis, default_ttl=7200)
        mock_redis.set.return_value = True
        store.set("op", "h", "val")
        _, kwargs = mock_redis.set.call_args
        assert kwargs["ex"] == 7200

    def test_special_characters_in_operation(self, store, mock_redis):
        mock_redis.get.return_value = None
        store.get("op/with:special.chars", "hash")
        mock_redis.get.assert_called_once_with(
            "idempotency:op/with:special.chars:hash"
        )

    def test_large_result_serialization(self, store, mock_redis):
        mock_redis.set.return_value = True
        large_result = {"data": "x" * 10000, "items": list(range(100))}
        store.set("op", "h", large_result)
        serialized = mock_redis.set.call_args[0][1]
        assert json.loads(serialized) == large_result

    def test_unicode_in_result(self, store, mock_redis):
        mock_redis.set.return_value = True
        store.set("op", "h", {"emoji": "🚀", "chinese": "你好"})
        serialized = mock_redis.set.call_args[0][1]
        deserialized = json.loads(serialized)
        assert deserialized["emoji"] == "🚀"
        assert deserialized["chinese"] == "你好"

    def test_bulk_check_with_all_empty_hashes(self, store, mock_redis):
        result = store.bulk_check("op", ["", "", None])
        assert result == {}

    def test_get_with_bytes_string_value(self, store, mock_redis):
        mock_redis.get.return_value = b"\xff\xfe"
        result = store.get("op", "h")
        # Non-JSON bytes should be decoded or returned as-is
        assert result is not None
