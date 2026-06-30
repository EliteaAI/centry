"""
Unit tests for elitea_core/utils/tracing.py — distributed tracing module.

Tests cover:
- TracerProvider initialization
- @traced and @traced_async decorators
- Redis, HTTP, and SQLAlchemy instrumentation
- Trace context propagation (inject/extract)
- Socket.IO context propagation helpers
- Shutdown behavior
"""

import os
import sys
import asyncio
from unittest.mock import patch, MagicMock, PropertyMock
import pytest


# ---------------------------------------------------------------------------
# Fixture: isolate tracing module state between tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_tracing_state(monkeypatch):
    """Reset tracing module globals before each test."""
    # We need to import and reset module state
    # Remove cached module to get fresh state
    mods_to_remove = [k for k in sys.modules if k.startswith("elitea_core.utils.tracing")]
    for mod in mods_to_remove:
        del sys.modules[mod]

    # Mock pylon imports
    mock_log = MagicMock()
    mock_log.info = MagicMock()
    mock_log.warning = MagicMock()
    mock_log.debug = MagicMock()

    monkeypatch.setitem(sys.modules, "pylon", MagicMock())
    monkeypatch.setitem(sys.modules, "pylon.core", MagicMock())
    monkeypatch.setitem(sys.modules, "pylon.core.tools", MagicMock(log=mock_log))

    yield


@pytest.fixture
def tracing_module(reset_tracing_state):
    """Import a fresh tracing module."""
    import importlib
    # Remove if cached
    if "elitea_core.utils.tracing" in sys.modules:
        del sys.modules["elitea_core.utils.tracing"]
    if "elitea_core" in sys.modules:
        del sys.modules["elitea_core"]
    if "elitea_core.utils" in sys.modules:
        del sys.modules["elitea_core.utils"]

    # Add paths
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
    elitea_core_parent = os.path.dirname(os.path.join(project_root, "elitea_core"))
    if elitea_core_parent not in sys.path:
        sys.path.insert(0, elitea_core_parent)

    from elitea_core.utils import tracing
    # Reset state
    tracing._tracer = None
    tracing._tracer_provider = None
    tracing._initialized = False
    return tracing


# ===========================================================================
# init_tracing tests
# ===========================================================================

class TestInitTracing:
    """Tests for init_tracing()."""

    def test_disabled_by_default(self, tracing_module):
        """Tracing is disabled when TRACING_ENABLED is not set."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("TRACING_ENABLED", None)
            result = tracing_module.init_tracing()
        assert result is False
        assert tracing_module.get_tracer() is None

    def test_disabled_when_env_false(self, tracing_module):
        """Tracing is disabled when TRACING_ENABLED=false."""
        with patch.dict(os.environ, {"TRACING_ENABLED": "false"}):
            result = tracing_module.init_tracing()
        assert result is False

    def test_enabled_via_parameter(self, tracing_module):
        """Tracing can be explicitly enabled via parameter."""
        mock_provider = MagicMock()
        mock_tracer = MagicMock()

        with patch("opentelemetry.sdk.trace.TracerProvider", return_value=mock_provider), \
             patch("opentelemetry.sdk.trace.export.BatchSpanProcessor"), \
             patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter"), \
             patch("opentelemetry.trace.set_tracer_provider"), \
             patch("opentelemetry.trace.get_tracer", return_value=mock_tracer), \
             patch("opentelemetry.sdk.resources.Resource.create"):
            result = tracing_module.init_tracing(enabled=True)

        assert result is True
        assert tracing_module.get_tracer() is mock_tracer

    def test_enabled_via_env(self, tracing_module):
        """Tracing is enabled when TRACING_ENABLED=true."""
        mock_provider = MagicMock()
        mock_tracer = MagicMock()

        with patch.dict(os.environ, {"TRACING_ENABLED": "true"}), \
             patch("opentelemetry.sdk.trace.TracerProvider", return_value=mock_provider), \
             patch("opentelemetry.sdk.trace.export.BatchSpanProcessor"), \
             patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter"), \
             patch("opentelemetry.trace.set_tracer_provider"), \
             patch("opentelemetry.trace.get_tracer", return_value=mock_tracer), \
             patch("opentelemetry.sdk.resources.Resource.create"):
            result = tracing_module.init_tracing()

        assert result is True

    def test_custom_service_name(self, tracing_module):
        """Custom service name is passed to resource and tracer."""
        with patch("opentelemetry.sdk.trace.TracerProvider") as mock_tp, \
             patch("opentelemetry.sdk.trace.export.BatchSpanProcessor"), \
             patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter"), \
             patch("opentelemetry.trace.set_tracer_provider"), \
             patch("opentelemetry.trace.get_tracer") as mock_get_tracer, \
             patch("opentelemetry.sdk.resources.Resource.create") as mock_resource:
            tracing_module.init_tracing(service_name="pylon-indexer", enabled=True)
            mock_get_tracer.assert_called_once_with("pylon-indexer", "1.0.0")

    def test_custom_endpoint(self, tracing_module):
        """Custom OTLP endpoint is used."""
        with patch("opentelemetry.sdk.trace.TracerProvider"), \
             patch("opentelemetry.sdk.trace.export.BatchSpanProcessor"), \
             patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter") as mock_exp, \
             patch("opentelemetry.trace.set_tracer_provider"), \
             patch("opentelemetry.trace.get_tracer"), \
             patch("opentelemetry.sdk.resources.Resource.create"):
            tracing_module.init_tracing(
                otlp_endpoint="http://otel-collector:4317", enabled=True
            )
            mock_exp.assert_called_once_with(
                endpoint="http://otel-collector:4317", insecure=True
            )

    def test_endpoint_from_env(self, tracing_module):
        """OTLP endpoint falls back to OTEL_EXPORTER_OTLP_ENDPOINT env var."""
        with patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://custom:4317"}), \
             patch("opentelemetry.sdk.trace.TracerProvider"), \
             patch("opentelemetry.sdk.trace.export.BatchSpanProcessor"), \
             patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter") as mock_exp, \
             patch("opentelemetry.trace.set_tracer_provider"), \
             patch("opentelemetry.trace.get_tracer"), \
             patch("opentelemetry.sdk.resources.Resource.create"):
            tracing_module.init_tracing(enabled=True)
            mock_exp.assert_called_once_with(
                endpoint="http://custom:4317", insecure=True
            )

    def test_https_endpoint_not_insecure(self, tracing_module):
        """HTTPS endpoints use secure connection."""
        with patch("opentelemetry.sdk.trace.TracerProvider"), \
             patch("opentelemetry.sdk.trace.export.BatchSpanProcessor"), \
             patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter") as mock_exp, \
             patch("opentelemetry.trace.set_tracer_provider"), \
             patch("opentelemetry.trace.get_tracer"), \
             patch("opentelemetry.sdk.resources.Resource.create"):
            tracing_module.init_tracing(
                otlp_endpoint="https://otel.prod:4317", enabled=True
            )
            mock_exp.assert_called_once_with(
                endpoint="https://otel.prod:4317", insecure=False
            )

    def test_idempotent_init(self, tracing_module):
        """Calling init_tracing twice does not reinitialize."""
        mock_tracer = MagicMock()
        with patch("opentelemetry.sdk.trace.TracerProvider"), \
             patch("opentelemetry.sdk.trace.export.BatchSpanProcessor"), \
             patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter"), \
             patch("opentelemetry.trace.set_tracer_provider"), \
             patch("opentelemetry.trace.get_tracer", return_value=mock_tracer), \
             patch("opentelemetry.sdk.resources.Resource.create"):
            tracing_module.init_tracing(enabled=True)

        # Second call should not re-import or recreate
        result = tracing_module.init_tracing(enabled=True)
        assert result is True  # Returns True because tracer exists

    def test_init_failure_returns_false(self, tracing_module):
        """If OTel setup raises, init returns False gracefully."""
        with patch("opentelemetry.sdk.trace.TracerProvider", side_effect=RuntimeError("fail")), \
             patch("opentelemetry.sdk.resources.Resource.create"):
            result = tracing_module.init_tracing(enabled=True)
        assert result is False
        assert tracing_module.get_tracer() is None

    def test_uses_plugin_tracer_when_available(self, tracing_module):
        """Delegates to tracing plugin if already loaded."""
        mock_tracer = MagicMock()
        mock_tracing_module = MagicMock()
        mock_tracing_module.enabled = True
        mock_tracing_module.get_tracer.return_value = mock_tracer

        mock_this = MagicMock()
        mock_this.for_module.return_value.module = mock_tracing_module

        with patch.dict(sys.modules, {"tools": MagicMock(this=mock_this)}):
            result = tracing_module.init_tracing(enabled=True)

        assert result is True
        assert tracing_module.get_tracer() is mock_tracer

    def test_falls_through_if_plugin_disabled(self, tracing_module):
        """Falls back to standalone if tracing plugin is disabled."""
        mock_tracing_module = MagicMock()
        mock_tracing_module.enabled = False

        mock_this = MagicMock()
        mock_this.for_module.return_value.module = mock_tracing_module

        mock_tracer = MagicMock()
        with patch.dict(sys.modules, {"tools": MagicMock(this=mock_this)}), \
             patch("opentelemetry.sdk.trace.TracerProvider"), \
             patch("opentelemetry.sdk.trace.export.BatchSpanProcessor"), \
             patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter"), \
             patch("opentelemetry.trace.set_tracer_provider"), \
             patch("opentelemetry.trace.get_tracer", return_value=mock_tracer), \
             patch("opentelemetry.sdk.resources.Resource.create"):
            result = tracing_module.init_tracing(enabled=True)

        assert result is True
        assert tracing_module.get_tracer() is mock_tracer


# ===========================================================================
# get_tracer tests
# ===========================================================================

class TestGetTracer:
    """Tests for get_tracer()."""

    def test_returns_none_when_disabled(self, tracing_module):
        """Returns None when tracing is not enabled."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("TRACING_ENABLED", None)
            result = tracing_module.get_tracer()
        assert result is None

    def test_auto_initializes(self, tracing_module):
        """get_tracer triggers initialization if not done."""
        # _initialized is False, so it will call init_tracing
        with patch.dict(os.environ, {"TRACING_ENABLED": "false"}):
            result = tracing_module.get_tracer()
        assert result is None
        assert tracing_module._initialized is True

    def test_returns_tracer_when_enabled(self, tracing_module):
        """Returns tracer when already initialized."""
        mock_tracer = MagicMock()
        tracing_module._tracer = mock_tracer
        tracing_module._initialized = True
        assert tracing_module.get_tracer() is mock_tracer


# ===========================================================================
# shutdown tests
# ===========================================================================

class TestShutdown:
    """Tests for shutdown()."""

    def test_shutdown_flushes_and_shuts_down(self, tracing_module):
        """Shutdown calls force_flush and shutdown on provider."""
        mock_provider = MagicMock()
        tracing_module._tracer_provider = mock_provider

        tracing_module.shutdown()

        mock_provider.force_flush.assert_called_once()
        mock_provider.shutdown.assert_called_once()
        assert tracing_module._tracer_provider is None

    def test_shutdown_no_op_without_provider(self, tracing_module):
        """Shutdown is safe when no provider exists."""
        tracing_module._tracer_provider = None
        tracing_module.shutdown()  # Should not raise

    def test_shutdown_handles_error(self, tracing_module):
        """Shutdown handles errors gracefully."""
        mock_provider = MagicMock()
        mock_provider.force_flush.side_effect = RuntimeError("flush error")
        tracing_module._tracer_provider = mock_provider

        tracing_module.shutdown()  # Should not raise
        assert tracing_module._tracer_provider is None


# ===========================================================================
# @traced decorator tests
# ===========================================================================

class TestTracedDecorator:
    """Tests for the @traced decorator."""

    def test_no_op_when_tracing_disabled(self, tracing_module):
        """Decorated function runs normally when tracing is off."""
        tracing_module._initialized = True
        tracing_module._tracer = None

        @tracing_module.traced("test_op")
        def my_func(x):
            return x * 2

        assert my_func(5) == 10

    def test_creates_span_when_enabled(self, tracing_module):
        """Creates a span when tracer is available."""
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        mock_tracer.start_as_current_span.return_value = mock_span

        tracing_module._initialized = True
        tracing_module._tracer = mock_tracer

        @tracing_module.traced("test_operation")
        def my_func():
            return 42

        result = my_func()
        assert result == 42
        mock_tracer.start_as_current_span.assert_called_once()
        call_args = mock_tracer.start_as_current_span.call_args
        assert call_args[0][0] == "test_operation"

    def test_default_name_uses_qualname(self, tracing_module):
        """Uses module.qualname when no name is provided."""
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        mock_tracer.start_as_current_span.return_value = mock_span

        tracing_module._initialized = True
        tracing_module._tracer = mock_tracer

        @tracing_module.traced()
        def some_function():
            return "ok"

        some_function()
        call_args = mock_tracer.start_as_current_span.call_args
        assert "some_function" in call_args[0][0]

    def test_records_exception(self, tracing_module):
        """Records exceptions on the span."""
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        mock_tracer.start_as_current_span.return_value = mock_span

        tracing_module._initialized = True
        tracing_module._tracer = mock_tracer

        @tracing_module.traced("failing_op")
        def failing_func():
            raise ValueError("test error")

        with pytest.raises(ValueError, match="test error"):
            failing_func()

        mock_span.record_exception.assert_called_once()

    def test_no_record_exception_when_disabled(self, tracing_module):
        """Does not record exceptions when record_exception=False."""
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        mock_tracer.start_as_current_span.return_value = mock_span

        tracing_module._initialized = True
        tracing_module._tracer = mock_tracer

        @tracing_module.traced("op", record_exception=False)
        def fail():
            raise RuntimeError("err")

        with pytest.raises(RuntimeError):
            fail()

        mock_span.record_exception.assert_not_called()

    def test_custom_attributes(self, tracing_module):
        """Passes custom attributes to the span."""
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        mock_tracer.start_as_current_span.return_value = mock_span

        tracing_module._initialized = True
        tracing_module._tracer = mock_tracer

        @tracing_module.traced("op", attributes={"component": "indexer", "version": "1.0"})
        def op():
            return True

        op()
        call_kwargs = mock_tracer.start_as_current_span.call_args[1]
        assert call_kwargs["attributes"]["component"] == "indexer"
        assert call_kwargs["attributes"]["version"] == "1.0"

    def test_span_kind(self, tracing_module):
        """Passes span kind correctly."""
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        mock_tracer.start_as_current_span.return_value = mock_span

        tracing_module._initialized = True
        tracing_module._tracer = mock_tracer

        from opentelemetry.trace import SpanKind

        @tracing_module.traced("client_call", kind="client")
        def client_op():
            return True

        client_op()
        call_kwargs = mock_tracer.start_as_current_span.call_args[1]
        assert call_kwargs["kind"] == SpanKind.CLIENT

    def test_preserves_function_metadata(self, tracing_module):
        """@traced preserves __name__ and __doc__."""
        tracing_module._initialized = True
        tracing_module._tracer = None

        @tracing_module.traced("op")
        def documented_func():
            """My docstring."""
            pass

        assert documented_func.__name__ == "documented_func"
        assert documented_func.__doc__ == "My docstring."


# ===========================================================================
# @traced_async decorator tests
# ===========================================================================

class TestTracedAsyncDecorator:
    """Tests for the @traced_async decorator."""

    def test_no_op_when_tracing_disabled(self, tracing_module):
        """Async decorated function runs normally when tracing is off."""
        tracing_module._initialized = True
        tracing_module._tracer = None

        @tracing_module.traced_async("async_op")
        async def my_async_func(x):
            return x + 1

        result = asyncio.get_event_loop().run_until_complete(my_async_func(10))
        assert result == 11

    def test_creates_span_for_async(self, tracing_module):
        """Creates span for async functions."""
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        mock_tracer.start_as_current_span.return_value = mock_span

        tracing_module._initialized = True
        tracing_module._tracer = mock_tracer

        @tracing_module.traced_async("async_operation")
        async def async_func():
            return "async_result"

        result = asyncio.get_event_loop().run_until_complete(async_func())
        assert result == "async_result"
        mock_tracer.start_as_current_span.assert_called_once()

    def test_async_exception_recording(self, tracing_module):
        """Records exceptions in async spans."""
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        mock_tracer.start_as_current_span.return_value = mock_span

        tracing_module._initialized = True
        tracing_module._tracer = mock_tracer

        @tracing_module.traced_async("failing_async")
        async def failing_async():
            raise IOError("async error")

        with pytest.raises(IOError, match="async error"):
            asyncio.get_event_loop().run_until_complete(failing_async())

        mock_span.record_exception.assert_called_once()

    def test_async_kind_parameter(self, tracing_module):
        """Passes span kind for async decorator."""
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        mock_tracer.start_as_current_span.return_value = mock_span

        tracing_module._initialized = True
        tracing_module._tracer = mock_tracer

        from opentelemetry.trace import SpanKind

        @tracing_module.traced_async("consumer_op", kind="consumer")
        async def consume():
            return True

        asyncio.get_event_loop().run_until_complete(consume())
        call_kwargs = mock_tracer.start_as_current_span.call_args[1]
        assert call_kwargs["kind"] == SpanKind.CONSUMER


# ===========================================================================
# instrument_redis tests
# ===========================================================================

class TestInstrumentRedis:
    """Tests for instrument_redis()."""

    def test_no_op_when_tracing_disabled(self, tracing_module):
        """Does nothing when tracer is None."""
        tracing_module._initialized = True
        tracing_module._tracer = None

        with patch("opentelemetry.instrumentation.redis.RedisInstrumentor") as mock_instr:
            tracing_module.instrument_redis()
            mock_instr.assert_not_called()

    def test_instruments_globally(self, tracing_module):
        """Instruments all Redis connections when no client provided."""
        tracing_module._initialized = True
        tracing_module._tracer = MagicMock()

        mock_instrumentor = MagicMock()
        with patch("opentelemetry.instrumentation.redis.RedisInstrumentor", return_value=mock_instrumentor):
            tracing_module.instrument_redis()
            mock_instrumentor.instrument.assert_called_once_with()

    def test_instruments_specific_client(self, tracing_module):
        """Instruments a specific Redis client instance."""
        tracing_module._initialized = True
        tracing_module._tracer = MagicMock()

        mock_client = MagicMock()
        mock_instrumentor = MagicMock()
        with patch("opentelemetry.instrumentation.redis.RedisInstrumentor", return_value=mock_instrumentor):
            tracing_module.instrument_redis(redis_client=mock_client)
            mock_instrumentor.instrument.assert_called_once_with(client=mock_client)

    def test_handles_import_error(self, tracing_module):
        """Handles missing redis instrumentation library."""
        tracing_module._initialized = True
        tracing_module._tracer = MagicMock()

        with patch.dict(sys.modules, {"opentelemetry.instrumentation.redis": None}), \
             patch("builtins.__import__", side_effect=ImportError("no redis instr")):
            tracing_module.instrument_redis()  # Should not raise

    def test_handles_runtime_error(self, tracing_module):
        """Handles errors during instrumentation."""
        tracing_module._initialized = True
        tracing_module._tracer = MagicMock()

        mock_instrumentor = MagicMock()
        mock_instrumentor.instrument.side_effect = RuntimeError("already instrumented")
        with patch("opentelemetry.instrumentation.redis.RedisInstrumentor", return_value=mock_instrumentor):
            tracing_module.instrument_redis()  # Should not raise


# ===========================================================================
# instrument_http_client tests
# ===========================================================================

class TestInstrumentHttpClient:
    """Tests for instrument_http_client()."""

    def test_no_op_when_disabled(self, tracing_module):
        """Does nothing when tracer is None."""
        tracing_module._initialized = True
        tracing_module._tracer = None
        tracing_module.instrument_http_client()  # Should not raise

    def test_instruments_requests(self, tracing_module):
        """Instruments the requests library."""
        tracing_module._initialized = True
        tracing_module._tracer = MagicMock()

        mock_instrumentor = MagicMock()
        with patch("opentelemetry.instrumentation.requests.RequestsInstrumentor", return_value=mock_instrumentor):
            tracing_module.instrument_http_client()
            mock_instrumentor.instrument.assert_called_once()

    def test_handles_import_error(self, tracing_module):
        """Handles missing requests instrumentation library."""
        tracing_module._initialized = True
        tracing_module._tracer = MagicMock()

        with patch("builtins.__import__", side_effect=ImportError("no requests instr")):
            tracing_module.instrument_http_client()  # Should not raise


# ===========================================================================
# instrument_sqlalchemy tests
# ===========================================================================

class TestInstrumentSqlalchemy:
    """Tests for instrument_sqlalchemy()."""

    def test_no_op_when_disabled(self, tracing_module):
        """Does nothing when tracer is None."""
        tracing_module._initialized = True
        tracing_module._tracer = None
        tracing_module.instrument_sqlalchemy()  # Should not raise

    def test_instruments_with_engine(self, tracing_module):
        """Instruments with a specific engine."""
        tracing_module._initialized = True
        tracing_module._tracer = MagicMock()

        mock_engine = MagicMock()
        mock_instrumentor = MagicMock()
        with patch("opentelemetry.instrumentation.sqlalchemy.SQLAlchemyInstrumentor", return_value=mock_instrumentor):
            tracing_module.instrument_sqlalchemy(engine=mock_engine)
            mock_instrumentor.instrument.assert_called_once_with(engine=mock_engine)

    def test_instruments_globally(self, tracing_module):
        """Instruments without engine (global)."""
        tracing_module._initialized = True
        tracing_module._tracer = MagicMock()

        mock_instrumentor = MagicMock()
        with patch("opentelemetry.instrumentation.sqlalchemy.SQLAlchemyInstrumentor", return_value=mock_instrumentor):
            tracing_module.instrument_sqlalchemy()
            mock_instrumentor.instrument.assert_called_once_with()

    def test_handles_import_error(self, tracing_module):
        """Handles missing SQLAlchemy instrumentation."""
        tracing_module._initialized = True
        tracing_module._tracer = MagicMock()

        with patch("builtins.__import__", side_effect=ImportError("no sqla instr")):
            tracing_module.instrument_sqlalchemy()  # Should not raise


# ===========================================================================
# inject_trace_context tests
# ===========================================================================

class TestInjectTraceContext:
    """Tests for inject_trace_context()."""

    def test_creates_headers_dict_if_none(self, tracing_module):
        """Creates a new dict if headers is None."""
        with patch("opentelemetry.propagate.inject") as mock_inject:
            result = tracing_module.inject_trace_context(None)
        assert isinstance(result, dict)

    def test_injects_into_existing_headers(self, tracing_module):
        """Injects into existing headers dict."""
        existing = {"Authorization": "Bearer token"}
        with patch("opentelemetry.propagate.inject") as mock_inject:
            result = tracing_module.inject_trace_context(existing)
        assert result is existing
        mock_inject.assert_called_once_with(existing)

    def test_handles_import_error(self, tracing_module):
        """Returns empty dict on import error."""
        with patch("builtins.__import__", side_effect=ImportError("no otel")):
            result = tracing_module.inject_trace_context()
        assert isinstance(result, dict)

    def test_handles_propagation_error(self, tracing_module):
        """Handles errors during injection."""
        with patch("opentelemetry.propagate.inject", side_effect=RuntimeError("inject fail")):
            result = tracing_module.inject_trace_context({"key": "val"})
        assert result == {"key": "val"}


# ===========================================================================
# extract_trace_context tests
# ===========================================================================

class TestExtractTraceContext:
    """Tests for extract_trace_context()."""

    def test_extracts_context(self, tracing_module):
        """Extracts context from headers."""
        mock_ctx = MagicMock()
        with patch("opentelemetry.propagate.extract", return_value=mock_ctx) as mock_extract:
            result = tracing_module.extract_trace_context({"traceparent": "00-abc-def-01"})
        assert result is mock_ctx
        mock_extract.assert_called_once_with({"traceparent": "00-abc-def-01"})

    def test_returns_none_on_import_error(self, tracing_module):
        """Returns None if OTel not available."""
        with patch("builtins.__import__", side_effect=ImportError("no otel")):
            result = tracing_module.extract_trace_context({"traceparent": "00-abc-def-01"})
        assert result is None

    def test_returns_none_on_error(self, tracing_module):
        """Returns None on extraction error."""
        with patch("opentelemetry.propagate.extract", side_effect=ValueError("bad")):
            result = tracing_module.extract_trace_context({})
        assert result is None


# ===========================================================================
# propagate_via_socketio tests
# ===========================================================================

class TestPropagateViaSocketio:
    """Tests for propagate_via_socketio()."""

    def test_no_op_when_tracing_disabled(self, tracing_module):
        """Returns data unchanged when tracer is None."""
        tracing_module._initialized = True
        tracing_module._tracer = None

        data = {"event": "chat", "message": "hello"}
        result = tracing_module.propagate_via_socketio(data)
        assert result is data
        assert "_trace_context" not in result

    def test_adds_trace_context(self, tracing_module):
        """Adds _trace_context field with headers."""
        tracing_module._initialized = True
        tracing_module._tracer = MagicMock()

        def mock_inject(headers):
            headers["traceparent"] = "00-abc123-def456-01"

        with patch("opentelemetry.propagate.inject", side_effect=mock_inject):
            data = {"event": "update"}
            result = tracing_module.propagate_via_socketio(data)

        assert "_trace_context" in result
        assert result["_trace_context"]["traceparent"] == "00-abc123-def456-01"

    def test_preserves_existing_data(self, tracing_module):
        """Does not modify existing event data fields."""
        tracing_module._initialized = True
        tracing_module._tracer = MagicMock()

        with patch("opentelemetry.propagate.inject"):
            data = {"user_id": 123, "project_id": 1}
            result = tracing_module.propagate_via_socketio(data)

        assert result["user_id"] == 123
        assert result["project_id"] == 1


# ===========================================================================
# restore_from_socketio tests
# ===========================================================================

class TestRestoreFromSocketio:
    """Tests for restore_from_socketio()."""

    def test_extracts_from_trace_context_field(self, tracing_module):
        """Extracts context from _trace_context in event data."""
        mock_ctx = MagicMock()
        headers = {"traceparent": "00-abc-def-01"}

        with patch("opentelemetry.propagate.extract", return_value=mock_ctx) as mock_extract:
            result = tracing_module.restore_from_socketio({"_trace_context": headers})

        assert result is mock_ctx
        mock_extract.assert_called_once_with(headers)

    def test_returns_none_without_context(self, tracing_module):
        """Returns None when _trace_context is missing."""
        result = tracing_module.restore_from_socketio({"event": "data"})
        assert result is None

    def test_returns_none_for_non_dict_context(self, tracing_module):
        """Returns None when _trace_context is not a dict."""
        result = tracing_module.restore_from_socketio({"_trace_context": "invalid"})
        assert result is None

    def test_returns_none_for_empty_context(self, tracing_module):
        """Returns None when _trace_context is empty/None."""
        result = tracing_module.restore_from_socketio({"_trace_context": None})
        assert result is None


# ===========================================================================
# Docker-compose / Jaeger configuration tests
# ===========================================================================

class TestDockerComposeJaeger:
    """Tests verifying Jaeger is configured in docker-compose."""

    def test_jaeger_service_defined(self):
        """Jaeger service exists in docker-compose.yml."""
        compose_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
            "docker-compose.yml"
        )
        with open(compose_path) as f:
            content = f.read()

        assert "jaeger:" in content
        assert "jaegertracing/all-in-one" in content

    def test_jaeger_otlp_enabled(self):
        """Jaeger has COLLECTOR_OTLP_ENABLED=true."""
        compose_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
            "docker-compose.yml"
        )
        with open(compose_path) as f:
            content = f.read()

        assert "COLLECTOR_OTLP_ENABLED=true" in content

    def test_jaeger_ports_exposed(self):
        """Jaeger exposes UI (16686) and OTLP gRPC (4317) ports."""
        compose_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
            "docker-compose.yml"
        )
        with open(compose_path) as f:
            content = f.read()

        assert "16686:16686" in content
        assert "4317:4317" in content


# ===========================================================================
# Requirements tests
# ===========================================================================

class TestRequirements:
    """Tests verifying OTel dependencies are in requirements."""

    def test_opentelemetry_api_in_requirements(self):
        """opentelemetry-api is listed in elitea_core requirements."""
        req_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))),
            "elitea_core", "requirements.txt"
        )
        with open(req_path) as f:
            content = f.read()
        assert "opentelemetry-api" in content

    def test_opentelemetry_sdk_in_requirements(self):
        """opentelemetry-sdk is listed in elitea_core requirements."""
        req_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))),
            "elitea_core", "requirements.txt"
        )
        with open(req_path) as f:
            content = f.read()
        assert "opentelemetry-sdk" in content

    def test_opentelemetry_otlp_in_requirements(self):
        """opentelemetry OTLP exporter is listed."""
        req_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))),
            "elitea_core", "requirements.txt"
        )
        with open(req_path) as f:
            content = f.read()
        assert "opentelemetry-exporter-otlp" in content

    def test_opentelemetry_redis_in_requirements(self):
        """opentelemetry Redis instrumentation is listed."""
        req_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))),
            "elitea_core", "requirements.txt"
        )
        with open(req_path) as f:
            content = f.read()
        assert "opentelemetry-instrumentation-redis" in content
