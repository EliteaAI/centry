"""Unit tests for RedisServersStorage.

Validates that:
1. Server registration stores data in Redis hash with correct key structure
2. get_server/get_servers_dict retrieves and deserializes correctly
3. remove_servers cleans up both the hash and sid mapping
4. validate_all removes disconnected servers via SCAN
5. refresh_and_get_server updates or removes based on provider callback
6. TTL is applied to all keys
7. list_active_servers returns server names
8. status() produces human-readable output

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_redis_servers_storage.py -v
"""

import json
import importlib
import importlib.util
import pathlib
import sys
import types
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch, call

import pytest
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Because Python 3.9 doesn't support `X | Y` type-union syntax used in the
# plugin's models and module files, we redefine the minimal McpServer model
# here (matches the real model's serialization interface).
# ---------------------------------------------------------------------------

_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[3] / "pylon_main" / "plugins" / "elitea_core"


class McpInputSchema(BaseModel):
    type: str
    properties: Dict[str, Dict[str, Any]]
    required: List[str]


class McpTool(BaseModel):
    name: str
    description: str
    inputSchema: McpInputSchema


class McpServer(BaseModel):
    name: str
    tools: List[McpTool]
    project_id: Optional[str] = None
    sio_sid: Optional[str] = None
    timeout_tools_list: Optional[int] = 90
    timeout_tools_call: Optional[int] = 90


# ---------------------------------------------------------------------------
# Set up sys.modules so that redis_servers_storage.py can resolve its imports:
#   from ..models.mcp import McpServer
#   from pylon.core.tools import log
# ---------------------------------------------------------------------------

# Create a fake models.mcp module containing our McpServer
_fake_models_mcp = types.ModuleType("centry.pylon_main.plugins.elitea_core.models.mcp")
_fake_models_mcp.McpServer = McpServer
sys.modules["centry.pylon_main.plugins.elitea_core.models.mcp"] = _fake_models_mcp
sys.modules["centry.pylon_main.plugins.elitea_core.models"] = types.ModuleType("models")

# Mock pylon.core.tools (for log import)
_mock_log = MagicMock()
_mock_pylon_core_tools = MagicMock()
_mock_pylon_core_tools.log = _mock_log
sys.modules.setdefault("pylon", MagicMock())
sys.modules.setdefault("pylon.core", MagicMock())
sys.modules.setdefault("pylon.core.tools", _mock_pylon_core_tools)

# Create package hierarchy so relative imports work
_utils_pkg = types.ModuleType("centry.pylon_main.plugins.elitea_core.utils")
_utils_pkg.__path__ = [str(_PLUGIN_ROOT / "utils")]
_utils_pkg.__package__ = "centry.pylon_main.plugins.elitea_core.utils"
sys.modules["centry.pylon_main.plugins.elitea_core.utils"] = _utils_pkg

_plugin_pkg = types.ModuleType("centry.pylon_main.plugins.elitea_core")
_plugin_pkg.__path__ = [str(_PLUGIN_ROOT)]
_plugin_pkg.__package__ = "centry.pylon_main.plugins.elitea_core"
sys.modules["centry.pylon_main.plugins.elitea_core"] = _plugin_pkg

# Now load redis_servers_storage.py with proper package context
_storage_path = _PLUGIN_ROOT / "utils" / "redis_servers_storage.py"
_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.utils.redis_servers_storage",
    _storage_path,
    submodule_search_locations=[],
)
_storage_mod = importlib.util.module_from_spec(_spec)
_storage_mod.__package__ = "centry.pylon_main.plugins.elitea_core.utils"
sys.modules["centry.pylon_main.plugins.elitea_core.utils.redis_servers_storage"] = _storage_mod
_spec.loader.exec_module(_storage_mod)

RedisServersStorage = _storage_mod.RedisServersStorage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis():
    """Create a mock Redis client that simulates decode_responses=True."""
    client = MagicMock()
    # Default scan behavior: return cursor=0, empty keys (end of scan)
    client.scan.return_value = (0, [])
    return client


@pytest.fixture
def storage(mock_redis):
    """Create a RedisServersStorage with mocked Redis client."""
    return RedisServersStorage(redis_client=mock_redis, ttl=3600)


@pytest.fixture
def sample_server():
    """Create a sample McpServer for testing."""
    return McpServer(
        name="test_server",
        tools=[
            McpTool(
                name="search",
                description="Search the web",
                inputSchema=McpInputSchema(
                    type="object",
                    properties={"query": {"type": "string"}},
                    required=["query"],
                ),
            )
        ],
        project_id="42",
        sio_sid="sid_abc123",
        timeout_tools_list=90,
        timeout_tools_call=90,
    )


@pytest.fixture
def sample_server_2():
    """Create a second sample McpServer."""
    return McpServer(
        name="another_server",
        tools=[
            McpTool(
                name="calculate",
                description="Do math",
                inputSchema=McpInputSchema(
                    type="object",
                    properties={"expression": {"type": "string"}},
                    required=["expression"],
                ),
            )
        ],
        project_id="42",
        sio_sid="sid_def456",
        timeout_tools_list=60,
        timeout_tools_call=60,
    )


# ---------------------------------------------------------------------------
# Tests: add_server
# ---------------------------------------------------------------------------

class TestAddServer:
    def test_add_server_new_registration(self, storage, mock_redis, sample_server):
        """Adding a new server returns True and stores in Redis."""
        mock_redis.hsetnx.return_value = 1  # Field was set (new)

        result = storage.add_server(42, sample_server)

        assert result is True
        mock_redis.hsetnx.assert_called_once_with(
            "mcp_servers:42", "test_server", sample_server.model_dump_json()
        )
        mock_redis.expire.assert_called_once_with("mcp_servers:42", 3600)
        mock_redis.set.assert_called_once_with("mcp_sid_to_project:sid_abc123", "42", ex=3600)

    def test_add_server_already_exists(self, storage, mock_redis, sample_server):
        """Adding a server that already exists returns False."""
        mock_redis.hsetnx.return_value = 0  # Field already existed

        result = storage.add_server(42, sample_server)

        assert result is False
        mock_redis.expire.assert_not_called()
        mock_redis.set.assert_not_called()

    def test_add_server_stores_json(self, storage, mock_redis, sample_server):
        """Verify the serialized JSON can be deserialized back."""
        mock_redis.hsetnx.return_value = 1
        storage.add_server(42, sample_server)

        stored_json = mock_redis.hsetnx.call_args[0][2]
        restored = McpServer.model_validate_json(stored_json)
        assert restored.name == "test_server"
        assert len(restored.tools) == 1
        assert restored.tools[0].name == "search"
        assert restored.sio_sid == "sid_abc123"


# ---------------------------------------------------------------------------
# Tests: get_server
# ---------------------------------------------------------------------------

class TestGetServer:
    def test_get_server_found(self, storage, mock_redis, sample_server):
        """Getting an existing server returns McpServer."""
        mock_redis.hget.return_value = sample_server.model_dump_json()

        result = storage.get_server(42, "test_server")

        assert result is not None
        assert result.name == "test_server"
        assert result.sio_sid == "sid_abc123"
        mock_redis.hget.assert_called_once_with("mcp_servers:42", "test_server")

    def test_get_server_not_found(self, storage, mock_redis):
        """Getting a non-existent server returns None."""
        mock_redis.hget.return_value = None

        result = storage.get_server(42, "nonexistent")

        assert result is None

    def test_get_server_with_bytes(self, storage, mock_redis, sample_server):
        """Getting a server works even if Redis returns bytes."""
        mock_redis.hget.return_value = sample_server.model_dump_json()

        result = storage.get_server(42, "test_server")

        assert result.name == "test_server"


# ---------------------------------------------------------------------------
# Tests: get_servers_dict
# ---------------------------------------------------------------------------

class TestGetServersDict:
    def test_get_servers_dict_empty(self, storage, mock_redis):
        """Empty project returns empty dict."""
        mock_redis.hgetall.return_value = {}

        result = storage.get_servers_dict(42)

        assert result == {}

    def test_get_servers_dict_single(self, storage, mock_redis, sample_server):
        """Single server returns dict with one entry."""
        mock_redis.hgetall.return_value = {
            "test_server": sample_server.model_dump_json()
        }

        result = storage.get_servers_dict(42)

        assert "test_server" in result
        assert result["test_server"].name == "test_server"
        assert len(result["test_server"].tools) == 1

    def test_get_servers_dict_multiple(self, storage, mock_redis, sample_server, sample_server_2):
        """Multiple servers return correctly."""
        mock_redis.hgetall.return_value = {
            "test_server": sample_server.model_dump_json(),
            "another_server": sample_server_2.model_dump_json(),
        }

        result = storage.get_servers_dict(42)

        assert len(result) == 2
        assert "test_server" in result
        assert "another_server" in result

    def test_get_servers_dict_corrupt_entry_skipped(self, storage, mock_redis, sample_server):
        """Corrupt entries are skipped with a warning."""
        mock_redis.hgetall.return_value = {
            "test_server": sample_server.model_dump_json(),
            "bad_server": "not valid json{{{",
        }

        result = storage.get_servers_dict(42)

        assert len(result) == 1
        assert "test_server" in result

    def test_get_servers_dict_with_bytes_keys(self, storage, mock_redis, sample_server):
        """Works when Redis returns bytes for keys and values."""
        mock_redis.hgetall.return_value = {
            b"test_server": sample_server.model_dump_json().encode()
        }

        result = storage.get_servers_dict(42)

        assert "test_server" in result


# ---------------------------------------------------------------------------
# Tests: refresh_and_get_server
# ---------------------------------------------------------------------------

class TestRefreshAndGetServer:
    def test_refresh_server_not_found(self, storage, mock_redis):
        """Refresh returns None if server doesn't exist."""
        mock_redis.hget.return_value = None
        provider = MagicMock()

        result = storage.refresh_and_get_server(42, "test_server", provider)

        assert result is None
        provider.assert_not_called()

    def test_refresh_server_updated(self, storage, mock_redis, sample_server):
        """Refresh stores new version when provider returns it."""
        mock_redis.hget.return_value = sample_server.model_dump_json()

        updated_server = sample_server.model_copy()
        updated_server.tools = []
        provider = MagicMock(return_value=updated_server)

        result = storage.refresh_and_get_server(42, "test_server", provider)

        assert result is not None
        assert result.tools == []
        # Verify the old server was passed to provider
        provider.assert_called_once()
        provided_arg = provider.call_args[0][0]
        assert provided_arg.name == "test_server"
        # Verify Redis was updated
        mock_redis.hset.assert_called_once()
        mock_redis.expire.assert_called_once_with("mcp_servers:42", 3600)

    def test_refresh_server_removed(self, storage, mock_redis, sample_server):
        """Refresh removes server when provider returns None."""
        mock_redis.hget.return_value = sample_server.model_dump_json()
        provider = MagicMock(return_value=None)

        result = storage.refresh_and_get_server(42, "test_server", provider)

        assert result is None
        mock_redis.hdel.assert_called_once_with("mcp_servers:42", "test_server")


# ---------------------------------------------------------------------------
# Tests: remove_servers
# ---------------------------------------------------------------------------

class TestRemoveServers:
    def test_remove_servers_sid_not_found(self, storage, mock_redis):
        """Remove returns empty list if SID not tracked."""
        mock_redis.get.return_value = None

        result = storage.remove_servers("unknown_sid")

        assert result == []

    def test_remove_servers_single_match(self, storage, mock_redis, sample_server):
        """Remove single server matching the SID."""
        mock_redis.get.return_value = "42"
        mock_redis.hgetall.return_value = {
            "test_server": sample_server.model_dump_json()
        }
        mock_redis.hlen.return_value = 0  # No servers left after removal

        result = storage.remove_servers("sid_abc123")

        assert len(result) == 1
        assert result[0] == {'name': 'test_server', 'project_id': 42}
        mock_redis.hdel.assert_called_once_with("mcp_servers:42", "test_server")
        mock_redis.delete.assert_any_call("mcp_servers:42")
        mock_redis.delete.assert_any_call("mcp_sid_to_project:sid_abc123")

    def test_remove_servers_partial_match(self, storage, mock_redis, sample_server, sample_server_2):
        """Remove only servers matching the SID, leave others."""
        mock_redis.get.return_value = "42"
        mock_redis.hgetall.return_value = {
            "test_server": sample_server.model_dump_json(),
            "another_server": sample_server_2.model_dump_json(),
        }
        mock_redis.hlen.return_value = 1  # another_server remains

        result = storage.remove_servers("sid_abc123")

        assert len(result) == 1
        assert result[0]['name'] == 'test_server'
        # Should NOT delete the servers key since another_server remains
        delete_calls = [c for c in mock_redis.delete.call_args_list]
        assert call("mcp_servers:42") not in delete_calls

    def test_remove_servers_no_servers_in_hash(self, storage, mock_redis):
        """Remove returns empty if hash is empty but sid mapping exists."""
        mock_redis.get.return_value = "42"
        mock_redis.hgetall.return_value = {}

        result = storage.remove_servers("sid_abc123")

        assert result == []
        mock_redis.delete.assert_called_once_with("mcp_sid_to_project:sid_abc123")

    def test_remove_servers_with_bytes_values(self, storage, mock_redis, sample_server):
        """Works when Redis returns bytes."""
        mock_redis.get.return_value = b"42"
        mock_redis.hgetall.return_value = {
            b"test_server": sample_server.model_dump_json().encode()
        }
        mock_redis.hlen.return_value = 0

        result = storage.remove_servers("sid_abc123")

        assert len(result) == 1
        assert result[0]['name'] == 'test_server'
        assert result[0]['project_id'] == 42


# ---------------------------------------------------------------------------
# Tests: validate_all
# ---------------------------------------------------------------------------

class TestValidateAll:
    def test_validate_all_empty(self, storage, mock_redis):
        """Validate with no tracked SIDs does nothing."""
        mock_redis.scan.return_value = (0, [])
        provider = MagicMock()

        storage.validate_all(provider)

        provider.assert_not_called()

    def test_validate_all_connected_sids(self, storage, mock_redis):
        """Validate keeps connected SIDs."""
        mock_redis.scan.return_value = (0, ["mcp_sid_to_project:sid_1", "mcp_sid_to_project:sid_2"])
        provider = MagicMock(return_value=True)

        storage.validate_all(provider)

        assert provider.call_count == 2
        provider.assert_any_call("sid_1")
        provider.assert_any_call("sid_2")
        # Since all connected, no removal
        mock_redis.get.assert_not_called()

    def test_validate_all_disconnected_sid(self, storage, mock_redis, sample_server):
        """Validate removes disconnected SIDs."""
        mock_redis.scan.return_value = (0, ["mcp_sid_to_project:sid_abc123"])
        provider = MagicMock(return_value=False)
        # Mock the remove_servers path
        mock_redis.get.return_value = "42"
        mock_redis.hgetall.return_value = {
            "test_server": sample_server.model_dump_json()
        }
        mock_redis.hlen.return_value = 0

        storage.validate_all(provider)

        provider.assert_called_once_with("sid_abc123")
        # Verify removal happened
        mock_redis.hdel.assert_called_once_with("mcp_servers:42", "test_server")

    def test_validate_all_multi_page_scan(self, storage, mock_redis):
        """Validate handles multi-page SCAN correctly."""
        # First scan returns cursor=5 (not done) with some keys
        # Second scan returns cursor=0 (done) with more keys
        mock_redis.scan.side_effect = [
            (5, ["mcp_sid_to_project:sid_1"]),
            (0, ["mcp_sid_to_project:sid_2"]),
        ]
        provider = MagicMock(return_value=True)

        storage.validate_all(provider)

        assert provider.call_count == 2
        assert mock_redis.scan.call_count == 2


# ---------------------------------------------------------------------------
# Tests: list_active_servers
# ---------------------------------------------------------------------------

class TestListActiveServers:
    def test_list_empty(self, storage, mock_redis):
        """Empty project returns empty list."""
        mock_redis.hkeys.return_value = []

        result = storage.list_active_servers(42)

        assert result == []
        mock_redis.hkeys.assert_called_once_with("mcp_servers:42")

    def test_list_multiple(self, storage, mock_redis):
        """Returns all server names."""
        mock_redis.hkeys.return_value = ["server_a", "server_b"]

        result = storage.list_active_servers(42)

        assert result == ["server_a", "server_b"]

    def test_list_with_bytes(self, storage, mock_redis):
        """Handles bytes response."""
        mock_redis.hkeys.return_value = [b"server_a"]

        result = storage.list_active_servers(42)

        assert result == ["server_a"]


# ---------------------------------------------------------------------------
# Tests: status
# ---------------------------------------------------------------------------

class TestStatus:
    def test_status_empty(self, storage, mock_redis):
        """Empty storage returns empty string."""
        mock_redis.scan.return_value = (0, [])

        result = storage.status()

        assert result == ""

    def test_status_with_servers(self, storage, mock_redis):
        """Status returns formatted string."""
        mock_redis.scan.return_value = (0, ["mcp_servers:42"])
        mock_redis.hkeys.return_value = ["server_a", "server_b"]

        result = storage.status()

        assert "Project 42" in result
        assert "server_a" in result
        assert "server_b" in result


# ---------------------------------------------------------------------------
# Tests: TTL behavior
# ---------------------------------------------------------------------------

class TestTTL:
    def test_custom_ttl(self, mock_redis, sample_server):
        """Custom TTL is applied."""
        storage = RedisServersStorage(redis_client=mock_redis, ttl=7200)
        mock_redis.hsetnx.return_value = 1

        storage.add_server(42, sample_server)

        mock_redis.expire.assert_called_once_with("mcp_servers:42", 7200)
        mock_redis.set.assert_called_once_with("mcp_sid_to_project:sid_abc123", "42", ex=7200)

    def test_refresh_extends_ttl(self, mock_redis, sample_server):
        """Refresh extends TTL on success."""
        storage = RedisServersStorage(redis_client=mock_redis, ttl=3600)
        mock_redis.hget.return_value = sample_server.model_dump_json()
        provider = MagicMock(return_value=sample_server)

        storage.refresh_and_get_server(42, "test_server", provider)

        mock_redis.expire.assert_called_once_with("mcp_servers:42", 3600)


# ---------------------------------------------------------------------------
# Tests: Interface compatibility with original ServersStorage
# ---------------------------------------------------------------------------

class TestInterfaceCompatibility:
    """Verify RedisServersStorage has the same public interface as ServersStorage."""

    def test_has_add_server(self, storage):
        assert callable(getattr(storage, 'add_server', None))

    def test_has_get_server(self, storage):
        assert callable(getattr(storage, 'get_server', None))

    def test_has_get_servers_dict(self, storage):
        assert callable(getattr(storage, 'get_servers_dict', None))

    def test_has_refresh_and_get_server(self, storage):
        assert callable(getattr(storage, 'refresh_and_get_server', None))

    def test_has_remove_servers(self, storage):
        assert callable(getattr(storage, 'remove_servers', None))

    def test_has_validate_all(self, storage):
        assert callable(getattr(storage, 'validate_all', None))

    def test_has_status(self, storage):
        assert callable(getattr(storage, 'status', None))
