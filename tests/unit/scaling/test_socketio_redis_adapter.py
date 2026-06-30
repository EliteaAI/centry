"""Unit tests for Socket.IO Redis adapter configuration.

Validates that:
1. The shared.yml config has the correct socketio.redis structure
2. The configuration values produce the correct Redis URL
3. Boolean env vars expand correctly via YAML SafeLoader

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_socketio_redis_adapter.py -v
"""

import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Replicate the URL construction logic from pylon's create_client_manager
# so we can test it without importing the entire pylon framework.
# Source: pylon/pylon/core/tools/server/socketio.py lines 136-171
# ---------------------------------------------------------------------------

def build_redis_url(redis_config):
    """Build Redis URL from config dict (mirrors pylon framework logic)."""
    host = redis_config.get("host")
    if not host:
        return None

    port = redis_config.get("port", 6379)
    password = redis_config.get("password", "")
    database = redis_config.get("database", 0)
    use_ssl = redis_config.get("use_ssl", False)
    username = redis_config.get("username", "")

    if password is None:
        password = ""
    if username is None:
        username = ""

    username_password = ""
    if password or username:
        username_password = ":".join([username, password])
        username_password = f"{username_password}@"

    scheme = "rediss" if use_ssl else "redis"
    return f"{scheme}://{username_password}{host}:{port}/{database}"


def load_shared_yml_with_env(env_overrides=None):
    """Load shared.yml and simulate env var expansion."""
    config_path = (
        pathlib.Path(__file__).resolve().parents[3]
        / "pylon_main" / "configs" / "shared.yml"
    )
    with open(config_path, "r") as f:
        raw = f.read()

    defaults = {
        "REDIS_HOST": "redis",
        "REDIS_PORT": "6379",
        "REDIS_PASSWORD": "changeme",
        "REDIS_SSL": "false",
        "APP_HOST": "localhost",
        "APP_PROTO": "http",
        "POSTGRES_HOST": "postgres",
        "POSTGRES_PORT": "5432",
        "POSTGRES_USER": "user",
        "POSTGRES_PASSWORD": "pass",
        "POSTGRES_DB": "db",
        "SECRETS_MASTER_KEY": "key",
    }
    if env_overrides:
        defaults.update(env_overrides)

    for var, val in defaults.items():
        raw = raw.replace(f"${{{var}}}", val)

    return yaml.safe_load(raw)


class TestRedisUrlConstruction:
    """Tests that config produces correct Redis URL (mirrors pylon logic)."""

    def test_basic_redis_url(self):
        url = build_redis_url({
            "host": "redis",
            "port": 6379,
            "password": "",
            "use_ssl": False,
        })
        assert url == "redis://redis:6379/0"

    def test_redis_url_with_password(self):
        url = build_redis_url({
            "host": "redis-host",
            "port": 6380,
            "password": "secret123",
            "use_ssl": False,
        })
        assert url == "redis://:secret123@redis-host:6380/0"

    def test_redis_url_with_ssl(self):
        url = build_redis_url({
            "host": "redis-secure",
            "port": 6379,
            "password": "pass",
            "use_ssl": True,
        })
        assert url == "rediss://:pass@redis-secure:6379/0"

    def test_redis_url_with_username_and_password(self):
        url = build_redis_url({
            "host": "redis-host",
            "port": 6379,
            "password": "pass",
            "username": "myuser",
            "use_ssl": False,
        })
        assert url == "redis://myuser:pass@redis-host:6379/0"

    def test_redis_url_with_database(self):
        url = build_redis_url({
            "host": "redis",
            "port": 6379,
            "password": "",
            "use_ssl": False,
            "database": 3,
        })
        assert url == "redis://redis:6379/3"

    def test_redis_url_none_when_no_host(self):
        url = build_redis_url({})
        assert url is None

    def test_redis_url_none_password_treated_as_empty(self):
        url = build_redis_url({
            "host": "redis",
            "port": 6379,
            "password": None,
            "use_ssl": False,
        })
        assert url == "redis://redis:6379/0"

    def test_redis_url_defaults(self):
        url = build_redis_url({"host": "redis"})
        assert url == "redis://redis:6379/0"

    def test_redis_url_empty_username_with_password(self):
        url = build_redis_url({
            "host": "redis",
            "port": 6379,
            "password": "secret",
            "username": "",
            "use_ssl": False,
        })
        assert url == "redis://:secret@redis:6379/0"


class TestSharedYmlConfig:
    """Validate the shared.yml config structure matches framework expectations."""

    def test_socketio_redis_section_present(self):
        """shared.yml has socketio.redis section under settings."""
        config = load_shared_yml_with_env()
        settings = config["settings"]
        assert "socketio" in settings
        assert "redis" in settings["socketio"]

    def test_socketio_redis_has_required_fields(self):
        """socketio.redis section has host, port, password, use_ssl, queue."""
        config = load_shared_yml_with_env()
        redis_cfg = config["settings"]["socketio"]["redis"]
        assert "host" in redis_cfg
        assert "port" in redis_cfg
        assert "password" in redis_cfg
        assert "use_ssl" in redis_cfg
        assert "queue" in redis_cfg

    def test_socketio_redis_values_from_env(self):
        """Values are correctly populated from env vars."""
        config = load_shared_yml_with_env({
            "REDIS_HOST": "my-redis",
            "REDIS_PORT": "6380",
            "REDIS_PASSWORD": "mypass",
            "REDIS_SSL": "false",
        })
        redis_cfg = config["settings"]["socketio"]["redis"]
        assert redis_cfg["host"] == "my-redis"
        assert redis_cfg["port"] == 6380
        assert redis_cfg["password"] == "mypass"
        assert redis_cfg["use_ssl"] is False
        assert redis_cfg["queue"] == "socketio"

    def test_socketio_redis_ssl_false_is_boolean(self):
        """use_ssl: ${REDIS_SSL} expands to boolean False, not string 'false'."""
        config = load_shared_yml_with_env({"REDIS_SSL": "false"})
        use_ssl = config["settings"]["socketio"]["redis"]["use_ssl"]
        assert use_ssl is False
        assert isinstance(use_ssl, bool)

    def test_socketio_redis_ssl_true_is_boolean(self):
        """use_ssl: ${REDIS_SSL} expands to boolean True when env is 'true'."""
        config = load_shared_yml_with_env({"REDIS_SSL": "true"})
        use_ssl = config["settings"]["socketio"]["redis"]["use_ssl"]
        assert use_ssl is True
        assert isinstance(use_ssl, bool)

    def test_socketio_redis_produces_valid_url(self):
        """Full config produces a valid Redis URL."""
        config = load_shared_yml_with_env({
            "REDIS_HOST": "redis",
            "REDIS_PORT": "6379",
            "REDIS_PASSWORD": "changeme",
            "REDIS_SSL": "false",
        })
        redis_cfg = config["settings"]["socketio"]["redis"]
        url = build_redis_url(redis_cfg)
        assert url == "redis://:changeme@redis:6379/0"

    def test_socketio_redis_queue_defaults_to_socketio(self):
        """The queue (channel) is set to 'socketio'."""
        config = load_shared_yml_with_env()
        queue = config["settings"]["socketio"]["redis"]["queue"]
        assert queue == "socketio"

    def test_redis_vars_shared_with_arbiter(self):
        """socketio.redis uses same env vars as arbiter_runtime Redis config."""
        config = load_shared_yml_with_env({
            "REDIS_HOST": "shared-redis",
            "REDIS_PORT": "6379",
            "REDIS_PASSWORD": "shared-pass",
        })
        settings = config["settings"]
        # Arbiter uses these top-level settings
        assert settings["redis_host"] == "shared-redis"
        assert settings["redis_port"] == 6379
        assert settings["redis_password"] == "shared-pass"
        # Socket.IO uses the same Redis instance
        assert settings["socketio"]["redis"]["host"] == "shared-redis"
        assert settings["socketio"]["redis"]["port"] == 6379
        assert settings["socketio"]["redis"]["password"] == "shared-pass"
