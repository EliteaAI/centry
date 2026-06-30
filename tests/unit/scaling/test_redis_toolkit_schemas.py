"""Unit tests for RedisToolkitSchemas.

Validates that:
1. set_schema stores a schema in Redis hash with correct key
2. set_schemas_batch stores multiple schemas in one pipeline
3. get_schema retrieves a single schema by title
4. get_all returns the full registry as a dict
5. remove_schema deletes a single schema
6. clear removes the entire registry
7. count returns the correct number of schemas
8. exists checks presence correctly
9. keys returns all toolkit titles
10. refresh_ttl resets TTL
11. Dict-like interface (__getitem__, __setitem__, __contains__, __len__, get, items, values)
12. TTL is applied on writes
13. Handles both str and bytes Redis responses
14. Handles malformed JSON gracefully
15. Empty registry returns empty dict

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_redis_toolkit_schemas.py -v
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

# Load the module via importlib to bypass elitea_core's __init__.py chain
_spec = importlib.util.spec_from_file_location(
    "redis_toolkit_schemas",
    str(_PLUGIN_ROOT / "utils" / "redis_toolkit_schemas.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["redis_toolkit_schemas"] = _mod
_spec.loader.exec_module(_mod)

RedisToolkitSchemas = _mod.RedisToolkitSchemas
DEFAULT_TTL = _mod.DEFAULT_TTL


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis():
    """Create a mock Redis client."""
    client = MagicMock()
    pipe = MagicMock()
    pipe.execute.return_value = [True, True]
    client.pipeline.return_value = pipe
    return client


@pytest.fixture
def store(mock_redis):
    """Create a RedisToolkitSchemas instance with a mock Redis client."""
    return RedisToolkitSchemas(mock_redis)


@pytest.fixture
def sample_schema():
    """A sample toolkit schema."""
    return {
        "title": "Jira",
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "api_key": {"type": "string", "toolkit_name": True},
        },
        "name_required": False,
    }


@pytest.fixture
def sample_schemas():
    """Multiple sample toolkit schemas."""
    return [
        {
            "title": "Jira",
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "name_required": False,
        },
        {
            "title": "GitHub",
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "name_required": True,
        },
        {
            "title": "Slack",
            "type": "object",
            "properties": {"channel": {"type": "string"}},
            "name_required": True,
        },
    ]


# ---------------------------------------------------------------------------
# Tests: set_schema
# ---------------------------------------------------------------------------

class TestSetSchema:
    def test_stores_schema_in_redis_hash(self, store, mock_redis, sample_schema):
        store.set_schema("Jira", sample_schema)
        pipe = mock_redis.pipeline.return_value
        pipe.hset.assert_called_once_with(
            "toolkit_schemas:global", "Jira", json.dumps(sample_schema, default=str)
        )

    def test_applies_ttl_on_write(self, store, mock_redis, sample_schema):
        store.set_schema("Jira", sample_schema)
        pipe = mock_redis.pipeline.return_value
        pipe.expire.assert_called_once_with("toolkit_schemas:global", DEFAULT_TTL)

    def test_executes_pipeline(self, store, mock_redis, sample_schema):
        store.set_schema("Jira", sample_schema)
        pipe = mock_redis.pipeline.return_value
        pipe.execute.assert_called_once()

    def test_custom_ttl(self, mock_redis, sample_schema):
        store = RedisToolkitSchemas(mock_redis, ttl=7200)
        store.set_schema("Jira", sample_schema)
        pipe = mock_redis.pipeline.return_value
        pipe.expire.assert_called_once_with("toolkit_schemas:global", 7200)


# ---------------------------------------------------------------------------
# Tests: set_schemas_batch
# ---------------------------------------------------------------------------

class TestSetSchemasBatch:
    def test_stores_multiple_schemas(self, store, mock_redis, sample_schemas):
        store.set_schemas_batch(sample_schemas)
        pipe = mock_redis.pipeline.return_value
        assert pipe.hset.call_count == 3

    def test_applies_single_ttl(self, store, mock_redis, sample_schemas):
        store.set_schemas_batch(sample_schemas)
        pipe = mock_redis.pipeline.return_value
        pipe.expire.assert_called_once_with("toolkit_schemas:global", DEFAULT_TTL)

    def test_empty_list_does_nothing(self, store, mock_redis):
        store.set_schemas_batch([])
        mock_redis.pipeline.assert_not_called()

    def test_skips_schemas_without_title(self, store, mock_redis):
        schemas = [
            {"title": "Jira", "type": "object"},
            {"type": "object"},  # no title
            {"title": "Slack", "type": "object"},
        ]
        store.set_schemas_batch(schemas)
        pipe = mock_redis.pipeline.return_value
        assert pipe.hset.call_count == 2

    def test_executes_pipeline_once(self, store, mock_redis, sample_schemas):
        store.set_schemas_batch(sample_schemas)
        pipe = mock_redis.pipeline.return_value
        pipe.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: get_schema
# ---------------------------------------------------------------------------

class TestGetSchema:
    def test_returns_schema_dict(self, store, mock_redis, sample_schema):
        mock_redis.hget.return_value = json.dumps(sample_schema)
        result = store.get_schema("Jira")
        assert result == sample_schema
        mock_redis.hget.assert_called_once_with("toolkit_schemas:global", "Jira")

    def test_returns_none_when_not_found(self, store, mock_redis):
        mock_redis.hget.return_value = None
        result = store.get_schema("NonExistent")
        assert result is None

    def test_handles_bytes_response(self, store, mock_redis, sample_schema):
        mock_redis.hget.return_value = json.dumps(sample_schema).encode()
        result = store.get_schema("Jira")
        assert result == sample_schema


# ---------------------------------------------------------------------------
# Tests: get_all
# ---------------------------------------------------------------------------

class TestGetAll:
    def test_returns_full_registry(self, store, mock_redis, sample_schemas):
        mock_redis.hgetall.return_value = {
            s["title"]: json.dumps(s) for s in sample_schemas
        }
        result = store.get_all()
        assert len(result) == 3
        assert "Jira" in result
        assert "GitHub" in result
        assert "Slack" in result
        assert result["Jira"]["type"] == "object"

    def test_returns_empty_dict_when_key_missing(self, store, mock_redis):
        mock_redis.hgetall.return_value = {}
        result = store.get_all()
        assert result == {}

    def test_handles_bytes_keys_and_values(self, store, mock_redis, sample_schema):
        mock_redis.hgetall.return_value = {
            b"Jira": json.dumps(sample_schema).encode()
        }
        result = store.get_all()
        assert "Jira" in result
        assert result["Jira"] == sample_schema

    def test_skips_malformed_json(self, store, mock_redis):
        mock_redis.hgetall.return_value = {
            "Jira": json.dumps({"title": "Jira"}),
            "Bad": "not valid json {{{",
        }
        result = store.get_all()
        assert "Jira" in result
        assert "Bad" not in result

    def test_returns_none_value_as_empty_dict(self, store, mock_redis):
        mock_redis.hgetall.return_value = None
        result = store.get_all()
        assert result == {}


# ---------------------------------------------------------------------------
# Tests: remove_schema
# ---------------------------------------------------------------------------

class TestRemoveSchema:
    def test_removes_existing_schema(self, store, mock_redis):
        mock_redis.hdel.return_value = 1
        assert store.remove_schema("Jira") is True
        mock_redis.hdel.assert_called_once_with("toolkit_schemas:global", "Jira")

    def test_returns_false_when_not_found(self, store, mock_redis):
        mock_redis.hdel.return_value = 0
        assert store.remove_schema("NonExistent") is False


# ---------------------------------------------------------------------------
# Tests: clear
# ---------------------------------------------------------------------------

class TestClear:
    def test_deletes_entire_key(self, store, mock_redis):
        mock_redis.delete.return_value = 1
        assert store.clear() is True
        mock_redis.delete.assert_called_once_with("toolkit_schemas:global")

    def test_returns_false_when_key_missing(self, store, mock_redis):
        mock_redis.delete.return_value = 0
        assert store.clear() is False


# ---------------------------------------------------------------------------
# Tests: count
# ---------------------------------------------------------------------------

class TestCount:
    def test_returns_hash_length(self, store, mock_redis):
        mock_redis.hlen.return_value = 5
        assert store.count() == 5
        mock_redis.hlen.assert_called_once_with("toolkit_schemas:global")

    def test_returns_zero_when_empty(self, store, mock_redis):
        mock_redis.hlen.return_value = 0
        assert store.count() == 0


# ---------------------------------------------------------------------------
# Tests: exists
# ---------------------------------------------------------------------------

class TestExists:
    def test_returns_true_when_field_exists(self, store, mock_redis):
        mock_redis.hexists.return_value = True
        assert store.exists("Jira") is True
        mock_redis.hexists.assert_called_once_with("toolkit_schemas:global", "Jira")

    def test_returns_false_when_field_missing(self, store, mock_redis):
        mock_redis.hexists.return_value = False
        assert store.exists("NonExistent") is False


# ---------------------------------------------------------------------------
# Tests: keys
# ---------------------------------------------------------------------------

class TestKeys:
    def test_returns_all_field_names(self, store, mock_redis):
        mock_redis.hkeys.return_value = ["Jira", "GitHub", "Slack"]
        result = store.keys()
        assert result == ["Jira", "GitHub", "Slack"]

    def test_handles_bytes_keys(self, store, mock_redis):
        mock_redis.hkeys.return_value = [b"Jira", b"GitHub"]
        result = store.keys()
        assert result == ["Jira", "GitHub"]

    def test_returns_empty_list_when_no_schemas(self, store, mock_redis):
        mock_redis.hkeys.return_value = []
        result = store.keys()
        assert result == []


# ---------------------------------------------------------------------------
# Tests: refresh_ttl
# ---------------------------------------------------------------------------

class TestRefreshTtl:
    def test_sets_ttl_on_key(self, store, mock_redis):
        mock_redis.expire.return_value = True
        assert store.refresh_ttl() is True
        mock_redis.expire.assert_called_once_with("toolkit_schemas:global", DEFAULT_TTL)

    def test_returns_false_when_key_missing(self, store, mock_redis):
        mock_redis.expire.return_value = False
        assert store.refresh_ttl() is False


# ---------------------------------------------------------------------------
# Tests: Dict-like interface
# ---------------------------------------------------------------------------

class TestDictInterface:
    def test_getitem_returns_schema(self, store, mock_redis, sample_schema):
        mock_redis.hget.return_value = json.dumps(sample_schema)
        result = store["Jira"]
        assert result == sample_schema

    def test_getitem_raises_keyerror_when_missing(self, store, mock_redis):
        mock_redis.hget.return_value = None
        with pytest.raises(KeyError, match="Jira"):
            _ = store["Jira"]

    def test_setitem_stores_schema(self, store, mock_redis, sample_schema):
        store["Jira"] = sample_schema
        pipe = mock_redis.pipeline.return_value
        pipe.hset.assert_called_once()

    def test_contains_returns_true(self, store, mock_redis):
        mock_redis.hexists.return_value = True
        assert ("Jira" in store) is True

    def test_contains_returns_false(self, store, mock_redis):
        mock_redis.hexists.return_value = False
        assert ("Jira" in store) is False

    def test_len_returns_count(self, store, mock_redis):
        mock_redis.hlen.return_value = 3
        assert len(store) == 3

    def test_get_returns_schema(self, store, mock_redis, sample_schema):
        mock_redis.hget.return_value = json.dumps(sample_schema)
        result = store.get("Jira")
        assert result == sample_schema

    def test_get_returns_default_when_missing(self, store, mock_redis):
        mock_redis.hget.return_value = None
        result = store.get("Jira", {"fallback": True})
        assert result == {"fallback": True}

    def test_get_returns_none_default_when_missing(self, store, mock_redis):
        mock_redis.hget.return_value = None
        result = store.get("Jira")
        assert result is None

    def test_items_returns_pairs(self, store, mock_redis, sample_schemas):
        mock_redis.hgetall.return_value = {
            s["title"]: json.dumps(s) for s in sample_schemas
        }
        items = list(store.items())
        assert len(items) == 3
        titles = [k for k, v in items]
        assert "Jira" in titles
        assert "GitHub" in titles

    def test_values_returns_schemas(self, store, mock_redis, sample_schemas):
        mock_redis.hgetall.return_value = {
            s["title"]: json.dumps(s) for s in sample_schemas
        }
        vals = list(store.values())
        assert len(vals) == 3
        assert all(isinstance(v, dict) for v in vals)


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_schema_with_nested_complex_objects(self, store, mock_redis):
        complex_schema = {
            "title": "ComplexTool",
            "type": "object",
            "properties": {
                "config": {
                    "type": "object",
                    "properties": {
                        "nested": {"type": "array", "items": {"type": "string"}}
                    }
                }
            },
            "required": ["config"],
            "name_required": True,
        }
        store.set_schema("ComplexTool", complex_schema)
        pipe = mock_redis.pipeline.return_value
        stored_json = pipe.hset.call_args[0][2]
        assert json.loads(stored_json) == complex_schema

    def test_schema_with_special_characters_in_title(self, store, mock_redis):
        schema = {"title": "My Tool (v2.0) - Special", "type": "object"}
        store.set_schema("My Tool (v2.0) - Special", schema)
        pipe = mock_redis.pipeline.return_value
        pipe.hset.assert_called_once_with(
            "toolkit_schemas:global",
            "My Tool (v2.0) - Special",
            json.dumps(schema, default=str),
        )

    def test_concurrent_writes_are_idempotent(self, store, mock_redis, sample_schema):
        store.set_schema("Jira", sample_schema)
        store.set_schema("Jira", sample_schema)
        pipe = mock_redis.pipeline.return_value
        assert pipe.hset.call_count == 2

    def test_get_all_with_mixed_valid_and_invalid(self, store, mock_redis):
        mock_redis.hgetall.return_value = {
            "Good1": json.dumps({"title": "Good1"}),
            "Good2": json.dumps({"title": "Good2"}),
            "Bad1": "{invalid json",
            "Good3": json.dumps({"title": "Good3"}),
        }
        result = store.get_all()
        assert len(result) == 3
        assert "Bad1" not in result

    def test_default_ttl_value(self):
        assert DEFAULT_TTL == 3600

    def test_custom_key_not_used(self, store):
        assert store._key == "toolkit_schemas:global"
