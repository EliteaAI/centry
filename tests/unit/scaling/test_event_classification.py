"""Tests for elitea_core/events/event_classification.py"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Module loading — bypass pylon/arbiter dependencies
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _load_module():
    """Load event_classification.py without pylon framework dependencies."""
    source_path = (
        Path(__file__).resolve().parents[4]
        / "elitea_core"
        / "events"
        / "event_classification.py"
    )
    assert source_path.exists(), f"Source not found: {source_path}"

    spec = importlib.util.spec_from_file_location(
        "event_classification", source_path
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["event_classification"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return sys.modules["event_classification"]


@pytest.fixture
def EventType():
    return sys.modules["event_classification"].EventType


@pytest.fixture
def StreamRetention():
    return sys.modules["event_classification"].StreamRetention


# ---------------------------------------------------------------------------
# EventType enum tests
# ---------------------------------------------------------------------------

class TestEventTypeEnum:
    def test_broadcast_value(self, EventType):
        assert EventType.BROADCAST.value == "broadcast"

    def test_work_value(self, EventType):
        assert EventType.WORK.value == "work"

    def test_notification_value(self, EventType):
        assert EventType.NOTIFICATION.value == "notification"

    def test_enum_members_count(self, EventType):
        assert len(EventType) == 3

    def test_enum_iteration(self, EventType):
        values = {e.value for e in EventType}
        assert values == {"broadcast", "work", "notification"}


# ---------------------------------------------------------------------------
# StreamRetention tests
# ---------------------------------------------------------------------------

class TestStreamRetention:
    def test_work_retention(self, StreamRetention):
        assert StreamRetention.WORK == 10000

    def test_notification_retention(self, StreamRetention):
        assert StreamRetention.NOTIFICATION == 1000

    def test_dlq_retention(self, StreamRetention):
        assert StreamRetention.DLQ == 50000


# ---------------------------------------------------------------------------
# Registry tests (use isolated registry via clear/restore)
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_registry(mod):
    """Save, clear, and restore registry around each test."""
    original = dict(mod._REGISTRY)
    original_streams = dict(mod._STREAM_RETENTION)
    mod.clear_registry()
    yield mod
    mod._REGISTRY.clear()
    mod._REGISTRY.update(original)
    mod._STREAM_RETENTION.clear()
    mod._STREAM_RETENTION.update(original_streams)


class TestRegisterEvent:
    def test_register_single_event(self, clean_registry, EventType):
        m = clean_registry
        m.register_event("test_event", EventType.WORK, "A test work event")
        assert m.is_registered("test_event")

    def test_register_without_description(self, clean_registry, EventType):
        m = clean_registry
        m.register_event("simple", EventType.BROADCAST)
        assert m.get_event_description("simple") == ""

    def test_register_overwrites(self, clean_registry, EventType):
        m = clean_registry
        m.register_event("evt", EventType.WORK, "v1")
        m.register_event("evt", EventType.NOTIFICATION, "v2")
        assert m.get_event_type("evt") == EventType.NOTIFICATION
        assert m.get_event_description("evt") == "v2"


class TestGetEventType:
    def test_get_registered(self, clean_registry, EventType):
        m = clean_registry
        m.register_event("my_event", EventType.BROADCAST, "desc")
        assert m.get_event_type("my_event") == EventType.BROADCAST

    def test_get_unregistered_returns_none(self, clean_registry):
        m = clean_registry
        assert m.get_event_type("nonexistent") is None


class TestGetEventDescription:
    def test_get_registered_description(self, clean_registry, EventType):
        m = clean_registry
        m.register_event("evt", EventType.WORK, "my description")
        assert m.get_event_description("evt") == "my description"

    def test_get_unregistered_returns_empty(self, clean_registry):
        m = clean_registry
        assert m.get_event_description("missing") == ""


class TestGetEventsByType:
    def test_filter_by_work(self, clean_registry, EventType):
        m = clean_registry
        m.register_event("a", EventType.WORK, "work a")
        m.register_event("b", EventType.BROADCAST, "broadcast b")
        m.register_event("c", EventType.WORK, "work c")
        result = m.get_events_by_type(EventType.WORK)
        assert result == {"a": "work a", "c": "work c"}

    def test_filter_by_broadcast(self, clean_registry, EventType):
        m = clean_registry
        m.register_event("x", EventType.BROADCAST, "bcast")
        m.register_event("y", EventType.NOTIFICATION, "notif")
        result = m.get_events_by_type(EventType.BROADCAST)
        assert result == {"x": "bcast"}

    def test_empty_result(self, clean_registry, EventType):
        m = clean_registry
        m.register_event("x", EventType.WORK, "w")
        result = m.get_events_by_type(EventType.NOTIFICATION)
        assert result == {}


class TestConvenienceMethods:
    def test_get_work_events(self, clean_registry, EventType):
        m = clean_registry
        m.register_event("w1", EventType.WORK, "w")
        m.register_event("b1", EventType.BROADCAST, "b")
        assert "w1" in m.get_work_events()
        assert "b1" not in m.get_work_events()

    def test_get_broadcast_events(self, clean_registry, EventType):
        m = clean_registry
        m.register_event("b1", EventType.BROADCAST, "b")
        m.register_event("n1", EventType.NOTIFICATION, "n")
        assert "b1" in m.get_broadcast_events()
        assert "n1" not in m.get_broadcast_events()

    def test_get_notification_events(self, clean_registry, EventType):
        m = clean_registry
        m.register_event("n1", EventType.NOTIFICATION, "n")
        m.register_event("w1", EventType.WORK, "w")
        assert "n1" in m.get_notification_events()
        assert "w1" not in m.get_notification_events()


class TestGetRetention:
    def test_work_event_retention(self, clean_registry, EventType, StreamRetention):
        m = clean_registry
        m.register_event("task", EventType.WORK, "")
        assert m.get_retention("task") == StreamRetention.WORK

    def test_notification_event_retention(self, clean_registry, EventType, StreamRetention):
        m = clean_registry
        m.register_event("notif", EventType.NOTIFICATION, "")
        assert m.get_retention("notif") == StreamRetention.NOTIFICATION

    def test_broadcast_event_retention_defaults_to_work(self, clean_registry, EventType, StreamRetention):
        m = clean_registry
        m.register_event("bcast", EventType.BROADCAST, "")
        assert m.get_retention("bcast") == StreamRetention.WORK

    def test_unregistered_event_defaults_to_work(self, clean_registry, StreamRetention):
        m = clean_registry
        assert m.get_retention("unknown") == StreamRetention.WORK


class TestIsRegistered:
    def test_registered(self, clean_registry, EventType):
        m = clean_registry
        m.register_event("exists", EventType.WORK, "")
        assert m.is_registered("exists") is True

    def test_not_registered(self, clean_registry):
        m = clean_registry
        assert m.is_registered("ghost") is False


class TestListAll:
    def test_returns_all_registered(self, clean_registry, EventType):
        m = clean_registry
        m.register_event("a", EventType.WORK, "wa")
        m.register_event("b", EventType.BROADCAST, "bb")
        result = m.list_all()
        assert len(result) == 2
        assert result["a"] == (EventType.WORK, "wa")
        assert result["b"] == (EventType.BROADCAST, "bb")

    def test_returns_copy(self, clean_registry, EventType):
        m = clean_registry
        m.register_event("x", EventType.WORK, "")
        result = m.list_all()
        result["injected"] = (EventType.NOTIFICATION, "hack")
        assert not m.is_registered("injected")


class TestClearRegistry:
    def test_clears_all(self, clean_registry, EventType):
        m = clean_registry
        m.register_event("a", EventType.WORK, "")
        m.register_event("b", EventType.BROADCAST, "")
        m.clear_registry()
        assert m.list_all() == {}


# ---------------------------------------------------------------------------
# Built-in registrations tests (uses the full module-level registry)
# ---------------------------------------------------------------------------

class TestBuiltinRegistrations:
    """Verify the module-level registrations match the event audit."""

    def test_total_registered_count(self, mod):
        all_events = mod.list_all()
        assert len(all_events) >= 45

    def test_broadcast_events_present(self, mod, EventType):
        broadcasts = mod.get_broadcast_events()
        expected_broadcasts = [
            "application_toolkit_configurations_collected",
            "application_toolkits_collected",
            "application_file_loaders_collected",
            "application_mcp_prebuilt_config_collected",
            "bootstrap_runtime_info",
            "bootstrap_runtime_info_prune",
            "runtime_engine_ready",
            "audit_event",
            "bootstrap_runtime_update",
            "application_toolkits_request",
            "application_file_loaders_request",
            "application_toolkit_configurations_request",
            "application_mcp_prebuilt_config_request",
            "task_node_announce",
            "task_node_withhold",
            "task_state_announce",
            "task_state_query",
            "task_pool_query",
            "task_pool_reply",
            "presence_join",
            "presence_leave",
            "service_discovery",
            "service_provider",
        ]
        for evt in expected_broadcasts:
            assert evt in broadcasts, f"{evt} should be classified as broadcast"

    def test_work_events_present(self, mod, EventType):
        work = mod.get_work_events()
        expected_work = [
            "application_full_response",
            "application_partial_response",
            "application_child_message",
            "voice_asr_transcript_done",
            "task_status_change",
            "indexer_empty_agent_state",
            "indexer_delete_checkpoint",
            "voice_events",
            "task_stop_request",
            "task_result_payload",
            "task_start_query",
            "task_start_candidate",
            "task_start_request",
            "task_start_ack",
            "task_state_reply",
            "service_request",
            "service_response",
        ]
        for evt in expected_work:
            assert evt in work, f"{evt} should be classified as work"

    def test_notification_events_present(self, mod, EventType):
        notifications = mod.get_notification_events()
        expected_notifications = [
            "application_stream_response",
            "voice_tts_audio_chunk",
            "voice_tts_done",
            "voice_tts_error",
            "voice_asr_transcript_delta",
            "voice_asr_error",
            "voice_asr_speech_started",
            "voice_asr_vad_flush",
            "stream_event",
            "log_data",
            "provider_invocation_started",
            "provider_invocation_ended",
        ]
        for evt in expected_notifications:
            assert evt in notifications, f"{evt} should be classified as notification"

    def test_work_events_currently_broadcast_as_bugs(self, mod, EventType):
        """These work events are currently delivered via broadcast (pub/sub)
        but should only be processed by one pod. Phase 4 will migrate them
        to Redis Streams."""
        work_events_on_broadcast_transport = [
            "application_full_response",
            "application_partial_response",
            "application_child_message",
            "voice_asr_transcript_done",
            "task_status_change",
            "indexer_empty_agent_state",
            "indexer_delete_checkpoint",
            "voice_events",
        ]
        work = mod.get_work_events()
        for evt in work_events_on_broadcast_transport:
            assert evt in work, f"{evt} must be marked as WORK for Streams migration"

    def test_stream_event_is_notification(self, mod, EventType):
        assert mod.get_event_type("stream_event") == EventType.NOTIFICATION

    def test_task_status_change_is_work(self, mod, EventType):
        assert mod.get_event_type("task_status_change") == EventType.WORK

    def test_toolkits_collected_is_broadcast(self, mod, EventType):
        assert mod.get_event_type("application_toolkits_collected") == EventType.BROADCAST

    def test_retention_for_work_event(self, mod, StreamRetention):
        assert mod.get_retention("task_status_change") == StreamRetention.WORK

    def test_retention_for_notification_event(self, mod, StreamRetention):
        assert mod.get_retention("voice_tts_audio_chunk") == StreamRetention.NOTIFICATION

    def test_all_events_have_descriptions(self, mod):
        for name, (etype, desc) in mod.list_all().items():
            assert desc != "", f"Event '{name}' is missing a description"


# ---------------------------------------------------------------------------
# Stream retention registry tests
# ---------------------------------------------------------------------------

class TestRegisterStreamRetention:
    def test_register_custom_retention(self, clean_registry):
        m = clean_registry
        m.register_stream_retention("work:my_custom_stream", 5000)
        assert m.get_stream_retention("work:my_custom_stream") == 5000

    def test_register_overwrites_previous(self, clean_registry):
        m = clean_registry
        m.register_stream_retention("work:x", 1000)
        m.register_stream_retention("work:x", 2000)
        assert m.get_stream_retention("work:x") == 2000

    def test_list_stream_retentions_empty(self, clean_registry):
        m = clean_registry
        assert m.list_stream_retentions() == {}

    def test_list_stream_retentions_populated(self, clean_registry, StreamRetention):
        m = clean_registry
        m.register_stream_retention("work:a", StreamRetention.WORK)
        m.register_stream_retention("notify:b", StreamRetention.NOTIFICATION)
        result = m.list_stream_retentions()
        assert result == {"work:a": StreamRetention.WORK, "notify:b": StreamRetention.NOTIFICATION}

    def test_list_returns_copy(self, clean_registry):
        m = clean_registry
        m.register_stream_retention("work:x", 100)
        result = m.list_stream_retentions()
        result["injected"] = 999
        assert m.get_stream_retention("injected") != 999


class TestGetStreamRetention:
    def test_explicit_registration_takes_priority(self, clean_registry, StreamRetention):
        m = clean_registry
        m.register_stream_retention("work:task_distribution", 7777)
        assert m.get_stream_retention("work:task_distribution") == 7777

    def test_dlq_prefix_detection(self, clean_registry, StreamRetention):
        m = clean_registry
        assert m.get_stream_retention("dlq:work:something") == StreamRetention.DLQ

    def test_dlq_prefix_explicit_override(self, clean_registry):
        m = clean_registry
        m.register_stream_retention("dlq:work:special", 99999)
        assert m.get_stream_retention("dlq:work:special") == 99999

    def test_work_prefix_detection(self, clean_registry, StreamRetention):
        m = clean_registry
        assert m.get_stream_retention("work:unknown_stream") == StreamRetention.WORK

    def test_notify_prefix_detection(self, clean_registry, StreamRetention):
        m = clean_registry
        assert m.get_stream_retention("notify:my_events") == StreamRetention.NOTIFICATION

    def test_notification_prefix_detection(self, clean_registry, StreamRetention):
        m = clean_registry
        assert m.get_stream_retention("notification:alerts") == StreamRetention.NOTIFICATION

    def test_unknown_prefix_defaults_to_work(self, clean_registry, StreamRetention):
        m = clean_registry
        assert m.get_stream_retention("custom:something") == StreamRetention.WORK

    def test_no_prefix_defaults_to_work(self, clean_registry, StreamRetention):
        m = clean_registry
        assert m.get_stream_retention("bare_stream_name") == StreamRetention.WORK


class TestClearRegistryIncludesStreams:
    def test_clear_removes_stream_retentions(self, clean_registry):
        m = clean_registry
        m.register_stream_retention("work:x", 5000)
        m.clear_registry()
        assert m.list_stream_retentions() == {}


class TestBuiltinStreamRetentions:
    """Verify the module-level stream retention registrations."""

    def test_task_distribution_registered(self, mod, StreamRetention):
        assert mod.get_stream_retention("work:task_distribution") == StreamRetention.WORK

    def test_voice_events_registered(self, mod, StreamRetention):
        assert mod.get_stream_retention("work:voice_events") == StreamRetention.WORK

    def test_service_request_registered(self, mod, StreamRetention):
        assert mod.get_stream_retention("work:service_request") == StreamRetention.WORK

    def test_notify_stream_event_registered(self, mod, StreamRetention):
        assert mod.get_stream_retention("notify:stream_event") == StreamRetention.NOTIFICATION

    def test_notify_log_data_registered(self, mod, StreamRetention):
        assert mod.get_stream_retention("notify:log_data") == StreamRetention.NOTIFICATION

    def test_notify_voice_tts_registered(self, mod, StreamRetention):
        assert mod.get_stream_retention("notify:voice_tts_audio_chunk") == StreamRetention.NOTIFICATION

    def test_dlq_task_distribution_registered(self, mod, StreamRetention):
        assert mod.get_stream_retention("dlq:work:task_distribution") == StreamRetention.DLQ

    def test_dlq_voice_events_registered(self, mod, StreamRetention):
        assert mod.get_stream_retention("dlq:work:voice_events") == StreamRetention.DLQ

    def test_dlq_service_request_registered(self, mod, StreamRetention):
        assert mod.get_stream_retention("dlq:work:service_request") == StreamRetention.DLQ
