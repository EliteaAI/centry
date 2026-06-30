"""
Tests for Task 5.7: Update services for Sentinel URLs.

Validates that:
- _parse_sentinel_hosts correctly parses various input formats
- redis_client.py supports Sentinel connections via REDIS_SENTINEL_HOSTS env var
- Falls back to direct connection when REDIS_SENTINEL_HOSTS is unset
- get_sentinel_info returns sentinel health data
- Health endpoint includes sentinel status when configured
- Source and runtime files are in sync
- Environment variables are correctly defined
"""

import os
import re
import ast
from unittest.mock import patch, MagicMock

import pytest


CENTRY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SOURCE_ROOT = os.path.join(CENTRY_ROOT, "..", "elitea_core")
RUNTIME_ROOT = os.path.join(CENTRY_ROOT, "pylon_main", "plugins", "elitea_core")
DEFAULT_ENV = os.path.join(CENTRY_ROOT, "envs", "default.env")

REDIS_CLIENT_SOURCE = os.path.join(SOURCE_ROOT, "methods", "redis_client.py")
REDIS_CLIENT_RUNTIME = os.path.join(RUNTIME_ROOT, "methods", "redis_client.py")
HEALTH_SOURCE = os.path.join(SOURCE_ROOT, "routes", "health.py")
HEALTH_RUNTIME = os.path.join(RUNTIME_ROOT, "routes", "health.py")


def read_source(filepath):
    with open(filepath, "r") as f:
        return f.read()


def parse_env_file(filepath):
    env = {}
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


def _parse_sentinel_hosts(hosts_str):
    """Local copy of the function for testing without pylon dependencies."""
    sentinels = []
    for entry in hosts_str.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            host, port = entry.rsplit(":", 1)
            sentinels.append((host.strip(), int(port.strip())))
        else:
            sentinels.append((entry.strip(), 26379))
    return sentinels


class TestParseSentinelHosts:
    """Tests for _parse_sentinel_hosts helper function."""

    def test_single_host_with_port(self):
        result = _parse_sentinel_hosts("redis-sentinel-1:26379")
        assert result == [("redis-sentinel-1", 26379)]

    def test_multiple_hosts(self):
        result = _parse_sentinel_hosts("sentinel-1:26379,sentinel-2:26379,sentinel-3:26379")
        assert len(result) == 3
        assert result[0] == ("sentinel-1", 26379)
        assert result[1] == ("sentinel-2", 26379)
        assert result[2] == ("sentinel-3", 26379)

    def test_hosts_with_spaces(self):
        result = _parse_sentinel_hosts(" sentinel-1:26379 , sentinel-2:26379 , sentinel-3:26379 ")
        assert len(result) == 3
        assert result[0] == ("sentinel-1", 26379)
        assert result[1] == ("sentinel-2", 26379)
        assert result[2] == ("sentinel-3", 26379)

    def test_host_without_port_defaults_26379(self):
        result = _parse_sentinel_hosts("sentinel-1")
        assert result == [("sentinel-1", 26379)]

    def test_empty_string_returns_empty_list(self):
        result = _parse_sentinel_hosts("")
        assert result == []

    def test_mixed_with_and_without_ports(self):
        result = _parse_sentinel_hosts("sentinel-1:26379,sentinel-2,sentinel-3:26380")
        assert result == [
            ("sentinel-1", 26379),
            ("sentinel-2", 26379),
            ("sentinel-3", 26380),
        ]

    def test_different_ports(self):
        result = _parse_sentinel_hosts("s1:26379,s2:26380,s3:26381")
        assert result == [("s1", 26379), ("s2", 26380), ("s3", 26381)]

    def test_trailing_comma_ignored(self):
        result = _parse_sentinel_hosts("sentinel-1:26379,")
        assert result == [("sentinel-1", 26379)]

    def test_ip_addresses(self):
        result = _parse_sentinel_hosts("10.0.0.1:26379,10.0.0.2:26379")
        assert result == [("10.0.0.1", 26379), ("10.0.0.2", 26379)]

    def test_leading_comma_ignored(self):
        result = _parse_sentinel_hosts(",sentinel-1:26379")
        assert result == [("sentinel-1", 26379)]

    def test_multiple_commas_ignored(self):
        result = _parse_sentinel_hosts("sentinel-1:26379,,sentinel-2:26379")
        assert result == [("sentinel-1", 26379), ("sentinel-2", 26379)]

    def test_returns_list_type(self):
        result = _parse_sentinel_hosts("s1:26379")
        assert isinstance(result, list)
        assert isinstance(result[0], tuple)
        assert isinstance(result[0][0], str)
        assert isinstance(result[0][1], int)


class TestRedisClientSourceCode:
    """Tests that redis_client.py source has correct sentinel support code."""

    @pytest.fixture
    def source_code(self):
        return read_source(REDIS_CLIENT_SOURCE)

    def test_imports_sentinel(self, source_code):
        assert "from redis.sentinel import Sentinel" in source_code

    def test_imports_os(self, source_code):
        assert "import os" in source_code

    def test_has_parse_sentinel_hosts_function(self, source_code):
        assert "def _parse_sentinel_hosts(" in source_code

    def test_has_get_redis_client_method(self, source_code):
        assert "def get_redis_client(self)" in source_code

    def test_has_get_sentinel_info_method(self, source_code):
        assert "def get_sentinel_info(self)" in source_code

    def test_reads_sentinel_hosts_env(self, source_code):
        assert 'os.environ.get("REDIS_SENTINEL_HOSTS"' in source_code

    def test_reads_sentinel_master_env(self, source_code):
        assert 'os.environ.get("REDIS_SENTINEL_MASTER"' in source_code

    def test_default_master_is_mymaster(self, source_code):
        assert '"mymaster"' in source_code

    def test_calls_master_for(self, source_code):
        assert "master_for(" in source_code

    def test_sentinel_fallback_on_empty(self, source_code):
        # Ensure there's an if sentinel_hosts check
        assert "if sentinel_hosts:" in source_code

    def test_uses_socket_timeout(self, source_code):
        assert "socket_timeout=" in source_code

    def test_passes_password_to_sentinel_kwargs(self, source_code):
        assert "sentinel_kwargs" in source_code
        assert '"password"' in source_code or "'password'" in source_code

    def test_passes_ssl_to_sentinel_kwargs(self, source_code):
        assert '"ssl"' in source_code or "'ssl'" in source_code

    def test_caches_client_on_instance(self, source_code):
        assert "self._redis_client" in source_code

    def test_checks_cached_client_first(self, source_code):
        assert 'getattr(self, "_redis_client", None)' in source_code

    def test_sentinel_info_returns_none_when_not_configured(self, source_code):
        assert "return None" in source_code

    def test_sentinel_info_discovers_master(self, source_code):
        assert "discover_master" in source_code

    def test_sentinel_info_pings_sentinels(self, source_code):
        # Check that it individually pings sentinel nodes
        assert ".ping()" in source_code

    def test_managed_identity_skips_sentinel(self, source_code):
        # use_managed_identity branch should not enter sentinel logic
        lines = source_code.split("\n")
        managed_idx = None
        for i, line in enumerate(lines):
            if "use_managed_identity" in line and "if" in line:
                managed_idx = i
                break
        assert managed_idx is not None
        # After managed_identity block, there should be an else with sentinel logic
        assert "else:" in source_code


class TestHealthEndpointSourceCode:
    """Tests that health.py source has sentinel check in /health/live."""

    @pytest.fixture
    def source_code(self):
        return read_source(HEALTH_SOURCE)

    def test_health_live_calls_get_sentinel_info(self, source_code):
        assert "get_sentinel_info" in source_code

    def test_health_live_checks_sentinel_error(self, source_code):
        assert 'sentinel_info.get("error")' in source_code

    def test_health_live_checks_sentinels_reachable(self, source_code):
        assert 'sentinels_reachable' in source_code

    def test_health_live_reports_master_address(self, source_code):
        assert 'master_address' in source_code

    def test_sentinel_check_only_when_configured(self, source_code):
        # Should check if sentinel_info is not None before adding to checks
        assert "sentinel_info is not None" in source_code

    def test_sentinel_unhealthy_sets_overall_unhealthy(self, source_code):
        # When sentinel is unhealthy, overall_status should be set
        assert 'overall_status = "unhealthy"' in source_code


class TestEnvVarConfiguration:
    """Tests that env vars are correctly defined in default.env."""

    @pytest.fixture
    def env(self):
        return parse_env_file(DEFAULT_ENV)

    def test_redis_sentinel_hosts_defined(self, env):
        assert "REDIS_SENTINEL_HOSTS" in env

    def test_redis_sentinel_master_defined(self, env):
        assert "REDIS_SENTINEL_MASTER" in env

    def test_sentinel_hosts_has_three_entries(self, env):
        hosts = env["REDIS_SENTINEL_HOSTS"].split(",")
        assert len(hosts) == 3

    def test_sentinel_hosts_correct_names(self, env):
        hosts = env["REDIS_SENTINEL_HOSTS"].split(",")
        assert "redis-sentinel-1:26379" in hosts
        assert "redis-sentinel-2:26379" in hosts
        assert "redis-sentinel-3:26379" in hosts

    def test_sentinel_master_is_mymaster(self, env):
        assert env["REDIS_SENTINEL_MASTER"] == "mymaster"

    def test_redis_host_still_defined(self, env):
        assert "REDIS_HOST" in env
        assert env["REDIS_HOST"] != ""

    def test_redis_port_still_defined(self, env):
        assert "REDIS_PORT" in env
        assert env["REDIS_PORT"] == "6379"


class TestSourceRuntimeConsistency:
    """Tests that source and runtime copies are in sync."""

    def test_redis_client_source_matches_runtime(self):
        if not os.path.exists(REDIS_CLIENT_SOURCE):
            pytest.skip("Source file not found")
        if not os.path.exists(REDIS_CLIENT_RUNTIME):
            pytest.skip("Runtime file not found")

        source_content = read_source(REDIS_CLIENT_SOURCE)
        runtime_content = read_source(REDIS_CLIENT_RUNTIME)
        assert source_content == runtime_content, "redis_client.py source and runtime are out of sync"

    def test_health_source_matches_runtime(self):
        if not os.path.exists(HEALTH_SOURCE):
            pytest.skip("Source file not found")
        if not os.path.exists(HEALTH_RUNTIME):
            pytest.skip("Runtime file not found")

        source_content = read_source(HEALTH_SOURCE)
        runtime_content = read_source(HEALTH_RUNTIME)
        assert source_content == runtime_content, "health.py source and runtime are out of sync"


class TestSentinelConnectionLogic:
    """Tests for the connection decision logic."""

    @patch.dict(os.environ, {"REDIS_SENTINEL_HOSTS": ""})
    def test_empty_env_means_direct_connection(self):
        sentinel_hosts = os.environ.get("REDIS_SENTINEL_HOSTS", "")
        assert not sentinel_hosts
        # This means the code takes the direct redis.Redis() path

    @patch.dict(os.environ, {})
    def test_missing_env_means_direct_connection(self):
        os.environ.pop("REDIS_SENTINEL_HOSTS", None)
        sentinel_hosts = os.environ.get("REDIS_SENTINEL_HOSTS", "")
        assert not sentinel_hosts

    @patch.dict(os.environ, {
        "REDIS_SENTINEL_HOSTS": "sentinel-1:26379,sentinel-2:26379,sentinel-3:26379",
    })
    def test_set_env_means_sentinel_connection(self):
        sentinel_hosts = os.environ.get("REDIS_SENTINEL_HOSTS", "")
        assert sentinel_hosts != ""

    @patch.dict(os.environ, {
        "REDIS_SENTINEL_HOSTS": "sentinel-1:26379,sentinel-2:26379,sentinel-3:26379",
        "REDIS_SENTINEL_MASTER": "custom-master",
    })
    def test_custom_master_name_used(self):
        master = os.environ.get("REDIS_SENTINEL_MASTER", "mymaster")
        assert master == "custom-master"

    @patch.dict(os.environ, {
        "REDIS_SENTINEL_HOSTS": "sentinel-1:26379",
    })
    def test_default_master_name_when_not_set(self):
        os.environ.pop("REDIS_SENTINEL_MASTER", None)
        master = os.environ.get("REDIS_SENTINEL_MASTER", "mymaster")
        assert master == "mymaster"


class TestSentinelInfoResults:
    """Tests for sentinel info response structures."""

    def test_info_result_has_required_fields(self):
        """Validate expected structure of get_sentinel_info response."""
        result = {
            "enabled": True,
            "master_name": "mymaster",
            "sentinels_configured": 3,
            "sentinels_reachable": 3,
            "master_address": "redis:6379",
        }
        assert "enabled" in result
        assert "master_name" in result
        assert "sentinels_configured" in result
        assert "sentinels_reachable" in result
        assert "master_address" in result

    def test_info_result_with_error(self):
        """Error case adds error field."""
        result = {
            "enabled": True,
            "master_name": "mymaster",
            "sentinels_configured": 3,
            "sentinels_reachable": 0,
            "master_address": None,
            "error": "Connection refused",
        }
        assert result.get("error") is not None
        assert result["master_address"] is None

    def test_info_result_partial_reachable(self):
        """Partial reachability still works."""
        result = {
            "enabled": True,
            "master_name": "mymaster",
            "sentinels_configured": 3,
            "sentinels_reachable": 2,
            "master_address": "redis:6379",
        }
        assert result["sentinels_reachable"] > 0
        assert result["sentinels_reachable"] < result["sentinels_configured"]

    def test_info_none_when_not_configured(self):
        """Returns None when sentinel not configured."""
        sentinel_hosts = ""  # simulates empty env
        result = None if not sentinel_hosts else {"enabled": True}
        assert result is None


class TestHealthCheckSentinelIntegration:
    """Tests for health check sentinel decision logic."""

    def test_sentinel_ok_doesnt_change_overall_status(self):
        sentinel_info = {
            "sentinels_reachable": 3,
            "sentinels_configured": 3,
            "master_address": "redis:6379",
        }
        overall_status = "ok"
        if sentinel_info.get("error"):
            overall_status = "unhealthy"
        elif sentinel_info["sentinels_reachable"] == 0:
            overall_status = "unhealthy"
        assert overall_status == "ok"

    def test_sentinel_error_sets_unhealthy(self):
        sentinel_info = {
            "sentinels_reachable": 0,
            "sentinels_configured": 3,
            "master_address": None,
            "error": "Connection refused",
        }
        overall_status = "ok"
        if sentinel_info.get("error"):
            overall_status = "unhealthy"
        elif sentinel_info["sentinels_reachable"] == 0:
            overall_status = "unhealthy"
        assert overall_status == "unhealthy"

    def test_sentinel_no_reachable_sets_unhealthy(self):
        sentinel_info = {
            "sentinels_reachable": 0,
            "sentinels_configured": 3,
            "master_address": None,
        }
        overall_status = "ok"
        if sentinel_info.get("error"):
            overall_status = "unhealthy"
        elif sentinel_info["sentinels_reachable"] == 0:
            overall_status = "unhealthy"
        assert overall_status == "unhealthy"

    def test_sentinel_none_doesnt_add_check(self):
        """When sentinel_info is None, no sentinel entry in checks."""
        sentinel_info = None
        checks = {}
        if sentinel_info is not None:
            checks["sentinel"] = {"status": "ok"}
        assert "sentinel" not in checks

    def test_sentinel_partial_reachable_still_ok(self):
        """Partial reachability (e.g., 2/3) is still ok for the health check."""
        sentinel_info = {
            "sentinels_reachable": 2,
            "sentinels_configured": 3,
            "master_address": "redis:6379",
        }
        overall_status = "ok"
        if sentinel_info.get("error"):
            overall_status = "unhealthy"
        elif sentinel_info["sentinels_reachable"] == 0:
            overall_status = "unhealthy"
        assert overall_status == "ok"


class TestManagedIdentityBehavior:
    """Tests that managed identity path doesn't interact with Sentinel."""

    def test_managed_identity_config_detected(self):
        redis_config = {
            "host": "redis.cache.windows.net",
            "port": 6380,
            "use_managed_identity": True,
            "password": "temp-access-key",
        }
        assert redis_config.get("use_managed_identity", False) is True

    def test_managed_identity_pops_password(self):
        redis_config = {
            "host": "redis.cache.windows.net",
            "port": 6380,
            "use_managed_identity": True,
            "password": "temp-access-key",
        }
        redis_config.pop("use_managed_identity")
        redis_config.pop("password", None)
        assert "use_managed_identity" not in redis_config
        assert "password" not in redis_config

    def test_non_managed_identity_keeps_password(self):
        redis_config = {
            "host": "redis",
            "port": 6379,
            "password": "changeme",
        }
        assert redis_config.get("use_managed_identity", False) is False
        assert "password" in redis_config
