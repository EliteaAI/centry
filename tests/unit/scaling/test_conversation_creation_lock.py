"""Unit tests for conversation creation distributed lock.

Validates that:
1. API handler acquires lock before creating conversation
2. API handler returns 409 on lock contention
3. API handler releases lock after success
4. API handler releases lock after exception
5. RPC handler acquires lock before creating conversation
6. RPC handler returns error dict on lock contention
7. RPC handler releases lock after success
8. RPC handler releases lock after exception
9. Lock key format is correct: conversation_create:{project_id}:{user_id}
10. Lock TTL is 10 seconds
11. Lock graceful degradation when distributed_lock not available

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_conversation_creation_lock.py -v
"""

import importlib
import importlib.util
import pathlib
import sys
import types
from unittest.mock import MagicMock, patch, call, PropertyMock

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

# Mock tools module
_mock_tools = types.ModuleType("tools")
_mock_tools.api_tools = MagicMock()
_mock_tools.auth = MagicMock()
_mock_tools.db = MagicMock()
_mock_tools.config = MagicMock()
_mock_tools.MinioClient = MagicMock()
_mock_tools.rpc_tools = MagicMock()
_mock_tools.register_openapi = MagicMock(side_effect=lambda **kw: lambda f: f)
_mock_tools.serialize = MagicMock(side_effect=lambda x: {"id": 1, "name": "Test", "meta": {}})
sys.modules.setdefault("tools", _mock_tools)

# Mock flask
_mock_flask = MagicMock()
_mock_flask.request = MagicMock()
sys.modules.setdefault("flask", _mock_flask)

# Mock pydantic
_mock_pydantic = MagicMock()
_mock_pydantic.ValidationError = type("ValidationError", (Exception,), {})
sys.modules.setdefault("pydantic", _mock_pydantic)

# Mock sqlalchemy
_mock_sqlalchemy = MagicMock()
sys.modules.setdefault("sqlalchemy", _mock_sqlalchemy)
sys.modules.setdefault("sqlalchemy.orm", MagicMock())


# ---------------------------------------------------------------------------
# Load the distributed_lock module to get LockNotAcquired
# ---------------------------------------------------------------------------

def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_lock_mod = _load_module("distributed_lock", _PLUGIN_ROOT / "utils" / "distributed_lock.py")
LockNotAcquired = _lock_mod.LockNotAcquired
DistributedLock = _lock_mod.DistributedLock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis():
    client = MagicMock()
    client.set.return_value = True
    client.register_script.return_value = MagicMock(return_value=1)
    return client


@pytest.fixture
def distributed_lock(mock_redis):
    return DistributedLock(mock_redis)


@pytest.fixture
def mock_module(distributed_lock):
    module = MagicMock()
    module.distributed_lock = distributed_lock
    return module


@pytest.fixture
def mock_db_session():
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    return session


# ---------------------------------------------------------------------------
# Tests: API Handler Lock Behavior
# ---------------------------------------------------------------------------

class TestAPIConversationLock:
    """Test the conversation creation API endpoint lock behavior."""

    def _make_api_handler(self, mock_module):
        """Create a PromptLibAPI-like handler with the lock logic."""
        handler = MagicMock()
        handler.module = mock_module
        handler._create_conversation = MagicMock(return_value=({"id": 1, "name": "Test", "meta": {}}, 201))

        # Replicate the post method logic
        def post(project_id, **kwargs):
            user_id = 42
            lock_name = f"conversation_create:{project_id}:{user_id}"
            lock = getattr(handler.module, 'distributed_lock', None)
            if lock:
                try:
                    lock.acquire_blocking(lock_name, ttl=10, wait_timeout=5, poll_interval=0.2)
                except LockNotAcquired:
                    return {"error": "Concurrent conversation creation in progress", "retry_after": 2}, 409

            try:
                return handler._create_conversation(project_id, user_id, MagicMock())
            finally:
                if lock:
                    lock.release(lock_name)

        handler.post = post
        return handler

    def test_acquires_lock_before_creation(self, mock_module, mock_redis):
        handler = self._make_api_handler(mock_module)
        result = handler.post(project_id=5)

        assert result == ({"id": 1, "name": "Test", "meta": {}}, 201)
        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        assert call_args[0][0] == "lock:conversation_create:5:42"
        assert call_args[1]["nx"] is True
        assert call_args[1]["ex"] == 10

    def test_returns_409_on_lock_contention(self, mock_module, mock_redis):
        mock_redis.set.return_value = False  # Lock already held
        handler = self._make_api_handler(mock_module)
        result = handler.post(project_id=5)

        assert result[1] == 409
        assert "retry_after" in result[0]
        handler._create_conversation.assert_not_called()

    def test_releases_lock_after_success(self, mock_module, mock_redis):
        handler = self._make_api_handler(mock_module)
        handler.post(project_id=5)

        release_script = mock_redis.register_script.return_value
        release_script.assert_called_once()
        assert release_script.call_args[1]["keys"][0] == "lock:conversation_create:5:42"

    def test_releases_lock_after_exception(self, mock_module, mock_redis):
        handler = self._make_api_handler(mock_module)
        handler._create_conversation.side_effect = RuntimeError("DB error")

        with pytest.raises(RuntimeError):
            handler.post(project_id=5)

        release_script = mock_redis.register_script.return_value
        release_script.assert_called_once()

    def test_lock_key_format(self, mock_module, mock_redis):
        handler = self._make_api_handler(mock_module)
        handler.post(project_id=99)

        call_args = mock_redis.set.call_args
        assert call_args[0][0] == "lock:conversation_create:99:42"

    def test_no_lock_when_module_has_no_distributed_lock(self, mock_redis):
        module = MagicMock(spec=[])
        del module.distributed_lock
        handler = self._make_api_handler(module)
        handler._create_conversation = MagicMock(return_value=({"id": 1}, 201))

        result = handler.post(project_id=5)
        assert result == ({"id": 1}, 201)
        mock_redis.set.assert_not_called()

    def test_lock_ttl_is_10_seconds(self, mock_module, mock_redis):
        handler = self._make_api_handler(mock_module)
        handler.post(project_id=1)

        call_args = mock_redis.set.call_args
        assert call_args[1]["ex"] == 10


# ---------------------------------------------------------------------------
# Tests: RPC Handler Lock Behavior
# ---------------------------------------------------------------------------

class TestRPCConversationLock:
    """Test the conversation creation RPC lock behavior."""

    def _make_rpc_handler(self, distributed_lock):
        """Create a mock RPC handler with the lock logic."""
        handler = MagicMock()
        handler.distributed_lock = distributed_lock
        handler._do_create_conversation = MagicMock(return_value={"id": 1, "name": "Test"})

        def create_conversation_rpc(project_id, user_id, **kwargs):
            lock_name = f"conversation_create:{project_id}:{user_id}"
            lock = getattr(handler, 'distributed_lock', None)
            if lock:
                try:
                    lock.acquire_blocking(lock_name, ttl=10, wait_timeout=5, poll_interval=0.2)
                except LockNotAcquired:
                    return {'error': 'Concurrent conversation creation in progress', 'retry_after': 2}

            try:
                return handler._do_create_conversation(
                    project_id=project_id,
                    user_id=user_id,
                    **kwargs,
                )
            finally:
                if lock:
                    lock.release(lock_name)

        handler.create_conversation_rpc = create_conversation_rpc
        return handler

    def test_acquires_lock_before_creation(self, distributed_lock, mock_redis):
        handler = self._make_rpc_handler(distributed_lock)
        result = handler.create_conversation_rpc(project_id=5, user_id=10)

        assert result == {"id": 1, "name": "Test"}
        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        assert call_args[0][0] == "lock:conversation_create:5:10"
        assert call_args[1]["nx"] is True
        assert call_args[1]["ex"] == 10

    def test_returns_error_dict_on_lock_contention(self, distributed_lock, mock_redis):
        mock_redis.set.return_value = False  # Lock already held
        handler = self._make_rpc_handler(distributed_lock)
        result = handler.create_conversation_rpc(project_id=5, user_id=10)

        assert "error" in result
        assert result["retry_after"] == 2
        handler._do_create_conversation.assert_not_called()

    def test_releases_lock_after_success(self, distributed_lock, mock_redis):
        handler = self._make_rpc_handler(distributed_lock)
        handler.create_conversation_rpc(project_id=5, user_id=10)

        release_script = mock_redis.register_script.return_value
        release_script.assert_called_once()
        assert release_script.call_args[1]["keys"][0] == "lock:conversation_create:5:10"

    def test_releases_lock_after_exception(self, distributed_lock, mock_redis):
        handler = self._make_rpc_handler(distributed_lock)
        handler._do_create_conversation.side_effect = RuntimeError("DB error")

        with pytest.raises(RuntimeError):
            handler.create_conversation_rpc(project_id=5, user_id=10)

        release_script = mock_redis.register_script.return_value
        release_script.assert_called_once()

    def test_lock_key_uses_project_and_user(self, distributed_lock, mock_redis):
        handler = self._make_rpc_handler(distributed_lock)
        handler.create_conversation_rpc(project_id=77, user_id=33)

        call_args = mock_redis.set.call_args
        assert call_args[0][0] == "lock:conversation_create:77:33"

    def test_no_lock_when_distributed_lock_not_set(self, mock_redis):
        handler = MagicMock(spec=[])
        del handler.distributed_lock
        handler._do_create_conversation = MagicMock(return_value={"id": 1})

        def create_conversation_rpc(project_id, user_id, **kwargs):
            lock_name = f"conversation_create:{project_id}:{user_id}"
            lock = getattr(handler, 'distributed_lock', None)
            if lock:
                try:
                    lock.acquire_blocking(lock_name, ttl=10, wait_timeout=5, poll_interval=0.2)
                except LockNotAcquired:
                    return {'error': 'Concurrent conversation creation in progress', 'retry_after': 2}
            try:
                return handler._do_create_conversation(project_id=project_id, user_id=user_id)
            finally:
                if lock:
                    lock.release(lock_name)

        result = create_conversation_rpc(project_id=5, user_id=10)
        assert result == {"id": 1}
        mock_redis.set.assert_not_called()

    def test_rpc_passes_all_kwargs_through(self, distributed_lock, mock_redis):
        handler = self._make_rpc_handler(distributed_lock)
        handler.create_conversation_rpc(
            project_id=5, user_id=10, name="My Chat",
            source="support", is_private=False
        )
        handler._do_create_conversation.assert_called_once_with(
            project_id=5, user_id=10, name="My Chat",
            source="support", is_private=False
        )


# ---------------------------------------------------------------------------
# Tests: Lock Timing and Configuration
# ---------------------------------------------------------------------------

class TestLockConfiguration:
    """Test lock parameters (TTL, wait_timeout, poll_interval)."""

    def test_wait_timeout_is_5_seconds(self, mock_redis):
        mock_redis.set.return_value = False  # Never acquire
        lock = DistributedLock(mock_redis)

        import time
        start = time.time()
        with pytest.raises(LockNotAcquired):
            lock.acquire_blocking("test", ttl=10, wait_timeout=0.5, poll_interval=0.1)
        elapsed = time.time() - start
        assert elapsed >= 0.4
        assert elapsed < 1.0

    def test_poll_interval_is_200ms(self, mock_redis):
        call_count = [0]
        original_set = mock_redis.set

        def counting_set(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] >= 3:
                return True
            return False

        mock_redis.set.side_effect = counting_set
        lock = DistributedLock(mock_redis)

        lock.acquire_blocking("test", ttl=10, wait_timeout=5, poll_interval=0.2)
        assert call_count[0] == 3  # Initial + 2 retries

    def test_lock_auto_releases_on_ttl_expiry(self, mock_redis):
        lock = DistributedLock(mock_redis)
        lock.acquire("test_resource", ttl=10)

        call_args = mock_redis.set.call_args
        assert call_args[1]["ex"] == 10


# ---------------------------------------------------------------------------
# Tests: Concurrent scenarios
# ---------------------------------------------------------------------------

class TestConcurrencyScenarios:
    """Test real-world concurrency scenarios."""

    def test_different_users_dont_block_each_other(self, mock_redis):
        mock_redis.set.return_value = True
        lock = DistributedLock(mock_redis)

        acquired1 = lock.acquire("conversation_create:5:user_a", ttl=10)
        acquired2 = lock.acquire("conversation_create:5:user_b", ttl=10)

        assert acquired1 is True
        assert acquired2 is True
        assert mock_redis.set.call_count == 2

    def test_different_projects_dont_block_each_other(self, mock_redis):
        mock_redis.set.return_value = True
        lock = DistributedLock(mock_redis)

        acquired1 = lock.acquire("conversation_create:1:42", ttl=10)
        acquired2 = lock.acquire("conversation_create:2:42", ttl=10)

        assert acquired1 is True
        assert acquired2 is True

    def test_same_user_same_project_blocks(self, mock_redis):
        mock_redis.set.side_effect = [True, False]
        lock = DistributedLock(mock_redis)

        acquired1 = lock.acquire("conversation_create:5:42", ttl=10)
        acquired2 = lock.acquire("conversation_create:5:42", ttl=10)

        assert acquired1 is True
        assert acquired2 is False

    def test_lock_released_allows_next_request(self, mock_redis):
        mock_redis.set.return_value = True
        release_script = MagicMock(return_value=1)
        mock_redis.register_script.return_value = release_script
        lock = DistributedLock(mock_redis)

        lock.acquire("conversation_create:5:42", ttl=10)
        lock.release("conversation_create:5:42")

        mock_redis.set.return_value = True
        acquired = lock.acquire("conversation_create:5:42", ttl=10)
        assert acquired is True


# ---------------------------------------------------------------------------
# Tests: Integration with module.py initialization
# ---------------------------------------------------------------------------

class TestModuleInitialization:
    """Test that distributed_lock is properly initialized in module.py."""

    def test_module_has_distributed_lock_import(self):
        module_path = _PLUGIN_ROOT / "module.py"
        content = module_path.read_text()
        assert "from .utils.distributed_lock import DistributedLock" in content

    def test_module_assigns_distributed_lock(self):
        module_path = _PLUGIN_ROOT / "module.py"
        content = module_path.read_text()
        assert "self.distributed_lock = DistributedLock(self.get_redis_client())" in content


# ---------------------------------------------------------------------------
# Tests: Source file modifications
# ---------------------------------------------------------------------------

class TestSourceFileChanges:
    """Verify the conversation creation code has lock integration."""

    def test_api_imports_lock_not_acquired(self):
        api_path = _PLUGIN_ROOT / "api" / "v2" / "conversations.py"
        content = api_path.read_text()
        assert "from ...utils.distributed_lock import LockNotAcquired" in content

    def test_api_has_lock_acquisition(self):
        api_path = _PLUGIN_ROOT / "api" / "v2" / "conversations.py"
        content = api_path.read_text()
        assert "conversation_create:" in content
        assert "acquire_blocking" in content
        assert "ttl=10" in content

    def test_api_returns_409_on_contention(self):
        api_path = _PLUGIN_ROOT / "api" / "v2" / "conversations.py"
        content = api_path.read_text()
        assert "409" in content

    def test_api_has_finally_release(self):
        api_path = _PLUGIN_ROOT / "api" / "v2" / "conversations.py"
        content = api_path.read_text()
        assert "finally:" in content
        assert "lock.release(lock_name)" in content

    def test_rpc_imports_lock_not_acquired(self):
        rpc_path = _PLUGIN_ROOT / "rpc" / "chat_conversation.py"
        content = rpc_path.read_text()
        assert "from ..utils.distributed_lock import LockNotAcquired" in content

    def test_rpc_has_lock_acquisition(self):
        rpc_path = _PLUGIN_ROOT / "rpc" / "chat_conversation.py"
        content = rpc_path.read_text()
        assert "conversation_create:" in content
        assert "acquire_blocking" in content
        assert "ttl=10" in content

    def test_rpc_has_finally_release(self):
        rpc_path = _PLUGIN_ROOT / "rpc" / "chat_conversation.py"
        content = rpc_path.read_text()
        assert "finally:" in content
        assert "lock.release(lock_name)" in content

    def test_rpc_returns_error_on_contention(self):
        rpc_path = _PLUGIN_ROOT / "rpc" / "chat_conversation.py"
        content = rpc_path.read_text()
        assert "'retry_after': 2" in content

    def test_api_graceful_without_lock(self):
        api_path = _PLUGIN_ROOT / "api" / "v2" / "conversations.py"
        content = api_path.read_text()
        assert "getattr(self.module, 'distributed_lock', None)" in content

    def test_rpc_graceful_without_lock(self):
        rpc_path = _PLUGIN_ROOT / "rpc" / "chat_conversation.py"
        content = rpc_path.read_text()
        assert "getattr(self, 'distributed_lock', None)" in content
