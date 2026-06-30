"""
Tests for Task 5.5: Redis Sentinel deployment (3 nodes).

Validates that:
- sentinel.conf exists and has correct monitoring config
- docker-compose.yml has all 3 sentinel containers defined
- Each sentinel depends on the redis master
- Healthcheck is configured for sentinel port (26379)
- Password is templated (not hardcoded in final config)
- env vars include sentinel host list and master name
- Sentinel quorum is set to 2 (majority of 3)
"""

import os
import re

import yaml
import pytest


CENTRY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SENTINEL_CONF = os.path.join(CENTRY_ROOT, "redis", "sentinel.conf")
DOCKER_COMPOSE = os.path.join(CENTRY_ROOT, "docker-compose.yml")
DEFAULT_ENV = os.path.join(CENTRY_ROOT, "envs", "default.env")


def load_docker_compose():
    with open(DOCKER_COMPOSE, "r") as f:
        return yaml.safe_load(f)


def parse_sentinel_conf():
    config = {}
    with open(SENTINEL_CONF, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                key = parts[0]
                if key not in config:
                    config[key] = []
                config[key].append(parts[1])
    return config


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


class TestSentinelConfFile:
    """Tests for centry/redis/sentinel.conf."""

    def test_sentinel_conf_exists(self):
        assert os.path.isfile(SENTINEL_CONF), f"sentinel.conf not found at {SENTINEL_CONF}"

    def test_sentinel_port(self):
        config = parse_sentinel_conf()
        assert "port" in config
        assert config["port"][0] == "26379"

    def test_sentinel_monitor_mymaster(self):
        config = parse_sentinel_conf()
        monitor_lines = [v for v in config.get("sentinel", []) if v.startswith("monitor")]
        assert len(monitor_lines) == 1
        monitor_line = monitor_lines[0]
        assert "mymaster" in monitor_line
        assert "redis" in monitor_line
        assert "6379" in monitor_line
        # quorum=2 (majority of 3 sentinels)
        assert monitor_line.endswith("2")

    def test_sentinel_auth_pass_templated(self):
        """Password should use placeholder for runtime substitution, not be hardcoded."""
        config = parse_sentinel_conf()
        auth_lines = [v for v in config.get("sentinel", []) if v.startswith("auth-pass")]
        assert len(auth_lines) == 1
        assert "REDIS_PASSWORD_PLACEHOLDER" in auth_lines[0]

    def test_sentinel_down_after_milliseconds(self):
        config = parse_sentinel_conf()
        down_lines = [v for v in config.get("sentinel", []) if v.startswith("down-after-milliseconds")]
        assert len(down_lines) == 1
        assert "5000" in down_lines[0]

    def test_sentinel_failover_timeout(self):
        config = parse_sentinel_conf()
        failover_lines = [v for v in config.get("sentinel", []) if v.startswith("failover-timeout")]
        assert len(failover_lines) == 1
        assert "10000" in failover_lines[0]

    def test_sentinel_parallel_syncs(self):
        config = parse_sentinel_conf()
        sync_lines = [v for v in config.get("sentinel", []) if v.startswith("parallel-syncs")]
        assert len(sync_lines) == 1
        assert "1" in sync_lines[0]


class TestDockerComposeSentinels:
    """Tests for sentinel services in docker-compose.yml."""

    @pytest.fixture
    def compose(self):
        return load_docker_compose()

    def test_sentinel_1_exists(self, compose):
        assert "redis-sentinel-1" in compose["services"]

    def test_sentinel_2_exists(self, compose):
        assert "redis-sentinel-2" in compose["services"]

    def test_sentinel_3_exists(self, compose):
        assert "redis-sentinel-3" in compose["services"]

    @pytest.mark.parametrize("service_name", [
        "redis-sentinel-1",
        "redis-sentinel-2",
        "redis-sentinel-3",
    ])
    def test_sentinel_uses_redis_image(self, compose, service_name):
        service = compose["services"][service_name]
        assert "redis" in service["image"]
        assert "alpine" in service["image"]

    @pytest.mark.parametrize("service_name", [
        "redis-sentinel-1",
        "redis-sentinel-2",
        "redis-sentinel-3",
    ])
    def test_sentinel_depends_on_redis(self, compose, service_name):
        service = compose["services"][service_name]
        assert "redis" in service["depends_on"]

    @pytest.mark.parametrize("service_name", [
        "redis-sentinel-1",
        "redis-sentinel-2",
        "redis-sentinel-3",
    ])
    def test_sentinel_has_healthcheck(self, compose, service_name):
        service = compose["services"][service_name]
        assert "healthcheck" in service
        hc = service["healthcheck"]
        assert "26379" in str(hc["test"])
        assert hc["interval"] == "10s"
        assert hc["timeout"] == "5s"

    @pytest.mark.parametrize("service_name", [
        "redis-sentinel-1",
        "redis-sentinel-2",
        "redis-sentinel-3",
    ])
    def test_sentinel_mounts_config(self, compose, service_name):
        service = compose["services"][service_name]
        volumes = service.get("volumes", [])
        sentinel_volume = [v for v in volumes if "sentinel.conf" in v]
        assert len(sentinel_volume) == 1
        assert ":ro" in sentinel_volume[0]

    @pytest.mark.parametrize("service_name", [
        "redis-sentinel-1",
        "redis-sentinel-2",
        "redis-sentinel-3",
    ])
    def test_sentinel_has_env_file(self, compose, service_name):
        service = compose["services"][service_name]
        assert "env_file" in service
        env_files = service["env_file"]
        assert "./envs/default.env" in env_files

    @pytest.mark.parametrize("service_name", [
        "redis-sentinel-1",
        "redis-sentinel-2",
        "redis-sentinel-3",
    ])
    def test_sentinel_command_substitutes_password(self, compose, service_name):
        service = compose["services"][service_name]
        command = service["command"]
        assert "REDIS_PASSWORD_PLACEHOLDER" in command
        assert "REDIS_PASSWORD" in command
        assert "redis-sentinel" in command

    @pytest.mark.parametrize("service_name", [
        "redis-sentinel-1",
        "redis-sentinel-2",
        "redis-sentinel-3",
    ])
    def test_sentinel_on_centry_network(self, compose, service_name):
        service = compose["services"][service_name]
        assert "centry" in service.get("networks", [])

    @pytest.mark.parametrize("service_name", [
        "redis-sentinel-1",
        "redis-sentinel-2",
        "redis-sentinel-3",
    ])
    def test_sentinel_restart_policy(self, compose, service_name):
        service = compose["services"][service_name]
        assert service.get("restart") == "unless-stopped"

    @pytest.mark.parametrize("service_name", [
        "redis-sentinel-1",
        "redis-sentinel-2",
        "redis-sentinel-3",
    ])
    def test_sentinel_logging_configured(self, compose, service_name):
        service = compose["services"][service_name]
        assert "logging" in service
        assert service["logging"]["driver"] == "json-file"


class TestEnvVarsSentinel:
    """Tests for sentinel-related environment variables."""

    @pytest.fixture
    def env(self):
        return parse_env_file(DEFAULT_ENV)

    def test_redis_sentinel_hosts_defined(self, env):
        assert "REDIS_SENTINEL_HOSTS" in env

    def test_redis_sentinel_hosts_has_three_entries(self, env):
        hosts = env["REDIS_SENTINEL_HOSTS"].split(",")
        assert len(hosts) == 3

    def test_redis_sentinel_hosts_correct_names(self, env):
        hosts = env["REDIS_SENTINEL_HOSTS"].split(",")
        assert "redis-sentinel-1:26379" in hosts
        assert "redis-sentinel-2:26379" in hosts
        assert "redis-sentinel-3:26379" in hosts

    def test_redis_sentinel_master_defined(self, env):
        assert "REDIS_SENTINEL_MASTER" in env
        assert env["REDIS_SENTINEL_MASTER"] == "mymaster"

    def test_redis_password_still_defined(self, env):
        assert "REDIS_PASSWORD" in env
        assert env["REDIS_PASSWORD"] != ""


class TestSentinelQuorumLogic:
    """Tests for Sentinel quorum correctness."""

    def test_quorum_is_majority(self):
        """With 3 sentinels, quorum should be 2 (majority)."""
        config = parse_sentinel_conf()
        monitor_lines = [v for v in config.get("sentinel", []) if v.startswith("monitor")]
        assert len(monitor_lines) == 1
        parts = monitor_lines[0].split()
        quorum = int(parts[-1])
        assert quorum == 2, f"Quorum should be 2 for 3 sentinels, got {quorum}"

    def test_failover_timeout_reasonable(self):
        """Failover timeout should be > down-after-milliseconds."""
        config = parse_sentinel_conf()
        down_lines = [v for v in config.get("sentinel", []) if v.startswith("down-after-milliseconds")]
        failover_lines = [v for v in config.get("sentinel", []) if v.startswith("failover-timeout")]

        down_ms = int(down_lines[0].split()[-1])
        failover_ms = int(failover_lines[0].split()[-1])
        assert failover_ms > down_ms

    def test_sentinel_monitors_correct_port(self):
        """Sentinel should monitor redis on port 6379."""
        config = parse_sentinel_conf()
        monitor_lines = [v for v in config.get("sentinel", []) if v.startswith("monitor")]
        assert "6379" in monitor_lines[0]

    def test_sentinel_monitors_correct_host(self):
        """Sentinel should monitor 'redis' hostname (docker service name)."""
        config = parse_sentinel_conf()
        monitor_lines = [v for v in config.get("sentinel", []) if v.startswith("monitor")]
        parts = monitor_lines[0].split()
        # parsed value is "monitor mymaster redis 6379 2" — host is at index 2
        assert parts[2] == "redis"


class TestSentinelDockerComposeConsistency:
    """Tests for consistency between sentinel config and docker-compose."""

    @pytest.fixture
    def compose(self):
        return load_docker_compose()

    @pytest.fixture
    def env(self):
        return parse_env_file(DEFAULT_ENV)

    def test_sentinel_count_matches_env(self, compose, env):
        """Number of sentinel containers should match REDIS_SENTINEL_HOSTS entries."""
        sentinel_services = [s for s in compose["services"] if s.startswith("redis-sentinel-")]
        hosts = env["REDIS_SENTINEL_HOSTS"].split(",")
        assert len(sentinel_services) == len(hosts)

    def test_sentinel_hostnames_match_services(self, compose, env):
        """Sentinel hostnames in env should match docker-compose service names."""
        sentinel_services = sorted([s for s in compose["services"] if s.startswith("redis-sentinel-")])
        hosts = sorted([h.split(":")[0] for h in env["REDIS_SENTINEL_HOSTS"].split(",")])
        assert sentinel_services == hosts

    def test_sentinel_port_in_env_matches_config(self, env):
        """Port in REDIS_SENTINEL_HOSTS should match sentinel.conf port."""
        config = parse_sentinel_conf()
        conf_port = config["port"][0]
        hosts = env["REDIS_SENTINEL_HOSTS"].split(",")
        for host in hosts:
            port = host.split(":")[1]
            assert port == conf_port

    def test_master_name_in_env_matches_config(self, env):
        """REDIS_SENTINEL_MASTER should match the monitored master in sentinel.conf."""
        config = parse_sentinel_conf()
        monitor_lines = [v for v in config.get("sentinel", []) if v.startswith("monitor")]
        parts = monitor_lines[0].split()
        conf_master = parts[0].split()[-1] if len(parts[0].split()) > 1 else parts[0]
        # The format after "monitor" is: mymaster redis 6379 2
        # So parts[0] is "monitor mymaster redis 6379 2" — wait no, we already split on "sentinel"
        # Re-parse: full line is "sentinel monitor mymaster redis 6379 2"
        # After our parse, the value is "monitor mymaster redis 6379 2"
        monitor_value = monitor_lines[0]
        monitor_parts = monitor_value.split()
        master_name = monitor_parts[1]
        assert env["REDIS_SENTINEL_MASTER"] == master_name

    def test_redis_master_depends_on_nothing(self, compose):
        """The redis master should not depend on sentinels (sentinels depend on it)."""
        redis_service = compose["services"]["redis"]
        deps = redis_service.get("depends_on", [])
        for dep in deps if isinstance(deps, list) else []:
            assert "sentinel" not in dep
