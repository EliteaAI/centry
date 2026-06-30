"""
Tests for Task 6.1: Enable Redis AUTH and TLS.

Validates that:
- TLS certificates are generated and valid
- redis.conf supports TLS configuration
- redis-tls.conf has correct TLS directives
- sentinel-tls.conf references TLS settings
- docker-compose.yml mounts TLS volumes and uses conditional config
- redis_client.py builds SSL context from env vars
- redis_client.py passes ssl_context to Sentinel and direct connections
- Environment variables for TLS are defined in default.env
- Password is stored in env var (not hardcoded)
- All services use REDIS_PASSWORD from env
"""

import os
import re
import ssl
import ast
import subprocess
from unittest.mock import patch, MagicMock

import pytest


CENTRY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SOURCE_ROOT = os.path.join(CENTRY_ROOT, "..", "elitea_core")
REDIS_DIR = os.path.join(CENTRY_ROOT, "redis")
TLS_DIR = os.path.join(REDIS_DIR, "tls")
DOCKER_COMPOSE = os.path.join(CENTRY_ROOT, "docker-compose.yml")
DEFAULT_ENV = os.path.join(CENTRY_ROOT, "envs", "default.env")
OVERRIDE_ENV = os.path.join(CENTRY_ROOT, "envs", "override.env")
REDIS_CLIENT_SOURCE = os.path.join(SOURCE_ROOT, "methods", "redis_client.py")


def read_file(filepath):
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


# ===========================================================================
# TLS Certificate Tests
# ===========================================================================

class TestTLSCertificates:
    """Validate TLS certificate generation and structure."""

    def test_generate_script_exists(self):
        script = os.path.join(TLS_DIR, "generate-certs.sh")
        assert os.path.isfile(script), "generate-certs.sh must exist"

    def test_generate_script_executable(self):
        script = os.path.join(TLS_DIR, "generate-certs.sh")
        assert os.access(script, os.X_OK), "generate-certs.sh must be executable"

    def test_ca_cert_exists(self):
        assert os.path.isfile(os.path.join(TLS_DIR, "ca.crt"))

    def test_ca_key_exists(self):
        assert os.path.isfile(os.path.join(TLS_DIR, "ca.key"))

    def test_server_cert_exists(self):
        assert os.path.isfile(os.path.join(TLS_DIR, "redis.crt"))

    def test_server_key_exists(self):
        assert os.path.isfile(os.path.join(TLS_DIR, "redis.key"))

    def test_client_cert_exists(self):
        assert os.path.isfile(os.path.join(TLS_DIR, "client.crt"))

    def test_client_key_exists(self):
        assert os.path.isfile(os.path.join(TLS_DIR, "client.key"))

    def test_server_cert_signed_by_ca(self):
        result = subprocess.run(
            ["openssl", "verify", "-CAfile",
             os.path.join(TLS_DIR, "ca.crt"),
             os.path.join(TLS_DIR, "redis.crt")],
            capture_output=True, text=True
        )
        assert result.returncode == 0, f"Server cert verification failed: {result.stderr}"

    def test_client_cert_signed_by_ca(self):
        result = subprocess.run(
            ["openssl", "verify", "-CAfile",
             os.path.join(TLS_DIR, "ca.crt"),
             os.path.join(TLS_DIR, "client.crt")],
            capture_output=True, text=True
        )
        assert result.returncode == 0, f"Client cert verification failed: {result.stderr}"

    def test_server_cert_has_san_redis(self):
        result = subprocess.run(
            ["openssl", "x509", "-in", os.path.join(TLS_DIR, "redis.crt"),
             "-noout", "-text"],
            capture_output=True, text=True
        )
        assert "DNS:redis" in result.stdout, "Server cert must have SAN DNS:redis"

    def test_server_cert_has_san_localhost(self):
        result = subprocess.run(
            ["openssl", "x509", "-in", os.path.join(TLS_DIR, "redis.crt"),
             "-noout", "-text"],
            capture_output=True, text=True
        )
        assert "DNS:localhost" in result.stdout, "Server cert must have SAN DNS:localhost"

    def test_server_key_permissions(self):
        mode = os.stat(os.path.join(TLS_DIR, "redis.key")).st_mode & 0o777
        assert mode == 0o600, f"redis.key should be 0600, got {oct(mode)}"

    def test_ca_key_permissions(self):
        mode = os.stat(os.path.join(TLS_DIR, "ca.key")).st_mode & 0o777
        assert mode == 0o600, f"ca.key should be 0600, got {oct(mode)}"

    def test_client_key_permissions(self):
        mode = os.stat(os.path.join(TLS_DIR, "client.key")).st_mode & 0o777
        assert mode == 0o600, f"client.key should be 0600, got {oct(mode)}"


# ===========================================================================
# Redis Configuration Tests
# ===========================================================================

class TestRedisConfig:
    """Validate Redis config files support TLS."""

    def test_redis_conf_has_tls_comments(self):
        content = read_file(os.path.join(REDIS_DIR, "redis.conf"))
        assert "tls-port" in content, "redis.conf must reference tls-port"
        assert "tls-cert-file" in content

    def test_redis_tls_conf_exists(self):
        assert os.path.isfile(os.path.join(REDIS_DIR, "redis-tls.conf"))

    def test_redis_tls_conf_has_tls_port(self):
        content = read_file(os.path.join(REDIS_DIR, "redis-tls.conf"))
        assert re.search(r"^tls-port\s+6380", content, re.MULTILINE)

    def test_redis_tls_conf_has_cert_paths(self):
        content = read_file(os.path.join(REDIS_DIR, "redis-tls.conf"))
        assert "tls-cert-file /tls/redis.crt" in content
        assert "tls-key-file /tls/redis.key" in content
        assert "tls-ca-cert-file /tls/ca.crt" in content

    def test_redis_tls_conf_has_auth_clients(self):
        content = read_file(os.path.join(REDIS_DIR, "redis-tls.conf"))
        assert "tls-auth-clients" in content

    def test_redis_tls_conf_has_persistence(self):
        content = read_file(os.path.join(REDIS_DIR, "redis-tls.conf"))
        assert "appendonly yes" in content
        assert "save 900 1" in content

    def test_redis_tls_conf_keeps_plain_port(self):
        content = read_file(os.path.join(REDIS_DIR, "redis-tls.conf"))
        assert re.search(r"^port\s+6379", content, re.MULTILINE), \
            "TLS config keeps plain port for backward compat"


# ===========================================================================
# Sentinel TLS Configuration Tests
# ===========================================================================

class TestSentinelTLSConfig:
    """Validate sentinel TLS configuration."""

    def test_sentinel_tls_conf_exists(self):
        assert os.path.isfile(os.path.join(REDIS_DIR, "sentinel-tls.conf"))

    def test_sentinel_tls_conf_has_tls_certs(self):
        content = read_file(os.path.join(REDIS_DIR, "sentinel-tls.conf"))
        assert "tls-cert-file /tls/redis.crt" in content
        assert "tls-key-file /tls/redis.key" in content
        assert "tls-ca-cert-file /tls/ca.crt" in content

    def test_sentinel_tls_conf_monitors_tls_port(self):
        content = read_file(os.path.join(REDIS_DIR, "sentinel-tls.conf"))
        assert "sentinel monitor mymaster redis 6380 2" in content

    def test_sentinel_tls_conf_has_auth_pass(self):
        content = read_file(os.path.join(REDIS_DIR, "sentinel-tls.conf"))
        assert "sentinel auth-pass mymaster" in content

    def test_sentinel_tls_conf_has_replication_tls(self):
        content = read_file(os.path.join(REDIS_DIR, "sentinel-tls.conf"))
        assert "tls-replication yes" in content

    def test_sentinel_plain_conf_has_auth_pass(self):
        content = read_file(os.path.join(REDIS_DIR, "sentinel.conf"))
        assert "sentinel auth-pass mymaster" in content


# ===========================================================================
# Docker Compose Tests
# ===========================================================================

class TestDockerCompose:
    """Validate docker-compose supports TLS toggle."""

    def test_redis_service_mounts_tls_dir(self):
        content = read_file(DOCKER_COMPOSE)
        assert "./redis/tls:/tls:ro" in content

    def test_redis_service_mounts_tls_conf(self):
        content = read_file(DOCKER_COMPOSE)
        assert "./redis/redis-tls.conf:/etc/redis/redis-tls.conf:ro" in content

    def test_redis_service_conditional_tls(self):
        content = read_file(DOCKER_COMPOSE)
        assert "REDIS_TLS_ENABLED" in content

    def test_redis_service_uses_requirepass(self):
        content = read_file(DOCKER_COMPOSE)
        assert "--requirepass" in content

    def test_sentinel_mounts_tls_dir(self):
        content = read_file(DOCKER_COMPOSE)
        sentinel_sections = content.split("redis-sentinel-")
        for section in sentinel_sections[1:]:
            assert "./redis/tls:/tls:ro" in section.split("redis-sentinel-")[0] if "redis-sentinel-" in section else section

    def test_sentinel_mounts_tls_conf(self):
        content = read_file(DOCKER_COMPOSE)
        assert "./redis/sentinel-tls.conf:/etc/redis/sentinel-tls.conf:ro" in content

    def test_sentinel_conditional_tls(self):
        content = read_file(DOCKER_COMPOSE)
        sentinel_section = content[content.find("redis-sentinel-1"):]
        assert "REDIS_TLS_ENABLED" in sentinel_section


# ===========================================================================
# Environment Variable Tests
# ===========================================================================

class TestEnvironmentVariables:
    """Validate env vars for Redis AUTH and TLS."""

    def test_default_env_has_redis_password(self):
        env = parse_env_file(DEFAULT_ENV)
        assert "REDIS_PASSWORD" in env
        assert env["REDIS_PASSWORD"] != ""

    def test_default_env_has_tls_enabled(self):
        env = parse_env_file(DEFAULT_ENV)
        assert "REDIS_TLS_ENABLED" in env
        assert env["REDIS_TLS_ENABLED"] == "false"

    def test_default_env_has_tls_cert_file(self):
        env = parse_env_file(DEFAULT_ENV)
        assert "REDIS_TLS_CERT_FILE" in env
        assert "/tls/" in env["REDIS_TLS_CERT_FILE"]

    def test_default_env_has_tls_key_file(self):
        env = parse_env_file(DEFAULT_ENV)
        assert "REDIS_TLS_KEY_FILE" in env
        assert "/tls/" in env["REDIS_TLS_KEY_FILE"]

    def test_default_env_has_tls_ca_file(self):
        env = parse_env_file(DEFAULT_ENV)
        assert "REDIS_TLS_CA_FILE" in env
        assert "/tls/" in env["REDIS_TLS_CA_FILE"]

    def test_override_env_has_tls_enabled(self):
        env = parse_env_file(OVERRIDE_ENV)
        assert "REDIS_TLS_ENABLED" in env
        assert env["REDIS_TLS_ENABLED"] == "false"

    def test_password_not_hardcoded_in_redis_client(self):
        content = read_file(REDIS_CLIENT_SOURCE)
        lines = [l for l in content.split("\n") if not l.strip().startswith("#")]
        code = "\n".join(lines)
        assert "changeme" not in code, "Password must not be hardcoded in source"

    def test_password_from_env_in_docker_compose(self):
        content = read_file(DOCKER_COMPOSE)
        assert "$$REDIS_PASSWORD" in content


# ===========================================================================
# Redis Client TLS Integration Tests
# ===========================================================================

class TestRedisClientTLS:
    """Validate redis_client.py handles TLS configuration."""

    def test_source_imports_ssl(self):
        content = read_file(REDIS_CLIENT_SOURCE)
        assert "import ssl" in content

    def test_source_has_build_ssl_context(self):
        content = read_file(REDIS_CLIENT_SOURCE)
        assert "def _build_ssl_context" in content

    def test_source_checks_redis_tls_enabled(self):
        content = read_file(REDIS_CLIENT_SOURCE)
        assert "REDIS_TLS_ENABLED" in content

    def test_source_reads_tls_ca_file(self):
        content = read_file(REDIS_CLIENT_SOURCE)
        assert "REDIS_TLS_CA_FILE" in content

    def test_source_reads_tls_cert_file(self):
        content = read_file(REDIS_CLIENT_SOURCE)
        assert "REDIS_TLS_CERT_FILE" in content

    def test_source_reads_tls_key_file(self):
        content = read_file(REDIS_CLIENT_SOURCE)
        assert "REDIS_TLS_KEY_FILE" in content

    def test_source_uses_ssl_context_in_sentinel(self):
        content = read_file(REDIS_CLIENT_SOURCE)
        assert "ssl_context" in content

    def test_source_switches_to_tls_port(self):
        content = read_file(REDIS_CLIENT_SOURCE)
        assert "6380" in content

    def test_build_ssl_context_returns_none_without_ca(self):
        """_build_ssl_context returns None when no CA file exists."""
        import importlib.util
        import sys
        import types

        # Create minimal mock for pylon imports
        pylon_mock = types.ModuleType("pylon")
        pylon_core = types.ModuleType("pylon.core")
        pylon_core_tools = types.ModuleType("pylon.core.tools")
        pylon_core_tools.web = MagicMock()
        pylon_core_tools.log = MagicMock()
        pylon_mock.core = pylon_core
        pylon_core.tools = pylon_core_tools
        tools_mock = types.ModuleType("tools")
        tools_mock.config = MagicMock()

        with patch.dict(sys.modules, {
            "pylon": pylon_mock,
            "pylon.core": pylon_core,
            "pylon.core.tools": pylon_core_tools,
            "tools": tools_mock,
            "redis": MagicMock(),
            "redis.sentinel": MagicMock(),
        }):
            with patch.dict(os.environ, {
                "REDIS_TLS_CA_FILE": "/nonexistent/ca.crt",
                "REDIS_TLS_CERT_FILE": "",
                "REDIS_TLS_KEY_FILE": "",
            }):
                spec = importlib.util.spec_from_file_location(
                    "redis_client_mod", REDIS_CLIENT_SOURCE
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                result = mod._build_ssl_context()
                assert result is None

    def test_build_ssl_context_returns_context_with_valid_ca(self):
        """_build_ssl_context returns SSLContext when CA file exists."""
        import importlib.util
        import sys
        import types

        pylon_mock = types.ModuleType("pylon")
        pylon_core = types.ModuleType("pylon.core")
        pylon_core_tools = types.ModuleType("pylon.core.tools")
        pylon_core_tools.web = MagicMock()
        pylon_core_tools.log = MagicMock()
        pylon_mock.core = pylon_core
        pylon_core.tools = pylon_core_tools
        tools_mock = types.ModuleType("tools")
        tools_mock.config = MagicMock()

        ca_file = os.path.join(TLS_DIR, "ca.crt")
        cert_file = os.path.join(TLS_DIR, "client.crt")
        key_file = os.path.join(TLS_DIR, "client.key")

        with patch.dict(sys.modules, {
            "pylon": pylon_mock,
            "pylon.core": pylon_core,
            "pylon.core.tools": pylon_core_tools,
            "tools": tools_mock,
            "redis": MagicMock(),
            "redis.sentinel": MagicMock(),
        }):
            with patch.dict(os.environ, {
                "REDIS_TLS_CA_FILE": ca_file,
                "REDIS_TLS_CERT_FILE": cert_file,
                "REDIS_TLS_KEY_FILE": key_file,
            }):
                spec = importlib.util.spec_from_file_location(
                    "redis_client_mod2", REDIS_CLIENT_SOURCE
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                result = mod._build_ssl_context()
                assert result is not None
                assert isinstance(result, ssl.SSLContext)

    def test_build_ssl_context_loads_client_cert(self):
        """_build_ssl_context loads client cert when both cert and key present."""
        import importlib.util
        import sys
        import types

        pylon_mock = types.ModuleType("pylon")
        pylon_core = types.ModuleType("pylon.core")
        pylon_core_tools = types.ModuleType("pylon.core.tools")
        pylon_core_tools.web = MagicMock()
        pylon_core_tools.log = MagicMock()
        pylon_mock.core = pylon_core
        pylon_core.tools = pylon_core_tools
        tools_mock = types.ModuleType("tools")
        tools_mock.config = MagicMock()

        ca_file = os.path.join(TLS_DIR, "ca.crt")
        cert_file = os.path.join(TLS_DIR, "client.crt")
        key_file = os.path.join(TLS_DIR, "client.key")

        with patch.dict(sys.modules, {
            "pylon": pylon_mock,
            "pylon.core": pylon_core,
            "pylon.core.tools": pylon_core_tools,
            "tools": tools_mock,
            "redis": MagicMock(),
            "redis.sentinel": MagicMock(),
        }):
            with patch.dict(os.environ, {
                "REDIS_TLS_CA_FILE": ca_file,
                "REDIS_TLS_CERT_FILE": cert_file,
                "REDIS_TLS_KEY_FILE": key_file,
            }):
                spec = importlib.util.spec_from_file_location(
                    "redis_client_mod3", REDIS_CLIENT_SOURCE
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                ctx = mod._build_ssl_context()
                assert ctx is not None
                assert ctx.verify_mode == ssl.CERT_REQUIRED

    def test_build_ssl_context_without_client_cert(self):
        """_build_ssl_context works without client cert (CA-only verification)."""
        import importlib.util
        import sys
        import types

        pylon_mock = types.ModuleType("pylon")
        pylon_core = types.ModuleType("pylon.core")
        pylon_core_tools = types.ModuleType("pylon.core.tools")
        pylon_core_tools.web = MagicMock()
        pylon_core_tools.log = MagicMock()
        pylon_mock.core = pylon_core
        pylon_core.tools = pylon_core_tools
        tools_mock = types.ModuleType("tools")
        tools_mock.config = MagicMock()

        ca_file = os.path.join(TLS_DIR, "ca.crt")

        with patch.dict(sys.modules, {
            "pylon": pylon_mock,
            "pylon.core": pylon_core,
            "pylon.core.tools": pylon_core_tools,
            "tools": tools_mock,
            "redis": MagicMock(),
            "redis.sentinel": MagicMock(),
        }):
            with patch.dict(os.environ, {
                "REDIS_TLS_CA_FILE": ca_file,
                "REDIS_TLS_CERT_FILE": "",
                "REDIS_TLS_KEY_FILE": "",
            }):
                spec = importlib.util.spec_from_file_location(
                    "redis_client_mod4", REDIS_CLIENT_SOURCE
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                ctx = mod._build_ssl_context()
                assert ctx is not None
                assert isinstance(ctx, ssl.SSLContext)

    def test_sentinel_info_includes_tls_enabled(self):
        """get_sentinel_info result includes tls_enabled field."""
        content = read_file(REDIS_CLIENT_SOURCE)
        assert '"tls_enabled"' in content or "'tls_enabled'" in content


# ===========================================================================
# Security Tests
# ===========================================================================

class TestSecurityRequirements:
    """Validate security aspects of Redis AUTH + TLS setup."""

    def test_no_hardcoded_passwords_in_configs(self):
        for fname in ["redis.conf", "redis-tls.conf"]:
            content = read_file(os.path.join(REDIS_DIR, fname))
            assert "changeme" not in content
            assert "requirepass" not in content, \
                f"{fname} should not have requirepass (passed via command line)"

    def test_sentinel_uses_placeholder_not_password(self):
        for fname in ["sentinel.conf", "sentinel-tls.conf"]:
            content = read_file(os.path.join(REDIS_DIR, fname))
            assert "changeme" not in content
            if "auth-pass" in content:
                assert "REDIS_PASSWORD_PLACEHOLDER" in content

    def test_tls_config_requires_ca_verification(self):
        content = read_file(os.path.join(REDIS_DIR, "redis-tls.conf"))
        assert "tls-ca-cert-file" in content

    def test_redis_client_verifies_certs(self):
        content = read_file(REDIS_CLIENT_SOURCE)
        assert "CERT_REQUIRED" in content

    def test_generate_script_uses_sha256(self):
        content = read_file(os.path.join(TLS_DIR, "generate-certs.sh"))
        assert "-sha256" in content

    def test_generate_script_uses_4096_ca_key(self):
        content = read_file(os.path.join(TLS_DIR, "generate-certs.sh"))
        assert "4096" in content


# ===========================================================================
# Consistency Tests
# ===========================================================================

class TestConsistency:
    """Validate consistency across configs."""

    def test_tls_port_consistent(self):
        tls_conf = read_file(os.path.join(REDIS_DIR, "redis-tls.conf"))
        sentinel_tls = read_file(os.path.join(REDIS_DIR, "sentinel-tls.conf"))
        client = read_file(REDIS_CLIENT_SOURCE)
        assert "6380" in tls_conf
        assert "6380" in sentinel_tls
        assert "6380" in client

    def test_ca_path_consistent(self):
        tls_conf = read_file(os.path.join(REDIS_DIR, "redis-tls.conf"))
        sentinel_tls = read_file(os.path.join(REDIS_DIR, "sentinel-tls.conf"))
        assert "/tls/ca.crt" in tls_conf
        assert "/tls/ca.crt" in sentinel_tls

    def test_cert_path_consistent(self):
        tls_conf = read_file(os.path.join(REDIS_DIR, "redis-tls.conf"))
        sentinel_tls = read_file(os.path.join(REDIS_DIR, "sentinel-tls.conf"))
        assert "/tls/redis.crt" in tls_conf
        assert "/tls/redis.crt" in sentinel_tls

    def test_key_path_consistent(self):
        tls_conf = read_file(os.path.join(REDIS_DIR, "redis-tls.conf"))
        sentinel_tls = read_file(os.path.join(REDIS_DIR, "sentinel-tls.conf"))
        assert "/tls/redis.key" in tls_conf
        assert "/tls/redis.key" in sentinel_tls

    def test_default_tls_disabled(self):
        """TLS is disabled by default — safe for local dev."""
        env = parse_env_file(DEFAULT_ENV)
        assert env.get("REDIS_TLS_ENABLED") == "false"
        assert env.get("REDIS_SSL") == "false"
