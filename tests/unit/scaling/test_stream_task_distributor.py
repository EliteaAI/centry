"""Unit tests for Stream-based task distribution.

Validates that:
1. TaskDistributionProducer.submit_task() publishes to correct stream
2. TaskDistributionProducer generates task_id if not provided
3. TaskDistributionProducer respects custom task_id
4. TaskDistributionConsumer starts/stops consumer loop thread
5. TaskDistributionConsumer processes messages via task_handler
6. TaskDistributionConsumer ACKs on successful processing
7. TaskDistributionConsumer retries on failure up to max_retries
8. TaskDistributionConsumer sends to DLQ after exhausting retries
9. TaskDistributionConsumer recovers pending messages on startup
10. TaskDistributionConsumer claims abandoned messages from dead consumers
11. TaskDistributionConsumer handles invalid JSON gracefully
12. TaskDistributionConsumer handles handler exceptions
13. TaskDistributionConsumer stops cleanly on stop() call
14. stream_depth() reports current queue size
15. pending_count() reports unacked messages

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_stream_task_distributor.py -v
"""

import importlib
import importlib.util
import json
import pathlib
import sys
import time
import types
import threading
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

_utils_pkg = types.ModuleType("centry.pylon_main.plugins.elitea_core.utils")
_utils_pkg.__path__ = [str(_PLUGIN_ROOT / "utils")]
_utils_pkg.__package__ = "centry.pylon_main.plugins.elitea_core.utils"
sys.modules.setdefault("centry.pylon_main.plugins.elitea_core.utils", _utils_pkg)

_plugin_pkg = types.ModuleType("centry.pylon_main.plugins.elitea_core")
_plugin_pkg.__path__ = [str(_PLUGIN_ROOT)]
_plugin_pkg.__package__ = "centry.pylon_main.plugins.elitea_core"
sys.modules.setdefault("centry.pylon_main.plugins.elitea_core", _plugin_pkg)

# Load redis_streams first (dependency)
_streams_path = _PLUGIN_ROOT / "utils" / "redis_streams.py"
_streams_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.utils.redis_streams",
    _streams_path,
    submodule_search_locations=[],
)
_streams_mod = importlib.util.module_from_spec(_streams_spec)
sys.modules[_streams_spec.name] = _streams_mod
_streams_spec.loader.exec_module(_streams_mod)

# Load the module under test
_module_path = _PLUGIN_ROOT / "utils" / "stream_task_distributor.py"
_spec = importlib.util.spec_from_file_location(
    "centry.pylon_main.plugins.elitea_core.utils.stream_task_distributor",
    _module_path,
    submodule_search_locations=[],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

TaskDistributionProducer = _mod.TaskDistributionProducer
TaskDistributionConsumer = _mod.TaskDistributionConsumer
STREAM_NAME = _mod.STREAM_NAME
CONSUMER_GROUP = _mod.CONSUMER_GROUP
DLQ_PREFIX = _mod.DLQ_PREFIX
DEFAULT_MAX_RETRIES = _mod.DEFAULT_MAX_RETRIES
_get_consumer_name = _mod._get_consumer_name


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def redis_mock():
    """Create a mock Redis client."""
    mock = MagicMock()
    mock.xadd.return_value = b"1700000000000-0"
    mock.xlen.return_value = 5
    mock.xinfo_stream.return_value = {"length": 5, "groups": 1}
    mock.xgroup_create.return_value = True
    mock.xreadgroup.return_value = []
    mock.xack.return_value = 1
    mock.xpending.return_value = {"pending": 0, "min": None, "max": None, "consumers": []}
    mock.xautoclaim.return_value = [b"0-0", [], []]
    return mock


@pytest.fixture
def producer(redis_mock):
    """Create a TaskDistributionProducer instance."""
    return TaskDistributionProducer(redis_mock)


@pytest.fixture
def handler_mock():
    """Create a mock task handler that returns True."""
    handler = MagicMock(return_value=True)
    return handler


@pytest.fixture
def consumer(redis_mock, handler_mock):
    """Create a TaskDistributionConsumer (not started)."""
    return TaskDistributionConsumer(
        redis_mock,
        task_handler=handler_mock,
        poll_interval_ms=100,
        poll_count=5,
        max_retries=3,
        claim_idle_ms=60000,
    )


# ---------------------------------------------------------------------------
# TaskDistributionProducer Tests
# ---------------------------------------------------------------------------


class TestTaskDistributionProducer:
    """Tests for TaskDistributionProducer."""

    def test_submit_task_publishes_to_stream(self, producer, redis_mock):
        producer.submit_task("indexer_agent", args=[1, 2], pool="agents")
        redis_mock.xadd.assert_called_once()
        call_args = redis_mock.xadd.call_args
        assert call_args[0][0] == "stream:work:task_distribution"

    def test_submit_task_includes_task_name_in_payload(self, producer, redis_mock):
        producer.submit_task("indexer_agent")
        call_args = redis_mock.xadd.call_args
        payload = call_args[0][1]
        data = json.loads(payload["data"])
        assert data["task_name"] == "indexer_agent"

    def test_submit_task_includes_args_and_kwargs(self, producer, redis_mock):
        producer.submit_task("test_task", args=[1, "two"], kwargs={"key": "val"})
        call_args = redis_mock.xadd.call_args
        payload = call_args[0][1]
        data = json.loads(payload["data"])
        assert data["args"] == [1, "two"]
        assert data["kwargs"] == {"key": "val"}

    def test_submit_task_includes_pool(self, producer, redis_mock):
        producer.submit_task("test_task", pool="indexer")
        call_args = redis_mock.xadd.call_args
        payload = call_args[0][1]
        data = json.loads(payload["data"])
        assert data["pool"] == "indexer"

    def test_submit_task_includes_meta(self, producer, redis_mock):
        meta = {"project_id": 42, "user_id": 7}
        producer.submit_task("test_task", meta=meta)
        call_args = redis_mock.xadd.call_args
        payload = call_args[0][1]
        data = json.loads(payload["data"])
        assert data["meta"] == meta

    def test_submit_task_generates_task_id_if_not_provided(self, producer, redis_mock):
        producer.submit_task("test_task")
        call_args = redis_mock.xadd.call_args
        payload = call_args[0][1]
        data = json.loads(payload["data"])
        assert "task_id" in data
        assert len(data["task_id"]) > 0

    def test_submit_task_uses_provided_task_id(self, producer, redis_mock):
        producer.submit_task("test_task", task_id="my-custom-id-123")
        call_args = redis_mock.xadd.call_args
        payload = call_args[0][1]
        data = json.loads(payload["data"])
        assert data["task_id"] == "my-custom-id-123"

    def test_submit_task_includes_submitted_at_timestamp(self, producer, redis_mock):
        before = time.time()
        producer.submit_task("test_task")
        after = time.time()
        call_args = redis_mock.xadd.call_args
        payload = call_args[0][1]
        data = json.loads(payload["data"])
        assert before <= data["submitted_at"] <= after

    def test_submit_task_returns_message_id(self, producer, redis_mock):
        redis_mock.xadd.return_value = b"1700000000000-0"
        msg_id = producer.submit_task("test_task")
        assert msg_id == "1700000000000-0"

    def test_submit_task_defaults_args_to_empty_list(self, producer, redis_mock):
        producer.submit_task("test_task")
        call_args = redis_mock.xadd.call_args
        payload = call_args[0][1]
        data = json.loads(payload["data"])
        assert data["args"] == []

    def test_submit_task_defaults_kwargs_to_empty_dict(self, producer, redis_mock):
        producer.submit_task("test_task")
        call_args = redis_mock.xadd.call_args
        payload = call_args[0][1]
        data = json.loads(payload["data"])
        assert data["kwargs"] == {}

    def test_submit_task_defaults_meta_to_empty_dict(self, producer, redis_mock):
        producer.submit_task("test_task")
        call_args = redis_mock.xadd.call_args
        payload = call_args[0][1]
        data = json.loads(payload["data"])
        assert data["meta"] == {}

    def test_stream_depth_returns_xlen(self, producer, redis_mock):
        redis_mock.xlen.return_value = 42
        assert producer.stream_depth() == 42

    def test_stream_depth_calls_correct_key(self, producer, redis_mock):
        producer.stream_depth()
        redis_mock.xlen.assert_called_once_with("stream:work:task_distribution")

    def test_custom_stream_name(self, redis_mock):
        prod = TaskDistributionProducer(redis_mock, stream_name="work:custom")
        prod.submit_task("test_task")
        call_args = redis_mock.xadd.call_args
        assert call_args[0][0] == "stream:work:custom"

    def test_custom_maxlen(self, redis_mock):
        prod = TaskDistributionProducer(redis_mock, maxlen=500)
        prod.submit_task("test_task")
        call_args = redis_mock.xadd.call_args
        assert call_args[1]["maxlen"] == 500 or call_args[0][2] == 500


# ---------------------------------------------------------------------------
# TaskDistributionConsumer Tests
# ---------------------------------------------------------------------------


class TestTaskDistributionConsumer:
    """Tests for TaskDistributionConsumer."""

    def test_consumer_creates_group_on_init(self, redis_mock, handler_mock):
        TaskDistributionConsumer(redis_mock, handler_mock)
        redis_mock.xgroup_create.assert_called()

    def test_consumer_uses_hostname_as_consumer_name(self, redis_mock, handler_mock):
        with patch.dict("os.environ", {"HOSTNAME": "pod-abc-123"}):
            c = TaskDistributionConsumer(redis_mock, handler_mock)
            assert c.consumer_name() == "pod-abc-123"

    def test_consumer_generates_name_without_hostname(self, redis_mock, handler_mock):
        with patch.dict("os.environ", {}, clear=True):
            # Remove HOSTNAME if present
            import os
            orig = os.environ.pop("HOSTNAME", None)
            try:
                c = TaskDistributionConsumer(redis_mock, handler_mock)
                name = c.consumer_name()
                assert name.startswith("consumer-")
                assert len(name) > len("consumer-")
            finally:
                if orig is not None:
                    os.environ["HOSTNAME"] = orig

    def test_start_creates_thread(self, consumer):
        consumer.start()
        assert consumer._thread is not None
        assert consumer._thread.is_alive()
        consumer.stop(timeout=2)

    def test_stop_sets_stop_event(self, consumer):
        consumer.start()
        consumer.stop(timeout=2)
        assert consumer._stop_event.is_set()

    def test_stop_joins_thread(self, consumer):
        consumer.start()
        consumer.stop(timeout=2)
        assert consumer._thread is None

    def test_running_property(self, consumer):
        assert consumer.running is True
        consumer._stop_event.set()
        assert consumer.running is False

    def test_process_message_calls_handler(self, consumer, handler_mock, redis_mock):
        task_data = {"task_id": "t1", "task_name": "agent", "args": [], "kwargs": {}}
        raw_data = {"data": task_data}
        consumer._process_message("msg-1", raw_data)
        handler_mock.assert_called_once_with(task_data)

    def test_process_message_acks_on_success(self, consumer, handler_mock, redis_mock):
        handler_mock.return_value = True
        task_data = {"task_id": "t1", "task_name": "agent"}
        consumer._process_message("msg-1", {"data": task_data})
        redis_mock.xack.assert_called()

    def test_process_message_retries_on_failure(self, consumer, handler_mock, redis_mock):
        handler_mock.return_value = False
        task_data = {"task_id": "t1", "task_name": "agent"}
        consumer._process_message("msg-1", {"data": task_data})
        # Should NOT ack on first failure (retries remaining)
        redis_mock.xack.assert_not_called()
        assert consumer._retry_counts["msg-1"] == 1

    def test_process_message_sends_to_dlq_after_max_retries(self, consumer, handler_mock, redis_mock):
        handler_mock.return_value = False
        task_data = {"task_id": "t1", "task_name": "agent"}

        # Exhaust retries (max_retries=3)
        consumer._process_message("msg-1", {"data": task_data})
        consumer._process_message("msg-1", {"data": task_data})
        consumer._process_message("msg-1", {"data": task_data})

        # After 3rd failure, should ack (moved to DLQ) and xadd to DLQ stream
        assert redis_mock.xack.called
        # DLQ publish: xadd called with dlq stream
        dlq_calls = [c for c in redis_mock.xadd.call_args_list
                     if "dlq:" in str(c)]
        assert len(dlq_calls) > 0

    def test_process_message_clears_retry_count_on_success(self, consumer, handler_mock, redis_mock):
        handler_mock.return_value = False
        task_data = {"task_id": "t1", "task_name": "agent"}
        consumer._process_message("msg-1", {"data": task_data})
        assert consumer._retry_counts["msg-1"] == 1

        # Now succeed
        handler_mock.return_value = True
        consumer._process_message("msg-1", {"data": task_data})
        assert "msg-1" not in consumer._retry_counts

    def test_process_message_handles_handler_exception(self, consumer, handler_mock, redis_mock):
        handler_mock.side_effect = RuntimeError("boom")
        task_data = {"task_id": "t1", "task_name": "agent"}
        consumer._process_message("msg-1", {"data": task_data})
        # Should retry (not crash)
        assert consumer._retry_counts["msg-1"] == 1

    def test_process_message_handles_invalid_json_string(self, consumer, handler_mock, redis_mock):
        raw_data = {"data": "not valid json {{{"}
        consumer._process_message("msg-1", raw_data)
        # Should ack (sent to DLQ as invalid)
        redis_mock.xack.assert_called()
        # Handler should NOT be called
        handler_mock.assert_not_called()

    def test_process_message_handles_json_string_data(self, consumer, handler_mock, redis_mock):
        task_data = {"task_id": "t1", "task_name": "agent"}
        raw_data = {"data": json.dumps(task_data)}
        handler_mock.return_value = True
        consumer._process_message("msg-1", raw_data)
        handler_mock.assert_called_once_with(task_data)

    def test_recover_pending_processes_existing_messages(self, consumer, handler_mock, redis_mock):
        task_data = {"task_id": "t1", "task_name": "agent"}
        # Simulate pending messages response
        redis_mock.xreadgroup.return_value = [
            [b"stream:work:task_distribution", [
                (b"1234-0", {b"data": json.dumps(task_data).encode()})
            ]]
        ]
        consumer._recover_pending()
        handler_mock.assert_called()

    def test_recover_pending_skips_deleted_messages(self, consumer, handler_mock, redis_mock):
        # Simulate pending with None data (deleted message) — StreamConsumer
        # filters these out during parsing, so _recover_pending never sees them.
        redis_mock.xreadgroup.return_value = [
            [b"stream:work:task_distribution", [
                (b"1234-0", None)
            ]]
        ]
        consumer._recover_pending()
        # None-data entries are filtered by StreamConsumer._parse_entries
        handler_mock.assert_not_called()
        redis_mock.xack.assert_not_called()

    def test_claim_abandoned_processes_claimed_messages(self, consumer, handler_mock, redis_mock):
        task_data = {"task_id": "t1", "task_name": "agent"}
        redis_mock.xautoclaim.return_value = [
            b"0-0",
            [(b"9999-0", {b"data": json.dumps(task_data).encode()})],
            []
        ]
        handler_mock.return_value = True
        consumer._claim_abandoned()
        handler_mock.assert_called_once_with(task_data)

    def test_claim_abandoned_does_nothing_when_empty(self, consumer, handler_mock, redis_mock):
        redis_mock.xautoclaim.return_value = [b"0-0", [], []]
        consumer._claim_abandoned()
        handler_mock.assert_not_called()

    def test_pending_count_delegates_to_stream_consumer(self, consumer, redis_mock):
        redis_mock.xpending.return_value = {"pending": 7}
        count = consumer.pending_count()
        assert count == 7

    def test_send_to_dlq_publishes_to_dlq_stream(self, consumer, redis_mock):
        task_data = {"task_id": "t1", "task_name": "agent"}
        consumer._send_to_dlq("msg-1", task_data, "test_error")

        # Find the DLQ xadd call
        dlq_calls = [c for c in redis_mock.xadd.call_args_list
                     if "dlq:" in str(c[0][0])]
        assert len(dlq_calls) == 1
        dlq_key = dlq_calls[0][0][0]
        assert dlq_key == "stream:dlq:work:task_distribution"

    def test_send_to_dlq_includes_error_info(self, consumer, redis_mock):
        task_data = {"task_id": "t1", "task_name": "agent"}
        consumer._send_to_dlq("msg-1", task_data, "handler_failed")

        dlq_calls = [c for c in redis_mock.xadd.call_args_list
                     if "dlq:" in str(c[0][0])]
        payload = dlq_calls[0][0][1]
        data = json.loads(payload["data"])
        assert data["error"] == "handler_failed"
        assert data["original_msg_id"] == "msg-1"
        assert data["original_stream"] == STREAM_NAME

    def test_send_to_dlq_handles_publish_failure(self, consumer, redis_mock):
        redis_mock.xadd.side_effect = [Exception("connection lost")]
        task_data = {"task_id": "t1", "task_name": "agent"}
        # Should not raise
        consumer._send_to_dlq("msg-1", task_data, "error")

    def test_consumer_loop_stops_on_stop_event(self, consumer, handler_mock, redis_mock):
        consumer.start()
        time.sleep(0.2)
        consumer.stop(timeout=2)
        assert not consumer._stop_event.is_set() or consumer._thread is None

    def test_start_is_idempotent(self, consumer):
        consumer.start()
        thread1 = consumer._thread
        consumer.start()  # Second call should be no-op
        assert consumer._thread is thread1
        consumer.stop(timeout=2)

    def test_multiple_messages_processed_sequentially(self, consumer, handler_mock, redis_mock):
        call_order = []
        def track_handler(task_data):
            call_order.append(task_data.get("task_id"))
            return True

        handler_mock.side_effect = track_handler

        for i in range(3):
            task_data = {"task_id": f"t{i}", "task_name": "agent"}
            consumer._process_message(f"msg-{i}", {"data": task_data})

        assert call_order == ["t0", "t1", "t2"]

    def test_run_loop_catches_exceptions(self, redis_mock, handler_mock):
        # Simulate an exception during consume
        redis_mock.xreadgroup.side_effect = [Exception("conn error"), []]

        c = TaskDistributionConsumer(
            redis_mock, handler_mock,
            poll_interval_ms=50, poll_count=1,
        )
        c.start()
        time.sleep(0.3)
        c.stop(timeout=2)
        # Should not crash — loop continues after exception


# ---------------------------------------------------------------------------
# _get_consumer_name Tests
# ---------------------------------------------------------------------------


class TestGetConsumerName:
    """Tests for _get_consumer_name helper."""

    def test_returns_hostname_when_set(self):
        with patch.dict("os.environ", {"HOSTNAME": "my-pod-xyz"}):
            assert _get_consumer_name() == "my-pod-xyz"

    def test_returns_uuid_based_name_when_no_hostname(self):
        with patch.dict("os.environ", {}, clear=True):
            import os
            orig = os.environ.pop("HOSTNAME", None)
            try:
                name = _get_consumer_name()
                assert name.startswith("consumer-")
                assert len(name) == len("consumer-") + 8
            finally:
                if orig is not None:
                    os.environ["HOSTNAME"] = orig

    def test_returns_empty_hostname_fallback(self):
        with patch.dict("os.environ", {"HOSTNAME": ""}):
            name = _get_consumer_name()
            assert name.startswith("consumer-")


# ---------------------------------------------------------------------------
# Integration-style Tests (producer + consumer)
# ---------------------------------------------------------------------------


class TestProducerConsumerIntegration:
    """Tests verifying producer/consumer protocol compatibility."""

    def test_producer_output_consumable_by_consumer(self, redis_mock):
        """Verify the data format produced is parseable by consumer."""
        captured_payload = {}

        def capture_xadd(key, fields, **kwargs):
            captured_payload.update(fields)
            return b"9999-0"

        redis_mock.xadd.side_effect = capture_xadd

        prod = TaskDistributionProducer(redis_mock)
        prod.submit_task("agent_task", args=[1], kwargs={"x": 2}, pool="agents",
                         meta={"project_id": 5}, task_id="tid-1")

        # Now simulate consuming this message
        handler = MagicMock(return_value=True)
        cons = TaskDistributionConsumer(redis_mock, handler, poll_interval_ms=50)

        raw_data = {"data": captured_payload.get("data", "")}
        cons._process_message("9999-0", raw_data)

        handler.assert_called_once()
        received = handler.call_args[0][0]
        assert received["task_id"] == "tid-1"
        assert received["task_name"] == "agent_task"
        assert received["args"] == [1]
        assert received["kwargs"] == {"x": 2}
        assert received["pool"] == "agents"
        assert received["meta"] == {"project_id": 5}

    def test_dlq_stream_name_is_deterministic(self, redis_mock):
        handler = MagicMock(return_value=False)
        cons = TaskDistributionConsumer(redis_mock, handler, max_retries=1)

        task_data = {"task_id": "t1", "task_name": "test"}
        cons._process_message("msg-1", {"data": task_data})

        dlq_calls = [c for c in redis_mock.xadd.call_args_list
                     if "dlq:" in str(c[0][0])]
        assert len(dlq_calls) == 1
        assert dlq_calls[0][0][0] == "stream:dlq:work:task_distribution"

    def test_consumer_default_stream_matches_producer_default(self):
        assert STREAM_NAME == "work:task_distribution"
        assert CONSUMER_GROUP == "task_workers"
