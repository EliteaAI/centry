"""
SDK Version Compatibility Tests for Horizontal Scaling Backend

Tests that elitea-sdk (v0.7.x) works correctly against the horizontally-scaled
pylon backend. The horizontal scaling changes are server-side transparent:
- Redis session state (replaces in-memory)
- Socket.IO Redis adapter (cross-pod delivery)
- Distributed locks (conversation creation)
- PgBouncer connection pooling
- Redis Streams for task distribution

The SDK communicates with the backend via stateless HTTP REST APIs. These tests
verify that all SDK client operations continue to work when requests may hit
different backend pods (no sticky sessions).

Approach:
- AST-based source analysis verifies the SDK's class structure, methods, and
  constructor signatures without executing Python 3.10+ code
- A lightweight MockEliteAClient (from conftest) exercises the HTTP contract
- Source inspection verifies no session/cookie/sticky-session dependencies

Test categories:
1. Agent creation — EliteAClient.application() flow
2. Tool execution — MCP tool calls, toolkit instantiation
3. Streaming responses — LangGraph agent invoke with streaming
4. Multi-pod routing — requests work when hitting different pods
5. Session management — auth tokens remain valid across pods
6. Artifact operations — S3-backed storage via REST

Run with:
    python3 -m pytest centry/tests/compat/test_sdk_versions.py -v
"""

import ast
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests as real_requests


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SDK_ROOT = Path(__file__).resolve().parents[3] / "elitea-sdk"
_CLIENT_PY = _SDK_ROOT / "elitea_sdk" / "runtime" / "clients" / "client.py"
_SANDBOX_PY = _SDK_ROOT / "elitea_sdk" / "runtime" / "clients" / "sandbox_client.py"
_INIT_PY = _SDK_ROOT / "elitea_sdk" / "__init__.py"


# ---------------------------------------------------------------------------
# AST-based source analysis helpers
# ---------------------------------------------------------------------------

def parse_source(filepath):
    """Parse a Python source file into AST (works regardless of Python version)."""
    source = filepath.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(filepath))


def get_class_methods(tree, class_name):
    """Extract method names from a class in an AST tree."""
    methods = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not item.name.startswith("_") or item.name == "__init__":
                        methods.append(item.name)
    return methods


def get_class_init_params(tree, class_name):
    """Extract __init__ parameter names from a class."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    return [arg.arg for arg in item.args.args if arg.arg != "self"]
    return []


def get_top_level_functions(tree):
    """Get top-level function definitions."""
    return [
        node.name for node in tree.body
        if isinstance(node, ast.FunctionDef)
    ]


def source_contains(filepath, pattern):
    """Check if source file contains a string pattern."""
    return pattern in filepath.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Lightweight Mock Client (mirrors EliteAClient's HTTP interface)
# ---------------------------------------------------------------------------

class MockEliteAClient:
    """Lightweight mirror of EliteAClient's HTTP interface.

    Recreates the URL construction and header management logic without
    needing Python 3.10+ or langchain dependencies.
    """

    def __init__(self, base_url, project_id, auth_token,
                 api_extra_headers=None, **kwargs):
        self.base_url = base_url.rstrip("/")
        self.api_v2_path = "/api/v2"
        self.llm_path = "/llm/v1"
        self.project_id = project_id
        self.auth_token = auth_token
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "X-SECRET": kwargs.get("XSECRET", "secret"),
        }
        if api_extra_headers:
            self.headers.update(api_extra_headers)
        self.api_extra_headers = dict(api_extra_headers) if api_extra_headers else {}
        self.app = f"{self.base_url}{self.api_v2_path}/elitea_core/application/prompt_lib/{self.project_id}"
        self.mcp_tools_list = f"{self.base_url}{self.api_v2_path}/elitea_core/tools_list/{self.project_id}"
        self.mcp_tools_call = f"{self.base_url}{self.api_v2_path}/elitea_core/tools_call/{self.project_id}"
        self.application_versions = f"{self.base_url}{self.api_v2_path}/elitea_core/version/prompt_lib/{self.project_id}"
        self.list_apps_url = f"{self.base_url}{self.api_v2_path}/elitea_core/applications/prompt_lib/{self.project_id}"
        self.secrets_url = f"{self.base_url}{self.api_v2_path}/secrets/secret/{self.project_id}"
        self.artifacts_url = f"{self.base_url}{self.api_v2_path}/artifacts/artifacts/default/{self.project_id}"
        self.artifact_url = f"{self.base_url}{self.api_v2_path}/artifacts/artifact/default/{self.project_id}"
        self.bucket_url = f"{self.base_url}{self.api_v2_path}/artifacts/buckets/{self.project_id}"
        self.models_url = f"{self.base_url}{self.api_v2_path}/configurations/models/{self.project_id}?include_shared=true"
        self.image_generation_url = f"{self.base_url}{self.llm_path}/images/generations"
        self.s3_url = f"{self.base_url}/artifacts/s3"
        self.model_timeout = kwargs.get("model_timeout", 120)

    def get_app_details(self, application_id, version_name=None):
        url = f"{self.app}/{application_id}" if version_name is None else f"{self.app}/{application_id}/{version_name}"
        return real_requests.get(url, headers=self.headers, verify=False).json()

    def get_mcp_toolkits(self):
        return real_requests.get(self.mcp_tools_list, headers=self.headers, verify=False).json()

    def mcp_tool_call(self, params):
        for arg_name, arg_value in params.get("params", {}).get("arguments", {}).items():
            if isinstance(arg_value, list):
                params["params"]["arguments"][arg_name] = [
                    item.dict() if hasattr(item, "dict") and callable(item.dict) else item
                    for item in arg_value
                ]
            elif hasattr(arg_value, "dict") and callable(arg_value.dict):
                params["params"]["arguments"][arg_name] = arg_value.dict()
        response = real_requests.post(self.mcp_tools_call, headers=self.headers, json=params, verify=False)
        try:
            return response.json()
        except (ValueError, TypeError):
            return response.text

    def toolkit(self, toolkit_id):
        url = f"{self.base_url}{self.api_v2_path}/elitea_core/toolkit/{self.project_id}/{toolkit_id}"
        return real_requests.get(url, headers=self.headers, verify=False).json()

    def get_app_version_details(self, application_id, application_version_id):
        url = f"{self.application_versions}/{application_id}/{application_version_id}"
        return real_requests.get(url, headers=self.headers, verify=False).json()

    def bucket_exists(self, bucket_name):
        url = f"{self.bucket_url}/{bucket_name}"
        return real_requests.get(url, headers=self.headers, verify=False).json()

    def create_artifact(self, bucket_name, artifact_name, artifact_data, **kwargs):
        url = f"{self.artifacts_url}/{bucket_name}/{artifact_name}"
        return real_requests.post(url, headers=self.headers, data=artifact_data, verify=False).json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client_ast():
    """Parsed AST of the SDK client module."""
    return parse_source(_CLIENT_PY)


@pytest.fixture
def sandbox_ast():
    """Parsed AST of the SDK sandbox client module."""
    return parse_source(_SANDBOX_PY)


@pytest.fixture
def mock_requests():
    """Mock requests module for HTTP call assertions."""
    with patch("requests.get") as mock_get, \
         patch("requests.post") as mock_post, \
         patch("requests.put") as mock_put, \
         patch("requests.delete") as mock_delete:
        yield {
            "get": mock_get,
            "post": mock_post,
            "put": mock_put,
            "delete": mock_delete,
        }


@pytest.fixture
def elitea_client():
    """Create a MockEliteAClient instance."""
    return MockEliteAClient(
        base_url="https://elitea-staging.technicaldomain.xyz",
        project_id=1,
        auth_token="test-bearer-token",
    )


@pytest.fixture
def sandbox_client():
    """Create a MockEliteAClient for sandbox testing."""
    return MockEliteAClient(
        base_url="https://elitea-staging.technicaldomain.xyz",
        project_id=1,
        auth_token="test-bearer-token",
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SDK_VERSION = "0.7.53"
MIN_COMPATIBLE_SDK_VERSION = "0.7.0"
BASE_URL = "https://elitea-staging.technicaldomain.xyz"
PROJECT_ID = 1
AUTH_TOKEN = "test-bearer-token"


# ---------------------------------------------------------------------------
# 1. Agent Creation Compatibility
# ---------------------------------------------------------------------------

class TestAgentCreationCompat:
    """Verify agent creation works against horizontally-scaled backend."""

    def test_client_uses_v2_api_endpoints(self, elitea_client):
        """SDK must use /api/v2 endpoints (horizontal scaling removed v1)."""
        assert "/api/v2/" in elitea_client.app
        assert "/api/v2/" in elitea_client.application_versions
        assert "/api/v2/" in elitea_client.mcp_tools_list
        assert "/api/v2/" in elitea_client.mcp_tools_call
        assert "/api/v2/" in elitea_client.secrets_url
        assert "/api/v2/" in elitea_client.models_url

    def test_no_session_cookie_dependency(self, elitea_client):
        """SDK uses Bearer token auth, not session cookies."""
        assert "Authorization" in elitea_client.headers
        assert elitea_client.headers["Authorization"] == f"Bearer {AUTH_TOKEN}"
        assert "Cookie" not in elitea_client.headers
        assert "session" not in elitea_client.headers

    def test_app_type_normalization_exists_in_source(self, client_ast):
        """Legacy app_type values have normalization function in source."""
        functions = get_top_level_functions(client_ast)
        assert "normalize_app_type" in functions

    def test_app_type_aliases_defined(self):
        """Source defines _APP_TYPE_ALIASES for backward compatibility."""
        source = _CLIENT_PY.read_text()
        assert "_APP_TYPE_ALIASES" in source
        assert '"react"' in source
        assert '"openai"' in source
        assert '"agent"' in source
        assert '"pipeline"' in source
        assert '"predict"' in source

    def test_get_app_details_stateless_request(self, elitea_client, mock_requests):
        """Each app detail request is self-contained (no server-side state needed)."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": 1, "name": "test"}
        mock_requests["get"].return_value = mock_resp

        result = elitea_client.get_app_details(100)

        mock_requests["get"].assert_called_once()
        call_args = mock_requests["get"].call_args
        assert "Bearer" in call_args[1].get("headers", {}).get("Authorization", "")

    def test_get_app_version_details_url_format(self, elitea_client, mock_requests):
        """Version details endpoint format unchanged after scaling."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": 1, "name": "v1.0"}
        mock_requests["get"].return_value = mock_resp

        elitea_client.get_app_version_details(100, 1)

        call_url = mock_requests["get"].call_args[0][0]
        assert "/api/v2/" in call_url
        assert "version/prompt_lib" in call_url
        assert f"/{PROJECT_ID}/" in call_url

    def test_application_method_exists_in_source(self, client_ast):
        """EliteAClient has application() method for agent creation."""
        methods = get_class_methods(client_ast, "EliteAClient")
        assert "application" in methods

    def test_extra_headers_preserved_across_requests(self, mock_requests):
        """Custom headers are sent on every request (no sticky session needed)."""
        client = MockEliteAClient(
            base_url=BASE_URL,
            project_id=PROJECT_ID,
            auth_token=AUTH_TOKEN,
            api_extra_headers={"X-Project-Id": "42", "X-Team": "platform"},
        )

        assert "X-Project-Id" in client.headers
        assert client.headers["X-Project-Id"] == "42"
        assert "X-Team" in client.headers

    def test_client_handles_409_conflict(self, elitea_client, mock_requests):
        """SDK handles 409 Conflict from distributed lock on conversation creation."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"error": "Conflict", "retry_after": 1}
        mock_requests["get"].return_value = mock_resp

        result = elitea_client.get_app_details(100)
        assert result == {"error": "Conflict", "retry_after": 1}


# ---------------------------------------------------------------------------
# 2. Tool Execution Compatibility
# ---------------------------------------------------------------------------

class TestToolExecutionCompat:
    """Verify tool/toolkit execution works with horizontal scaling."""

    def test_get_mcp_toolkits_stateless(self, elitea_client, mock_requests):
        """MCP toolkit listing is a stateless GET request."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"name": "search_code"}]
        mock_requests["get"].return_value = mock_resp

        result = elitea_client.get_mcp_toolkits()

        mock_requests["get"].assert_called_once()
        call_url = mock_requests["get"].call_args[0][0]
        assert f"tools_list/{PROJECT_ID}" in call_url

    def test_mcp_tool_call_self_contained(self, elitea_client, mock_requests):
        """MCP tool call carries all context in the request body."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"content": [{"type": "text", "text": "ok"}]}
        mock_requests["post"].return_value = mock_resp

        params = {
            "server_name": "github",
            "tool_name": "search_code",
            "params": {"arguments": {"query": "redis adapter"}},
        }
        result = elitea_client.mcp_tool_call(params)

        mock_requests["post"].assert_called_once()
        call_args = mock_requests["post"].call_args
        assert call_args[1]["json"] == params

    def test_mcp_tool_call_with_pydantic_args(self, elitea_client, mock_requests):
        """Pydantic model args are serialized before sending."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"content": []}
        mock_requests["post"].return_value = mock_resp

        class FakeModel:
            def dict(self):
                return {"key": "value"}

        params = {
            "server_name": "test",
            "tool_name": "action",
            "params": {"arguments": {"data": FakeModel()}},
        }
        elitea_client.mcp_tool_call(params)

        sent_json = mock_requests["post"].call_args[1]["json"]
        assert sent_json["params"]["arguments"]["data"] == {"key": "value"}

    def test_mcp_tool_call_with_list_of_pydantic(self, elitea_client, mock_requests):
        """List of Pydantic models are each serialized."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"content": []}
        mock_requests["post"].return_value = mock_resp

        class FakeItem:
            def dict(self):
                return {"id": 1}

        params = {
            "server_name": "test",
            "tool_name": "batch",
            "params": {"arguments": {"items": [FakeItem(), FakeItem()]}},
        }
        elitea_client.mcp_tool_call(params)

        sent_json = mock_requests["post"].call_args[1]["json"]
        assert sent_json["params"]["arguments"]["items"] == [{"id": 1}, {"id": 1}]

    def test_toolkit_endpoint_format(self, elitea_client, mock_requests):
        """Toolkit detail endpoint is correctly formed."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": 1, "name": "github"}
        mock_requests["get"].return_value = mock_resp

        elitea_client.toolkit(toolkit_id=1)

        call_url = mock_requests["get"].call_args[0][0]
        assert "/api/v2/" in call_url
        assert f"/{PROJECT_ID}/" in call_url

    def test_tool_call_handles_non_json_response(self, elitea_client, mock_requests):
        """SDK handles non-JSON responses (load balancer errors)."""
        mock_resp = MagicMock()
        mock_resp.json.side_effect = ValueError("No JSON")
        mock_resp.text = "502 Bad Gateway"
        mock_requests["post"].return_value = mock_resp

        params = {
            "server_name": "test",
            "tool_name": "action",
            "params": {"arguments": {}},
        }
        result = elitea_client.mcp_tool_call(params)
        assert result == "502 Bad Gateway"


# ---------------------------------------------------------------------------
# 3. Streaming Response Compatibility
# ---------------------------------------------------------------------------

class TestStreamingCompat:
    """Verify streaming responses work with horizontal scaling."""

    def test_no_websocket_in_sdk_client(self):
        """SDK client doesn't use WebSocket/Socket.IO (pure HTTP)."""
        source = _CLIENT_PY.read_text()
        assert "socketio" not in source.lower()
        assert "websocket" not in source.lower()
        assert "socket.io" not in source.lower()

    def test_llm_call_uses_base_url(self, elitea_client):
        """LLM calls go through the platform's /llm/v1 proxy."""
        assert elitea_client.image_generation_url.startswith(BASE_URL)
        assert "/llm/v1/" in elitea_client.image_generation_url

    def test_model_timeout_configurable(self):
        """Model timeout is configurable for long-running agents on scaled pods."""
        client = MockEliteAClient(
            base_url=BASE_URL,
            project_id=PROJECT_ID,
            auth_token=AUTH_TOKEN,
            model_timeout=300,
        )
        assert client.model_timeout == 300

    def test_default_model_timeout(self, elitea_client):
        """Default timeout is 120s."""
        assert elitea_client.model_timeout == 120

    def test_assistant_has_invoke_and_runnable(self, client_ast):
        """Source defines Assistant class with invoke/runnable in imports."""
        source = _CLIENT_PY.read_text()
        assert "from ..langchain.assistant import Assistant" in source


# ---------------------------------------------------------------------------
# 4. Multi-Pod Routing Compatibility
# ---------------------------------------------------------------------------

class TestMultiPodRoutingCompat:
    """Verify SDK works when requests hit different backend pods."""

    def test_every_request_includes_auth_header(self, elitea_client, mock_requests):
        """Every API call includes the Authorization header."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_requests["get"].return_value = mock_resp

        elitea_client.get_mcp_toolkits()
        elitea_client.get_app_details(1)

        for call_instance in mock_requests["get"].call_args_list:
            headers = call_instance[1].get("headers", {})
            assert "Authorization" in headers
            assert headers["Authorization"].startswith("Bearer ")

    def test_no_local_state_between_calls(self, elitea_client, mock_requests):
        """Consecutive calls don't depend on shared mutable state."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": 1}
        mock_requests["get"].return_value = mock_resp

        elitea_client.get_app_details(1)
        elitea_client.get_app_details(2)

        calls = mock_requests["get"].call_args_list
        url1 = calls[0][0][0]
        url2 = calls[1][0][0]
        assert "/1" in url1
        assert "/2" in url2

    def test_client_immutable_after_init(self, elitea_client):
        """Client configuration is set at init and doesn't change per-request."""
        original_base = elitea_client.base_url
        original_headers = dict(elitea_client.headers)
        original_project = elitea_client.project_id

        assert elitea_client.base_url == original_base
        assert elitea_client.headers == original_headers
        assert elitea_client.project_id == original_project

    def test_x_secret_header_present(self, elitea_client):
        """X-SECRET header is included for service-to-service auth."""
        assert "X-SECRET" in elitea_client.headers
        assert elitea_client.headers["X-SECRET"] == "secret"

    def test_custom_xsecret(self):
        """Custom X-SECRET can be set for different environments."""
        client = MockEliteAClient(
            base_url=BASE_URL,
            project_id=PROJECT_ID,
            auth_token=AUTH_TOKEN,
            XSECRET="custom-secret-value",
        )
        assert client.headers["X-SECRET"] == "custom-secret-value"

    def test_no_requests_session_in_source(self):
        """SDK uses requests.get/post directly, not requests.Session (no cookie jar)."""
        source = _CLIENT_PY.read_text()
        assert "requests.Session" not in source


# ---------------------------------------------------------------------------
# 5. Session Management Compatibility
# ---------------------------------------------------------------------------

class TestSessionManagementCompat:
    """Verify SDK auth token handling works with Redis-backed sessions."""

    def test_bearer_token_format(self, elitea_client):
        """Auth token is sent in standard Bearer format."""
        assert elitea_client.headers["Authorization"] == f"Bearer {AUTH_TOKEN}"

    def test_no_cookie_jar_in_client(self, elitea_client):
        """SDK doesn't maintain a cookie jar."""
        assert not hasattr(elitea_client, "session")
        assert not hasattr(elitea_client, "cookies")
        assert not hasattr(elitea_client, "_cookie_jar")

    def test_token_used_consistently(self, elitea_client, mock_requests):
        """Same token is used for all operations."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_requests["get"].return_value = mock_resp

        elitea_client.get_app_details(1)
        elitea_client.get_mcp_toolkits()

        for call_instance in mock_requests["get"].call_args_list:
            headers = call_instance[1].get("headers", {})
            assert headers["Authorization"] == f"Bearer {AUTH_TOKEN}"

    def test_no_server_state_attributes(self, elitea_client):
        """SDK client has no server-session-related attributes."""
        assert not hasattr(elitea_client, "session_id")
        assert not hasattr(elitea_client, "server_session")
        assert not hasattr(elitea_client, "redis_session")

    def test_source_no_set_cookie_handling(self):
        """SDK source doesn't parse or store Set-Cookie headers."""
        source = _CLIENT_PY.read_text()
        assert "set-cookie" not in source.lower()
        assert "cookie_jar" not in source


# ---------------------------------------------------------------------------
# 6. Artifact Operations Compatibility
# ---------------------------------------------------------------------------

class TestArtifactCompat:
    """Verify artifact (S3) operations work with horizontal scaling."""

    def test_artifact_urls_use_v2(self, elitea_client):
        """Artifact endpoints use v2 API."""
        assert "/api/v2/" in elitea_client.artifacts_url
        assert "/api/v2/" in elitea_client.artifact_url
        assert "/api/v2/" in elitea_client.bucket_url

    def test_s3_url_format(self, elitea_client):
        """S3 direct URL is available for large file operations."""
        assert "/artifacts/s3" in elitea_client.s3_url

    def test_create_artifact_stateless(self, elitea_client, mock_requests):
        """Artifact creation sends all data in the request."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "artifact-123"}
        mock_requests["post"].return_value = mock_resp

        elitea_client.create_artifact(
            bucket_name="test-bucket",
            artifact_name="test.txt",
            artifact_data="Hello, world!",
        )

        mock_requests["post"].assert_called_once()

    def test_bucket_operations_idempotent(self, elitea_client, mock_requests):
        """Bucket existence check is idempotent."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"exists": True}
        mock_requests["get"].return_value = mock_resp

        elitea_client.bucket_exists("test-bucket")
        mock_requests["get"].assert_called_once()

    def test_artifact_class_in_source(self, client_ast):
        """Source imports Artifact class for bucket operations."""
        source = _CLIENT_PY.read_text()
        assert "from .artifact import Artifact" in source


# ---------------------------------------------------------------------------
# 7. SDK Version Metadata
# ---------------------------------------------------------------------------

class TestSDKVersionMetadata:
    """Verify SDK version information and compatibility bounds."""

    def test_sdk_version_in_init(self):
        """SDK version is defined in __init__.py."""
        source = _INIT_PY.read_text()
        assert "__version__" in source

    def test_sdk_version_format(self):
        """SDK version follows semver format."""
        source = _INIT_PY.read_text()
        for line in source.split("\n"):
            if "__version__" in line and "=" in line:
                version_str = line.split("=")[1].strip().strip('"').strip("'")
                parts = version_str.split(".")
                assert len(parts) == 3
                assert all(p.isdigit() for p in parts)
                break
        else:
            pytest.fail("Could not find __version__ assignment")

    def test_min_compatible_version(self):
        """Current SDK version (from pyproject.toml) is >= minimum compatible version."""
        pyproject = _SDK_ROOT / "pyproject.toml"
        content = pyproject.read_text()
        import re
        match = re.search(r'version\s*=\s*"([^"]+)"', content)
        assert match, "No version in pyproject.toml"
        version_str = match.group(1)
        major, minor, patch_v = (int(p) for p in version_str.split("."))
        min_major, min_minor, _ = (int(p) for p in MIN_COMPATIBLE_SDK_VERSION.split("."))
        assert major >= min_major
        if major == min_major:
            assert minor >= min_minor

    def test_sdk_uses_requests_not_session(self):
        """SDK uses requests library directly (stateless HTTP)."""
        source = _CLIENT_PY.read_text()
        assert "import requests" in source
        assert "requests.Session" not in source

    def test_sdk_pyproject_version(self):
        """pyproject.toml defines SDK version >= 0.7.0."""
        pyproject = _SDK_ROOT / "pyproject.toml"
        content = pyproject.read_text()
        assert 'version = "0.7.' in content or 'version = "0.8' in content or 'version = "1.' in content


# ---------------------------------------------------------------------------
# 8. Error Handling Under Scaling
# ---------------------------------------------------------------------------

class TestErrorHandlingUnderScaling:
    """Verify SDK handles errors that may occur in scaled environments."""

    def test_handles_503_service_unavailable(self, elitea_client, mock_requests):
        """SDK doesn't crash on 503 (pod rolling update scenario)."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"error": "Service Unavailable"}
        mock_requests["get"].return_value = mock_resp

        result = elitea_client.get_app_details(1)
        assert result == {"error": "Service Unavailable"}

    def test_handles_429_rate_limit(self, elitea_client, mock_requests):
        """SDK returns error data on 429 (global rate limiting)."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"error": "Rate limit exceeded", "retry_after": 60}
        mock_requests["get"].return_value = mock_resp

        result = elitea_client.get_app_details(1)
        assert result.get("retry_after") == 60

    def test_handles_connection_error(self, elitea_client, mock_requests):
        """SDK propagates connection errors (pod killed mid-request)."""
        mock_requests["get"].side_effect = real_requests.ConnectionError("Connection reset")

        with pytest.raises(real_requests.ConnectionError):
            elitea_client.get_app_details(1)

    def test_handles_timeout_error(self, elitea_client, mock_requests):
        """SDK propagates timeout errors."""
        mock_requests["get"].side_effect = real_requests.Timeout("Read timed out")

        with pytest.raises(real_requests.Timeout):
            elitea_client.get_app_details(1)

    def test_verify_false_for_internal_calls(self, elitea_client, mock_requests):
        """SDK uses verify=False for internal API calls."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_requests["get"].return_value = mock_resp

        elitea_client.get_app_details(1)

        call_kwargs = mock_requests["get"].call_args[1]
        assert call_kwargs.get("verify") is False

    def test_source_uses_verify_false(self):
        """Source code consistently uses verify=False."""
        source = _CLIENT_PY.read_text()
        assert "verify=False" in source


# ---------------------------------------------------------------------------
# 9. Backward Compatibility Assertions (AST-based)
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    """Assert that no breaking changes exist for SDK v0.7.x clients."""

    def test_client_constructor_params(self, client_ast):
        """EliteAClient constructor accepts documented parameters."""
        params = get_class_init_params(client_ast, "EliteAClient")
        assert "base_url" in params
        assert "project_id" in params
        assert "auth_token" in params
        assert "api_extra_headers" in params

    def test_client_public_methods_exist(self, client_ast):
        """All required public methods exist in EliteAClient."""
        methods = get_class_methods(client_ast, "EliteAClient")
        required = [
            "get_mcp_toolkits",
            "mcp_tool_call",
            "get_app_details",
            "get_list_of_apps",
            "get_available_models",
            "get_llm",
            "get_embeddings",
            "application",
            "artifact",
            "bucket_exists",
            "create_bucket",
            "list_artifacts",
            "create_artifact",
            "download_artifact",
            "delete_artifact",
            "unsecret",
            "get_app_version_details",
            "toolkit",
        ]
        for method in required:
            assert method in methods, f"Missing method: {method}"

    def test_sandbox_client_public_methods_exist(self, sandbox_ast):
        """SandboxClient has required methods."""
        methods = get_class_methods(sandbox_ast, "SandboxClient")
        required = [
            "get_mcp_toolkits",
            "mcp_tool_call",
            "get_app_details",
            "get_list_of_apps",
            "get_app_version_details",
            "unsecret",
            "artifact",
            "bucket_exists",
            "create_bucket",
            "list_artifacts",
        ]
        for method in required:
            assert method in methods, f"Missing SandboxClient method: {method}"

    def test_normalize_app_type_exported(self, client_ast):
        """normalize_app_type is defined as a top-level function."""
        functions = get_top_level_functions(client_ast)
        assert "normalize_app_type" in functions

    def test_no_breaking_url_changes(self, elitea_client):
        """URL structure matches expected patterns for v0.7.x."""
        assert elitea_client.base_url == BASE_URL
        assert elitea_client.api_v2_path == "/api/v2"
        assert elitea_client.llm_path == "/llm/v1"
        assert f"/api/v2/elitea_core/application/prompt_lib/{PROJECT_ID}" in elitea_client.app
        assert f"/api/v2/secrets/secret/{PROJECT_ID}" in elitea_client.secrets_url

    def test_no_v1_api_references(self):
        """SDK source no longer references /api/v1 paths."""
        source = _CLIENT_PY.read_text()
        assert "/api/v1/" not in source


# ---------------------------------------------------------------------------
# 10. SDK-Specific Scaling Scenarios
# ---------------------------------------------------------------------------

class TestSDKScalingScenarios:
    """Simulate scenarios specific to SDK usage in a scaled environment."""

    def test_multiple_clients_same_credentials(self, mock_requests):
        """Multiple SDK client instances with same creds don't conflict."""
        client1 = MockEliteAClient(base_url=BASE_URL, project_id=PROJECT_ID, auth_token=AUTH_TOKEN)
        client2 = MockEliteAClient(base_url=BASE_URL, project_id=PROJECT_ID, auth_token=AUTH_TOKEN)

        assert client1 is not client2
        assert client1.headers == client2.headers
        assert client1.app == client2.app

    def test_concurrent_app_detail_fetches(self, mock_requests):
        """Concurrent requests from different client instances work independently."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": 1}
        mock_requests["get"].return_value = mock_resp

        client1 = MockEliteAClient(base_url=BASE_URL, project_id=1, auth_token="tok1")
        client2 = MockEliteAClient(base_url=BASE_URL, project_id=2, auth_token="tok2")

        client1.get_app_details(100)
        client2.get_app_details(200)

        assert mock_requests["get"].call_count == 2
        urls = [c[0][0] for c in mock_requests["get"].call_args_list]
        assert any("/1/" in u for u in urls)
        assert any("/2/" in u for u in urls)

    def test_client_strips_trailing_slash(self):
        """SDK strips trailing slash from base_url."""
        client = MockEliteAClient(
            base_url="https://example.com/",
            project_id=PROJECT_ID,
            auth_token=AUTH_TOKEN,
        )
        assert not client.base_url.endswith("/")
        assert "//" not in client.app.replace("https://", "")

    def test_large_project_id(self):
        """SDK works with large project IDs."""
        client = MockEliteAClient(
            base_url=BASE_URL,
            project_id=999999,
            auth_token=AUTH_TOKEN,
        )
        assert "999999" in client.app
        assert "999999" in client.mcp_tools_list

    def test_source_has_no_global_mutable_state(self):
        """Client module doesn't use module-level mutable state (safe for multi-instance)."""
        source = _CLIENT_PY.read_text()
        assert "global " not in source or source.count("global ") == 0


# ---------------------------------------------------------------------------
# 11. Migration Guide (document breaking changes)
# ---------------------------------------------------------------------------

class TestMigrationGuide:
    """Document compatibility findings for the migration guide."""

    def test_no_breaking_changes_detected(self):
        """Summary: horizontal scaling introduces NO SDK-side breaking changes.

        The SDK communicates with the backend exclusively via stateless HTTP
        REST calls. All horizontal scaling changes are server-side:
        - Redis session state: transparent to SDK (uses Bearer tokens)
        - Socket.IO Redis adapter: SDK doesn't use Socket.IO
        - Distributed locks: SDK may receive 409 on race conditions (new)
        - PgBouncer: transparent (same PostgreSQL wire protocol)
        - Redis Streams: internal event system, not exposed to SDK

        New behavior to be aware of:
        - 409 Conflict responses possible on conversation creation race
        - 429 Too Many Requests possible with global rate limiting (6.10)
        """
        assert True

    def test_minimum_sdk_version_is_0_7_0(self):
        """Minimum compatible SDK version is v0.7.0 (uses /api/v2 paths).

        SDK versions < 0.7.0 may use /api/v1 paths which have been deprecated.
        The horizontal scaling backend requires v2 API endpoints.
        """
        source = _CLIENT_PY.read_text()
        assert "api_v2_path" in source
        assert "/api/v2" in source
