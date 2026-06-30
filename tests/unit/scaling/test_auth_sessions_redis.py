"""
Tests for Task 2.1 (Move auth_core sessions to Redis) and
Task 2.2 (Configure secure session cookies).

These tasks verify that the pylon framework's built-in Redis session support
is correctly configured in pylon_auth, ensuring sessions survive pod restarts
and are shared across multiple replicas.
"""

import os
import sys
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import yaml


# Paths
PROJECT_ROOT = Path(__file__).resolve().parents[3]
PYLON_AUTH_DIR = PROJECT_ROOT / "pylon_auth"
PYLON_AUTH_PYLON_YML = PYLON_AUTH_DIR / "pylon.yml"
PYLON_AUTH_LOCAL_YML = PYLON_AUTH_DIR / "pylon.local.yml"
PYLON_SESSION_MODULE = PROJECT_ROOT.parent / "pylon" / "pylon" / "core" / "tools" / "session.py"
STAGING_VALUES = PROJECT_ROOT.parent.parent / "kharkevich" / "argocd-public" / "elitea-platform" / "values" / "staging" / "pylon-auth.yaml"


def _load_yaml(path):
    """Load YAML file with environment variable placeholders preserved as strings."""
    with open(path, "r") as f:
        content = f.read()
    # Replace ${...} placeholders with dummy strings so YAML parses
    import re
    content = re.sub(r'\$\{([^}]+)\}', r'PLACEHOLDER_\1', content)
    return yaml.safe_load(content)


def _load_yaml_raw(path):
    """Load YAML file raw (may fail on env vars, use for staging which has no vars)."""
    with open(path, "r") as f:
        return yaml.safe_load(f)


# ============================================================
# Task 2.1: Sessions stored in Redis
# ============================================================


class TestSessionsRedisConfig:
    """Verify pylon_auth pylon.yml has sessions.redis configuration."""

    @pytest.fixture
    def pylon_config(self):
        return _load_yaml(PYLON_AUTH_PYLON_YML)

    @pytest.fixture
    def local_config(self):
        return _load_yaml(PYLON_AUTH_LOCAL_YML)

    def test_sessions_section_exists(self, pylon_config):
        """pylon.yml must have a 'sessions' section."""
        assert "sessions" in pylon_config

    def test_sessions_redis_section_exists(self, pylon_config):
        """sessions section must contain 'redis' subsection."""
        assert "redis" in pylon_config["sessions"]

    def test_redis_host_configured(self, pylon_config):
        """Redis host must be configured (via env var)."""
        redis_config = pylon_config["sessions"]["redis"]
        assert "host" in redis_config
        assert redis_config["host"] is not None

    def test_redis_port_configured(self, pylon_config):
        """Redis port must be configured."""
        redis_config = pylon_config["sessions"]["redis"]
        assert "port" in redis_config

    def test_session_prefix_configured(self, pylon_config):
        """Session key prefix must be configured to avoid collisions."""
        sessions = pylon_config["sessions"]
        assert "prefix" in sessions
        assert sessions["prefix"] is not None
        assert len(str(sessions["prefix"])) > 0

    def test_session_prefix_contains_auth(self, pylon_config):
        """Session prefix should indicate it belongs to auth service."""
        prefix = str(pylon_config["sessions"]["prefix"])
        assert "auth" in prefix.lower() or "session" in prefix.lower()

    def test_local_config_has_redis_sessions(self, local_config):
        """pylon.local.yml must also have sessions.redis (for local dev)."""
        assert "sessions" in local_config
        assert "redis" in local_config["sessions"]

    def test_secret_key_configured(self, pylon_config):
        """Application SECRET_KEY must be set (required for session signing)."""
        app_config = pylon_config.get("application", {})
        assert "SECRET_KEY" in app_config

    def test_session_cookie_name_configured(self, pylon_config):
        """SESSION_COOKIE_NAME must be explicitly set."""
        app_config = pylon_config.get("application", {})
        assert "SESSION_COOKIE_NAME" in app_config


class TestSessionRedisInterface:
    """Verify pylon framework creates RedisSessionInterface."""

    def test_session_module_exists(self):
        """pylon/core/tools/session.py must exist."""
        assert PYLON_SESSION_MODULE.exists()

    def test_session_module_imports_redis_interface(self):
        """session.py must import RedisSessionInterface."""
        content = PYLON_SESSION_MODULE.read_text()
        assert "RedisSessionInterface" in content

    def test_session_module_uses_redis_when_configured(self):
        """make_session_interface should use Redis when sessions.redis is set."""
        content = PYLON_SESSION_MODULE.read_text()
        assert "if redis_config:" in content
        assert "RedisSessionInterface" in content

    def test_session_module_falls_back_to_memory(self):
        """make_session_interface should fall back to memory when no redis."""
        content = PYLON_SESSION_MODULE.read_text()
        assert "CacheLibSessionInterface" in content

    def test_session_module_uses_pickle_serializer(self):
        """Session serializer should be PickleSerializer for complex objects."""
        content = PYLON_SESSION_MODULE.read_text()
        assert "PickleSerializer" in content

    def test_session_module_configures_sid_length(self):
        """Session ID length should be configured."""
        content = PYLON_SESSION_MODULE.read_text()
        assert "sid_length" in content


class TestSessionUsedByAuthCore:
    """Verify auth_core properly uses flask.session (backed by Redis)."""

    def test_auth_context_reads_from_session(self):
        """get_auth_context reads auth state from flask.session."""
        auth_context_path = PYLON_AUTH_DIR / "plugins" / "auth_core" / "methods" / "auth_context.py"
        content = auth_context_path.read_text()
        assert "flask.session" in content
        assert "session.get(" in content

    def test_auth_context_writes_to_session(self):
        """set_auth_context writes auth state to flask.session."""
        auth_context_path = PYLON_AUTH_DIR / "plugins" / "auth_core" / "methods" / "auth_context.py"
        content = auth_context_path.read_text()
        assert 'session["auth_done"]' in content
        assert 'session["auth_user_id"]' in content

    def test_oidc_login_stores_state_in_session(self):
        """OIDC login stores OAuth state in session for CSRF protection."""
        oidc_login_path = PYLON_AUTH_DIR / "plugins" / "auth_oidc" / "routes" / "login.py"
        content = oidc_login_path.read_text()
        assert 'flask.session["auth_oidc"]' in content
        assert "flask.session.modified = True" in content

    def test_oidc_callback_reads_state_from_session(self):
        """OIDC callback reads OAuth state from session."""
        oidc_login_path = PYLON_AUTH_DIR / "plugins" / "auth_oidc" / "routes" / "login.py"
        content = oidc_login_path.read_text()
        assert 'flask.session["auth_oidc"]' in content
        assert "auth_core.set_auth_context" in content


class TestSessionTTL:
    """Verify session lifetime is properly configured."""

    @pytest.fixture
    def pylon_config(self):
        return _load_yaml(PYLON_AUTH_PYLON_YML)

    def test_permanent_session_lifetime_set(self, pylon_config):
        """PERMANENT_SESSION_LIFETIME should be configured."""
        app_config = pylon_config.get("application", {})
        assert "PERMANENT_SESSION_LIFETIME" in app_config

    def test_session_lifetime_reasonable(self):
        """Session lifetime in staging should be between 1h and 7d."""
        if not STAGING_VALUES.exists():
            pytest.skip("Staging values not available")
        config = _load_yaml_raw(STAGING_VALUES)
        # Find PERMANENT_SESSION_LIFETIME in nested pylon.yml
        pylon_yml_content = config.get("config", {}).get("files", {}).get("pylon.yml", "")
        if pylon_yml_content:
            inner_config = yaml.safe_load(pylon_yml_content)
            lifetime = inner_config.get("application", {}).get("PERMANENT_SESSION_LIFETIME")
            if lifetime:
                assert 3600 <= int(lifetime) <= 604800


# ============================================================
# Task 2.2: Secure session cookies
# ============================================================


class TestCookieSecurityFlags:
    """Verify session cookie security flags are properly configured."""

    @pytest.fixture
    def pylon_config(self):
        return _load_yaml(PYLON_AUTH_PYLON_YML)

    @pytest.fixture
    def app_config(self, pylon_config):
        return pylon_config.get("application", {})

    def test_httponly_flag_set(self, app_config):
        """SESSION_COOKIE_HTTPONLY must be true (prevents XSS session theft)."""
        assert "SESSION_COOKIE_HTTPONLY" in app_config
        assert app_config["SESSION_COOKIE_HTTPONLY"] is True

    def test_samesite_flag_set(self, app_config):
        """SESSION_COOKIE_SAMESITE must be 'Lax' (CSRF protection)."""
        assert "SESSION_COOKIE_SAMESITE" in app_config
        assert app_config["SESSION_COOKIE_SAMESITE"] == "Lax"

    def test_secure_flag_configurable(self, app_config):
        """SESSION_COOKIE_SECURE must be configurable via env var."""
        assert "SESSION_COOKIE_SECURE" in app_config

    def test_cookie_name_explicit(self, app_config):
        """SESSION_COOKIE_NAME must be explicitly set (not Flask default)."""
        assert "SESSION_COOKIE_NAME" in app_config
        cookie_name = str(app_config["SESSION_COOKIE_NAME"])
        assert cookie_name != "session"  # Not Flask default

    def test_cookie_path_set(self, app_config):
        """SESSION_COOKIE_PATH must be set to '/' for all paths."""
        assert "SESSION_COOKIE_PATH" in app_config
        assert app_config["SESSION_COOKIE_PATH"] == "/"


class TestStagingCookieSecurity:
    """Verify staging deployment has all security flags active."""

    @pytest.fixture
    def staging_config(self):
        if not STAGING_VALUES.exists():
            pytest.skip("Staging values not available")
        return _load_yaml_raw(STAGING_VALUES)

    def test_staging_cookies_secure_true(self, staging_config):
        """Staging must have COOKIES_SECURE=true (HTTPS-only cookies)."""
        env = staging_config.get("env", {})
        assert env.get("COOKIES_SECURE") == "true"

    def test_staging_pylon_yml_cookie_secure(self, staging_config):
        """Staging pylon.yml has SESSION_COOKIE_SECURE: true."""
        pylon_yml_content = staging_config.get("config", {}).get("files", {}).get("pylon.yml", "")
        assert pylon_yml_content
        inner_config = yaml.safe_load(pylon_yml_content)
        app_config = inner_config.get("application", {})
        assert app_config.get("SESSION_COOKIE_SECURE") is True

    def test_staging_pylon_yml_cookie_httponly(self, staging_config):
        """Staging pylon.yml has SESSION_COOKIE_HTTPONLY: true."""
        pylon_yml_content = staging_config.get("config", {}).get("files", {}).get("pylon.yml", "")
        inner_config = yaml.safe_load(pylon_yml_content)
        app_config = inner_config.get("application", {})
        assert app_config.get("SESSION_COOKIE_HTTPONLY") is True

    def test_staging_pylon_yml_cookie_samesite(self, staging_config):
        """Staging pylon.yml has SESSION_COOKIE_SAMESITE: Lax."""
        pylon_yml_content = staging_config.get("config", {}).get("files", {}).get("pylon.yml", "")
        inner_config = yaml.safe_load(pylon_yml_content)
        app_config = inner_config.get("application", {})
        assert app_config.get("SESSION_COOKIE_SAMESITE") == "Lax"

    def test_staging_session_uses_redis(self, staging_config):
        """Staging pylon.yml must have sessions.redis configured."""
        pylon_yml_content = staging_config.get("config", {}).get("files", {}).get("pylon.yml", "")
        inner_config = yaml.safe_load(pylon_yml_content)
        sessions = inner_config.get("sessions", {})
        assert "redis" in sessions
        assert sessions["redis"].get("host") is not None

    def test_staging_session_prefix_unique(self, staging_config):
        """Staging session prefix must be unique to avoid collisions."""
        pylon_yml_content = staging_config.get("config", {}).get("files", {}).get("pylon.yml", "")
        inner_config = yaml.safe_load(pylon_yml_content)
        sessions = inner_config.get("sessions", {})
        prefix = sessions.get("prefix", "")
        assert "staging" in prefix
        assert "auth" in prefix or "session" in prefix

    def test_staging_secret_key_not_default(self, staging_config):
        """Staging SECRET_KEY must not be empty."""
        pylon_yml_content = staging_config.get("config", {}).get("files", {}).get("pylon.yml", "")
        inner_config = yaml.safe_load(pylon_yml_content)
        app_config = inner_config.get("application", {})
        secret_key = app_config.get("SECRET_KEY", "")
        assert len(str(secret_key)) > 0


class TestLocalDevCookies:
    """Verify local dev has appropriate cookie settings."""

    @pytest.fixture
    def env_config(self):
        env_path = PROJECT_ROOT / "envs" / "default.env"
        if not env_path.exists():
            pytest.skip("default.env not available")
        config = {}
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    config[key] = value
        return config

    def test_local_dev_cookies_not_secure(self, env_config):
        """Local dev should have COOKIES_SECURE=false (no HTTPS locally)."""
        assert env_config.get("COOKIES_SECURE") == "false"

    def test_local_dev_has_cookie_lifetime(self, env_config):
        """Local dev should have a session lifetime configured."""
        lifetime = env_config.get("COOKIES_LIFETIME")
        assert lifetime is not None
        assert int(lifetime) > 0


class TestSessionPersistenceAcrossReplicas:
    """Verify architecture enables session sharing across pods."""

    @pytest.fixture
    def pylon_config(self):
        return _load_yaml(PYLON_AUTH_PYLON_YML)

    def test_no_memory_session_in_production_config(self, pylon_config):
        """When redis is configured, memory fallback must not be used."""
        sessions = pylon_config.get("sessions", {})
        assert "redis" in sessions
        # memory section should not exist alongside redis
        assert "memory" not in sessions or not sessions.get("memory")

    def test_session_not_filesystem_backed(self, pylon_config):
        """Sessions must not be filesystem-backed (not portable across pods)."""
        sessions = pylon_config.get("sessions", {})
        assert "filesystem" not in sessions

    def test_redis_connection_has_keepalive(self):
        """Redis session client should use socket_keepalive for reliability."""
        content = PYLON_SESSION_MODULE.read_text()
        assert "socket_keepalive" in content

    def test_redis_connection_has_timeout(self):
        """Redis session client should have socket_timeout configured."""
        content = PYLON_SESSION_MODULE.read_text()
        assert "socket_timeout" in content


class TestMultiReplicaSessionConsistency:
    """Verify session configuration is consistent across pylon.yml and pylon.local.yml."""

    def test_both_configs_use_redis(self):
        """Both pylon.yml and pylon.local.yml must use Redis sessions."""
        config1 = _load_yaml(PYLON_AUTH_PYLON_YML)
        config2 = _load_yaml(PYLON_AUTH_LOCAL_YML)
        assert "redis" in config1.get("sessions", {})
        assert "redis" in config2.get("sessions", {})

    def test_same_prefix_pattern(self):
        """Both configs must use the same session prefix pattern."""
        config1 = _load_yaml(PYLON_AUTH_PYLON_YML)
        config2 = _load_yaml(PYLON_AUTH_LOCAL_YML)
        prefix1 = config1.get("sessions", {}).get("prefix", "")
        prefix2 = config2.get("sessions", {}).get("prefix", "")
        # Both should end with "_auth_session_"
        assert str(prefix1).endswith("_auth_session_") or "auth_session" in str(prefix1)
        assert str(prefix2).endswith("_auth_session_") or "auth_session" in str(prefix2)

    def test_cookie_flags_consistent(self):
        """Cookie security flags must be consistent across configs."""
        config1 = _load_yaml(PYLON_AUTH_PYLON_YML)
        config2 = _load_yaml(PYLON_AUTH_LOCAL_YML)
        app1 = config1.get("application", {})
        app2 = config2.get("application", {})
        # HTTPONLY and SAMESITE should be same
        assert app1.get("SESSION_COOKIE_HTTPONLY") == app2.get("SESSION_COOKIE_HTTPONLY")
        assert app1.get("SESSION_COOKIE_SAMESITE") == app2.get("SESSION_COOKIE_SAMESITE")
