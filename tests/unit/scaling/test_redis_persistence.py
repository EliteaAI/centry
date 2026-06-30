"""
Tests for Task 5.6: Redis persistence (AOF + RDB) configuration.

Validates that:
- redis.conf exists and contains correct persistence settings
- AOF is enabled with everysec fsync
- RDB snapshots are configured with multiple save rules
- docker-compose mounts the config file and data volume correctly
- Settings are consistent with production requirements
"""

import os
import re

import yaml
import pytest


CENTRY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
REDIS_CONF = os.path.join(CENTRY_ROOT, "redis", "redis.conf")
DOCKER_COMPOSE = os.path.join(CENTRY_ROOT, "docker-compose.yml")


def parse_redis_conf(filepath):
    """Parse redis.conf into a dict of key=value pairs (last wins for duplicates)."""
    config = {}
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                key, value = parts
                config[key] = value
            elif len(parts) == 1:
                config[parts[0]] = ""
    return config


def parse_redis_conf_multi(filepath):
    """Parse redis.conf returning all values for each key (supports save with multiple rules)."""
    config = {}
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) >= 1:
                key = parts[0]
                value = parts[1] if len(parts) == 2 else ""
                config.setdefault(key, []).append(value)
    return config


class TestRedisConfExists:
    """Verify redis.conf file exists and is parseable."""

    def test_config_file_exists(self):
        assert os.path.exists(REDIS_CONF), f"redis.conf not found at {REDIS_CONF}"

    def test_config_file_is_readable(self):
        with open(REDIS_CONF, "r") as f:
            content = f.read()
        assert len(content) > 0, "redis.conf is empty"

    def test_config_file_parseable(self):
        config = parse_redis_conf(REDIS_CONF)
        assert len(config) > 0, "No directives found in redis.conf"

    def test_no_requirepass_hardcoded(self):
        """requirepass must NOT be in the config file — it's passed via CLI from env."""
        config = parse_redis_conf(REDIS_CONF)
        assert "requirepass" not in config, (
            "requirepass should not be hardcoded in redis.conf — use --requirepass via env"
        )


class TestAOFConfiguration:
    """Verify AOF (Append Only File) is correctly configured."""

    def test_appendonly_enabled(self):
        config = parse_redis_conf(REDIS_CONF)
        assert "appendonly" in config, "appendonly not set in redis.conf"
        assert config["appendonly"] == "yes", (
            f"appendonly should be 'yes', got '{config['appendonly']}'"
        )

    def test_appendfsync_everysec(self):
        config = parse_redis_conf(REDIS_CONF)
        assert "appendfsync" in config, "appendfsync not set in redis.conf"
        assert config["appendfsync"] == "everysec", (
            f"appendfsync should be 'everysec', got '{config['appendfsync']}'"
        )

    def test_aof_use_rdb_preamble_enabled(self):
        config = parse_redis_conf(REDIS_CONF)
        assert "aof-use-rdb-preamble" in config, "aof-use-rdb-preamble not set"
        assert config["aof-use-rdb-preamble"] == "yes", (
            f"aof-use-rdb-preamble should be 'yes', got '{config['aof-use-rdb-preamble']}'"
        )

    def test_aof_load_truncated_enabled(self):
        config = parse_redis_conf(REDIS_CONF)
        assert "aof-load-truncated" in config, "aof-load-truncated not set"
        assert config["aof-load-truncated"] == "yes", (
            f"aof-load-truncated should be 'yes', got '{config['aof-load-truncated']}'"
        )

    def test_auto_aof_rewrite_percentage(self):
        config = parse_redis_conf(REDIS_CONF)
        assert "auto-aof-rewrite-percentage" in config, "auto-aof-rewrite-percentage not set"
        pct = int(config["auto-aof-rewrite-percentage"])
        assert 50 <= pct <= 200, f"auto-aof-rewrite-percentage should be 50-200, got {pct}"

    def test_auto_aof_rewrite_min_size(self):
        config = parse_redis_conf(REDIS_CONF)
        assert "auto-aof-rewrite-min-size" in config, "auto-aof-rewrite-min-size not set"
        value = config["auto-aof-rewrite-min-size"]
        assert "mb" in value.lower() or "gb" in value.lower(), (
            f"auto-aof-rewrite-min-size should specify size unit, got '{value}'"
        )

    def test_no_appendfsync_on_rewrite(self):
        config = parse_redis_conf(REDIS_CONF)
        assert "no-appendfsync-on-rewrite" in config, "no-appendfsync-on-rewrite not set"
        assert config["no-appendfsync-on-rewrite"] in ("yes", "no"), (
            f"no-appendfsync-on-rewrite should be yes or no, got '{config['no-appendfsync-on-rewrite']}'"
        )


class TestRDBConfiguration:
    """Verify RDB snapshot configuration."""

    def test_save_rules_exist(self):
        config = parse_redis_conf_multi(REDIS_CONF)
        assert "save" in config, "No save rules in redis.conf"
        assert len(config["save"]) >= 2, (
            f"Expected at least 2 save rules, got {len(config['save'])}"
        )

    def test_save_rule_900_1(self):
        """RDB every 15min if >=1 key changed."""
        config = parse_redis_conf_multi(REDIS_CONF)
        save_rules = config.get("save", [])
        assert "900 1" in save_rules, (
            f"Expected 'save 900 1' rule, got rules: {save_rules}"
        )

    def test_save_rule_300_10(self):
        """RDB every 5min if >=10 keys changed."""
        config = parse_redis_conf_multi(REDIS_CONF)
        save_rules = config.get("save", [])
        assert "300 10" in save_rules, (
            f"Expected 'save 300 10' rule, got rules: {save_rules}"
        )

    def test_save_rule_60_10000(self):
        """RDB every 1min if >=10000 keys changed (high write load)."""
        config = parse_redis_conf_multi(REDIS_CONF)
        save_rules = config.get("save", [])
        assert "60 10000" in save_rules, (
            f"Expected 'save 60 10000' rule, got rules: {save_rules}"
        )

    def test_rdb_compression_enabled(self):
        config = parse_redis_conf(REDIS_CONF)
        assert "rdbcompression" in config, "rdbcompression not set"
        assert config["rdbcompression"] == "yes", (
            f"rdbcompression should be 'yes', got '{config['rdbcompression']}'"
        )

    def test_rdb_checksum_enabled(self):
        config = parse_redis_conf(REDIS_CONF)
        assert "rdbchecksum" in config, "rdbchecksum not set"
        assert config["rdbchecksum"] == "yes", (
            f"rdbchecksum should be 'yes', got '{config['rdbchecksum']}'"
        )

    def test_dbfilename_set(self):
        config = parse_redis_conf(REDIS_CONF)
        assert "dbfilename" in config, "dbfilename not set"
        assert config["dbfilename"] == "dump.rdb", (
            f"dbfilename should be 'dump.rdb', got '{config['dbfilename']}'"
        )

    def test_dir_set_to_data(self):
        config = parse_redis_conf(REDIS_CONF)
        assert "dir" in config, "dir not set"
        assert config["dir"] == "/data", (
            f"dir should be '/data', got '{config['dir']}'"
        )

    def test_stop_writes_on_bgsave_error(self):
        config = parse_redis_conf(REDIS_CONF)
        assert "stop-writes-on-bgsave-error" in config, "stop-writes-on-bgsave-error not set"
        assert config["stop-writes-on-bgsave-error"] == "yes", (
            f"stop-writes-on-bgsave-error should be 'yes', got '{config['stop-writes-on-bgsave-error']}'"
        )


class TestDockerComposeMounts:
    """Verify docker-compose correctly mounts redis.conf and data volume."""

    @pytest.fixture
    def compose_config(self):
        with open(DOCKER_COMPOSE, "r") as f:
            return yaml.safe_load(f)

    def test_redis_service_exists(self, compose_config):
        assert "redis" in compose_config["services"], "redis service not in docker-compose"

    def test_redis_conf_mounted(self, compose_config):
        redis = compose_config["services"]["redis"]
        volumes = redis.get("volumes", [])
        conf_mounted = any("redis.conf" in v and "/etc/redis/redis.conf" in v for v in volumes)
        assert conf_mounted, (
            f"redis.conf not mounted to /etc/redis/redis.conf. Volumes: {volumes}"
        )

    def test_redis_conf_mounted_readonly(self, compose_config):
        redis = compose_config["services"]["redis"]
        volumes = redis.get("volumes", [])
        ro_mount = any("redis.conf" in v and ":ro" in v for v in volumes)
        assert ro_mount, "redis.conf should be mounted read-only (:ro)"

    def test_data_volume_mounted(self, compose_config):
        redis = compose_config["services"]["redis"]
        volumes = redis.get("volumes", [])
        data_mounted = any("redis-data" in v and "/data" in v for v in volumes)
        assert data_mounted, (
            f"redis-data volume not mounted at /data. Volumes: {volumes}"
        )

    def test_data_volume_defined(self, compose_config):
        volumes = compose_config.get("volumes", {})
        assert "redis-data" in volumes, "redis-data volume not defined in top-level volumes"

    def test_command_references_config_file(self, compose_config):
        redis = compose_config["services"]["redis"]
        command = redis.get("command", "")
        assert "/etc/redis/redis.conf" in command, (
            f"redis command should reference /etc/redis/redis.conf, got: {command}"
        )

    def test_command_passes_requirepass_from_env(self, compose_config):
        redis = compose_config["services"]["redis"]
        command = redis.get("command", "")
        assert "requirepass" in command.lower() or "REDIS_PASSWORD" in command, (
            "redis command should pass --requirepass from environment variable"
        )

    def test_command_does_not_override_persistence(self, compose_config):
        """Command should NOT have --save or --appendonly since redis.conf handles those."""
        redis = compose_config["services"]["redis"]
        command = redis.get("command", "")
        assert "--save" not in command, (
            "Command should not override save rules — they're in redis.conf"
        )
        assert "--appendonly" not in command, (
            "Command should not override appendonly — it's in redis.conf"
        )


class TestConfigConsistency:
    """Verify config values are consistent across persistence mechanisms."""

    def test_dir_matches_volume_mount(self):
        """The dir in redis.conf must match where the volume is mounted."""
        config = parse_redis_conf(REDIS_CONF)
        with open(DOCKER_COMPOSE, "r") as f:
            compose = yaml.safe_load(f)

        redis_dir = config.get("dir", "")
        volumes = compose["services"]["redis"].get("volumes", [])
        data_mount = [v for v in volumes if "redis-data" in v]
        assert len(data_mount) == 1, f"Expected exactly one redis-data mount, got {data_mount}"
        mount_target = data_mount[0].split(":")[1] if ":" in data_mount[0] else ""
        assert redis_dir == mount_target, (
            f"redis.conf dir='{redis_dir}' doesn't match mount target '{mount_target}'"
        )

    def test_aof_and_rdb_both_enabled(self):
        """Both AOF and RDB should be enabled for maximum durability."""
        config = parse_redis_conf(REDIS_CONF)
        multi = parse_redis_conf_multi(REDIS_CONF)
        assert config.get("appendonly") == "yes", "AOF not enabled"
        assert "save" in multi and len(multi["save"]) > 0, "RDB save rules not configured"

    def test_bind_allows_container_access(self):
        """bind must allow connections from other containers (0.0.0.0 or not set)."""
        config = parse_redis_conf(REDIS_CONF)
        if "bind" in config:
            assert "0.0.0.0" in config["bind"], (
                f"bind should include 0.0.0.0 for container networking, got '{config['bind']}'"
            )

    def test_port_is_6379(self):
        config = parse_redis_conf(REDIS_CONF)
        assert "port" in config, "port not set in redis.conf"
        assert config["port"] == "6379", f"port should be 6379, got {config['port']}"

    def test_no_maxmemory_hardcoded(self):
        """maxmemory should NOT be set — container limits handle this."""
        config = parse_redis_conf(REDIS_CONF)
        assert "maxmemory" not in config, (
            "maxmemory should not be in redis.conf — use container memory limits"
        )
