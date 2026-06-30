"""Unit tests for RedisProviderHealth.

Validates that:
1. Top-level dict interface (__contains__, __getitem__, __setitem__, __iter__, __len__, __bool__, keys, items, values, get, clear)
2. Middle-level _ProviderDict proxy (__contains__, __getitem__, __setitem__, __len__, __bool__, __iter__, keys, items, values, get)
3. Inner-level _UrlDict proxy (__setitem__, __getitem__, __contains__, __len__, __bool__, __iter__, pop, values, keys, items, get)
4. Descriptor serialization with Pydantic model (model_dump_json / model_validate_json)
5. Descriptor serialization fallback to dict (json.dumps / json.loads)
6. TTL applied on writes
7. Handles both str and bytes Redis responses
8. Handles malformed JSON gracefully
9. SCAN-based project enumeration
10. Field separator correctly encodes provider_name + url
11. set_descriptor_model / refresh_ttl / get_flat_list helpers
12. Pop removes entry from Redis
13. clear removes all project keys

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_redis_provider_health.py -v
"""

import importlib
import importlib.util
import json
import pathlib
import sys
from unittest.mock import MagicMock, call, patch

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

_spec = importlib.util.spec_from_file_location(
    "redis_provider_health",
    str(_PLUGIN_ROOT / "utils" / "redis_provider_health.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["redis_provider_health"] = _mod
_spec.loader.exec_module(_mod)

RedisProviderHealth = _mod.RedisProviderHealth
DEFAULT_TTL = _mod.DEFAULT_TTL
FIELD_SEP = _mod.FIELD_SEP
_make_field = _mod._make_field
_parse_field = _mod._parse_field


# ---------------------------------------------------------------------------
# Mock descriptor model (mimics dynamically generated Pydantic model)
# ---------------------------------------------------------------------------

class MockDescriptor:
    """Simulates a Pydantic v2 model with model_dump_json/model_validate_json."""

    def __init__(self, name: str, service_location_url: str, extra: str = ""):
        self.name = name
        self.service_location_url = service_location_url
        self.extra = extra

    def model_dump_json(self) -> str:
        return json.dumps({
            "name": self.name,
            "service_location_url": self.service_location_url,
            "extra": self.extra,
        })

    @classmethod
    def model_validate_json(cls, data: str):
        d = json.loads(data)
        return cls(
            name=d["name"],
            service_location_url=d["service_location_url"],
            extra=d.get("extra", ""),
        )

    def __eq__(self, other):
        if not isinstance(other, MockDescriptor):
            return False
        return (self.name == other.name and
                self.service_location_url == other.service_location_url and
                self.extra == other.extra)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis():
    """Create a mock Redis client with a backing store for realistic behavior."""
    client = MagicMock()
    store = {}  # key -> {field: value}
    key_ttls = {}  # key -> ttl

    def hset(key, field=None, value=None, mapping=None):
        if key not in store:
            store[key] = {}
        if field is not None and value is not None:
            store[key][field] = value
        if mapping:
            store[key].update(mapping)

    def hget(key, field):
        return store.get(key, {}).get(field)

    def hgetall(key):
        return dict(store.get(key, {}))

    def hkeys(key):
        return list(store.get(key, {}).keys())

    def hexists(key, field):
        return field in store.get(key, {})

    def hdel(key, *fields):
        removed = 0
        if key in store:
            for f in fields:
                if f in store[key]:
                    del store[key][f]
                    removed += 1
        return removed

    def hlen(key):
        return len(store.get(key, {}))

    def exists(key):
        return key in store and len(store[key]) > 0

    def delete(*keys):
        removed = 0
        for k in keys:
            if k in store:
                del store[k]
                removed += 1
        return removed

    def expire(key, ttl):
        if key in store:
            key_ttls[key] = ttl
            return True
        return False

    def scan(cursor=0, match=None, count=100):
        import fnmatch
        matched = []
        for k in store:
            if match and fnmatch.fnmatch(k, match):
                matched.append(k)
        return (0, matched)

    def pipeline():
        pipe = MagicMock()
        commands = []

        def pipe_hset(key, field=None, value=None, mapping=None):
            commands.append(("hset", key, field, value, mapping))

        def pipe_expire(key, ttl):
            commands.append(("expire", key, ttl))

        def pipe_delete(key):
            commands.append(("delete", key))

        def pipe_execute():
            results = []
            for cmd in commands:
                if cmd[0] == "hset":
                    hset(cmd[1], field=cmd[2], value=cmd[3], mapping=cmd[4])
                    results.append(1)
                elif cmd[0] == "expire":
                    results.append(expire(cmd[1], cmd[2]))
                elif cmd[0] == "delete":
                    results.append(delete(cmd[1]))
            commands.clear()
            return results

        pipe.hset = pipe_hset
        pipe.expire = pipe_expire
        pipe.delete = pipe_delete
        pipe.execute = pipe_execute
        return pipe

    client.hset = hset
    client.hget = hget
    client.hgetall = hgetall
    client.hkeys = hkeys
    client.hexists = hexists
    client.hdel = hdel
    client.hlen = hlen
    client.exists = exists
    client.delete = delete
    client.expire = expire
    client.scan = scan
    client.pipeline = pipeline
    client._store = store
    client._key_ttls = key_ttls
    return client


@pytest.fixture
def present_store(mock_redis):
    return RedisProviderHealth(mock_redis, "present", descriptor_model=MockDescriptor)


@pytest.fixture
def unhealthy_store(mock_redis):
    return RedisProviderHealth(mock_redis, "unhealthy", descriptor_model=MockDescriptor)


@pytest.fixture
def store_no_model(mock_redis):
    return RedisProviderHealth(mock_redis, "present", descriptor_model=None)


def _add_entry(store, project_id, provider_name, url, descriptor):
    """Helper to add an entry through the nested interface."""
    store._set_entry(store._category, project_id, provider_name, url, descriptor)


# ---------------------------------------------------------------------------
# Tests: field helpers
# ---------------------------------------------------------------------------

class TestFieldHelpers:
    def test_make_field(self):
        result = _make_field("openai", "http://localhost:8080")
        assert result == f"openai{FIELD_SEP}http://localhost:8080"

    def test_parse_field(self):
        field = _make_field("openai", "http://localhost:8080")
        name, url = _parse_field(field)
        assert name == "openai"
        assert url == "http://localhost:8080"

    def test_parse_field_with_separator_in_url(self):
        url = f"http://host{FIELD_SEP}extra"
        field = _make_field("provider", url)
        name, parsed_url = _parse_field(field)
        assert name == "provider"
        assert parsed_url == url

    def test_parse_field_invalid(self):
        name, url = _parse_field("no_separator_here")
        assert name is None
        assert url is None


# ---------------------------------------------------------------------------
# Tests: Top-level dict interface
# ---------------------------------------------------------------------------

class TestTopLevelInterface:
    def test_empty_store(self, present_store):
        assert len(present_store) == 0
        assert not present_store
        assert list(present_store) == []
        assert present_store.keys() == []
        assert present_store.items() == []
        assert present_store.values() == []

    def test_contains_true(self, present_store):
        desc = MockDescriptor("openai", "http://localhost:8080")
        _add_entry(present_store, "proj1", "openai", "http://localhost:8080", desc)
        assert "proj1" in present_store

    def test_contains_false(self, present_store):
        assert "proj_nonexistent" not in present_store

    def test_getitem_existing(self, present_store):
        desc = MockDescriptor("openai", "http://localhost:8080")
        _add_entry(present_store, "proj1", "openai", "http://localhost:8080", desc)
        result = present_store["proj1"]
        assert result is not None

    def test_getitem_missing_raises(self, present_store):
        with pytest.raises(KeyError):
            _ = present_store["nonexistent"]

    def test_setitem(self, present_store):
        desc = MockDescriptor("azure", "http://azure:443")
        present_store["proj2"] = {"azure": {"http://azure:443": desc}}
        assert "proj2" in present_store

    def test_iter(self, present_store):
        desc1 = MockDescriptor("openai", "http://a:80")
        desc2 = MockDescriptor("azure", "http://b:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc1)
        _add_entry(present_store, "proj2", "azure", "http://b:80", desc2)
        projects = list(present_store)
        assert set(projects) == {"proj1", "proj2"}

    def test_len(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        _add_entry(present_store, "proj2", "openai", "http://a:80", desc)
        assert len(present_store) == 2

    def test_bool_true(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        assert present_store

    def test_get_existing(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        result = present_store.get("proj1")
        assert result is not None

    def test_get_missing_returns_default(self, present_store):
        assert present_store.get("nonexistent") is None
        assert present_store.get("nonexistent", "fallback") == "fallback"

    def test_clear(self, present_store, mock_redis):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        _add_entry(present_store, "proj2", "azure", "http://b:80", desc)
        present_store.clear()
        assert len(present_store) == 0
        assert "proj1" not in present_store
        assert "proj2" not in present_store

    def test_clear_empty(self, present_store):
        present_store.clear()
        assert len(present_store) == 0

    def test_keys_returns_project_ids(self, present_store):
        desc = MockDescriptor("x", "http://a:80")
        _add_entry(present_store, "proj1", "x", "http://a:80", desc)
        _add_entry(present_store, "proj2", "y", "http://b:80", desc)
        assert set(present_store.keys()) == {"proj1", "proj2"}

    def test_items(self, present_store):
        desc = MockDescriptor("x", "http://a:80")
        _add_entry(present_store, "proj1", "x", "http://a:80", desc)
        items_list = present_store.items()
        assert len(items_list) == 1
        assert items_list[0][0] == "proj1"

    def test_values(self, present_store):
        desc = MockDescriptor("x", "http://a:80")
        _add_entry(present_store, "proj1", "x", "http://a:80", desc)
        vals = present_store.values()
        assert len(vals) == 1


# ---------------------------------------------------------------------------
# Tests: _ProviderDict (middle level)
# ---------------------------------------------------------------------------

class TestProviderDict:
    def test_contains_true(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        provider_dict = present_store["proj1"]
        assert "openai" in provider_dict

    def test_contains_false(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        provider_dict = present_store["proj1"]
        assert "azure" not in provider_dict

    def test_getitem(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        provider_dict = present_store["proj1"]
        url_dict = provider_dict["openai"]
        assert url_dict is not None

    def test_getitem_missing_raises(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        provider_dict = present_store["proj1"]
        with pytest.raises(KeyError):
            _ = provider_dict["nonexistent"]

    def test_setitem(self, present_store):
        desc = MockDescriptor("azure", "http://b:443")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        provider_dict = present_store["proj1"]
        provider_dict["azure"] = {"http://b:443": desc}
        assert "azure" in provider_dict

    def test_len(self, present_store):
        desc1 = MockDescriptor("openai", "http://a:80")
        desc2 = MockDescriptor("azure", "http://b:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc1)
        _add_entry(present_store, "proj1", "azure", "http://b:80", desc2)
        provider_dict = present_store["proj1"]
        assert len(provider_dict) == 2

    def test_bool_true(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        provider_dict = present_store["proj1"]
        assert provider_dict

    def test_iter(self, present_store):
        desc1 = MockDescriptor("openai", "http://a:80")
        desc2 = MockDescriptor("azure", "http://b:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc1)
        _add_entry(present_store, "proj1", "azure", "http://b:80", desc2)
        provider_dict = present_store["proj1"]
        providers = list(provider_dict)
        assert set(providers) == {"openai", "azure"}

    def test_keys(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        provider_dict = present_store["proj1"]
        assert "openai" in provider_dict.keys()

    def test_items_returns_list(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        provider_dict = present_store["proj1"]
        items_list = provider_dict.items()
        assert len(items_list) == 1
        assert items_list[0][0] == "openai"

    def test_values_returns_url_dicts(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        provider_dict = present_store["proj1"]
        vals = provider_dict.values()
        assert len(vals) == 1

    def test_get_existing(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        provider_dict = present_store["proj1"]
        url_dict = provider_dict.get("openai")
        assert url_dict is not None

    def test_get_missing_returns_default(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        provider_dict = present_store["proj1"]
        assert provider_dict.get("missing") is None
        assert provider_dict.get("missing", "default") == "default"


# ---------------------------------------------------------------------------
# Tests: _UrlDict (inner level)
# ---------------------------------------------------------------------------

class TestUrlDict:
    def test_setitem_and_getitem(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        url_dict = present_store["proj1"]["openai"]
        result = url_dict["http://a:80"]
        assert result == desc

    def test_getitem_missing_raises(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        url_dict = present_store["proj1"]["openai"]
        with pytest.raises(KeyError):
            _ = url_dict["http://nonexistent:80"]

    def test_contains_true(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        url_dict = present_store["proj1"]["openai"]
        assert "http://a:80" in url_dict

    def test_contains_false(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        url_dict = present_store["proj1"]["openai"]
        assert "http://missing:80" not in url_dict

    def test_len(self, present_store):
        desc1 = MockDescriptor("openai", "http://a:80")
        desc2 = MockDescriptor("openai", "http://b:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc1)
        _add_entry(present_store, "proj1", "openai", "http://b:80", desc2)
        url_dict = present_store["proj1"]["openai"]
        assert len(url_dict) == 2

    def test_bool_true(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        url_dict = present_store["proj1"]["openai"]
        assert url_dict

    def test_bool_false(self, present_store, mock_redis):
        mock_redis._store["provider_health:present:proj1"] = {}
        # Force the project to exist but be empty
        mock_redis._store["provider_health:present:proj1"]["dummy"] = "x"
        del mock_redis._store["provider_health:present:proj1"]["dummy"]
        # The project key now exists but is empty. However our `exists` checks len > 0
        # Actually for this test, just ensure empty url dict returns False
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        # Pop to empty
        url_dict = present_store["proj1"]["openai"]
        url_dict.pop("http://a:80")
        # Now url_dict for "openai" should be empty
        assert not url_dict

    def test_iter(self, present_store):
        desc1 = MockDescriptor("openai", "http://a:80")
        desc2 = MockDescriptor("openai", "http://b:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc1)
        _add_entry(present_store, "proj1", "openai", "http://b:80", desc2)
        url_dict = present_store["proj1"]["openai"]
        urls = list(url_dict)
        assert set(urls) == {"http://a:80", "http://b:80"}

    def test_pop_existing(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        url_dict = present_store["proj1"]["openai"]
        result = url_dict.pop("http://a:80")
        assert result == desc
        assert "http://a:80" not in url_dict

    def test_pop_missing_with_default(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        url_dict = present_store["proj1"]["openai"]
        result = url_dict.pop("http://missing:80", None)
        assert result is None

    def test_pop_missing_raises(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        url_dict = present_store["proj1"]["openai"]
        with pytest.raises(KeyError):
            url_dict.pop("http://missing:80")

    def test_values(self, present_store):
        desc1 = MockDescriptor("openai", "http://a:80")
        desc2 = MockDescriptor("openai", "http://b:80", extra="v2")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc1)
        _add_entry(present_store, "proj1", "openai", "http://b:80", desc2)
        url_dict = present_store["proj1"]["openai"]
        values = list(url_dict.values())
        assert len(values) == 2
        assert desc1 in values
        assert desc2 in values

    def test_keys(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        url_dict = present_store["proj1"]["openai"]
        assert list(url_dict.keys()) == ["http://a:80"]

    def test_items(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        url_dict = present_store["proj1"]["openai"]
        items_list = list(url_dict.items())
        assert len(items_list) == 1
        assert items_list[0][0] == "http://a:80"
        assert items_list[0][1] == desc

    def test_get_existing(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        url_dict = present_store["proj1"]["openai"]
        result = url_dict.get("http://a:80")
        assert result == desc

    def test_get_missing_returns_default(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        url_dict = present_store["proj1"]["openai"]
        assert url_dict.get("http://missing:80") is None
        assert url_dict.get("http://missing:80", "fallback") == "fallback"


# ---------------------------------------------------------------------------
# Tests: Serialization
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_pydantic_model_serialization(self, present_store, mock_redis):
        desc = MockDescriptor("openai", "http://a:80", extra="test")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        # Check raw Redis value
        key = "provider_health:present:proj1"
        field = _make_field("openai", "http://a:80")
        raw = mock_redis._store[key][field]
        parsed = json.loads(raw)
        assert parsed["name"] == "openai"
        assert parsed["service_location_url"] == "http://a:80"
        assert parsed["extra"] == "test"

    def test_pydantic_model_deserialization(self, present_store):
        desc = MockDescriptor("openai", "http://a:80", extra="test")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        result = present_store["proj1"]["openai"]["http://a:80"]
        assert isinstance(result, MockDescriptor)
        assert result.name == "openai"
        assert result.extra == "test"

    def test_dict_fallback_serialization(self, store_no_model, mock_redis):
        desc_dict = {"name": "openai", "url": "http://a:80"}
        _add_entry(store_no_model, "proj1", "openai", "http://a:80", desc_dict)
        key = "provider_health:present:proj1"
        field = _make_field("openai", "http://a:80")
        raw = mock_redis._store[key][field]
        parsed = json.loads(raw)
        assert parsed["name"] == "openai"

    def test_dict_fallback_deserialization(self, store_no_model):
        desc_dict = {"name": "openai", "url": "http://a:80"}
        _add_entry(store_no_model, "proj1", "openai", "http://a:80", desc_dict)
        result = store_no_model["proj1"]["openai"]["http://a:80"]
        assert isinstance(result, dict)
        assert result["name"] == "openai"

    def test_malformed_json_returns_none(self, present_store, mock_redis):
        key = "provider_health:present:proj1"
        field = _make_field("openai", "http://a:80")
        mock_redis._store[key] = {field: "not valid json {{{"}
        url_dict = present_store["proj1"]["openai"]
        # get returns None for malformed
        result = url_dict.get("http://a:80")
        assert result is None

    def test_bytes_response_handling(self, mock_redis):
        """Test that bytes responses from Redis are decoded properly."""
        store = RedisProviderHealth(mock_redis, "present", descriptor_model=MockDescriptor)
        # Manually insert bytes (simulating decode_responses=False)
        key = "provider_health:present:proj1"
        field = _make_field("openai", "http://a:80")
        desc_json = json.dumps({"name": "openai", "service_location_url": "http://a:80", "extra": ""})
        mock_redis._store[key] = {field.encode(): desc_json.encode()}
        # hgetall returns bytes keys/values
        original_hgetall = mock_redis.hgetall
        mock_redis.hgetall = lambda k: {field.encode(): desc_json.encode()} if k == key else {}
        mock_redis.hkeys = lambda k: [field.encode()] if k == key else []
        mock_redis.exists = lambda k: k == key
        mock_redis.hget = lambda k, f: desc_json.encode() if k == key and f == field else None
        mock_redis.hexists = lambda k, f: k == key and f == field

        url_dict = store["proj1"]["openai"]
        values = list(url_dict.values())
        assert len(values) == 1
        assert values[0].name == "openai"

        # Restore
        mock_redis.hgetall = original_hgetall

    def test_descriptor_model_validate_failure_falls_to_dict(self, mock_redis):
        """When descriptor model validation fails, falls back to json.loads."""

        class FailingModel:
            @classmethod
            def model_validate_json(cls, data):
                raise ValueError("model validation failed")

        store = RedisProviderHealth(mock_redis, "present", descriptor_model=FailingModel)
        key = "provider_health:present:proj1"
        field = _make_field("openai", "http://a:80")
        desc_json = json.dumps({"name": "openai", "url": "http://a:80"})
        mock_redis._store[key] = {field: desc_json}

        result = store._get_entry("present", "proj1", "openai", "http://a:80")
        assert isinstance(result, dict)
        assert result["name"] == "openai"


# ---------------------------------------------------------------------------
# Tests: TTL behavior
# ---------------------------------------------------------------------------

class TestTTL:
    def test_ttl_set_on_write(self, present_store, mock_redis):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        key = "provider_health:present:proj1"
        assert mock_redis._key_ttls.get(key) == DEFAULT_TTL

    def test_custom_ttl(self, mock_redis):
        store = RedisProviderHealth(mock_redis, "present", ttl=600)
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(store, "proj1", "openai", "http://a:80", desc)
        key = "provider_health:present:proj1"
        assert mock_redis._key_ttls.get(key) == 600

    def test_refresh_ttl(self, present_store, mock_redis):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        result = present_store.refresh_ttl("proj1")
        assert result is True

    def test_refresh_ttl_missing_key(self, present_store):
        result = present_store.refresh_ttl("nonexistent")
        assert result is False


# ---------------------------------------------------------------------------
# Tests: set_descriptor_model
# ---------------------------------------------------------------------------

class TestSetDescriptorModel:
    def test_set_descriptor_model(self, store_no_model, mock_redis):
        desc_json = json.dumps({"name": "openai", "service_location_url": "http://a:80", "extra": ""})
        key = "provider_health:present:proj1"
        field = _make_field("openai", "http://a:80")
        mock_redis._store[key] = {field: desc_json}

        # Before setting model, returns dict
        result = store_no_model._get_entry("present", "proj1", "openai", "http://a:80")
        assert isinstance(result, dict)

        # After setting model, returns MockDescriptor
        store_no_model.set_descriptor_model(MockDescriptor)
        result = store_no_model._get_entry("present", "proj1", "openai", "http://a:80")
        assert isinstance(result, MockDescriptor)


# ---------------------------------------------------------------------------
# Tests: get_flat_list
# ---------------------------------------------------------------------------

class TestGetFlatList:
    def test_empty(self, present_store):
        result = present_store.get_flat_list()
        assert result == []

    def test_single_entry(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc)
        result = present_store.get_flat_list()
        assert len(result) == 1
        assert result[0]["project_id"] == "proj1"
        assert result[0]["provider_name"] == "openai"
        assert result[0]["service_location_url"] == "http://a:80"
        assert result[0]["descriptor"] == desc

    def test_multiple_entries(self, present_store):
        desc1 = MockDescriptor("openai", "http://a:80")
        desc2 = MockDescriptor("azure", "http://b:443")
        desc3 = MockDescriptor("openai", "http://c:80")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc1)
        _add_entry(present_store, "proj1", "azure", "http://b:443", desc2)
        _add_entry(present_store, "proj2", "openai", "http://c:80", desc3)
        result = present_store.get_flat_list()
        assert len(result) == 3
        project_ids = [r["project_id"] for r in result]
        assert "proj1" in project_ids
        assert "proj2" in project_ids


# ---------------------------------------------------------------------------
# Tests: Integration patterns (mimicking real usage)
# ---------------------------------------------------------------------------

class TestIntegrationPatterns:
    def test_init_provider_pattern(self, present_store, unhealthy_store):
        """Mimics providers.py init_provider pattern."""
        project_id = "42"
        provider_name = "openai"
        url = "http://provider:8080/v1"
        desc = MockDescriptor(provider_name, url)

        # Healthy path
        if project_id not in present_store:
            pass  # auto-creates on write
        present_store._set_entry("present", project_id, provider_name, url, desc)

        assert project_id in present_store
        assert provider_name in present_store[project_id]
        assert url in present_store[project_id][provider_name]
        assert present_store[project_id][provider_name][url] == desc

    def test_deinit_provider_pattern(self, present_store, unhealthy_store):
        """Mimics providers.py deinit_provider pattern."""
        project_id = "42"
        url1 = "http://provider:8080/v1"
        url2 = "http://provider:8081/v1"
        desc1 = MockDescriptor("openai", url1)
        desc2 = MockDescriptor("openai", url2)

        _add_entry(present_store, project_id, "openai", url1, desc1)
        _add_entry(present_store, project_id, "openai", url2, desc2)
        _add_entry(unhealthy_store, project_id, "openai", url1, desc1)
        _add_entry(unhealthy_store, project_id, "openai", url2, desc2)

        # Deinit pattern (pop url1 from both, url2 remains)
        if project_id in present_store:
            if "openai" in present_store[project_id]:
                present_store[project_id]["openai"].pop(url1, None)
        if project_id in unhealthy_store:
            if "openai" in unhealthy_store[project_id]:
                unhealthy_store[project_id]["openai"].pop(url1, None)

        assert url1 not in present_store[project_id]["openai"]
        assert url1 not in unhealthy_store[project_id]["openai"]
        # url2 still present
        assert url2 in present_store[project_id]["openai"]
        assert url2 in unhealthy_store[project_id]["openai"]

    def test_lookup_provider_pattern(self, present_store):
        """Mimics provider_lookup.py lookup_provider pattern."""
        import random

        desc1 = MockDescriptor("openai", "http://a:80")
        desc2 = MockDescriptor("openai", "http://b:80")
        _add_entry(present_store, "42", "openai", "http://a:80", desc1)
        _add_entry(present_store, "42", "openai", "http://b:80", desc2)

        projects = ["99", "42", "1"]
        result = None
        for project in projects:
            if project not in present_store:
                continue
            if "openai" not in present_store[project]:
                continue
            providers = present_store[project]["openai"]
            if not providers:
                continue
            result = random.choice(list(providers.values()))
            break

        assert result is not None
        assert result in [desc1, desc2]

    def test_admin_api_pattern(self, present_store, unhealthy_store):
        """Mimics admin.py GET pattern."""
        desc_healthy = MockDescriptor("openai", "http://a:80")
        desc_unhealthy = MockDescriptor("azure", "http://b:443")
        _add_entry(present_store, "42", "openai", "http://a:80", desc_healthy)
        _add_entry(unhealthy_store, "42", "azure", "http://b:443", desc_unhealthy)

        result = []
        for project_id in present_store:
            for provider_name in present_store[project_id]:
                for service_location_url in present_store[project_id][provider_name]:
                    result.append({
                        "project_id": project_id,
                        "provider_name": provider_name,
                        "service_location_url": service_location_url,
                        "healthy": True,
                    })

        for project_id in unhealthy_store:
            for provider_name in unhealthy_store[project_id]:
                for service_location_url in unhealthy_store[project_id][provider_name]:
                    result.append({
                        "project_id": project_id,
                        "provider_name": provider_name,
                        "service_location_url": service_location_url,
                        "healthy": False,
                    })

        assert len(result) == 2
        healthy = [r for r in result if r["healthy"]]
        unhealthy = [r for r in result if not r["healthy"]]
        assert len(healthy) == 1
        assert healthy[0]["provider_name"] == "openai"
        assert len(unhealthy) == 1
        assert unhealthy[0]["provider_name"] == "azure"

    def test_provider_hub_schemas_pattern(self, present_store):
        """Mimics provider_hub_schemas.py get_tool_schemas_provider_hub."""
        import random

        desc1 = MockDescriptor("openai", "http://a:80")
        desc2 = MockDescriptor("azure", "http://b:443")
        _add_entry(present_store, "42", "openai", "http://a:80", desc1)
        _add_entry(present_store, "42", "azure", "http://b:443", desc2)

        projects = ["42"]
        providers_found = []
        for project in projects:
            if project not in present_store:
                continue
            for provider_name, providers in present_store[project].items():
                if not providers:
                    continue
                provider = random.choice(list(providers.values()))
                providers_found.append((provider_name, provider))

        assert len(providers_found) == 2
        provider_names = [p[0] for p in providers_found]
        assert "openai" in provider_names
        assert "azure" in provider_names

    def test_clear_on_deinit(self, present_store, unhealthy_store):
        """Mimics module.py provider_hub_deinit pattern."""
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "42", "openai", "http://a:80", desc)
        _add_entry(unhealthy_store, "42", "openai", "http://a:80", desc)

        # hasattr check as in module.py
        if hasattr(present_store, 'clear'):
            present_store.clear()
        if hasattr(unhealthy_store, 'clear'):
            unhealthy_store.clear()

        assert len(present_store) == 0
        assert len(unhealthy_store) == 0


# ---------------------------------------------------------------------------
# Tests: Category separation
# ---------------------------------------------------------------------------

class TestCategorySeparation:
    def test_present_and_unhealthy_isolated(self, mock_redis):
        present = RedisProviderHealth(mock_redis, "present", descriptor_model=MockDescriptor)
        unhealthy = RedisProviderHealth(mock_redis, "unhealthy", descriptor_model=MockDescriptor)
        desc = MockDescriptor("openai", "http://a:80")

        _add_entry(present, "proj1", "openai", "http://a:80", desc)

        assert "proj1" in present
        assert "proj1" not in unhealthy

    def test_same_project_different_categories(self, mock_redis):
        present = RedisProviderHealth(mock_redis, "present", descriptor_model=MockDescriptor)
        unhealthy = RedisProviderHealth(mock_redis, "unhealthy", descriptor_model=MockDescriptor)
        desc_p = MockDescriptor("openai", "http://a:80", extra="healthy")
        desc_u = MockDescriptor("azure", "http://b:443", extra="sick")

        _add_entry(present, "proj1", "openai", "http://a:80", desc_p)
        _add_entry(unhealthy, "proj1", "azure", "http://b:443", desc_u)

        assert present["proj1"]["openai"]["http://a:80"].extra == "healthy"
        assert unhealthy["proj1"]["azure"]["http://b:443"].extra == "sick"
        assert "azure" not in present["proj1"]
        assert "openai" not in unhealthy["proj1"]


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_url_with_special_chars(self, present_store):
        url = "http://provider:8080/v1/chat?model=gpt-4&temp=0.7"
        desc = MockDescriptor("openai", url)
        _add_entry(present_store, "proj1", "openai", url, desc)
        result = present_store["proj1"]["openai"][url]
        assert result == desc

    def test_multiple_urls_same_provider(self, present_store):
        desc1 = MockDescriptor("openai", "http://primary:80")
        desc2 = MockDescriptor("openai", "http://secondary:80")
        desc3 = MockDescriptor("openai", "http://tertiary:80")
        _add_entry(present_store, "proj1", "openai", "http://primary:80", desc1)
        _add_entry(present_store, "proj1", "openai", "http://secondary:80", desc2)
        _add_entry(present_store, "proj1", "openai", "http://tertiary:80", desc3)
        url_dict = present_store["proj1"]["openai"]
        assert len(url_dict) == 3

    def test_project_id_as_integer_string(self, present_store):
        desc = MockDescriptor("openai", "http://a:80")
        _add_entry(present_store, "42", "openai", "http://a:80", desc)
        assert "42" in present_store
        assert present_store["42"]["openai"]["http://a:80"] == desc

    def test_empty_provider_name(self, present_store):
        desc = MockDescriptor("", "http://a:80")
        _add_entry(present_store, "proj1", "", "http://a:80", desc)
        assert "" in present_store["proj1"]

    def test_overwrite_descriptor(self, present_store):
        desc1 = MockDescriptor("openai", "http://a:80", extra="v1")
        desc2 = MockDescriptor("openai", "http://a:80", extra="v2")
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc1)
        _add_entry(present_store, "proj1", "openai", "http://a:80", desc2)
        result = present_store["proj1"]["openai"]["http://a:80"]
        assert result.extra == "v2"

    def test_scan_with_many_projects(self, present_store):
        desc = MockDescriptor("x", "http://a:80")
        for i in range(20):
            _add_entry(present_store, f"proj_{i}", "x", "http://a:80", desc)
        assert len(present_store) == 20
        assert set(present_store.keys()) == {f"proj_{i}" for i in range(20)}
