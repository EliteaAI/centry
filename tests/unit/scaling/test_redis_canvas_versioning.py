"""Tests for RedisCanvasVersioning — optimistic locking for canvas content in Redis."""

import importlib.util
import sys
from unittest.mock import MagicMock, patch, call
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module loading (same pattern as other scaling tests)
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parents[4] / "elitea_core" / "utils" / "redis_canvas_versioning.py"
_spec = importlib.util.spec_from_file_location("redis_canvas_versioning", _SRC)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["redis_canvas_versioning"] = _mod
_spec.loader.exec_module(_mod)

RedisCanvasVersioning = _mod.RedisCanvasVersioning
CanvasVersionConflict = _mod.CanvasVersionConflict
DEFAULT_TTL = _mod.DEFAULT_TTL
MAX_RETRIES = _mod.MAX_RETRIES
VERSION_KEY_SUFFIX = _mod.VERSION_KEY_SUFFIX


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_redis():
    client = MagicMock()
    client.get.return_value = None
    pipe = MagicMock()
    pipe.execute.return_value = [True, 1, True]
    pipe.__enter__ = MagicMock(return_value=pipe)
    pipe.__exit__ = MagicMock(return_value=False)
    client.pipeline.return_value = pipe
    return client


@pytest.fixture
def versioning(mock_redis):
    return RedisCanvasVersioning(mock_redis)


CANVAS_KEY = "canvas:42_abc-def-123"
VERSION_KEY = CANVAS_KEY + VERSION_KEY_SUFFIX


# ---------------------------------------------------------------------------
# Construction and key format tests
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_default_ttl(self, mock_redis):
        v = RedisCanvasVersioning(mock_redis)
        assert v._content_ttl == DEFAULT_TTL

    def test_custom_ttl(self, mock_redis):
        v = RedisCanvasVersioning(mock_redis, content_ttl=300)
        assert v._content_ttl == 300

    def test_version_key_suffix(self, versioning):
        assert versioning._version_key(CANVAS_KEY) == VERSION_KEY


# ---------------------------------------------------------------------------
# get_content tests
# ---------------------------------------------------------------------------


class TestGetContent:
    def test_returns_content_and_version(self, versioning, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = ["hello world", "5"]
        content, version = versioning.get_content(CANVAS_KEY)
        assert content == "hello world"
        assert version == 5

    def test_returns_none_content_when_missing(self, versioning, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [None, None]
        content, version = versioning.get_content(CANVAS_KEY)
        assert content is None
        assert version == 0

    def test_returns_zero_version_when_missing(self, versioning, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = ["content", None]
        content, version = versioning.get_content(CANVAS_KEY)
        assert content == "content"
        assert version == 0

    def test_pipeline_called_without_transaction(self, versioning, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [None, None]
        versioning.get_content(CANVAS_KEY)
        mock_redis.pipeline.assert_called_with(transaction=False)

    def test_get_commands_issued(self, versioning, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [None, None]
        versioning.get_content(CANVAS_KEY)
        pipe.get.assert_any_call(CANVAS_KEY)
        pipe.get.assert_any_call(VERSION_KEY)


# ---------------------------------------------------------------------------
# set_content_atomic — unconditional write (expected_version=None)
# ---------------------------------------------------------------------------


class TestUnconditionalSet:
    def test_sets_content_with_default_ttl(self, versioning, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [True, 1, True]
        result = versioning.set_content_atomic(CANVAS_KEY, "new content")
        assert result == 1
        pipe.set.assert_called_once_with(CANVAS_KEY, "new content", ex=DEFAULT_TTL)

    def test_increments_version(self, versioning, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [True, 3, True]
        result = versioning.set_content_atomic(CANVAS_KEY, "x")
        assert result == 3
        pipe.incr.assert_called_once_with(VERSION_KEY)

    def test_sets_version_ttl(self, versioning, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [True, 1, True]
        versioning.set_content_atomic(CANVAS_KEY, "x")
        pipe.expire.assert_called_once_with(VERSION_KEY, DEFAULT_TTL)

    def test_custom_ttl(self, versioning, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [True, 1, True]
        versioning.set_content_atomic(CANVAS_KEY, "x", ttl=600)
        pipe.set.assert_called_once_with(CANVAS_KEY, "x", ex=600)
        pipe.expire.assert_called_once_with(VERSION_KEY, 600)

    def test_pipeline_is_transactional(self, versioning, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [True, 1, True]
        versioning.set_content_atomic(CANVAS_KEY, "x")
        mock_redis.pipeline.assert_called_with(transaction=True)


# ---------------------------------------------------------------------------
# set_content_atomic — conditional write (expected_version provided)
# ---------------------------------------------------------------------------


class TestConditionalSet:
    def test_success_when_versions_match(self, mock_redis):
        pipe = MagicMock()
        pipe.get.return_value = "5"
        pipe.execute.return_value = [True, 6, True]
        pipe.__enter__ = MagicMock(return_value=pipe)
        pipe.__exit__ = MagicMock(return_value=False)
        mock_redis.pipeline.return_value = pipe

        v = RedisCanvasVersioning(mock_redis)
        result = v.set_content_atomic(CANVAS_KEY, "updated", expected_version=5)
        assert result == 6
        pipe.watch.assert_called_once_with(VERSION_KEY)
        pipe.multi.assert_called_once()

    def test_raises_conflict_when_version_mismatch(self, mock_redis):
        pipe = MagicMock()
        pipe.get.return_value = "7"
        pipe.__enter__ = MagicMock(return_value=pipe)
        pipe.__exit__ = MagicMock(return_value=False)
        mock_redis.pipeline.return_value = pipe

        v = RedisCanvasVersioning(mock_redis)
        with pytest.raises(CanvasVersionConflict) as exc_info:
            v.set_content_atomic(CANVAS_KEY, "x", expected_version=5)
        assert exc_info.value.current_version == 7
        assert exc_info.value.canvas_key == CANVAS_KEY
        pipe.unwatch.assert_called_once()

    def test_raises_conflict_on_watch_error(self, mock_redis):
        pipe = MagicMock()
        pipe.get.return_value = "5"

        class WatchError(Exception):
            pass

        pipe.execute.side_effect = WatchError("key modified")
        pipe.__enter__ = MagicMock(return_value=pipe)
        pipe.__exit__ = MagicMock(return_value=False)
        mock_redis.pipeline.return_value = pipe
        mock_redis.get.return_value = "6"

        v = RedisCanvasVersioning(mock_redis)
        with pytest.raises(CanvasVersionConflict) as exc_info:
            v.set_content_atomic(CANVAS_KEY, "x", expected_version=5)
        assert exc_info.value.current_version == 6

    def test_reraises_non_watch_errors(self, mock_redis):
        pipe = MagicMock()
        pipe.get.return_value = "5"
        pipe.execute.side_effect = ConnectionError("lost connection")
        pipe.__enter__ = MagicMock(return_value=pipe)
        pipe.__exit__ = MagicMock(return_value=False)
        mock_redis.pipeline.return_value = pipe

        v = RedisCanvasVersioning(mock_redis)
        with pytest.raises(ConnectionError):
            v.set_content_atomic(CANVAS_KEY, "x", expected_version=5)

    def test_version_zero_when_key_missing(self, mock_redis):
        pipe = MagicMock()
        pipe.get.return_value = None
        pipe.execute.return_value = [True, 1, True]
        pipe.__enter__ = MagicMock(return_value=pipe)
        pipe.__exit__ = MagicMock(return_value=False)
        mock_redis.pipeline.return_value = pipe

        v = RedisCanvasVersioning(mock_redis)
        result = v.set_content_atomic(CANVAS_KEY, "first", expected_version=0)
        assert result == 1

    def test_conflict_when_expected_zero_but_key_exists(self, mock_redis):
        pipe = MagicMock()
        pipe.get.return_value = "3"
        pipe.__enter__ = MagicMock(return_value=pipe)
        pipe.__exit__ = MagicMock(return_value=False)
        mock_redis.pipeline.return_value = pipe

        v = RedisCanvasVersioning(mock_redis)
        with pytest.raises(CanvasVersionConflict) as exc_info:
            v.set_content_atomic(CANVAS_KEY, "x", expected_version=0)
        assert exc_info.value.current_version == 3

    def test_sets_content_and_version_with_ttl(self, mock_redis):
        pipe = MagicMock()
        pipe.get.return_value = "2"
        pipe.execute.return_value = [True, 3, True]
        pipe.__enter__ = MagicMock(return_value=pipe)
        pipe.__exit__ = MagicMock(return_value=False)
        mock_redis.pipeline.return_value = pipe

        v = RedisCanvasVersioning(mock_redis, content_ttl=200)
        v.set_content_atomic(CANVAS_KEY, "content", expected_version=2, ttl=200)
        pipe.set.assert_called_once_with(CANVAS_KEY, "content", ex=200)
        pipe.incr.assert_called_once_with(VERSION_KEY)
        pipe.expire.assert_called_once_with(VERSION_KEY, 200)


# ---------------------------------------------------------------------------
# set_content_with_retry
# ---------------------------------------------------------------------------


class TestRetry:
    def test_unconditional_set_no_retry(self, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [True, 1, True]
        v = RedisCanvasVersioning(mock_redis)
        result = v.set_content_with_retry(CANVAS_KEY, "x")
        assert result == 1

    def test_retries_on_conflict(self, mock_redis):
        v = RedisCanvasVersioning(mock_redis)
        call_count = [0]
        original_set = v.set_content_atomic

        def mock_set(key, content, expected_version=None, ttl=None):
            call_count[0] += 1
            if call_count[0] == 1:
                raise CanvasVersionConflict(key, 2)
            return 3

        with patch.object(v, "set_content_atomic", side_effect=mock_set):
            result = v.set_content_with_retry(CANVAS_KEY, "x", expected_version=1)
        assert result == 3
        assert call_count[0] == 2

    def test_exhausts_retries_and_raises(self, mock_redis):
        v = RedisCanvasVersioning(mock_redis)

        def always_conflict(key, content, expected_version=None, ttl=None):
            raise CanvasVersionConflict(key, expected_version + 1 if expected_version else 1)

        with patch.object(v, "set_content_atomic", side_effect=always_conflict):
            with pytest.raises(CanvasVersionConflict):
                v.set_content_with_retry(CANVAS_KEY, "x", expected_version=1, max_retries=3)

    def test_updates_expected_version_on_retry(self, mock_redis):
        v = RedisCanvasVersioning(mock_redis)
        versions_seen = []

        def track_versions(key, content, expected_version=None, ttl=None):
            versions_seen.append(expected_version)
            if len(versions_seen) < 3:
                raise CanvasVersionConflict(key, expected_version + 1)
            return expected_version + 1

        with patch.object(v, "set_content_atomic", side_effect=track_versions):
            v.set_content_with_retry(CANVAS_KEY, "x", expected_version=1, max_retries=5)
        assert versions_seen == [1, 2, 3]

    def test_custom_max_retries(self, mock_redis):
        v = RedisCanvasVersioning(mock_redis)
        call_count = [0]

        def always_conflict(key, content, expected_version=None, ttl=None):
            call_count[0] += 1
            raise CanvasVersionConflict(key, call_count[0])

        with patch.object(v, "set_content_atomic", side_effect=always_conflict):
            with pytest.raises(CanvasVersionConflict):
                v.set_content_with_retry(CANVAS_KEY, "x", expected_version=0, max_retries=5)
        assert call_count[0] == 5


# ---------------------------------------------------------------------------
# delete_content
# ---------------------------------------------------------------------------


class TestDeleteContent:
    def test_deletes_both_keys(self, versioning, mock_redis):
        versioning.delete_content(CANVAS_KEY)
        mock_redis.delete.assert_called_once_with(CANVAS_KEY, VERSION_KEY)


# ---------------------------------------------------------------------------
# get_version
# ---------------------------------------------------------------------------


class TestGetVersion:
    def test_returns_version_int(self, versioning, mock_redis):
        mock_redis.get.return_value = "42"
        assert versioning.get_version(CANVAS_KEY) == 42

    def test_returns_zero_when_missing(self, versioning, mock_redis):
        mock_redis.get.return_value = None
        assert versioning.get_version(CANVAS_KEY) == 0


# ---------------------------------------------------------------------------
# refresh_ttl
# ---------------------------------------------------------------------------


class TestRefreshTTL:
    def test_refreshes_both_keys(self, versioning, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [True, True]
        versioning.refresh_ttl(CANVAS_KEY)
        pipe.expire.assert_any_call(CANVAS_KEY, DEFAULT_TTL)
        pipe.expire.assert_any_call(VERSION_KEY, DEFAULT_TTL)

    def test_custom_ttl(self, versioning, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [True, True]
        versioning.refresh_ttl(CANVAS_KEY, ttl=500)
        pipe.expire.assert_any_call(CANVAS_KEY, 500)
        pipe.expire.assert_any_call(VERSION_KEY, 500)

    def test_non_transactional_pipeline(self, versioning, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [True, True]
        versioning.refresh_ttl(CANVAS_KEY)
        mock_redis.pipeline.assert_called_with(transaction=False)


# ---------------------------------------------------------------------------
# CanvasVersionConflict exception
# ---------------------------------------------------------------------------


class TestCanvasVersionConflict:
    def test_stores_attributes(self):
        e = CanvasVersionConflict("canvas:1_abc", 5)
        assert e.canvas_key == "canvas:1_abc"
        assert e.current_version == 5

    def test_message_format(self):
        e = CanvasVersionConflict("canvas:1_abc", 5)
        assert "canvas:1_abc" in str(e)
        assert "5" in str(e)

    def test_inherits_from_exception(self):
        e = CanvasVersionConflict("k", 1)
        assert isinstance(e, Exception)


# ---------------------------------------------------------------------------
# Integration-style: test full flow with mock Redis
# ---------------------------------------------------------------------------


class TestIntegrationFlow:
    def test_read_then_write_success(self, mock_redis):
        """Simulate: read version → write with expected version → success."""
        v = RedisCanvasVersioning(mock_redis)

        # First call: pipeline for get_content
        get_pipe = MagicMock()
        get_pipe.execute.return_value = ["old content", "3"]

        # Second call: pipeline for conditional set
        set_pipe = MagicMock()
        set_pipe.get.return_value = "3"
        set_pipe.execute.return_value = [True, 4, True]
        set_pipe.__enter__ = MagicMock(return_value=set_pipe)
        set_pipe.__exit__ = MagicMock(return_value=False)

        mock_redis.pipeline.side_effect = [get_pipe, set_pipe]

        content, version = v.get_content(CANVAS_KEY)
        assert content == "old content"
        assert version == 3

        new_version = v.set_content_atomic(CANVAS_KEY, "new content", expected_version=3)
        assert new_version == 4

    def test_read_then_write_conflict(self, mock_redis):
        """Simulate: read version → another writer increments → write fails."""
        v = RedisCanvasVersioning(mock_redis)

        get_pipe = MagicMock()
        get_pipe.execute.return_value = ["old content", "3"]

        set_pipe = MagicMock()
        set_pipe.get.return_value = "4"  # Someone else wrote
        set_pipe.__enter__ = MagicMock(return_value=set_pipe)
        set_pipe.__exit__ = MagicMock(return_value=False)

        mock_redis.pipeline.side_effect = [get_pipe, set_pipe]

        content, version = v.get_content(CANVAS_KEY)
        assert version == 3

        with pytest.raises(CanvasVersionConflict) as exc_info:
            v.set_content_atomic(CANVAS_KEY, "new", expected_version=3)
        assert exc_info.value.current_version == 4

    def test_unconditional_then_conditional(self, mock_redis):
        """First write is unconditional, subsequent writes are conditional."""
        v = RedisCanvasVersioning(mock_redis)

        # Unconditional set
        pipe1 = MagicMock()
        pipe1.execute.return_value = [True, 1, True]
        mock_redis.pipeline.return_value = pipe1

        ver = v.set_content_atomic(CANVAS_KEY, "init")
        assert ver == 1

        # Conditional set
        pipe2 = MagicMock()
        pipe2.get.return_value = "1"
        pipe2.execute.return_value = [True, 2, True]
        pipe2.__enter__ = MagicMock(return_value=pipe2)
        pipe2.__exit__ = MagicMock(return_value=False)
        mock_redis.pipeline.return_value = pipe2

        ver2 = v.set_content_atomic(CANVAS_KEY, "edit", expected_version=1)
        assert ver2 == 2


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_default_ttl_value(self):
        assert DEFAULT_TTL == 120

    def test_max_retries_value(self):
        assert MAX_RETRIES == 3

    def test_version_key_suffix(self):
        assert VERSION_KEY_SUFFIX == ":version"
