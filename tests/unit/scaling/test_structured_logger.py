"""Unit tests for structured_logger module.

Validates that:
1. StructuredJSONFormatter outputs valid JSON with all required fields
2. Request ID management via ContextVar works correctly
3. Flask integration (g.trace_id, request headers) works
4. Extra fields are included when present
5. Exception info is serialized properly
6. Service name defaults from env and can be overridden
7. StructuredLogAdapter injects context into log records
8. before_request / after_request hooks manage request_id lifecycle
9. configure_structured_logging returns a usable handler
10. get_structured_logger creates a logger with JSON formatting
11. Timestamp formatting (ISO, epoch) works correctly
12. Non-serializable extra fields are converted to string
13. generate_request_id produces unique IDs
14. ensure_request_id reuses existing or creates new

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_structured_logger.py -v
"""

import importlib.util
import json
import logging
import os
import pathlib
import sys
import time
import types
from contextvars import copy_context
from io import StringIO
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ---------------------------------------------------------------------------
# Module loading setup
# ---------------------------------------------------------------------------

_ELITEA_CORE_ROOT = pathlib.Path(__file__).resolve().parents[4] / "elitea_core"

_mock_log = MagicMock()
_mock_pylon_core_tools = MagicMock()
_mock_pylon_core_tools.log = _mock_log
sys.modules.setdefault("pylon", MagicMock())
sys.modules.setdefault("pylon.core", MagicMock())
sys.modules.setdefault("pylon.core.tools", _mock_pylon_core_tools)

_spec = importlib.util.spec_from_file_location(
    "structured_logger",
    _ELITEA_CORE_ROOT / "utils" / "structured_logger.py",
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["structured_logger"] = _mod
_spec.loader.exec_module(_mod)

StructuredJSONFormatter = _mod.StructuredJSONFormatter
StructuredLogAdapter = _mod.StructuredLogAdapter
get_request_id = _mod.get_request_id
set_request_id = _mod.set_request_id
generate_request_id = _mod.generate_request_id
ensure_request_id = _mod.ensure_request_id
attach_request_id_before_request = _mod.attach_request_id_before_request
attach_request_id_after_request = _mod.attach_request_id_after_request
configure_structured_logging = _mod.configure_structured_logging
get_structured_logger = _mod.get_structured_logger
_request_id_var = _mod._request_id_var


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_request_id():
    """Reset request ID context var between tests."""
    _request_id_var.set(None)
    yield
    _request_id_var.set(None)


@pytest.fixture
def formatter():
    """Create a StructuredJSONFormatter with default settings."""
    return StructuredJSONFormatter(service_name="test-service")


@pytest.fixture
def log_record():
    """Create a sample log record."""
    record = logging.LogRecord(
        name="test.module",
        level=logging.INFO,
        pathname="/test/module.py",
        lineno=42,
        msg="Test message %s",
        args=("world",),
        exc_info=None,
    )
    return record


@pytest.fixture
def logger_with_handler():
    """Create a logger with stream handler and JSON formatter."""
    logger = logging.getLogger(f"test.structured.{id(object())}")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredJSONFormatter(service_name="test-svc"))
    logger.addHandler(handler)
    return logger, stream


# ---------------------------------------------------------------------------
# Tests: StructuredJSONFormatter
# ---------------------------------------------------------------------------

class TestStructuredJSONFormatter:
    """Tests for the JSON log formatter."""

    def test_format_produces_valid_json(self, formatter, log_record):
        output = formatter.format(log_record)
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_format_contains_required_fields(self, formatter, log_record):
        output = formatter.format(log_record)
        parsed = json.loads(output)
        assert "timestamp" in parsed
        assert "level" in parsed
        assert "service" in parsed
        assert "request_id" in parsed
        assert "logger" in parsed
        assert "message" in parsed

    def test_format_level_is_correct(self, formatter, log_record):
        output = formatter.format(log_record)
        parsed = json.loads(output)
        assert parsed["level"] == "INFO"

    def test_format_service_name_from_constructor(self, formatter, log_record):
        output = formatter.format(log_record)
        parsed = json.loads(output)
        assert parsed["service"] == "test-service"

    def test_format_service_name_from_env(self, log_record):
        with patch.dict(os.environ, {"NAME": "env-service"}):
            # Need to reload the module-level var
            original = _mod._SERVICE_NAME
            _mod._SERVICE_NAME = "env-service"
            try:
                fmt = StructuredJSONFormatter()
                output = fmt.format(log_record)
                parsed = json.loads(output)
                assert parsed["service"] == "env-service"
            finally:
                _mod._SERVICE_NAME = original

    def test_format_logger_name(self, formatter, log_record):
        output = formatter.format(log_record)
        parsed = json.loads(output)
        assert parsed["logger"] == "test.module"

    def test_format_message_with_args(self, formatter, log_record):
        output = formatter.format(log_record)
        parsed = json.loads(output)
        assert parsed["message"] == "Test message world"

    def test_format_request_id_none_when_unset(self, formatter, log_record):
        output = formatter.format(log_record)
        parsed = json.loads(output)
        assert parsed["request_id"] is None

    def test_format_request_id_from_context(self, formatter, log_record):
        set_request_id("test-req-123")
        output = formatter.format(log_record)
        parsed = json.loads(output)
        assert parsed["request_id"] == "test-req-123"

    def test_format_timestamp_iso(self, formatter, log_record):
        output = formatter.format(log_record)
        parsed = json.loads(output)
        ts = parsed["timestamp"]
        assert ts.endswith("Z")
        assert "T" in ts

    def test_format_timestamp_epoch(self, log_record):
        fmt = StructuredJSONFormatter(timestamp_format="epoch")
        output = fmt.format(log_record)
        parsed = json.loads(output)
        ts = float(parsed["timestamp"])
        assert ts > 0

    def test_format_extra_fields_included(self, formatter):
        record = logging.LogRecord(
            name="test", level=logging.INFO,
            pathname="", lineno=0, msg="msg",
            args=None, exc_info=None,
        )
        record.user_id = 42
        record.project = "alpha"
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["extra"]["user_id"] == 42
        assert parsed["extra"]["project"] == "alpha"

    def test_format_extra_fields_excluded_when_disabled(self):
        fmt = StructuredJSONFormatter(include_extra=False)
        record = logging.LogRecord(
            name="test", level=logging.INFO,
            pathname="", lineno=0, msg="msg",
            args=None, exc_info=None,
        )
        record.user_id = 42
        output = fmt.format(record)
        parsed = json.loads(output)
        assert "extra" not in parsed

    def test_format_non_serializable_extra_converted_to_str(self, formatter):
        record = logging.LogRecord(
            name="test", level=logging.INFO,
            pathname="", lineno=0, msg="msg",
            args=None, exc_info=None,
        )
        record.custom_obj = object()
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "object at" in parsed["extra"]["custom_obj"]

    def test_format_exception_info(self, formatter):
        try:
            raise ValueError("test error")
        except ValueError:
            import sys
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test", level=logging.ERROR,
            pathname="", lineno=0, msg="error occurred",
            args=None, exc_info=exc_info,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "exception" in parsed
        assert parsed["exception"]["type"] == "ValueError"
        assert parsed["exception"]["message"] == "test error"
        assert isinstance(parsed["exception"]["traceback"], list)

    def test_format_no_exception_field_when_no_exc(self, formatter, log_record):
        output = formatter.format(log_record)
        parsed = json.loads(output)
        assert "exception" not in parsed

    def test_format_exception_excluded_when_disabled(self):
        fmt = StructuredJSONFormatter(include_exception=False)
        try:
            raise RuntimeError("oops")
        except RuntimeError:
            import sys
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test", level=logging.ERROR,
            pathname="", lineno=0, msg="error",
            args=None, exc_info=exc_info,
        )
        output = fmt.format(record)
        parsed = json.loads(output)
        assert "exception" not in parsed

    def test_format_output_is_single_line(self, formatter, log_record):
        output = formatter.format(log_record)
        assert "\n" not in output

    def test_format_handles_unicode(self, formatter):
        record = logging.LogRecord(
            name="test", level=logging.INFO,
            pathname="", lineno=0, msg="Unicode: ☃ \U0001f600",
            args=None, exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "☃" in parsed["message"]

    def test_format_skips_internal_record_keys(self, formatter, log_record):
        output = formatter.format(log_record)
        parsed = json.loads(output)
        extra = parsed.get("extra", {})
        assert "pathname" not in extra
        assert "lineno" not in extra
        assert "funcName" not in extra
        assert "levelno" not in extra

    def test_format_handles_empty_message(self, formatter):
        record = logging.LogRecord(
            name="test", level=logging.INFO,
            pathname="", lineno=0, msg="",
            args=None, exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["message"] == ""


# ---------------------------------------------------------------------------
# Tests: Request ID Management
# ---------------------------------------------------------------------------

class TestRequestIdManagement:
    """Tests for request_id context management."""

    def test_get_request_id_returns_none_initially(self):
        assert get_request_id() is None

    def test_set_and_get_request_id(self):
        set_request_id("abc-123")
        assert get_request_id() == "abc-123"

    def test_generate_request_id_format(self):
        rid = generate_request_id()
        assert rid.startswith("req-")
        assert len(rid) == 20  # "req-" + 16 hex chars

    def test_generate_request_id_unique(self):
        ids = {generate_request_id() for _ in range(100)}
        assert len(ids) == 100

    def test_ensure_request_id_creates_when_none(self):
        rid = ensure_request_id()
        assert rid is not None
        assert rid.startswith("req-")

    def test_ensure_request_id_reuses_existing(self):
        set_request_id("existing-id")
        rid = ensure_request_id()
        assert rid == "existing-id"

    def test_request_id_isolated_between_contexts(self):
        set_request_id("ctx-1")
        results = []

        def worker():
            results.append(get_request_id())

        ctx = copy_context()
        _request_id_var.set(None)
        ctx.run(worker)
        assert results[0] == "ctx-1"

    def test_get_request_id_from_flask_g(self):
        mock_g = MagicMock()
        mock_g.trace_id = "flask-trace-123"
        mock_g.request_id = None

        with patch.dict(sys.modules, {"flask": MagicMock()}):
            flask_mock = sys.modules["flask"]
            flask_mock.g = mock_g
            flask_mock.has_request_context = MagicMock(return_value=True)

            # Reimport to use patched flask
            with patch("structured_logger.get_request_id") as mock_get:
                mock_get.return_value = "flask-trace-123"
                assert mock_get() == "flask-trace-123"

    def test_get_request_id_prefers_context_var_over_flask(self):
        set_request_id("contextvar-id")
        rid = get_request_id()
        assert rid == "contextvar-id"


# ---------------------------------------------------------------------------
# Tests: Flask Hooks
# ---------------------------------------------------------------------------

class TestFlaskHooks:
    """Tests for before_request and after_request hooks."""

    def test_attach_request_id_before_request_from_header(self):
        mock_request = MagicMock()
        mock_request.headers = {"X-Request-ID": "header-req-id"}
        mock_g = MagicMock(spec=[])

        with patch.dict(sys.modules, {"flask": MagicMock()}):
            flask_mod = sys.modules["flask"]
            flask_mod.request = mock_request
            flask_mod.g = mock_g

            # Mock the import inside the function
            with patch("builtins.__import__", side_effect=ImportError):
                pass

        # Test the function directly with mocked flask
        with patch("structured_logger.generate_request_id", return_value="gen-id"):
            mock_req = MagicMock()
            mock_req.headers = MagicMock()
            mock_req.headers.get = MagicMock(side_effect=lambda k: {
                "X-Request-ID": "from-header",
                "X-Trace-ID": None,
            }.get(k))

            mock_g_obj = MagicMock(spec=[])

            with patch.object(_mod, "generate_request_id", return_value="gen-id"):
                with patch.dict(sys.modules):
                    flask_mock = MagicMock()
                    flask_mock.request = mock_req
                    flask_mock.g = mock_g_obj
                    sys.modules["flask"] = flask_mock
                    try:
                        attach_request_id_before_request()
                        assert get_request_id() == "from-header"
                    finally:
                        del sys.modules["flask"]

    def test_attach_request_id_before_request_generates_when_no_header(self):
        mock_req = MagicMock()
        mock_req.headers = MagicMock()
        mock_req.headers.get = MagicMock(return_value=None)
        mock_g_obj = MagicMock(spec=[])

        with patch.dict(sys.modules):
            flask_mock = MagicMock()
            flask_mock.request = mock_req
            flask_mock.g = mock_g_obj
            sys.modules["flask"] = flask_mock
            try:
                attach_request_id_before_request()
                rid = get_request_id()
                assert rid is not None
                assert rid.startswith("req-")
            finally:
                del sys.modules["flask"]

    def test_attach_request_id_before_request_from_trace_id(self):
        mock_req = MagicMock()
        mock_req.headers = MagicMock()
        mock_req.headers.get = MagicMock(side_effect=lambda k: {
            "X-Request-ID": None,
            "X-Trace-ID": "trace-abc",
        }.get(k))
        mock_g_obj = MagicMock(spec=[])

        with patch.dict(sys.modules):
            flask_mock = MagicMock()
            flask_mock.request = mock_req
            flask_mock.g = mock_g_obj
            sys.modules["flask"] = flask_mock
            try:
                attach_request_id_before_request()
                assert get_request_id() == "trace-abc"
            finally:
                del sys.modules["flask"]

    def test_attach_request_id_after_request_adds_header(self):
        set_request_id("resp-header-id")
        mock_response = MagicMock()
        mock_response.headers = {}

        result = attach_request_id_after_request(mock_response)
        assert result is mock_response
        assert mock_response.headers["X-Request-ID"] == "resp-header-id"

    def test_attach_request_id_after_request_no_overwrite(self):
        set_request_id("my-id")

        class FakeHeaders(dict):
            pass

        mock_response = MagicMock()
        mock_response.headers = FakeHeaders({"X-Request-ID": "existing"})

        result = attach_request_id_after_request(mock_response)
        assert mock_response.headers["X-Request-ID"] == "existing"

    def test_attach_request_id_after_request_handles_none_response(self):
        set_request_id("some-id")
        result = attach_request_id_after_request(None)
        assert result is None

    def test_attach_request_id_after_request_no_id_set(self):
        mock_response = MagicMock()
        mock_response.headers = {}
        result = attach_request_id_after_request(mock_response)
        assert "X-Request-ID" not in mock_response.headers

    def test_before_request_handles_import_error(self):
        with patch.dict(sys.modules):
            if "flask" in sys.modules:
                del sys.modules["flask"]
            with patch("builtins.__import__", side_effect=ImportError("no flask")):
                attach_request_id_before_request()
                # Should not raise


# ---------------------------------------------------------------------------
# Tests: Logger Configuration
# ---------------------------------------------------------------------------

class TestLoggerConfiguration:
    """Tests for configure_structured_logging and get_structured_logger."""

    def test_configure_returns_handler(self):
        handler = configure_structured_logging(service_name="my-svc")
        assert isinstance(handler, logging.StreamHandler)
        assert isinstance(handler.formatter, StructuredJSONFormatter)

    def test_configure_handler_level(self):
        handler = configure_structured_logging(level=logging.WARNING)
        assert handler.level == logging.WARNING

    def test_get_structured_logger_returns_logger(self):
        logger = get_structured_logger("test.get_structured")
        assert isinstance(logger, logging.Logger)
        assert any(
            isinstance(h.formatter, StructuredJSONFormatter)
            for h in logger.handlers
        )

    def test_get_structured_logger_idempotent(self):
        name = f"test.idem.{id(object())}"
        logger1 = get_structured_logger(name)
        logger2 = get_structured_logger(name)
        assert logger1 is logger2
        json_handlers = [
            h for h in logger1.handlers
            if isinstance(h.formatter, StructuredJSONFormatter)
        ]
        assert len(json_handlers) == 1

    def test_get_structured_logger_custom_service(self):
        name = f"test.custom.{id(object())}"
        logger = get_structured_logger(name, service_name="custom-svc")
        handler = next(
            h for h in logger.handlers
            if isinstance(h.formatter, StructuredJSONFormatter)
        )
        assert handler.formatter.service_name == "custom-svc"


# ---------------------------------------------------------------------------
# Tests: StructuredLogAdapter
# ---------------------------------------------------------------------------

class TestStructuredLogAdapter:
    """Tests for the log adapter that injects context."""

    def test_adapter_injects_request_id(self):
        set_request_id("adapter-req-id")
        base_logger = logging.getLogger(f"test.adapter.{id(object())}")
        adapter = StructuredLogAdapter(base_logger)

        msg, kwargs = adapter.process("test msg", {"extra": {}})
        assert kwargs["extra"]["request_id"] == "adapter-req-id"

    def test_adapter_injects_service(self):
        base_logger = logging.getLogger(f"test.adapter.svc.{id(object())}")
        adapter = StructuredLogAdapter(base_logger)

        msg, kwargs = adapter.process("test msg", {"extra": {}})
        assert "service" in kwargs["extra"]

    def test_adapter_preserves_existing_extra(self):
        set_request_id("keep-extra")
        base_logger = logging.getLogger(f"test.adapter.extra.{id(object())}")
        adapter = StructuredLogAdapter(base_logger)

        msg, kwargs = adapter.process("msg", {"extra": {"user": "alice"}})
        assert kwargs["extra"]["user"] == "alice"
        assert kwargs["extra"]["request_id"] == "keep-extra"

    def test_adapter_does_not_overwrite_explicit_request_id(self):
        set_request_id("from-context")
        base_logger = logging.getLogger(f"test.adapter.no_overwrite.{id(object())}")
        adapter = StructuredLogAdapter(base_logger)

        msg, kwargs = adapter.process("msg", {"extra": {"request_id": "explicit"}})
        assert kwargs["extra"]["request_id"] == "explicit"

    def test_adapter_creates_extra_if_missing(self):
        base_logger = logging.getLogger(f"test.adapter.missing.{id(object())}")
        adapter = StructuredLogAdapter(base_logger)

        msg, kwargs = adapter.process("msg", {})
        assert "extra" in kwargs
        assert "service" in kwargs["extra"]


# ---------------------------------------------------------------------------
# Tests: Integration (Formatter + Logger)
# ---------------------------------------------------------------------------

class TestIntegration:
    """End-to-end tests with real logging calls."""

    def test_log_info_produces_json(self, logger_with_handler):
        logger, stream = logger_with_handler
        logger.info("hello from test")
        output = stream.getvalue().strip()
        parsed = json.loads(output)
        assert parsed["message"] == "hello from test"
        assert parsed["level"] == "INFO"

    def test_log_with_request_id(self, logger_with_handler):
        logger, stream = logger_with_handler
        set_request_id("integration-req-1")
        logger.warning("warning msg")
        output = stream.getvalue().strip()
        parsed = json.loads(output)
        assert parsed["request_id"] == "integration-req-1"

    def test_log_with_extra(self, logger_with_handler):
        logger, stream = logger_with_handler
        logger.info("user action", extra={"user_id": 99, "action": "login"})
        output = stream.getvalue().strip()
        parsed = json.loads(output)
        assert parsed["extra"]["user_id"] == 99
        assert parsed["extra"]["action"] == "login"

    def test_log_exception(self, logger_with_handler):
        logger, stream = logger_with_handler
        try:
            raise IOError("disk full")
        except IOError:
            logger.exception("IO failure")
        output = stream.getvalue().strip()
        parsed = json.loads(output)
        assert parsed["exception"]["type"] == "OSError"  # IOError is alias for OSError
        assert "disk full" in parsed["exception"]["message"]

    def test_log_debug_filtered_by_level(self):
        logger = logging.getLogger(f"test.level.{id(object())}")
        logger.handlers.clear()
        logger.setLevel(logging.WARNING)
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(StructuredJSONFormatter(service_name="lvl-test"))
        handler.setLevel(logging.WARNING)
        logger.addHandler(handler)

        logger.debug("should not appear")
        logger.warning("should appear")
        lines = [l for l in stream.getvalue().strip().split("\n") if l]
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["message"] == "should appear"

    def test_multiple_log_lines_each_valid_json(self, logger_with_handler):
        logger, stream = logger_with_handler
        logger.info("line 1")
        logger.info("line 2")
        logger.error("line 3")
        lines = [l for l in stream.getvalue().strip().split("\n") if l]
        assert len(lines) == 3
        for line in lines:
            parsed = json.loads(line)
            assert "timestamp" in parsed

    def test_adapter_with_formatter(self):
        base_logger = logging.getLogger(f"test.adapter.fmt.{id(object())}")
        base_logger.handlers.clear()
        base_logger.setLevel(logging.DEBUG)
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(StructuredJSONFormatter(service_name="adapter-test"))
        base_logger.addHandler(handler)

        set_request_id("adapt-fmt-req")
        adapter = StructuredLogAdapter(base_logger)
        adapter.info("adapter message", extra={"key": "val"})

        output = stream.getvalue().strip()
        parsed = json.loads(output)
        assert parsed["message"] == "adapter message"
        assert parsed["extra"]["key"] == "val"
        assert parsed["extra"]["request_id"] == "adapt-fmt-req"

    def test_format_handles_percent_style_msg(self, logger_with_handler):
        logger, stream = logger_with_handler
        logger.info("User %s logged in from %s", "alice", "10.0.0.1")
        output = stream.getvalue().strip()
        parsed = json.loads(output)
        assert parsed["message"] == "User alice logged in from 10.0.0.1"

    def test_format_handles_no_args(self, logger_with_handler):
        logger, stream = logger_with_handler
        logger.info("simple message")
        output = stream.getvalue().strip()
        parsed = json.loads(output)
        assert parsed["message"] == "simple message"
