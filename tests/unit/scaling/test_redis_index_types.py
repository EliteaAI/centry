"""Unit tests for RedisIndexTypes.

Validates that:
1. set_all stores the full payload in Redis hash
2. get_all retrieves the full registry
3. get_category retrieves a single category
4. clear removes the registry
5. count returns total extensions across categories
6. exists checks key presence
7. refresh_ttl resets TTL
8. Dict-like interface (__getitem__, __setitem__, __contains__, __len__, __bool__, get, items, values, keys)
9. TTL is applied on writes
10. Handles both str and bytes Redis responses
11. Handles malformed JSON gracefully
12. Empty registry returns empty dict
13. set_all with empty dict is a no-op

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_redis_index_types.py -v
"""

import importlib
import importlib.util
import json
import pathlib
import sys
from unittest.mock import MagicMock, call

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
    "redis_index_types",
    str(_PLUGIN_ROOT / "utils" / "redis_index_types.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["redis_index_types"] = _mod
_spec.loader.exec_module(_mod)

RedisIndexTypes = _mod.RedisIndexTypes
DEFAULT_TTL = _mod.DEFAULT_TTL


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_PAYLOAD = {
    "document_types": {".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "image_types": {".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif"},
    "code_types": {".py": "text/x-python", ".js": "text/javascript"},
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis():
    """Create a mock Redis client."""
    client = MagicMock()
    pipe = MagicMock()
    pipe.execute.return_value = [True, True, True, True]
    client.pipeline.return_value = pipe
    return client


@pytest.fixture
def store(mock_redis):
    """Create a RedisIndexTypes instance with a mock Redis client."""
    return RedisIndexTypes(mock_redis)


# ---------------------------------------------------------------------------
# Tests: set_all
# ---------------------------------------------------------------------------

class TestSetAll:
    def test_stores_all_categories(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        store.set_all(SAMPLE_PAYLOAD)

        mock_redis.pipeline.assert_called_once()
        assert pipe.hset.call_count == 3
        pipe.hset.assert_any_call(
            "index_types:global", "document_types",
            json.dumps(SAMPLE_PAYLOAD["document_types"])
        )
        pipe.hset.assert_any_call(
            "index_types:global", "image_types",
            json.dumps(SAMPLE_PAYLOAD["image_types"])
        )
        pipe.hset.assert_any_call(
            "index_types:global", "code_types",
            json.dumps(SAMPLE_PAYLOAD["code_types"])
        )
        pipe.expire.assert_called_once_with("index_types:global", DEFAULT_TTL)
        pipe.execute.assert_called_once()

    def test_empty_payload_is_noop(self, store, mock_redis):
        store.set_all({})
        mock_redis.pipeline.assert_not_called()

    def test_none_payload_is_noop(self, store, mock_redis):
        store.set_all(None)
        mock_redis.pipeline.assert_not_called()

    def test_missing_category_stores_empty_dict(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        payload = {"document_types": {".pdf": "application/pdf"}}
        store.set_all(payload)

        pipe.hset.assert_any_call(
            "index_types:global", "image_types", json.dumps({})
        )
        pipe.hset.assert_any_call(
            "index_types:global", "code_types", json.dumps({})
        )

    def test_custom_ttl(self, mock_redis):
        pipe = mock_redis.pipeline.return_value
        store = RedisIndexTypes(mock_redis, ttl=7200)
        store.set_all(SAMPLE_PAYLOAD)
        pipe.expire.assert_called_once_with("index_types:global", 7200)


# ---------------------------------------------------------------------------
# Tests: get_all
# ---------------------------------------------------------------------------

class TestGetAll:
    def test_returns_full_registry(self, store, mock_redis):
        mock_redis.hgetall.return_value = {
            "document_types": json.dumps(SAMPLE_PAYLOAD["document_types"]),
            "image_types": json.dumps(SAMPLE_PAYLOAD["image_types"]),
            "code_types": json.dumps(SAMPLE_PAYLOAD["code_types"]),
        }
        result = store.get_all()
        assert result == SAMPLE_PAYLOAD
        mock_redis.hgetall.assert_called_once_with("index_types:global")

    def test_empty_registry_returns_empty_dict(self, store, mock_redis):
        mock_redis.hgetall.return_value = {}
        assert store.get_all() == {}

    def test_none_registry_returns_empty_dict(self, store, mock_redis):
        mock_redis.hgetall.return_value = None
        assert store.get_all() == {}

    def test_handles_bytes_keys_and_values(self, store, mock_redis):
        mock_redis.hgetall.return_value = {
            b"document_types": b'{".pdf": "application/pdf"}',
            b"image_types": b'{".png": "image/png"}',
        }
        result = store.get_all()
        assert result == {
            "document_types": {".pdf": "application/pdf"},
            "image_types": {".png": "image/png"},
        }

    def test_malformed_json_returns_empty_dict_for_category(self, store, mock_redis):
        mock_redis.hgetall.return_value = {
            "document_types": "not-valid-json{{{",
            "image_types": json.dumps({".png": "image/png"}),
        }
        result = store.get_all()
        assert result["document_types"] == {}
        assert result["image_types"] == {".png": "image/png"}


# ---------------------------------------------------------------------------
# Tests: get_category
# ---------------------------------------------------------------------------

class TestGetCategory:
    def test_returns_category_dict(self, store, mock_redis):
        mock_redis.hget.return_value = json.dumps({".pdf": "application/pdf"})
        result = store.get_category("document_types")
        assert result == {".pdf": "application/pdf"}
        mock_redis.hget.assert_called_once_with("index_types:global", "document_types")

    def test_missing_category_returns_empty_dict(self, store, mock_redis):
        mock_redis.hget.return_value = None
        assert store.get_category("nonexistent") == {}

    def test_handles_bytes_response(self, store, mock_redis):
        mock_redis.hget.return_value = b'{".py": "text/x-python"}'
        result = store.get_category("code_types")
        assert result == {".py": "text/x-python"}

    def test_malformed_json_returns_empty_dict(self, store, mock_redis):
        mock_redis.hget.return_value = "broken{json"
        result = store.get_category("document_types")
        assert result == {}


# ---------------------------------------------------------------------------
# Tests: clear
# ---------------------------------------------------------------------------

class TestClear:
    def test_deletes_key(self, store, mock_redis):
        mock_redis.delete.return_value = 1
        assert store.clear() is True
        mock_redis.delete.assert_called_once_with("index_types:global")

    def test_returns_false_when_key_missing(self, store, mock_redis):
        mock_redis.delete.return_value = 0
        assert store.clear() is False


# ---------------------------------------------------------------------------
# Tests: count
# ---------------------------------------------------------------------------

class TestCount:
    def test_sums_extensions_across_categories(self, store, mock_redis):
        mock_redis.hgetall.return_value = {
            "document_types": json.dumps({".pdf": "application/pdf", ".docx": "application/msword"}),
            "image_types": json.dumps({".png": "image/png"}),
            "code_types": json.dumps({".py": "text/x-python", ".js": "text/javascript"}),
        }
        assert store.count() == 5

    def test_empty_registry_returns_zero(self, store, mock_redis):
        mock_redis.hgetall.return_value = {}
        assert store.count() == 0


# ---------------------------------------------------------------------------
# Tests: exists
# ---------------------------------------------------------------------------

class TestExists:
    def test_returns_true_when_key_exists(self, store, mock_redis):
        mock_redis.exists.return_value = 1
        assert store.exists() is True
        mock_redis.exists.assert_called_once_with("index_types:global")

    def test_returns_false_when_key_missing(self, store, mock_redis):
        mock_redis.exists.return_value = 0
        assert store.exists() is False


# ---------------------------------------------------------------------------
# Tests: refresh_ttl
# ---------------------------------------------------------------------------

class TestRefreshTtl:
    def test_sets_ttl(self, store, mock_redis):
        mock_redis.expire.return_value = True
        assert store.refresh_ttl() is True
        mock_redis.expire.assert_called_once_with("index_types:global", DEFAULT_TTL)

    def test_returns_false_when_key_missing(self, store, mock_redis):
        mock_redis.expire.return_value = False
        assert store.refresh_ttl() is False


# ---------------------------------------------------------------------------
# Tests: Dict-like interface
# ---------------------------------------------------------------------------

class TestDictInterface:
    def test_getitem_existing_key(self, store, mock_redis):
        mock_redis.hgetall.return_value = {
            "document_types": json.dumps({".pdf": "application/pdf"}),
        }
        result = store["document_types"]
        assert result == {".pdf": "application/pdf"}

    def test_getitem_missing_key_raises_keyerror(self, store, mock_redis):
        mock_redis.hgetall.return_value = {}
        with pytest.raises(KeyError):
            _ = store["nonexistent"]

    def test_setitem(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        store["document_types"] = {".pdf": "application/pdf"}
        pipe.hset.assert_called_once_with(
            "index_types:global", "document_types",
            json.dumps({".pdf": "application/pdf"})
        )
        pipe.expire.assert_called_once_with("index_types:global", DEFAULT_TTL)
        pipe.execute.assert_called_once()

    def test_contains_true(self, store, mock_redis):
        mock_redis.hexists.return_value = True
        assert "document_types" in store
        mock_redis.hexists.assert_called_once_with("index_types:global", "document_types")

    def test_contains_false(self, store, mock_redis):
        mock_redis.hexists.return_value = False
        assert "nonexistent" not in store

    def test_len(self, store, mock_redis):
        mock_redis.hlen.return_value = 3
        assert len(store) == 3
        mock_redis.hlen.assert_called_once_with("index_types:global")

    def test_bool_true_when_populated(self, store, mock_redis):
        mock_redis.hlen.return_value = 3
        assert bool(store) is True

    def test_bool_false_when_empty(self, store, mock_redis):
        mock_redis.hlen.return_value = 0
        assert bool(store) is False

    def test_get_existing_key(self, store, mock_redis):
        mock_redis.hget.return_value = json.dumps({".pdf": "application/pdf"})
        result = store.get("document_types")
        assert result == {".pdf": "application/pdf"}

    def test_get_missing_key_returns_default(self, store, mock_redis):
        mock_redis.hget.return_value = None
        assert store.get("nonexistent") is None
        assert store.get("nonexistent", {}) == {}

    def test_get_with_custom_default(self, store, mock_redis):
        mock_redis.hget.return_value = None
        assert store.get("missing", {"fallback": True}) == {"fallback": True}

    def test_get_malformed_json_returns_default(self, store, mock_redis):
        mock_redis.hget.return_value = "not-json{{{"
        assert store.get("broken", {}) == {}

    def test_get_handles_bytes(self, store, mock_redis):
        mock_redis.hget.return_value = b'{".pdf": "application/pdf"}'
        result = store.get("document_types")
        assert result == {".pdf": "application/pdf"}

    def test_items(self, store, mock_redis):
        mock_redis.hgetall.return_value = {
            "document_types": json.dumps({".pdf": "application/pdf"}),
            "code_types": json.dumps({".py": "text/x-python"}),
        }
        items = list(store.items())
        assert ("document_types", {".pdf": "application/pdf"}) in items
        assert ("code_types", {".py": "text/x-python"}) in items

    def test_values(self, store, mock_redis):
        mock_redis.hgetall.return_value = {
            "document_types": json.dumps({".pdf": "application/pdf"}),
        }
        vals = list(store.values())
        assert {".pdf": "application/pdf"} in vals

    def test_keys(self, store, mock_redis):
        mock_redis.hkeys.return_value = ["document_types", "image_types", "code_types"]
        result = store.keys()
        assert result == ["document_types", "image_types", "code_types"]

    def test_keys_handles_bytes(self, store, mock_redis):
        mock_redis.hkeys.return_value = [b"document_types", b"image_types"]
        result = store.keys()
        assert result == ["document_types", "image_types"]


# ---------------------------------------------------------------------------
# Tests: Integration-like scenarios
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_full_lifecycle(self, store, mock_redis):
        """Simulate: startup (set_all) → read (get) → api return (get_all)."""
        pipe = mock_redis.pipeline.return_value

        # 1. Startup: indexer emits file loaders
        store.set_all(SAMPLE_PAYLOAD)
        assert pipe.hset.call_count == 3

        # 2. RPC call: get_supported_index_documents
        mock_redis.hget.return_value = json.dumps(SAMPLE_PAYLOAD["document_types"])
        result = store.get("document_types", {})
        assert ".pdf" in result
        assert ".docx" in result

        # 3. API call: return full registry
        mock_redis.hgetall.return_value = {
            k: json.dumps(v) for k, v in SAMPLE_PAYLOAD.items()
        }
        all_types = store.get_all()
        assert len(all_types) == 3
        assert all_types["code_types"][".py"] == "text/x-python"

    def test_attachments_code_types_access(self, store, mock_redis):
        """Simulate utils/attachments.py accessing code_types via get()."""
        mock_redis.hget.return_value = json.dumps({".py": "text/x-python", ".js": "text/javascript"})
        code_types = store.get("code_types", {})
        assert ".py" in code_types
        assert code_types[".py"] == "text/x-python"

    def test_api_v2_direct_return(self, store, mock_redis):
        """Simulate api/v2/index_types.py: return self.module.index_types, 200."""
        mock_redis.hgetall.return_value = {
            "document_types": json.dumps({".pdf": "application/pdf"}),
            "image_types": json.dumps({".png": "image/png"}),
            "code_types": json.dumps({".py": "text/x-python"}),
        }
        # The API returns store directly — but it's serializable
        result = store.get_all()
        assert isinstance(result, dict)
        assert "document_types" in result
