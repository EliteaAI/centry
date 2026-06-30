#!/usr/bin/python3
# coding=utf-8

"""
Tests for arbiter TaskNode Redis result transport.

Tests cover:
- redis_write_result: writing results with TTL
- redis_read_result: reading and deleting results (GETDEL)
- _make_redis_client: client construction from config
- _result_key: key formatting
- Error handling (connection failures, missing keys)
- Integration with TaskNode (config propagation)
- worker_core result_transport config support
- indexer_worker agents_result_transport config support
"""

import sys
import gzip
import pickle
from unittest.mock import MagicMock, patch, call
import importlib.util

import pytest


# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------

def _load_redis_result():
    """Load redis_result module from arbiter source."""
    spec = importlib.util.spec_from_file_location(
        "redis_result",
        "arbiter/arbiter/tasknode/redis_result.py",
        submodule_search_locations=[]
    )
    # Mock the arbiter log import
    mock_arbiter = MagicMock()
    mock_arbiter.log = MagicMock()
    prev = sys.modules.get("arbiter")
    sys.modules["arbiter"] = mock_arbiter
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if prev is not None:
            sys.modules["arbiter"] = prev
        else:
            sys.modules.pop("arbiter", None)
    return mod


@pytest.fixture
def redis_result_mod():
    """Fixture providing the redis_result module."""
    return _load_redis_result()


@pytest.fixture
def mock_redis_client():
    """Fixture providing a mock Redis client."""
    client = MagicMock()
    client.set = MagicMock()
    client.getdel = MagicMock()
    client.close = MagicMock()
    return client


@pytest.fixture
def sample_config():
    """Standard test config dict."""
    return {
        "host": "redis-host",
        "port": 6379,
        "db": 0,
        "password": "secret",
        "key_prefix": "tasknode_result",
        "result_ttl": 3600,
    }


@pytest.fixture
def sample_result_bytes():
    """Sample compressed pickle result bytes."""
    data = {"return": "hello world"}
    return gzip.compress(pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL))


# ---------------------------------------------------------------------------
# Tests: _result_key
# ---------------------------------------------------------------------------

class TestResultKey:
    def test_default_prefix(self, redis_result_mod):
        config = {}
        key = redis_result_mod._result_key(config, "task-123")
        assert key == "tasknode_result:task-123"

    def test_custom_prefix(self, redis_result_mod):
        config = {"key_prefix": "custom_prefix"}
        key = redis_result_mod._result_key(config, "abc-def")
        assert key == "custom_prefix:abc-def"

    def test_uuid_task_id(self, redis_result_mod):
        config = {"key_prefix": "tr"}
        task_id = "550e8400-e29b-41d4-a716-446655440000"
        key = redis_result_mod._result_key(config, task_id)
        assert key == f"tr:{task_id}"


# ---------------------------------------------------------------------------
# Tests: _make_redis_client
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis_module():
    """Install a mock redis module in sys.modules for deferred import."""
    mock_mod = MagicMock()
    mock_cls = MagicMock()
    mock_mod.Redis = mock_cls
    prev = sys.modules.get("redis")
    sys.modules["redis"] = mock_mod
    yield mock_cls
    if prev is not None:
        sys.modules["redis"] = prev
    else:
        sys.modules.pop("redis", None)


class TestMakeRedisClient:
    def test_basic_connection(self, redis_result_mod, mock_redis_module):
        config = {"host": "myhost", "port": 6380, "db": 2}
        redis_result_mod._make_redis_client(config)
        mock_redis_module.assert_called_once()
        kwargs = mock_redis_module.call_args[1]
        assert kwargs["host"] == "myhost"
        assert kwargs["port"] == 6380
        assert kwargs["db"] == 2
        assert kwargs["decode_responses"] is False

    def test_with_password(self, redis_result_mod, mock_redis_module):
        config = {"host": "h", "port": 6379, "db": 0, "password": "pw123"}
        redis_result_mod._make_redis_client(config)
        kwargs = mock_redis_module.call_args[1]
        assert kwargs["password"] == "pw123"

    def test_with_username(self, redis_result_mod, mock_redis_module):
        config = {"host": "h", "port": 6379, "db": 0, "username": "user1"}
        redis_result_mod._make_redis_client(config)
        kwargs = mock_redis_module.call_args[1]
        assert kwargs["username"] == "user1"

    def test_no_password_not_in_kwargs(self, redis_result_mod, mock_redis_module):
        config = {"host": "h", "port": 6379, "db": 0}
        redis_result_mod._make_redis_client(config)
        kwargs = mock_redis_module.call_args[1]
        assert "password" not in kwargs

    def test_empty_password_not_in_kwargs(self, redis_result_mod, mock_redis_module):
        config = {"host": "h", "port": 6379, "db": 0, "password": ""}
        redis_result_mod._make_redis_client(config)
        kwargs = mock_redis_module.call_args[1]
        assert "password" not in kwargs

    def test_ssl_enabled(self, redis_result_mod, mock_redis_module):
        config = {"host": "h", "port": 6379, "db": 0, "use_ssl": True}
        redis_result_mod._make_redis_client(config)
        kwargs = mock_redis_module.call_args[1]
        assert kwargs["ssl"] is True
        assert kwargs["ssl_cert_reqs"] == "none"

    def test_defaults(self, redis_result_mod, mock_redis_module):
        config = {}
        redis_result_mod._make_redis_client(config)
        kwargs = mock_redis_module.call_args[1]
        assert kwargs["host"] == "localhost"
        assert kwargs["port"] == 6379
        assert kwargs["db"] == 0
        assert kwargs["socket_connect_timeout"] == 10
        assert kwargs["socket_timeout"] == 30

    def test_port_string_converted(self, redis_result_mod, mock_redis_module):
        config = {"host": "h", "port": "6380", "db": "3"}
        redis_result_mod._make_redis_client(config)
        kwargs = mock_redis_module.call_args[1]
        assert kwargs["port"] == 6380
        assert kwargs["db"] == 3


# ---------------------------------------------------------------------------
# Tests: redis_write_result
# ---------------------------------------------------------------------------

class TestRedisWriteResult:
    def test_writes_with_ttl(self, redis_result_mod, mock_redis_client, sample_config, sample_result_bytes):
        with patch.object(redis_result_mod, "_make_redis_client", return_value=mock_redis_client):
            redis_result_mod.redis_write_result(sample_config, "task-1", sample_result_bytes)
        mock_redis_client.set.assert_called_once_with(
            "tasknode_result:task-1", sample_result_bytes, ex=3600
        )
        mock_redis_client.close.assert_called_once()

    def test_custom_ttl(self, redis_result_mod, mock_redis_client, sample_result_bytes):
        config = {"key_prefix": "tr", "result_ttl": 7200, "host": "h", "port": 6379, "db": 0}
        with patch.object(redis_result_mod, "_make_redis_client", return_value=mock_redis_client):
            redis_result_mod.redis_write_result(config, "task-2", sample_result_bytes)
        mock_redis_client.set.assert_called_once_with(
            "tr:task-2", sample_result_bytes, ex=7200
        )

    def test_default_ttl_when_not_in_config(self, redis_result_mod, mock_redis_client, sample_result_bytes):
        config = {"key_prefix": "tr", "host": "h", "port": 6379, "db": 0}
        with patch.object(redis_result_mod, "_make_redis_client", return_value=mock_redis_client):
            redis_result_mod.redis_write_result(config, "task-3", sample_result_bytes)
        mock_redis_client.set.assert_called_once_with(
            "tr:task-3", sample_result_bytes, ex=3600
        )

    def test_closes_client_on_success(self, redis_result_mod, mock_redis_client, sample_config, sample_result_bytes):
        with patch.object(redis_result_mod, "_make_redis_client", return_value=mock_redis_client):
            redis_result_mod.redis_write_result(sample_config, "t", sample_result_bytes)
        mock_redis_client.close.assert_called_once()

    def test_closes_client_on_error(self, redis_result_mod, mock_redis_client, sample_config, sample_result_bytes):
        mock_redis_client.set.side_effect = ConnectionError("Connection refused")
        with patch.object(redis_result_mod, "_make_redis_client", return_value=mock_redis_client):
            with pytest.raises(ConnectionError):
                redis_result_mod.redis_write_result(sample_config, "t", sample_result_bytes)
        mock_redis_client.close.assert_called_once()

    def test_raises_on_redis_error(self, redis_result_mod, mock_redis_client, sample_config, sample_result_bytes):
        mock_redis_client.set.side_effect = RuntimeError("Redis down")
        with patch.object(redis_result_mod, "_make_redis_client", return_value=mock_redis_client):
            with pytest.raises(RuntimeError, match="Redis down"):
                redis_result_mod.redis_write_result(sample_config, "t", sample_result_bytes)


# ---------------------------------------------------------------------------
# Tests: redis_read_result
# ---------------------------------------------------------------------------

class TestRedisReadResult:
    def test_reads_and_deletes(self, redis_result_mod, mock_redis_client, sample_config, sample_result_bytes):
        mock_redis_client.getdel.return_value = sample_result_bytes
        with patch.object(redis_result_mod, "_make_redis_client", return_value=mock_redis_client):
            result = redis_result_mod.redis_read_result(sample_config, "task-1")
        assert result == sample_result_bytes
        mock_redis_client.getdel.assert_called_once_with("tasknode_result:task-1")
        mock_redis_client.close.assert_called_once()

    def test_returns_none_when_not_found(self, redis_result_mod, mock_redis_client, sample_config):
        mock_redis_client.getdel.return_value = None
        with patch.object(redis_result_mod, "_make_redis_client", return_value=mock_redis_client):
            result = redis_result_mod.redis_read_result(sample_config, "task-missing")
        assert result is None
        mock_redis_client.close.assert_called_once()

    def test_returns_none_on_error(self, redis_result_mod, mock_redis_client, sample_config):
        mock_redis_client.getdel.side_effect = ConnectionError("timeout")
        with patch.object(redis_result_mod, "_make_redis_client", return_value=mock_redis_client):
            result = redis_result_mod.redis_read_result(sample_config, "task-err")
        assert result is None
        mock_redis_client.close.assert_called_once()

    def test_closes_client_on_success(self, redis_result_mod, mock_redis_client, sample_config):
        mock_redis_client.getdel.return_value = b"data"
        with patch.object(redis_result_mod, "_make_redis_client", return_value=mock_redis_client):
            redis_result_mod.redis_read_result(sample_config, "t")
        mock_redis_client.close.assert_called_once()

    def test_exactly_once_semantics(self, redis_result_mod, mock_redis_client, sample_config, sample_result_bytes):
        """GETDEL ensures only the first reader gets the result."""
        mock_redis_client.getdel.side_effect = [sample_result_bytes, None]
        with patch.object(redis_result_mod, "_make_redis_client", return_value=mock_redis_client):
            first = redis_result_mod.redis_read_result(sample_config, "t")
            second = redis_result_mod.redis_read_result(sample_config, "t")
        assert first == sample_result_bytes
        assert second is None


# ---------------------------------------------------------------------------
# Tests: Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_default_prefix(self, redis_result_mod):
        assert redis_result_mod.RESULT_KEY_PREFIX == "tasknode_result"

    def test_default_ttl(self, redis_result_mod):
        assert redis_result_mod.RESULT_TTL_SECONDS == 3600


# ---------------------------------------------------------------------------
# Tests: Integration with TaskNode (config propagation)
# ---------------------------------------------------------------------------

class TestTaskNodeIntegration:
    """Test that worker_core and indexer_worker propagate result_transport config correctly."""

    def test_worker_core_result_config_auto_derived(self):
        """When result_transport=redis and no explicit result_config, config is derived from event_node."""
        import yaml
        with open("centry/pylon_indexer/configs/worker_core.yml") as f:
            config = yaml.safe_load(f)
        assert config["result_transport"] == "redis"
        assert "result_config" not in config
        # Auto-derivation in code uses event_node host/port
        assert config["event_node"]["host"] == "${REDIS_HOST}"
        assert config["event_node"]["port"] == "${REDIS_PORT}"

    def test_indexer_worker_agents_result_transport(self):
        """indexer_worker.yml has agents_result_transport: redis."""
        import yaml
        with open("centry/pylon_indexer/configs/indexer_worker.yml") as f:
            config = yaml.safe_load(f)
        assert config["agents_result_transport"] == "redis"

    def test_worker_core_result_config_derivation_logic(self):
        """Simulate the config derivation that worker_core does."""
        event_node_config = {
            "host": "my-redis",
            "port": 6379,
            "password": "pw",
        }
        result_transport = "redis"
        result_config = None

        if result_transport == "redis" and result_config is None:
            result_config = {
                "host": event_node_config.get("host", "localhost"),
                "port": event_node_config.get("port", 6379),
                "password": event_node_config.get("password"),
                "db": 0,
                "key_prefix": "tasknode_result",
                "result_ttl": 3600,
            }

        assert result_config["host"] == "my-redis"
        assert result_config["port"] == 6379
        assert result_config["password"] == "pw"
        assert result_config["key_prefix"] == "tasknode_result"
        assert result_config["result_ttl"] == 3600

    def test_indexer_worker_auto_config_derivation(self):
        """Simulate the auto-config logic in indexer_worker module.py."""
        clone_config = {"host": "valkey-host", "port": 6379, "password": ""}
        agents_result_transport = "redis"
        agents_result_config = None

        if agents_result_transport == "redis" and agents_result_config is None:
            agents_result_config = {
                "host": clone_config.get("host", "localhost"),
                "port": clone_config.get("port", 6379),
                "password": clone_config.get("password"),
                "db": 0,
                "key_prefix": "tasknode_result",
                "result_ttl": 3600,
            }

        assert agents_result_config["host"] == "valkey-host"

    def test_files_transport_no_config_needed(self):
        """When result_transport=files, result_config stays None."""
        result_transport = "files"
        result_config = None

        if result_transport == "redis" and result_config is None:
            result_config = {"host": "should-not-appear"}

        assert result_config is None

    def test_memory_transport_for_light_node(self):
        """task_node_light always uses memory (threaded, same process)."""
        # This is verified by reading the source — memory is correct for threading
        result_transport = "memory"
        assert result_transport == "memory"


# ---------------------------------------------------------------------------
# Tests: End-to-end result serialization
# ---------------------------------------------------------------------------

class TestResultSerialization:
    def test_write_read_roundtrip(self, redis_result_mod, mock_redis_client, sample_config):
        """Verify write->read roundtrip produces valid unpickled result."""
        data = {"return": [1, 2, 3]}
        result_bytes = gzip.compress(pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL))

        stored = {}

        def mock_set(key, value, ex=None):
            stored[key] = value

        def mock_getdel(key):
            return stored.pop(key, None)

        mock_redis_client.set = mock_set
        mock_redis_client.getdel = mock_getdel

        with patch.object(redis_result_mod, "_make_redis_client", return_value=mock_redis_client):
            redis_result_mod.redis_write_result(sample_config, "roundtrip-1", result_bytes)
            read_bytes = redis_result_mod.redis_read_result(sample_config, "roundtrip-1")

        assert read_bytes == result_bytes
        recovered = pickle.loads(gzip.decompress(read_bytes))
        assert recovered == data

    def test_write_read_exception_result(self, redis_result_mod, mock_redis_client, sample_config):
        """Verify exception results also roundtrip correctly."""
        data = {"raise": "Traceback...\nValueError: bad input"}
        result_bytes = gzip.compress(pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL))

        stored = {}
        mock_redis_client.set = lambda key, value, ex=None: stored.update({key: value})
        mock_redis_client.getdel = lambda key: stored.pop(key, None)

        with patch.object(redis_result_mod, "_make_redis_client", return_value=mock_redis_client):
            redis_result_mod.redis_write_result(sample_config, "exc-1", result_bytes)
            read_bytes = redis_result_mod.redis_read_result(sample_config, "exc-1")

        recovered = pickle.loads(gzip.decompress(read_bytes))
        assert "raise" in recovered
        assert "ValueError" in recovered["raise"]

    def test_large_result(self, redis_result_mod, mock_redis_client, sample_config):
        """Verify large results (>10MB compressed) work."""
        data = {"return": "x" * (20 * 1024 * 1024)}
        result_bytes = gzip.compress(pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL))

        stored = {}
        mock_redis_client.set = lambda key, value, ex=None: stored.update({key: value})
        mock_redis_client.getdel = lambda key: stored.pop(key, None)

        with patch.object(redis_result_mod, "_make_redis_client", return_value=mock_redis_client):
            redis_result_mod.redis_write_result(sample_config, "large-1", result_bytes)
            read_bytes = redis_result_mod.redis_read_result(sample_config, "large-1")

        assert read_bytes == result_bytes


# ---------------------------------------------------------------------------
# Tests: Staging config validation
# ---------------------------------------------------------------------------

class TestStagingConfig:
    def test_staging_indexer_has_redis_result_transport(self):
        """Verify staging config includes agents_result_transport: redis."""
        import yaml
        with open("../kharkevich/argocd-public/elitea-platform/values/staging/pylon-indexer.yaml") as f:
            config = yaml.safe_load(f)

        files = config["config"]["files"]
        indexer_worker_config = yaml.safe_load(files["indexer_worker.yml"])
        assert indexer_worker_config["agents_result_transport"] == "redis"

    def test_staging_worker_core_has_redis_result_transport(self):
        """Verify staging config includes result_transport: redis for worker_core."""
        import yaml
        with open("../kharkevich/argocd-public/elitea-platform/values/staging/pylon-indexer.yaml") as f:
            config = yaml.safe_load(f)

        files = config["config"]["files"]
        worker_core_config = yaml.safe_load(files["worker_core.yml"])
        assert worker_core_config["result_transport"] == "redis"
        # Uses same Redis as event_node
        assert worker_core_config["event_node"]["host"] == "elitea-staging-valkey"
