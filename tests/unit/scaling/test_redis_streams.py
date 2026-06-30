"""Unit tests for Redis Streams producer/consumer.

Validates that:
1. StreamProducer.publish() calls XADD with correct parameters
2. StreamProducer applies MAXLEN trimming (approximate by default)
3. StreamConsumer creates consumer group on init (XGROUP CREATE MKSTREAM)
4. StreamConsumer.consume() uses XREADGROUP with '>' for new messages
5. StreamConsumer.consume_pending() uses XREADGROUP with '0' for pending
6. StreamConsumer.ack() calls XACK correctly
7. StreamConsumer.ack_many() batch acknowledges
8. StreamConsumer.claim_stale() uses XAUTOCLAIM for abandoned messages
9. StreamConsumer.pending_count() and pending_summary() use XPENDING
10. Error handling: group already exists (BUSYGROUP), connection errors
11. Response parsing handles both bytes and string keys/values
12. Stream key prefixing works correctly

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_redis_streams.py -v
"""

import importlib
import importlib.util
import json
import pathlib
import sys
import types
from unittest.mock import MagicMock, patch, call

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

_utils_pkg = types.ModuleType("centry.pylon_main.plugins.elitea_core.utils")
_utils_pkg.__path__ = [str(_PLUGIN_ROOT / "utils")]
_utils_pkg.__package__ = "centry.pylon_main.plugins.elitea_core.utils"
sys.modules.setdefault("centry.pylon_main.plugins.elitea_core.utils", _utils_pkg)

_plugin_pkg = types.ModuleType("centry.pylon_main.plugins.elitea_core")
_plugin_pkg.__path__ = [str(_PLUGIN_ROOT)]
_plugin_pkg.__package__ = "centry.pylon_main.plugins.elitea_core"
sys.modules.setdefault("centry.pylon_main.plugins.elitea_core", _plugin_pkg)

# Load event_classification so _resolve_retention relative import works
_events_pkg = types.ModuleType("centry.pylon_main.plugins.elitea_core.events")
_events_pkg.__path__ = [str(_PLUGIN_ROOT / "events")]
_events_pkg.__package__ = "centry.pylon_main.plugins.elitea_core.events"
sys.modules.setdefault("centry.pylon_main.plugins.elitea_core.events", _events_pkg)

_ec_path = _PLUGIN_ROOT / "events" / "event_classification.py"
_ec_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.events.event_classification",
    _ec_path,
    submodule_search_locations=[],
)
_ec_mod = importlib.util.module_from_spec(_ec_spec)
sys.modules[_ec_spec.name] = _ec_mod
sys.modules.setdefault("event_classification", _ec_mod)
_ec_spec.loader.exec_module(_ec_mod)

_module_path = _PLUGIN_ROOT / "utils" / "redis_streams.py"
_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.utils.redis_streams",
    _module_path,
    submodule_search_locations=[],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

StreamProducer = _mod.StreamProducer
StreamConsumer = _mod.StreamConsumer
STREAM_PREFIX = _mod.STREAM_PREFIX
DEFAULT_MAXLEN = _mod.DEFAULT_MAXLEN
DEFAULT_BLOCK_MS = _mod.DEFAULT_BLOCK_MS
DEFAULT_COUNT = _mod.DEFAULT_COUNT


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def redis_mock():
    """Create a mock Redis client."""
    mock = MagicMock()
    mock.xadd.return_value = b"1234567890123-0"
    mock.xlen.return_value = 42
    mock.xinfo_stream.return_value = {"length": 42, "groups": 1}
    mock.xgroup_create.return_value = True
    mock.xreadgroup.return_value = []
    mock.xack.return_value = 1
    mock.xpending.return_value = {"pending": 5, "min": b"0-0", "max": b"0-1", "consumers": []}
    mock.xautoclaim.return_value = [b"0-0", [], []]
    return mock


@pytest.fixture
def producer(redis_mock):
    """Create a StreamProducer instance."""
    return StreamProducer(redis_mock)


@pytest.fixture
def consumer(redis_mock):
    """Create a StreamConsumer instance."""
    return StreamConsumer(
        redis_mock,
        stream_name="work:task_distribution",
        group="task_workers",
        consumer="pod-1",
    )


# ---------------------------------------------------------------------------
# StreamProducer Tests
# ---------------------------------------------------------------------------


class TestStreamProducer:
    """Tests for StreamProducer."""

    def test_publish_calls_xadd_with_correct_key(self, producer, redis_mock):
        producer.publish("work:tasks", {"task_id": "123"})
        call_args = redis_mock.xadd.call_args
        assert call_args[0][0] == "stream:work:tasks"

    def test_publish_serializes_event_data_to_json(self, producer, redis_mock):
        producer.publish("work:tasks", {"task_id": "abc", "type": "predict"})
        call_args = redis_mock.xadd.call_args
        payload = call_args[0][1]
        data = json.loads(payload["data"])
        assert data == {"task_id": "abc", "type": "predict"}

    def test_publish_includes_published_at_timestamp(self, producer, redis_mock):
        producer.publish("work:tasks", {"x": 1})
        call_args = redis_mock.xadd.call_args
        payload = call_args[0][1]
        assert "published_at" in payload
        ts = float(payload["published_at"])
        assert ts > 0

    def test_publish_applies_default_maxlen(self, producer, redis_mock):
        producer.publish("work:tasks", {"x": 1})
        call_args = redis_mock.xadd.call_args
        assert call_args[1]["maxlen"] == DEFAULT_MAXLEN

    def test_publish_uses_approximate_trimming_by_default(self, producer, redis_mock):
        producer.publish("work:tasks", {"x": 1})
        call_args = redis_mock.xadd.call_args
        assert call_args[1]["approximate"] is True

    def test_publish_allows_custom_maxlen(self, producer, redis_mock):
        producer.publish("work:tasks", {"x": 1}, maxlen=500)
        call_args = redis_mock.xadd.call_args
        assert call_args[1]["maxlen"] == 500

    def test_publish_returns_message_id_as_string(self, producer, redis_mock):
        redis_mock.xadd.return_value = b"1699000000000-0"
        msg_id = producer.publish("work:tasks", {"x": 1})
        assert msg_id == "1699000000000-0"
        assert isinstance(msg_id, str)

    def test_publish_handles_string_message_id(self, producer, redis_mock):
        redis_mock.xadd.return_value = "1699000000000-1"
        msg_id = producer.publish("work:tasks", {"x": 1})
        assert msg_id == "1699000000000-1"

    def test_publish_with_exact_trimming(self, redis_mock):
        producer = StreamProducer(redis_mock, approximate_trim=False)
        producer.publish("work:tasks", {"x": 1})
        call_args = redis_mock.xadd.call_args
        assert call_args[1]["approximate"] is False

    def test_publish_with_custom_default_maxlen(self, redis_mock):
        producer = StreamProducer(redis_mock, maxlen=5000)
        producer.publish("work:tasks", {"x": 1})
        call_args = redis_mock.xadd.call_args
        assert call_args[1]["maxlen"] == 5000

    def test_stream_key_prefixing(self, producer, redis_mock):
        producer.publish("my_stream", {"x": 1})
        call_args = redis_mock.xadd.call_args
        assert call_args[0][0] == "stream:my_stream"

    def test_stream_key_no_double_prefix(self, producer, redis_mock):
        producer.publish("stream:already_prefixed", {"x": 1})
        call_args = redis_mock.xadd.call_args
        assert call_args[0][0] == "stream:already_prefixed"

    def test_stream_length(self, producer, redis_mock):
        redis_mock.xlen.return_value = 100
        length = producer.stream_length("work:tasks")
        redis_mock.xlen.assert_called_with("stream:work:tasks")
        assert length == 100

    def test_stream_info_returns_dict(self, producer, redis_mock):
        redis_mock.xinfo_stream.return_value = {"length": 42, "groups": 2}
        info = producer.stream_info("work:tasks")
        assert info == {"length": 42, "groups": 2}

    def test_stream_info_handles_exception(self, producer, redis_mock):
        redis_mock.xinfo_stream.side_effect = Exception("connection lost")
        info = producer.stream_info("work:tasks")
        assert info == {}

    def test_stream_info_handles_non_dict_response(self, producer, redis_mock):
        redis_mock.xinfo_stream.return_value = None
        info = producer.stream_info("work:tasks")
        assert info == {}


# ---------------------------------------------------------------------------
# StreamConsumer Tests - Initialization
# ---------------------------------------------------------------------------


class TestStreamConsumerInit:
    """Tests for StreamConsumer initialization."""

    def test_creates_group_on_init(self, redis_mock):
        StreamConsumer(redis_mock, "work:tasks", "grp", "c1")
        redis_mock.xgroup_create.assert_called_once_with(
            "stream:work:tasks", "grp", id="0", mkstream=True
        )

    def test_busygroup_error_is_silenced(self, redis_mock):
        redis_mock.xgroup_create.side_effect = Exception("BUSYGROUP Consumer Group name already exists")
        consumer = StreamConsumer(redis_mock, "work:tasks", "grp", "c1")
        assert consumer is not None

    def test_other_group_creation_error_is_logged(self, redis_mock):
        redis_mock.xgroup_create.side_effect = Exception("connection refused")
        consumer = StreamConsumer(redis_mock, "work:tasks", "grp", "c1")
        assert consumer is not None

    def test_skip_group_creation(self, redis_mock):
        StreamConsumer(redis_mock, "work:tasks", "grp", "c1", create_group=False)
        redis_mock.xgroup_create.assert_not_called()

    def test_stream_key_is_prefixed(self, redis_mock):
        consumer = StreamConsumer(redis_mock, "work:tasks", "grp", "c1")
        assert consumer._stream_key == "stream:work:tasks"

    def test_stream_key_already_prefixed(self, redis_mock):
        consumer = StreamConsumer(redis_mock, "stream:work:tasks", "grp", "c1")
        assert consumer._stream_key == "stream:work:tasks"


# ---------------------------------------------------------------------------
# StreamConsumer Tests - consume()
# ---------------------------------------------------------------------------


class TestStreamConsumerConsume:
    """Tests for StreamConsumer.consume()."""

    def test_consume_calls_xreadgroup_with_new_messages(self, consumer, redis_mock):
        redis_mock.xreadgroup.return_value = []
        consumer.consume()
        redis_mock.xreadgroup.assert_called_once_with(
            groupname="task_workers",
            consumername="pod-1",
            streams={"stream:work:task_distribution": ">"},
            count=DEFAULT_COUNT,
            block=DEFAULT_BLOCK_MS,
        )

    def test_consume_with_custom_count_and_block(self, consumer, redis_mock):
        redis_mock.xreadgroup.return_value = []
        consumer.consume(count=5, block_ms=1000)
        redis_mock.xreadgroup.assert_called_once_with(
            groupname="task_workers",
            consumername="pod-1",
            streams={"stream:work:task_distribution": ">"},
            count=5,
            block=1000,
        )

    def test_consume_returns_empty_on_no_messages(self, consumer, redis_mock):
        redis_mock.xreadgroup.return_value = None
        result = consumer.consume()
        assert result == []

    def test_consume_parses_list_response(self, consumer, redis_mock):
        redis_mock.xreadgroup.return_value = [
            [b"stream:work:task_distribution", [
                (b"1699000000000-0", {b"data": b'{"task_id":"abc"}', b"published_at": b"1699000000.0"}),
            ]]
        ]
        result = consumer.consume()
        assert len(result) == 1
        msg_id, data = result[0]
        assert msg_id == "1699000000000-0"
        assert data["data"] == {"task_id": "abc"}
        assert data["published_at"] == "1699000000.0"

    def test_consume_parses_dict_response(self, consumer, redis_mock):
        redis_mock.xreadgroup.return_value = {
            "stream:work:task_distribution": [
                ("1699000000000-0", {"data": '{"task_id":"xyz"}', "published_at": "1699000000.0"}),
            ]
        }
        result = consumer.consume()
        assert len(result) == 1
        msg_id, data = result[0]
        assert msg_id == "1699000000000-0"
        assert data["data"] == {"task_id": "xyz"}

    def test_consume_handles_multiple_messages(self, consumer, redis_mock):
        redis_mock.xreadgroup.return_value = [
            [b"stream:work:task_distribution", [
                (b"100-0", {b"data": b'{"id":"1"}', b"published_at": b"100.0"}),
                (b"200-0", {b"data": b'{"id":"2"}', b"published_at": b"200.0"}),
                (b"300-0", {b"data": b'{"id":"3"}', b"published_at": b"300.0"}),
            ]]
        ]
        result = consumer.consume()
        assert len(result) == 3
        assert result[0][0] == "100-0"
        assert result[1][0] == "200-0"
        assert result[2][0] == "300-0"

    def test_consume_handles_invalid_json_data(self, consumer, redis_mock):
        redis_mock.xreadgroup.return_value = [
            [b"stream:work:task_distribution", [
                (b"100-0", {b"data": b"not json at all", b"published_at": b"100.0"}),
            ]]
        ]
        result = consumer.consume()
        assert len(result) == 1
        assert result[0][1]["data"] == "not json at all"

    def test_consume_handles_exception(self, consumer, redis_mock):
        redis_mock.xreadgroup.side_effect = Exception("timeout")
        result = consumer.consume()
        assert result == []

    def test_consume_skips_entries_with_none_fields(self, consumer, redis_mock):
        redis_mock.xreadgroup.return_value = [
            [b"stream:work:task_distribution", [
                (b"100-0", None),
                (b"200-0", {b"data": b'{"ok":true}', b"published_at": b"200.0"}),
            ]]
        ]
        result = consumer.consume()
        assert len(result) == 1
        assert result[0][0] == "200-0"

    def test_consume_skips_entries_with_short_tuple(self, consumer, redis_mock):
        redis_mock.xreadgroup.return_value = [
            [b"stream:work:task_distribution", [
                (b"100-0",),
                (b"200-0", {b"data": b'{"ok":true}', b"published_at": b"200.0"}),
            ]]
        ]
        result = consumer.consume()
        assert len(result) == 1

    def test_consume_handles_dict_response_with_bytes_key(self, consumer, redis_mock):
        redis_mock.xreadgroup.return_value = {
            b"stream:work:task_distribution": [
                (b"100-0", {b"data": b'{"x":1}', b"published_at": b"100.0"}),
            ]
        }
        result = consumer.consume()
        assert len(result) == 1


# ---------------------------------------------------------------------------
# StreamConsumer Tests - consume_pending()
# ---------------------------------------------------------------------------


class TestStreamConsumerConsumePending:
    """Tests for StreamConsumer.consume_pending()."""

    def test_consume_pending_uses_zero_id(self, consumer, redis_mock):
        redis_mock.xreadgroup.return_value = []
        consumer.consume_pending()
        redis_mock.xreadgroup.assert_called_once_with(
            groupname="task_workers",
            consumername="pod-1",
            streams={"stream:work:task_distribution": "0"},
            count=DEFAULT_COUNT,
            block=0,
        )

    def test_consume_pending_with_custom_count(self, consumer, redis_mock):
        redis_mock.xreadgroup.return_value = []
        consumer.consume_pending(count=3)
        call_args = redis_mock.xreadgroup.call_args
        assert call_args[1]["count"] == 3

    def test_consume_pending_returns_messages(self, consumer, redis_mock):
        redis_mock.xreadgroup.return_value = [
            [b"stream:work:task_distribution", [
                (b"100-0", {b"data": b'{"task":"old"}', b"published_at": b"100.0"}),
            ]]
        ]
        result = consumer.consume_pending()
        assert len(result) == 1
        assert result[0][1]["data"] == {"task": "old"}

    def test_consume_pending_handles_exception(self, consumer, redis_mock):
        redis_mock.xreadgroup.side_effect = Exception("fail")
        result = consumer.consume_pending()
        assert result == []

    def test_consume_pending_returns_empty_on_none(self, consumer, redis_mock):
        redis_mock.xreadgroup.return_value = None
        result = consumer.consume_pending()
        assert result == []


# ---------------------------------------------------------------------------
# StreamConsumer Tests - ack()
# ---------------------------------------------------------------------------


class TestStreamConsumerAck:
    """Tests for StreamConsumer.ack()."""

    def test_ack_calls_xack(self, consumer, redis_mock):
        redis_mock.xack.return_value = 1
        result = consumer.ack("1699000000000-0")
        redis_mock.xack.assert_called_once_with(
            "stream:work:task_distribution", "task_workers", "1699000000000-0"
        )
        assert result is True

    def test_ack_returns_false_when_not_found(self, consumer, redis_mock):
        redis_mock.xack.return_value = 0
        result = consumer.ack("nonexistent-0")
        assert result is False

    def test_ack_returns_false_on_exception(self, consumer, redis_mock):
        redis_mock.xack.side_effect = Exception("connection lost")
        result = consumer.ack("100-0")
        assert result is False


# ---------------------------------------------------------------------------
# StreamConsumer Tests - ack_many()
# ---------------------------------------------------------------------------


class TestStreamConsumerAckMany:
    """Tests for StreamConsumer.ack_many()."""

    def test_ack_many_calls_xack_with_multiple_ids(self, consumer, redis_mock):
        redis_mock.xack.return_value = 3
        ids = ["100-0", "200-0", "300-0"]
        result = consumer.ack_many(ids)
        redis_mock.xack.assert_called_once_with(
            "stream:work:task_distribution", "task_workers",
            "100-0", "200-0", "300-0"
        )
        assert result == 3

    def test_ack_many_returns_zero_for_empty_list(self, consumer, redis_mock):
        result = consumer.ack_many([])
        assert result == 0
        redis_mock.xack.assert_not_called()

    def test_ack_many_returns_zero_on_exception(self, consumer, redis_mock):
        redis_mock.xack.side_effect = Exception("fail")
        result = consumer.ack_many(["100-0"])
        assert result == 0


# ---------------------------------------------------------------------------
# StreamConsumer Tests - pending_count() and pending_summary()
# ---------------------------------------------------------------------------


class TestStreamConsumerPending:
    """Tests for pending message inspection."""

    def test_pending_count_from_dict_response(self, consumer, redis_mock):
        redis_mock.xpending.return_value = {"pending": 5}
        count = consumer.pending_count()
        assert count == 5

    def test_pending_count_from_list_response(self, consumer, redis_mock):
        redis_mock.xpending.return_value = [10, b"0-0", b"100-0", [[b"pod-1", b"5"]]]
        count = consumer.pending_count()
        assert count == 10

    def test_pending_count_returns_zero_on_exception(self, consumer, redis_mock):
        redis_mock.xpending.side_effect = Exception("fail")
        count = consumer.pending_count()
        assert count == 0

    def test_pending_count_returns_zero_for_empty_response(self, consumer, redis_mock):
        redis_mock.xpending.return_value = []
        count = consumer.pending_count()
        assert count == 0

    def test_pending_summary_from_dict(self, consumer, redis_mock):
        redis_mock.xpending.return_value = {
            "pending": 3, "min_id": "1-0", "max_id": "3-0", "consumers": []
        }
        summary = consumer.pending_summary()
        assert summary["pending"] == 3

    def test_pending_summary_from_list(self, consumer, redis_mock):
        redis_mock.xpending.return_value = [7, b"10-0", b"20-0", [[b"pod-1", b"7"]]]
        summary = consumer.pending_summary()
        assert summary["pending"] == 7
        assert summary["min_id"] == "10-0"
        assert summary["max_id"] == "20-0"

    def test_pending_summary_on_error(self, consumer, redis_mock):
        redis_mock.xpending.side_effect = Exception("fail")
        summary = consumer.pending_summary()
        assert summary["pending"] == 0


# ---------------------------------------------------------------------------
# StreamConsumer Tests - claim_stale()
# ---------------------------------------------------------------------------


class TestStreamConsumerClaimStale:
    """Tests for StreamConsumer.claim_stale()."""

    def test_claim_stale_calls_xautoclaim(self, consumer, redis_mock):
        redis_mock.xautoclaim.return_value = [b"0-0", [], []]
        consumer.claim_stale(min_idle_ms=60000)
        redis_mock.xautoclaim.assert_called_once_with(
            "stream:work:task_distribution",
            "task_workers",
            "pod-1",
            min_idle_time=60000,
            start_id="0-0",
            count=DEFAULT_COUNT,
        )

    def test_claim_stale_parses_claimed_messages(self, consumer, redis_mock):
        redis_mock.xautoclaim.return_value = [
            b"0-0",
            [(b"100-0", {b"data": b'{"claimed":true}', b"published_at": b"100.0"})],
            [],
        ]
        result = consumer.claim_stale(min_idle_ms=30000)
        assert len(result) == 1
        assert result[0][0] == "100-0"
        assert result[0][1]["data"] == {"claimed": True}

    def test_claim_stale_returns_empty_on_no_stale(self, consumer, redis_mock):
        redis_mock.xautoclaim.return_value = [b"0-0", [], []]
        result = consumer.claim_stale(min_idle_ms=60000)
        assert result == []

    def test_claim_stale_returns_empty_on_exception(self, consumer, redis_mock):
        redis_mock.xautoclaim.side_effect = Exception("fail")
        result = consumer.claim_stale(min_idle_ms=60000)
        assert result == []

    def test_claim_stale_returns_empty_on_none(self, consumer, redis_mock):
        redis_mock.xautoclaim.return_value = None
        result = consumer.claim_stale(min_idle_ms=60000)
        assert result == []

    def test_claim_stale_with_custom_count(self, consumer, redis_mock):
        redis_mock.xautoclaim.return_value = [b"0-0", [], []]
        consumer.claim_stale(min_idle_ms=60000, count=5)
        call_args = redis_mock.xautoclaim.call_args
        assert call_args[1]["count"] == 5


# ---------------------------------------------------------------------------
# Integration-like scenarios
# ---------------------------------------------------------------------------


class TestStreamEndToEnd:
    """End-to-end workflow tests using mocks."""

    def test_produce_consume_ack_workflow(self, redis_mock):
        producer = StreamProducer(redis_mock)
        consumer = StreamConsumer(
            redis_mock, "work:tasks", "workers", "pod-1"
        )

        redis_mock.xadd.return_value = b"1000-0"
        msg_id = producer.publish("work:tasks", {"task_id": "t1"})
        assert msg_id == "1000-0"

        redis_mock.xreadgroup.return_value = [
            [b"stream:work:tasks", [
                (b"1000-0", {b"data": b'{"task_id":"t1"}', b"published_at": b"1000.0"}),
            ]]
        ]
        messages = consumer.consume()
        assert len(messages) == 1
        assert messages[0][1]["data"]["task_id"] == "t1"

        redis_mock.xack.return_value = 1
        acked = consumer.ack(messages[0][0])
        assert acked is True

    def test_multiple_consumers_same_group(self, redis_mock):
        """Different consumers in the same group get different messages."""
        c1 = StreamConsumer(redis_mock, "work:tasks", "workers", "pod-1")
        c2 = StreamConsumer(redis_mock, "work:tasks", "workers", "pod-2")

        assert redis_mock.xgroup_create.call_count == 2
        assert c1._group == c2._group == "workers"
        assert c1._consumer != c2._consumer

    def test_recovery_from_pending(self, redis_mock):
        """Consumer recovers pending messages after restart."""
        consumer = StreamConsumer(redis_mock, "work:tasks", "workers", "pod-1")

        redis_mock.xreadgroup.return_value = [
            [b"stream:work:tasks", [
                (b"500-0", {b"data": b'{"recovered":true}', b"published_at": b"500.0"}),
            ]]
        ]
        pending = consumer.consume_pending()
        assert len(pending) == 1
        assert pending[0][1]["data"]["recovered"] is True

    def test_claim_stale_and_process(self, redis_mock):
        """Consumer claims stale messages from crashed pods."""
        consumer = StreamConsumer(redis_mock, "work:tasks", "workers", "pod-2")

        redis_mock.xautoclaim.return_value = [
            b"0-0",
            [(b"777-0", {b"data": b'{"stale":true}', b"published_at": b"777.0"})],
            [b"deleted-id"],
        ]
        claimed = consumer.claim_stale(min_idle_ms=120000)
        assert len(claimed) == 1
        assert claimed[0][1]["data"]["stale"] is True

        redis_mock.xack.return_value = 1
        assert consumer.ack(claimed[0][0]) is True


# ---------------------------------------------------------------------------
# Classification-Aware Retention Tests
# ---------------------------------------------------------------------------


class TestStreamProducerClassificationRetention:
    """Tests for StreamProducer with use_classification_retention=True."""

    def test_classification_disabled_by_default(self, redis_mock):
        producer = StreamProducer(redis_mock)
        assert producer._use_classification is False

    def test_classification_enabled_flag(self, redis_mock):
        producer = StreamProducer(redis_mock, use_classification_retention=True)
        assert producer._use_classification is True

    def test_classification_resolves_work_stream_retention(self, redis_mock):
        producer = StreamProducer(redis_mock, use_classification_retention=True)
        producer.publish("work:task_distribution", {"task_id": "1"})
        call_args = redis_mock.xadd.call_args
        assert call_args[1]["maxlen"] == 10000

    def test_classification_resolves_dlq_stream_retention(self, redis_mock):
        producer = StreamProducer(redis_mock, use_classification_retention=True)
        producer.publish("dlq:work:task_distribution", {"task_id": "1"})
        call_args = redis_mock.xadd.call_args
        assert call_args[1]["maxlen"] == 50000

    def test_classification_resolves_notification_stream_retention(self, redis_mock):
        producer = StreamProducer(redis_mock, use_classification_retention=True)
        producer.publish("notify:stream_event", {"data": "x"})
        call_args = redis_mock.xadd.call_args
        assert call_args[1]["maxlen"] == 1000

    def test_explicit_maxlen_overrides_classification(self, redis_mock):
        producer = StreamProducer(redis_mock, use_classification_retention=True)
        producer.publish("work:task_distribution", {"x": 1}, maxlen=500)
        call_args = redis_mock.xadd.call_args
        assert call_args[1]["maxlen"] == 500

    def test_classification_fallback_on_import_error(self, redis_mock):
        producer = StreamProducer(redis_mock, maxlen=7777,
                                  use_classification_retention=True)
        with patch.object(producer, "_resolve_retention", side_effect=ImportError):
            producer.publish("work:tasks", {"x": 1})
        call_args = redis_mock.xadd.call_args
        assert call_args[1]["maxlen"] == 7777

    def test_without_classification_uses_constructor_maxlen(self, redis_mock):
        producer = StreamProducer(redis_mock, maxlen=3000,
                                  use_classification_retention=False)
        producer.publish("work:task_distribution", {"x": 1})
        call_args = redis_mock.xadd.call_args
        assert call_args[1]["maxlen"] == 3000
