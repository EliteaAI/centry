"""
Tests for Task 5.4: PostgreSQL max_connections configuration.

Validates that:
- postgresql.conf override file exists and is valid
- max_connections is set to 200
- shared_buffers is proportional to connections (256MB)
- docker-compose mounts the config file correctly
- Config values are consistent with PgBouncer limits
"""

import os
import re

import yaml
import pytest


CENTRY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
POSTGRES_CONF = os.path.join(CENTRY_ROOT, "postgres", "postgresql.conf")
DOCKER_COMPOSE = os.path.join(CENTRY_ROOT, "docker-compose.yml")
PGBOUNCER_INI = os.path.join(CENTRY_ROOT, "pgbouncer", "pgbouncer.ini")


def parse_pg_conf(filepath):
    """Parse postgresql.conf into a dict of key=value pairs."""
    config = {}
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()
    return config


def parse_ini_value(filepath, section, key):
    """Parse a value from a .ini file."""
    current_section = None
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                current_section = line[1:-1]
            elif "=" in line and current_section == section:
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip()
    return None


class TestPostgresConfExists:
    """Verify postgresql.conf override file exists and is parseable."""

    def test_config_file_exists(self):
        assert os.path.exists(POSTGRES_CONF), (
            f"postgresql.conf not found at {POSTGRES_CONF}"
        )

    def test_config_file_is_readable(self):
        with open(POSTGRES_CONF, "r") as f:
            content = f.read()
        assert len(content) > 0, "postgresql.conf is empty"

    def test_config_file_parseable(self):
        config = parse_pg_conf(POSTGRES_CONF)
        assert len(config) > 0, "No key=value pairs found in postgresql.conf"


class TestMaxConnections:
    """Verify max_connections is correctly configured."""

    def test_max_connections_set_to_200(self):
        config = parse_pg_conf(POSTGRES_CONF)
        assert "max_connections" in config, "max_connections not set in postgresql.conf"
        assert config["max_connections"] == "200", (
            f"max_connections should be 200, got {config['max_connections']}"
        )

    def test_max_connections_exceeds_pgbouncer_max_client_conn(self):
        """PgBouncer max_client_conn should not exceed PostgreSQL max_connections."""
        config = parse_pg_conf(POSTGRES_CONF)
        pg_max = int(config["max_connections"])

        if os.path.exists(PGBOUNCER_INI):
            pgb_max_db = parse_ini_value(PGBOUNCER_INI, "pgbouncer", "max_db_connections")
            if pgb_max_db:
                assert int(pgb_max_db) <= pg_max, (
                    f"PgBouncer max_db_connections ({pgb_max_db}) exceeds PostgreSQL max_connections ({pg_max})"
                )

    def test_max_connections_supports_all_services(self):
        """Verify 200 connections supports steady state + admin headroom."""
        config = parse_pg_conf(POSTGRES_CONF)
        pg_max = int(config["max_connections"])
        # From task 1.8: steady state = 95, burst = 150, admin headroom = 50
        assert pg_max >= 150, (
            f"max_connections ({pg_max}) too low for burst connections (150)"
        )
        assert pg_max - 150 >= 20, (
            f"Only {pg_max - 150} connections left for admin tasks (need >= 20)"
        )


class TestSharedBuffers:
    """Verify shared_buffers is proportional to connections."""

    def test_shared_buffers_set(self):
        config = parse_pg_conf(POSTGRES_CONF)
        assert "shared_buffers" in config, "shared_buffers not set in postgresql.conf"

    def test_shared_buffers_is_256mb(self):
        config = parse_pg_conf(POSTGRES_CONF)
        assert config["shared_buffers"] == "256MB", (
            f"shared_buffers should be 256MB, got {config['shared_buffers']}"
        )


class TestAdditionalSettings:
    """Verify other performance-related settings."""

    def test_work_mem_set(self):
        config = parse_pg_conf(POSTGRES_CONF)
        assert "work_mem" in config

    def test_maintenance_work_mem_set(self):
        config = parse_pg_conf(POSTGRES_CONF)
        assert "maintenance_work_mem" in config

    def test_effective_cache_size_set(self):
        config = parse_pg_conf(POSTGRES_CONF)
        assert "effective_cache_size" in config

    def test_wal_buffers_set(self):
        config = parse_pg_conf(POSTGRES_CONF)
        assert "wal_buffers" in config

    def test_logging_slow_queries(self):
        config = parse_pg_conf(POSTGRES_CONF)
        assert "log_min_duration_statement" in config
        assert int(config["log_min_duration_statement"]) >= 500, (
            "log_min_duration_statement should be >= 500ms to avoid log noise"
        )

    def test_log_connections_enabled(self):
        config = parse_pg_conf(POSTGRES_CONF)
        assert config.get("log_connections") == "on"

    def test_log_disconnections_enabled(self):
        config = parse_pg_conf(POSTGRES_CONF)
        assert config.get("log_disconnections") == "on"


class TestDockerComposeMount:
    """Verify docker-compose.yml mounts postgresql.conf correctly."""

    def test_docker_compose_has_postgres_service(self):
        with open(DOCKER_COMPOSE, "r") as f:
            compose = yaml.safe_load(f)
        assert "postgres" in compose["services"]

    def test_docker_compose_mounts_config(self):
        with open(DOCKER_COMPOSE, "r") as f:
            compose = yaml.safe_load(f)
        postgres = compose["services"]["postgres"]
        volumes = postgres.get("volumes", [])
        config_mount = None
        for v in volumes:
            if "postgresql.conf" in str(v):
                config_mount = v
                break
        assert config_mount is not None, (
            "postgresql.conf not mounted in docker-compose postgres service"
        )
        assert ":ro" in config_mount, "postgresql.conf should be mounted read-only"

    def test_docker_compose_command_loads_config(self):
        with open(DOCKER_COMPOSE, "r") as f:
            compose = yaml.safe_load(f)
        postgres = compose["services"]["postgres"]
        command = postgres.get("command", "")
        assert "config_file" in command, (
            "postgres service command should reference config_file"
        )
        assert "/etc/postgresql/postgresql.conf" in command, (
            "postgres command should point to /etc/postgresql/postgresql.conf"
        )

    def test_mount_source_path_matches_real_file(self):
        """The mount source in docker-compose should correspond to our config file."""
        with open(DOCKER_COMPOSE, "r") as f:
            compose = yaml.safe_load(f)
        postgres = compose["services"]["postgres"]
        volumes = postgres.get("volumes", [])
        for v in volumes:
            if "postgresql.conf" in str(v):
                source = v.split(":")[0]
                # Resolve relative to centry/
                resolved = os.path.normpath(os.path.join(CENTRY_ROOT, source.lstrip("./")))
                assert os.path.exists(resolved), (
                    f"Mount source {source} does not exist at {resolved}"
                )


class TestConsistencyWithPgBouncer:
    """Verify PostgreSQL config is consistent with PgBouncer settings."""

    @pytest.fixture
    def pgbouncer_config(self):
        if not os.path.exists(PGBOUNCER_INI):
            pytest.skip("PgBouncer config not available")
        config = {}
        current_section = None
        with open(PGBOUNCER_INI, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("[") and line.endswith("]"):
                    current_section = line[1:-1]
                elif "=" in line and current_section:
                    k, v = line.split("=", 1)
                    config[f"{current_section}.{k.strip()}"] = v.strip()
        return config

    def test_pg_max_exceeds_pgbouncer_max_db_connections(self, pgbouncer_config):
        """PostgreSQL max_connections must be >= PgBouncer max_db_connections."""
        pg_config = parse_pg_conf(POSTGRES_CONF)
        pg_max = int(pg_config["max_connections"])
        pgb_max_db = int(pgbouncer_config.get("pgbouncer.max_db_connections", "50"))
        assert pg_max >= pgb_max_db, (
            f"PG max_connections ({pg_max}) < PgBouncer max_db_connections ({pgb_max_db})"
        )

    def test_pgbouncer_pool_mode_supports_advisory_locks(self, pgbouncer_config):
        """Session pooling mode required for advisory locks to work."""
        pool_mode = pgbouncer_config.get("pgbouncer.pool_mode", "session")
        assert pool_mode == "session", (
            f"PgBouncer pool_mode must be 'session' for advisory locks, got '{pool_mode}'"
        )
