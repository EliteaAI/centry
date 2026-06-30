"""Unit tests for RedisAsrSessionStore and the Redis-integrated ASR SIO handler.

Validates:
1. Session creation persists config to Redis hash with TTL
2. Audio buffer chunks are base64-encoded in Redis list with LTRIM
3. Session recovery reconstructs full state from Redis
4. VAD state updates persist speech_detected/silent_frames/call_in_flight
5. TTL refresh prevents premature expiry during active sessions
6. Stale session eviction works via last_active comparison
7. Session count uses SCAN pattern
8. Buffer clear/get operations work correctly
9. ASR handler integration: start/stop/reconnect flow

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_redis_asr_store.py -v
"""

import base64
import importlib
import importlib.util
import pathlib
import struct
import sys
import time
import types
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Module loading setup — bypass pylon/arbiter dependency chain
# ---------------------------------------------------------------------------

_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[3] / "pylon_main" / "plugins" / "elitea_core"

# Mock pylon.core.tools
_mock_log = MagicMock()
_mock_web = MagicMock()
_mock_pylon_core_tools = MagicMock()
_mock_pylon_core_tools.log = _mock_log
_mock_pylon_core_tools.web = _mock_web
sys.modules.setdefault("pylon", MagicMock())
sys.modules.setdefault("pylon.core", MagicMock())
sys.modules.setdefault("pylon.core.tools", _mock_pylon_core_tools)

# Mock tools module
_mock_tools = MagicMock()
_mock_tools.auth = MagicMock()
sys.modules.setdefault("tools", _mock_tools)

# Create package hierarchy
_plugin_pkg = types.ModuleType("centry.pylon_main.plugins.elitea_core")
_plugin_pkg.__path__ = [str(_PLUGIN_ROOT)]
_plugin_pkg.__package__ = "centry.pylon_main.plugins.elitea_core"
sys.modules["centry.pylon_main.plugins.elitea_core"] = _plugin_pkg

_utils_pkg = types.ModuleType("centry.pylon_main.plugins.elitea_core.utils")
_utils_pkg.__path__ = [str(_PLUGIN_ROOT / "utils")]
_utils_pkg.__package__ = "centry.pylon_main.plugins.elitea_core.utils"
sys.modules["centry.pylon_main.plugins.elitea_core.utils"] = _utils_pkg

_sio_pkg = types.ModuleType("centry.pylon_main.plugins.elitea_core.sio")
_sio_pkg.__path__ = [str(_PLUGIN_ROOT / "sio")]
_sio_pkg.__package__ = "centry.pylon_main.plugins.elitea_core.sio"
sys.modules["centry.pylon_main.plugins.elitea_core.sio"] = _sio_pkg

# Create a fake SioEvents enum (sio_utils.py uses Python 3.10+ syntax)
from enum import Enum


class _StrEnum(str, Enum):
    ...


class _FakeSioEvents(_StrEnum):
    asr_start = "asr_start"
    asr_audio_chunk = "asr_audio_chunk"
    asr_stop = "asr_stop"
    asr_transcript_delta = "asr_transcript_delta"
    asr_transcript_done = "asr_transcript_done"
    asr_error = "asr_error"
    asr_speech_started = "asr_speech_started"
    asr_vad_flush = "asr_vad_flush"
    socket_validation_error = "socket_validation_error"


_sio_utils_mod = types.ModuleType("centry.pylon_main.plugins.elitea_core.utils.sio_utils")
_sio_utils_mod.__package__ = "centry.pylon_main.plugins.elitea_core.utils"
_sio_utils_mod.SioEvents = _FakeSioEvents
sys.modules["centry.pylon_main.plugins.elitea_core.utils.sio_utils"] = _sio_utils_mod

# Load redis_asr_store.py
_store_path = _PLUGIN_ROOT / "utils" / "redis_asr_store.py"
_store_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.utils.redis_asr_store",
    _store_path,
    submodule_search_locations=[],
)
_store_mod = importlib.util.module_from_spec(_store_spec)
_store_mod.__package__ = "centry.pylon_main.plugins.elitea_core.utils"
sys.modules["centry.pylon_main.plugins.elitea_core.utils.redis_asr_store"] = _store_mod
_store_spec.loader.exec_module(_store_mod)

RedisAsrSessionStore = _store_mod.RedisAsrSessionStore

# Load asr.py
_asr_path = _PLUGIN_ROOT / "sio" / "asr.py"
_asr_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.sio.asr",
    _asr_path,
    submodule_search_locations=[],
)
_asr_mod = importlib.util.module_from_spec(_asr_spec)
_asr_mod.__package__ = "centry.pylon_main.plugins.elitea_core.sio"
sys.modules["centry.pylon_main.plugins.elitea_core.sio.asr"] = _asr_mod
_asr_spec.loader.exec_module(_asr_mod)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis():
    """Create a mock Redis client simulating decode_responses=True."""
    client = MagicMock()
    client.pipeline.return_value = MagicMock()
    client.pipeline.return_value.execute.return_value = [True, True, True]
    client.pipeline.return_value.__enter__ = MagicMock(return_value=client.pipeline.return_value)
    client.pipeline.return_value.__exit__ = MagicMock(return_value=False)
    client.scan.return_value = (0, [])
    client.exists.return_value = 0
    client.hgetall.return_value = {}
    client.lrange.return_value = []
    client.llen.return_value = 0
    return client


@pytest.fixture
def store(mock_redis):
    """Create a RedisAsrSessionStore with mock client."""
    return RedisAsrSessionStore(mock_redis, ttl=300)


@pytest.fixture(autouse=True)
def reset_asr_module():
    """Reset the ASR module's global state between tests."""
    _asr_mod._sessions.clear()
    _asr_mod._redis_store = None
    yield
    _asr_mod._sessions.clear()
    _asr_mod._redis_store = None


# ---------------------------------------------------------------------------
# RedisAsrSessionStore tests
# ---------------------------------------------------------------------------

class TestCreateSession:
    def test_creates_whisper_session_with_config(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        store.create_session("sid1", "whisper", {
            "project_id": "proj_42",
            "project_llm_key": "key_abc",
            "model_name": "whisper-1",
            "language": "en",
        })
        pipe.hset.assert_called_once()
        call_args = pipe.hset.call_args
        assert call_args[1]["mapping"]["type"] == "whisper"
        assert call_args[1]["mapping"]["project_id"] == "proj_42"
        assert call_args[1]["mapping"]["model_name"] == "whisper-1"
        assert call_args[1]["mapping"]["speech_detected"] == "0"
        assert call_args[1]["mapping"]["call_in_flight"] == "0"

    def test_creates_realtime_session(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        store.create_session("sid2", "realtime", {
            "project_id": "proj_99",
            "project_llm_key": "key_xyz",
            "model_name": "gpt-4o-realtime-preview",
            "language": "fr",
        })
        call_args = pipe.hset.call_args
        assert call_args[1]["mapping"]["type"] == "realtime"
        assert call_args[1]["mapping"]["language"] == "fr"

    def test_sets_ttl_on_session(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        store.create_session("sid1", "whisper", {"project_id": "1"})
        pipe.expire.assert_called_once_with("asr_session:sid1", 300)

    def test_handles_missing_config_fields(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        store.create_session("sid1", "whisper", {})
        call_args = pipe.hset.call_args
        assert call_args[1]["mapping"]["project_id"] == ""
        assert call_args[1]["mapping"]["language"] == "en"


class TestGetSessionConfig:
    def test_returns_session_data(self, store, mock_redis):
        mock_redis.hgetall.return_value = {
            "type": "whisper",
            "project_id": "42",
            "model_name": "whisper-1",
            "language": "en",
            "speech_detected": "0",
            "silent_frames": "0",
            "call_in_flight": "0",
            "last_active": "1000.0",
            "created_at": "1000.0",
        }
        result = store.get_session_config("sid1")
        assert result["type"] == "whisper"
        assert result["project_id"] == "42"
        mock_redis.hgetall.assert_called_with("asr_session:sid1")

    def test_returns_empty_dict_for_missing_session(self, store, mock_redis):
        mock_redis.hgetall.return_value = {}
        result = store.get_session_config("nonexistent")
        assert result == {}

    def test_handles_bytes_from_non_decode_client(self, store, mock_redis):
        mock_redis.hgetall.return_value = {
            b"type": b"realtime",
            b"project_id": b"5",
        }
        result = store.get_session_config("sid_bytes")
        assert result["type"] == "realtime"
        assert result["project_id"] == "5"


class TestSessionExists:
    def test_returns_true_when_session_present(self, store, mock_redis):
        mock_redis.exists.return_value = 1
        assert store.session_exists("sid1") is True
        mock_redis.exists.assert_called_with("asr_session:sid1")

    def test_returns_false_when_missing(self, store, mock_redis):
        mock_redis.exists.return_value = 0
        assert store.session_exists("sid_gone") is False


class TestUpdateVadState:
    def test_updates_all_vad_fields(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        store.update_vad_state("sid1", speech_detected=True, silent_frames=3, call_in_flight=True)
        call_args = pipe.hset.call_args
        mapping = call_args[1]["mapping"]
        assert mapping["speech_detected"] == "1"
        assert mapping["silent_frames"] == "3"
        assert mapping["call_in_flight"] == "1"

    def test_refreshes_ttl(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        store.update_vad_state("sid1", False, 0, False)
        pipe.expire.assert_called_once_with("asr_session:sid1", 300)


class TestRefreshActivity:
    def test_updates_last_active_and_ttl(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        mock_redis.exists.return_value = 0
        store.refresh_activity("sid1")
        pipe.hset.assert_called_once()
        pipe.expire.assert_called_once_with("asr_session:sid1", 300)

    def test_refreshes_buffer_ttl_if_exists(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        mock_redis.exists.return_value = 1
        store.refresh_activity("sid1")
        mock_redis.expire.assert_called_with("asr_buffer:sid1", 300)


class TestBufferOperations:
    def test_append_chunk_base64_encodes(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pcm = b"\x00\x01\x02\x03"
        store.append_buffer_chunk("sid1", pcm)
        expected_encoded = base64.b64encode(pcm).decode("ascii")
        pipe.rpush.assert_called_once_with("asr_buffer:sid1", expected_encoded)

    def test_append_trims_to_max_chunks(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        store.append_buffer_chunk("sid1", b"\x00")
        pipe.ltrim.assert_called_once_with("asr_buffer:sid1", -200, -1)

    def test_append_sets_ttl(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        store.append_buffer_chunk("sid1", b"\x00")
        pipe.expire.assert_called_once_with("asr_buffer:sid1", 300)

    def test_get_buffer_concatenates_chunks(self, store, mock_redis):
        chunk1 = b"\x00\x01\x02\x03"
        chunk2 = b"\x04\x05\x06\x07"
        mock_redis.lrange.return_value = [
            base64.b64encode(chunk1).decode("ascii"),
            base64.b64encode(chunk2).decode("ascii"),
        ]
        result = store.get_buffer("sid1")
        assert result == chunk1 + chunk2
        mock_redis.lrange.assert_called_with("asr_buffer:sid1", 0, -1)

    def test_get_buffer_returns_empty_bytes_for_no_data(self, store, mock_redis):
        mock_redis.lrange.return_value = []
        assert store.get_buffer("sid1") == b""

    def test_get_buffer_handles_bytes_values(self, store, mock_redis):
        chunk = b"\x10\x20"
        mock_redis.lrange.return_value = [base64.b64encode(chunk)]
        result = store.get_buffer("sid1")
        assert result == chunk

    def test_get_buffer_size(self, store, mock_redis):
        mock_redis.llen.return_value = 42
        assert store.get_buffer_size("sid1") == 42
        mock_redis.llen.assert_called_with("asr_buffer:sid1")

    def test_clear_buffer(self, store, mock_redis):
        store.clear_buffer("sid1")
        mock_redis.delete.assert_called_with("asr_buffer:sid1")


class TestRemoveSession:
    def test_removes_session_and_buffer(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [1, 0]
        result = store.remove_session("sid1")
        assert result is True
        pipe.delete.assert_any_call("asr_session:sid1")
        pipe.delete.assert_any_call("asr_buffer:sid1")

    def test_returns_false_if_session_not_found(self, store, mock_redis):
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [0, 0]
        result = store.remove_session("ghost")
        assert result is False


class TestRecoverSession:
    def test_recovers_whisper_session_with_buffer(self, store, mock_redis):
        mock_redis.hgetall.return_value = {
            "type": "whisper",
            "project_id": "42",
            "project_llm_key": "key_abc",
            "model_name": "whisper-1",
            "language": "en",
            "speech_detected": "1",
            "silent_frames": "2",
            "call_in_flight": "0",
            "last_active": "1000.0",
            "created_at": "999.0",
        }
        chunk = b"\x00\x01\x02\x03"
        mock_redis.lrange.return_value = [base64.b64encode(chunk).decode("ascii")]

        recovered = store.recover_session("sid1")
        assert recovered["type"] == "whisper"
        assert recovered["buffer"] == chunk
        assert recovered["speech_detected"] is True
        assert recovered["silent_frames"] == 2
        assert recovered["call_in_flight"] is False

    def test_recovers_realtime_session_without_buffer(self, store, mock_redis):
        mock_redis.hgetall.return_value = {
            "type": "realtime",
            "project_id": "99",
            "model_name": "gpt-4o-realtime",
            "language": "fr",
            "last_active": "1000.0",
            "created_at": "999.0",
        }
        recovered = store.recover_session("sid2")
        assert recovered["type"] == "realtime"
        assert "buffer" not in recovered

    def test_returns_empty_dict_for_missing_session(self, store, mock_redis):
        mock_redis.hgetall.return_value = {}
        assert store.recover_session("ghost") == {}


class TestGetActiveSessionCount:
    def test_counts_sessions_across_scan_pages(self, store, mock_redis):
        mock_redis.scan.side_effect = [
            (5, ["asr_session:a", "asr_session:b"]),
            (0, ["asr_session:c"]),
        ]
        assert store.get_active_session_count() == 3

    def test_returns_zero_when_no_sessions(self, store, mock_redis):
        mock_redis.scan.return_value = (0, [])
        assert store.get_active_session_count() == 0


class TestEvictStaleSessions:
    def test_evicts_old_sessions(self, store, mock_redis):
        old_time = str(time.time() - 120)
        mock_redis.scan.return_value = (0, ["asr_session:stale_sid"])
        mock_redis.hget.return_value = old_time
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [1, 0]

        evicted = store.evict_stale_sessions(timeout_seconds=60)
        assert evicted == ["stale_sid"]

    def test_keeps_active_sessions(self, store, mock_redis):
        recent_time = str(time.time() - 10)
        mock_redis.scan.return_value = (0, ["asr_session:active_sid"])
        mock_redis.hget.return_value = recent_time

        evicted = store.evict_stale_sessions(timeout_seconds=60)
        assert evicted == []

    def test_handles_missing_last_active(self, store, mock_redis):
        mock_redis.scan.return_value = (0, ["asr_session:no_active"])
        mock_redis.hget.return_value = None

        evicted = store.evict_stale_sessions(timeout_seconds=60)
        assert evicted == []

    def test_handles_bytes_keys_in_scan(self, store, mock_redis):
        old_time = str(time.time() - 120)
        mock_redis.scan.return_value = (0, [b"asr_session:bytes_sid"])
        mock_redis.hget.return_value = old_time
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [1, 0]

        evicted = store.evict_stale_sessions(timeout_seconds=60)
        assert evicted == ["bytes_sid"]

    def test_multi_page_scan(self, store, mock_redis):
        old_time = str(time.time() - 120)
        mock_redis.scan.side_effect = [
            (5, ["asr_session:s1"]),
            (0, ["asr_session:s2"]),
        ]
        mock_redis.hget.return_value = old_time
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [1, 0]

        evicted = store.evict_stale_sessions(timeout_seconds=60)
        assert set(evicted) == {"s1", "s2"}

    def test_handles_bytes_last_active(self, store, mock_redis):
        old_time = str(time.time() - 120).encode()
        mock_redis.scan.return_value = (0, ["asr_session:b_sid"])
        mock_redis.hget.return_value = old_time
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [1, 0]

        evicted = store.evict_stale_sessions(timeout_seconds=60)
        assert evicted == ["b_sid"]


# ---------------------------------------------------------------------------
# ASR SIO handler integration tests
# ---------------------------------------------------------------------------

class TestAsrModuleIntegration:
    def test_init_redis_store(self, mock_redis):
        result = _asr_mod.init_redis_store(mock_redis, ttl=600)
        assert isinstance(result, RedisAsrSessionStore)
        assert _asr_mod._redis_store is result

    def test_get_redis_store_returns_initialized(self, mock_redis):
        _asr_mod.init_redis_store(mock_redis)
        assert _asr_mod.get_redis_store() is _asr_mod._redis_store

    def test_get_redis_store_returns_none_before_init(self):
        assert _asr_mod.get_redis_store() is None


class TestIsWhisperModel:
    def test_whisper_1(self):
        assert _asr_mod._is_whisper_model("whisper-1") is True

    def test_gpt_4o_transcribe(self):
        assert _asr_mod._is_whisper_model("gpt-4o-transcribe") is True

    def test_realtime_model(self):
        assert _asr_mod._is_whisper_model("gpt-4o-realtime-preview") is False

    def test_empty_string(self):
        assert _asr_mod._is_whisper_model("") is False

    def test_none(self):
        assert _asr_mod._is_whisper_model(None) is False


class TestFrameIsSpeech:
    def test_loud_frame_is_speech(self):
        samples = [1000, -1000, 500, -500]
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        assert _asr_mod._frame_is_speech(pcm) is True

    def test_quiet_frame_is_not_speech(self):
        samples = [100, -100, 50, -50]
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        assert _asr_mod._frame_is_speech(pcm) is False

    def test_empty_frame(self):
        assert _asr_mod._frame_is_speech(b"") is False

    def test_threshold_boundary(self):
        samples = [500, 0]
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        assert _asr_mod._frame_is_speech(pcm) is False

        samples = [501, 0]
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        assert _asr_mod._frame_is_speech(pcm) is True


class TestCloseSession:
    def test_closes_whisper_session_removes_from_redis(self, mock_redis):
        _asr_mod.init_redis_store(mock_redis)
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [1, 0]

        _asr_mod._sessions["sid_w"] = {
            "type": "whisper",
            "flush_timer": None,
            "lock": MagicMock(),
        }
        sio_handler = MagicMock()
        _asr_mod._close_session(sio_handler, "sid_w")
        assert "sid_w" not in _asr_mod._sessions
        pipe.delete.assert_any_call("asr_session:sid_w")

    def test_closes_realtime_session_emits_stop(self, mock_redis):
        _asr_mod.init_redis_store(mock_redis)
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [1, 0]

        sio_handler = MagicMock()
        _asr_mod._sessions["sid_r"] = {
            "type": "realtime",
            "event_node": sio_handler.event_node,
        }
        _asr_mod._close_session(sio_handler, "sid_r")
        sio_handler.event_node.emit.assert_called_with(
            "voice_events", {"type": "asr_stop", "sid": "sid_r"}
        )

    def test_close_nonexistent_session_only_removes_redis(self, mock_redis):
        _asr_mod.init_redis_store(mock_redis)
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [0, 0]
        sio_handler = MagicMock()
        _asr_mod._close_session(sio_handler, "ghost")
        pipe.delete.assert_any_call("asr_session:ghost")


class TestAsrEvictStaleSessions:
    def test_evicts_stale_local_sessions_and_redis(self, mock_redis):
        _asr_mod.init_redis_store(mock_redis)
        pipe = mock_redis.pipeline.return_value
        pipe.execute.return_value = [1, 0]

        _asr_mod._sessions["stale"] = {
            "type": "whisper",
            "last_active": time.monotonic() - 120,
            "flush_timer": None,
            "lock": MagicMock(),
        }
        _asr_mod._evict_stale_sessions()
        assert "stale" not in _asr_mod._sessions
        pipe.delete.assert_any_call("asr_session:stale")

    def test_keeps_active_sessions(self, mock_redis):
        _asr_mod.init_redis_store(mock_redis)
        _asr_mod._sessions["active"] = {
            "type": "whisper",
            "last_active": time.monotonic(),
            "flush_timer": None,
            "lock": MagicMock(),
        }
        _asr_mod._evict_stale_sessions()
        assert "active" in _asr_mod._sessions


class TestTryRecoverSession:
    def test_recovers_whisper_session_from_redis(self, mock_redis):
        _asr_mod.init_redis_store(mock_redis)
        mock_redis.hgetall.return_value = {
            "type": "whisper",
            "project_id": "42",
            "project_llm_key": "key_abc",
            "model_name": "whisper-1",
            "language": "en",
            "speech_detected": "0",
            "silent_frames": "0",
            "call_in_flight": "0",
            "last_active": "1000.0",
            "created_at": "999.0",
        }
        mock_redis.lrange.return_value = []

        sio_handler = MagicMock()
        _asr_mod._try_recover_session(sio_handler, "sid_recover")
        assert "sid_recover" in _asr_mod._sessions
        session = _asr_mod._sessions["sid_recover"]
        assert session["type"] == "whisper"
        assert session["project_id"] == "42"
        assert session["event_node"] is sio_handler.event_node

    def test_recovers_realtime_session(self, mock_redis):
        _asr_mod.init_redis_store(mock_redis)
        mock_redis.hgetall.return_value = {
            "type": "realtime",
            "project_id": "99",
            "model_name": "gpt-4o-realtime",
            "language": "fr",
            "last_active": "1000.0",
            "created_at": "999.0",
        }

        sio_handler = MagicMock()
        _asr_mod._try_recover_session(sio_handler, "sid_rt")
        assert "sid_rt" in _asr_mod._sessions
        assert _asr_mod._sessions["sid_rt"]["type"] == "realtime"

    def test_noop_when_no_store(self):
        sio_handler = MagicMock()
        _asr_mod._try_recover_session(sio_handler, "sid_x")
        assert "sid_x" not in _asr_mod._sessions

    def test_noop_when_no_session_in_redis(self, mock_redis):
        _asr_mod.init_redis_store(mock_redis)
        mock_redis.hgetall.return_value = {}
        sio_handler = MagicMock()
        _asr_mod._try_recover_session(sio_handler, "missing")
        assert "missing" not in _asr_mod._sessions


class TestOnWhisperCallDone:
    def test_clears_in_flight_when_no_pending(self, mock_redis):
        import threading
        _asr_mod.init_redis_store(mock_redis)
        _asr_mod._sessions["sid1"] = {
            "type": "whisper",
            "lock": threading.Lock(),
            "pending_buffer": bytearray(),
            "call_in_flight": True,
            "speech_detected": False,
            "silent_frames": 0,
            "task_node": MagicMock(),
            "project_id": "42",
            "project_llm_key": "key",
            "model_name": "whisper-1",
            "language": "en",
        }
        _asr_mod.on_whisper_call_done("sid1")
        assert _asr_mod._sessions["sid1"]["call_in_flight"] is False

    def test_dispatches_pending_buffer(self, mock_redis):
        import threading
        _asr_mod.init_redis_store(mock_redis)
        task_node = MagicMock()
        _asr_mod._sessions["sid2"] = {
            "type": "whisper",
            "lock": threading.Lock(),
            "pending_buffer": bytearray(b"\x00" * 5000),
            "call_in_flight": True,
            "speech_detected": False,
            "silent_frames": 0,
            "task_node": task_node,
            "project_id": "42",
            "project_llm_key": "key",
            "model_name": "whisper-1",
            "language": "en",
        }
        _asr_mod.on_whisper_call_done("sid2")
        task_node.start_task.assert_called_once()
        assert _asr_mod._sessions["sid2"]["call_in_flight"] is True

    def test_noop_for_nonexistent_session(self):
        _asr_mod.on_whisper_call_done("nonexistent")

    def test_noop_for_realtime_session(self):
        _asr_mod._sessions["rt_sid"] = {"type": "realtime"}
        _asr_mod.on_whisper_call_done("rt_sid")
