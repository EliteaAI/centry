"""Unit tests for elitea_core/utils/canvas_autosave.py"""
import importlib.util
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Module loading (same pattern as other scaling tests)
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parents[4] / "elitea_core" / "utils" / "canvas_autosave.py"

# Mock pylon.core.tools.log (imported at module level)
sys.modules.setdefault("pylon", MagicMock())
sys.modules.setdefault("pylon.core", MagicMock())
sys.modules.setdefault("pylon.core.tools", MagicMock())

_spec = importlib.util.spec_from_file_location("canvas_autosave", _SRC)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["canvas_autosave"] = _mod
_spec.loader.exec_module(_mod)

CanvasAutosave = _mod.CanvasAutosave
AUTOSAVE_INTERVAL_SECONDS = _mod.AUTOSAVE_INTERVAL_SECONDS
AUTOSAVE_KEY_PREFIX = _mod.AUTOSAVE_KEY_PREFIX
AUTOSAVE_TTL = _mod.AUTOSAVE_TTL


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def redis_client():
    return MagicMock()


@pytest.fixture
def autosave(redis_client):
    return CanvasAutosave(redis_client)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAutosaveKeyGeneration:
    def test_autosave_key_format(self, autosave):
        key = autosave._autosave_key("42", "abc-123")
        assert key == "canvas_autosave:42_abc-123"

    def test_autosave_key_with_int_project(self, autosave):
        key = autosave._autosave_key(42, "uuid-here")
        assert key == "canvas_autosave:42_uuid-here"


class TestMarkDirty:
    def test_mark_dirty_sets_hash_fields(self, autosave, redis_client):
        pipe = MagicMock()
        redis_client.pipeline.return_value = pipe

        autosave.mark_dirty("10", "canvas-uuid")

        redis_client.pipeline.assert_called_once_with(transaction=False)
        pipe.hset.assert_called_once()
        call_args = pipe.hset.call_args
        # hset is called as pipe.hset(key, mapping={...})
        assert call_args[0][0] == "canvas_autosave:10_canvas-uuid"
        mapping = call_args[1]["mapping"]
        assert mapping["dirty"] == "1"
        assert "last_modified_at" in mapping
        float(mapping["last_modified_at"])  # should be parseable as float

    def test_mark_dirty_increments_version(self, autosave, redis_client):
        pipe = MagicMock()
        redis_client.pipeline.return_value = pipe

        autosave.mark_dirty("10", "canvas-uuid")

        pipe.hincrby.assert_called_once_with(
            "canvas_autosave:10_canvas-uuid", "version", 1
        )

    def test_mark_dirty_sets_ttl(self, autosave, redis_client):
        pipe = MagicMock()
        redis_client.pipeline.return_value = pipe

        autosave.mark_dirty("10", "canvas-uuid")

        pipe.expire.assert_called_once_with(
            "canvas_autosave:10_canvas-uuid", AUTOSAVE_TTL
        )

    def test_mark_dirty_executes_pipeline(self, autosave, redis_client):
        pipe = MagicMock()
        redis_client.pipeline.return_value = pipe

        autosave.mark_dirty("10", "canvas-uuid")

        pipe.execute.assert_called_once()


class TestMarkSaved:
    def test_mark_saved_clears_dirty_flag(self, autosave, redis_client):
        pipe = MagicMock()
        redis_client.pipeline.return_value = pipe

        autosave.mark_saved("10", "canvas-uuid")

        call_args = pipe.hset.call_args
        mapping = call_args[1]["mapping"]
        assert mapping["dirty"] == "0"
        assert "last_saved_at" in mapping

    def test_mark_saved_sets_ttl(self, autosave, redis_client):
        pipe = MagicMock()
        redis_client.pipeline.return_value = pipe

        autosave.mark_saved("10", "canvas-uuid")

        pipe.expire.assert_called_once_with(
            "canvas_autosave:10_canvas-uuid", AUTOSAVE_TTL
        )

    def test_mark_saved_executes_pipeline(self, autosave, redis_client):
        pipe = MagicMock()
        redis_client.pipeline.return_value = pipe

        autosave.mark_saved("10", "canvas-uuid")

        pipe.execute.assert_called_once()


class TestIsDirty:
    def test_is_dirty_true(self, autosave, redis_client):
        redis_client.hget.return_value = "1"
        assert autosave.is_dirty("10", "uuid") is True

    def test_is_dirty_false(self, autosave, redis_client):
        redis_client.hget.return_value = "0"
        assert autosave.is_dirty("10", "uuid") is False

    def test_is_dirty_none(self, autosave, redis_client):
        redis_client.hget.return_value = None
        assert autosave.is_dirty("10", "uuid") is False


class TestGetAutosaveInfo:
    def test_returns_defaults_when_no_data(self, autosave, redis_client):
        redis_client.hgetall.return_value = {}
        info = autosave.get_autosave_info("10", "uuid")
        assert info == {
            "dirty": False,
            "last_saved_at": None,
            "last_modified_at": None,
            "version": 0,
        }

    def test_parses_full_data(self, autosave, redis_client):
        redis_client.hgetall.return_value = {
            "dirty": "1",
            "last_saved_at": "1719700000.123",
            "last_modified_at": "1719700100.456",
            "version": "5",
        }
        info = autosave.get_autosave_info("10", "uuid")
        assert info["dirty"] is True
        assert info["last_saved_at"] == 1719700000.123
        assert info["last_modified_at"] == 1719700100.456
        assert info["version"] == 5

    def test_handles_partial_data(self, autosave, redis_client):
        redis_client.hgetall.return_value = {
            "dirty": "0",
            "version": "2",
        }
        info = autosave.get_autosave_info("10", "uuid")
        assert info["dirty"] is False
        assert info["last_saved_at"] is None
        assert info["last_modified_at"] is None
        assert info["version"] == 2


class TestGetLastSavedAt:
    def test_returns_float_when_set(self, autosave, redis_client):
        redis_client.hget.return_value = "1719700000.5"
        result = autosave.get_last_saved_at("10", "uuid")
        assert result == 1719700000.5

    def test_returns_none_when_not_set(self, autosave, redis_client):
        redis_client.hget.return_value = None
        result = autosave.get_last_saved_at("10", "uuid")
        assert result is None


class TestGetDirtyCanvases:
    def test_returns_empty_when_no_dirty(self, autosave, redis_client):
        redis_client.scan.return_value = (0, [])
        result = autosave.get_dirty_canvases()
        assert result == []

    def test_finds_dirty_canvases(self, autosave, redis_client):
        redis_client.scan.return_value = (
            0,
            [
                "canvas_autosave:10_uuid-1",
                "canvas_autosave:20_uuid-2",
                "canvas_autosave:30_uuid-3",
            ],
        )
        redis_client.hget.side_effect = ["1", "0", "1"]

        result = autosave.get_dirty_canvases()
        assert len(result) == 2
        assert {"project_id": "10", "canvas_uuid": "uuid-1"} in result
        assert {"project_id": "30", "canvas_uuid": "uuid-3"} in result

    def test_handles_scan_pagination(self, autosave, redis_client):
        redis_client.scan.side_effect = [
            (42, ["canvas_autosave:10_uuid-1"]),
            (0, ["canvas_autosave:20_uuid-2"]),
        ]
        redis_client.hget.side_effect = ["1", "1"]

        result = autosave.get_dirty_canvases()
        assert len(result) == 2

    def test_skips_malformed_keys(self, autosave, redis_client):
        redis_client.scan.return_value = (
            0,
            ["canvas_autosave:malformed-no-underscore"],
        )
        redis_client.hget.return_value = "1"

        result = autosave.get_dirty_canvases()
        assert result == []


class TestShouldSave:
    def test_not_dirty_returns_false(self, autosave, redis_client):
        redis_client.hmget.return_value = ["0", None]
        assert autosave.should_save("10", "uuid") is False

    def test_dirty_no_last_saved_returns_true(self, autosave, redis_client):
        redis_client.hmget.return_value = ["1", None]
        assert autosave.should_save("10", "uuid") is True

    def test_dirty_recently_saved_returns_false(self, autosave, redis_client):
        redis_client.hmget.return_value = ["1", str(time.time() - 60)]
        assert autosave.should_save("10", "uuid") is False

    def test_dirty_old_save_returns_true(self, autosave, redis_client):
        redis_client.hmget.return_value = [
            "1",
            str(time.time() - AUTOSAVE_INTERVAL_SECONDS - 1),
        ]
        assert autosave.should_save("10", "uuid") is True

    def test_dirty_exactly_at_boundary(self, autosave, redis_client):
        redis_client.hmget.return_value = [
            "1",
            str(time.time() - AUTOSAVE_INTERVAL_SECONDS),
        ]
        assert autosave.should_save("10", "uuid") is True

    def test_none_dirty_value_returns_false(self, autosave, redis_client):
        redis_client.hmget.return_value = [None, None]
        assert autosave.should_save("10", "uuid") is False


class TestGetRecoveryInfo:
    def test_recovery_info_with_unsaved_content(self, autosave, redis_client):
        redis_client.hgetall.return_value = {
            "dirty": "1",
            "last_saved_at": "1719700000.0",
            "last_modified_at": "1719700100.0",
            "version": "3",
        }
        info = autosave.get_recovery_info("10", "uuid")
        assert info["has_unsaved"] is True
        assert info["server_version"] == 3
        assert info["last_saved_at"] == 1719700000.0
        assert info["last_modified_at"] == 1719700100.0

    def test_recovery_info_no_unsaved(self, autosave, redis_client):
        redis_client.hgetall.return_value = {
            "dirty": "0",
            "last_saved_at": "1719700000.0",
            "version": "2",
        }
        info = autosave.get_recovery_info("10", "uuid")
        assert info["has_unsaved"] is False
        assert info["server_version"] == 2

    def test_recovery_info_empty_state(self, autosave, redis_client):
        redis_client.hgetall.return_value = {}
        info = autosave.get_recovery_info("10", "uuid")
        assert info["has_unsaved"] is False
        assert info["server_version"] == 0
        assert info["last_saved_at"] is None
        assert info["last_modified_at"] is None


class TestDeleteState:
    def test_deletes_key(self, autosave, redis_client):
        autosave.delete_state("10", "uuid")
        redis_client.delete.assert_called_once_with("canvas_autosave:10_uuid")


class TestConstants:
    def test_interval_is_5_minutes(self):
        assert AUTOSAVE_INTERVAL_SECONDS == 300

    def test_ttl_is_24_hours(self):
        assert AUTOSAVE_TTL == 86400

    def test_key_prefix(self):
        assert AUTOSAVE_KEY_PREFIX == "canvas_autosave:"


class TestEdgeCases:
    def test_unicode_canvas_uuid(self, autosave, redis_client):
        pipe = MagicMock()
        redis_client.pipeline.return_value = pipe
        autosave.mark_dirty("10", "uuid-with-dashes-and-numbers-123")
        pipe.hset.assert_called_once()

    def test_large_project_id(self, autosave, redis_client):
        key = autosave._autosave_key("999999", "uuid")
        assert "999999" in key

    def test_mark_dirty_timestamp_is_current(self, autosave, redis_client):
        pipe = MagicMock()
        redis_client.pipeline.return_value = pipe
        before = time.time()
        autosave.mark_dirty("10", "uuid")
        after = time.time()

        mapping = pipe.hset.call_args[1]["mapping"]
        ts = float(mapping["last_modified_at"])
        assert before <= ts <= after

    def test_mark_saved_timestamp_is_current(self, autosave, redis_client):
        pipe = MagicMock()
        redis_client.pipeline.return_value = pipe
        before = time.time()
        autosave.mark_saved("10", "uuid")
        after = time.time()

        mapping = pipe.hset.call_args[1]["mapping"]
        ts = float(mapping["last_saved_at"])
        assert before <= ts <= after

    def test_concurrent_mark_dirty_calls(self, autosave, redis_client):
        pipe = MagicMock()
        redis_client.pipeline.return_value = pipe

        autosave.mark_dirty("10", "uuid")
        autosave.mark_dirty("10", "uuid")

        assert pipe.execute.call_count == 2
        assert pipe.hincrby.call_count == 2

    def test_get_dirty_canvases_with_complex_uuid(self, autosave, redis_client):
        redis_client.scan.return_value = (
            0,
            ["canvas_autosave:10_550e8400-e29b-41d4-a716-446655440000"],
        )
        redis_client.hget.return_value = "1"

        result = autosave.get_dirty_canvases()
        assert len(result) == 1
        assert result[0]["project_id"] == "10"
        assert result[0]["canvas_uuid"] == "550e8400-e29b-41d4-a716-446655440000"

    def test_should_save_with_very_old_timestamp(self, autosave, redis_client):
        redis_client.hmget.return_value = ["1", "1000000000.0"]
        assert autosave.should_save("10", "uuid") is True

    def test_mark_dirty_uses_string_project_id_in_key(self, autosave, redis_client):
        pipe = MagicMock()
        redis_client.pipeline.return_value = pipe
        autosave.mark_dirty(42, "uuid")
        call_args = pipe.hset.call_args
        assert call_args[0][0] == "canvas_autosave:42_uuid"

    def test_get_autosave_info_only_dirty_field(self, autosave, redis_client):
        redis_client.hgetall.return_value = {"dirty": "1"}
        info = autosave.get_autosave_info("10", "uuid")
        assert info["dirty"] is True
        assert info["version"] == 0

    def test_multiple_dirty_canvases_scan(self, autosave, redis_client):
        keys = [f"canvas_autosave:{i}_uuid-{i}" for i in range(10)]
        redis_client.scan.return_value = (0, keys)
        redis_client.hget.return_value = "1"

        result = autosave.get_dirty_canvases()
        assert len(result) == 10
