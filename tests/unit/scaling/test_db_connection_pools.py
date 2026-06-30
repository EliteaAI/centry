"""
Tests for Task 1.8: Database connection pool configuration.

Validates that:
- Pool sizes are correctly configured for multi-replica scaling
- Total connections remain under PostgreSQL max_connections limit
- pool_pre_ping is enabled for connection health checks
- Configuration is consistent between local dev and staging
"""

import os
import yaml
import pytest


CENTRY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
ARGOCD_STAGING = os.path.join(
    os.path.dirname(os.path.dirname(CENTRY_ROOT)),
    "kharkevich", "argocd-public", "elitea-platform", "values", "staging"
)

POSTGRES_MAX_CONNECTIONS = 200

EXPECTED_POOLS = {
    "pylon_auth": {"pool_size": 10, "max_overflow": 5},
    "pylon_main": {"pool_size": 15, "max_overflow": 10},
    "pylon_indexer": {"pool_size": 10, "max_overflow": 5},
}

REPLICA_COUNTS = {
    "pylon_auth": 2,
    "pylon_main": 3,
    "pylon_indexer": 3,
}


def load_yaml(filepath):
    with open(filepath, "r") as f:
        return yaml.safe_load(f)


class TestLocalDevPoolConfig:
    """Tests for local development (docker-compose) pool configuration."""

    def test_pylon_main_pool_size(self):
        config = load_yaml(os.path.join(CENTRY_ROOT, "pylon_main", "configs", "shared.yml"))
        engine_opts = config["settings"]["database_engine_options"]
        assert engine_opts["pool_size"] == EXPECTED_POOLS["pylon_main"]["pool_size"]

    def test_pylon_main_max_overflow(self):
        config = load_yaml(os.path.join(CENTRY_ROOT, "pylon_main", "configs", "shared.yml"))
        engine_opts = config["settings"]["database_engine_options"]
        assert engine_opts["max_overflow"] == EXPECTED_POOLS["pylon_main"]["max_overflow"]

    def test_pylon_main_pool_pre_ping(self):
        config = load_yaml(os.path.join(CENTRY_ROOT, "pylon_main", "configs", "shared.yml"))
        engine_opts = config["settings"]["database_engine_options"]
        assert engine_opts["pool_pre_ping"] is True

    def test_pylon_main_pool_recycle(self):
        config = load_yaml(os.path.join(CENTRY_ROOT, "pylon_main", "configs", "shared.yml"))
        engine_opts = config["settings"]["database_engine_options"]
        assert engine_opts["pool_recycle"] == 3600

    def test_pylon_auth_pool_size(self):
        config = load_yaml(os.path.join(CENTRY_ROOT, "pylon_auth", "configs", "auth_core.yml"))
        db_opts = config["db_options"]
        assert db_opts["pool_size"] == EXPECTED_POOLS["pylon_auth"]["pool_size"]

    def test_pylon_auth_max_overflow(self):
        config = load_yaml(os.path.join(CENTRY_ROOT, "pylon_auth", "configs", "auth_core.yml"))
        db_opts = config["db_options"]
        assert db_opts["max_overflow"] == EXPECTED_POOLS["pylon_auth"]["max_overflow"]

    def test_pylon_auth_pool_pre_ping(self):
        config = load_yaml(os.path.join(CENTRY_ROOT, "pylon_auth", "configs", "auth_core.yml"))
        db_opts = config["db_options"]
        assert db_opts["pool_pre_ping"] is True

    def test_pylon_auth_pool_recycle(self):
        config = load_yaml(os.path.join(CENTRY_ROOT, "pylon_auth", "configs", "auth_core.yml"))
        db_opts = config["db_options"]
        assert db_opts["pool_recycle"] == 3600


class TestConnectionMath:
    """Validates total connection counts remain within PostgreSQL limits."""

    def test_steady_state_connections_under_limit(self):
        """pool_size * replicas summed across all services < max_connections"""
        total = sum(
            EXPECTED_POOLS[svc]["pool_size"] * REPLICA_COUNTS[svc]
            for svc in EXPECTED_POOLS
        )
        assert total < POSTGRES_MAX_CONNECTIONS, (
            f"Steady-state connections ({total}) exceed max_connections ({POSTGRES_MAX_CONNECTIONS})"
        )

    def test_max_burst_connections_under_limit(self):
        """(pool_size + max_overflow) * replicas summed across all services < max_connections"""
        total = sum(
            (EXPECTED_POOLS[svc]["pool_size"] + EXPECTED_POOLS[svc]["max_overflow"]) * REPLICA_COUNTS[svc]
            for svc in EXPECTED_POOLS
        )
        assert total < POSTGRES_MAX_CONNECTIONS, (
            f"Max burst connections ({total}) exceed max_connections ({POSTGRES_MAX_CONNECTIONS})"
        )

    def test_steady_state_connections_exact_value(self):
        """Verify the documented math: 2×10 + 3×15 + 3×10 = 95"""
        total = (
            REPLICA_COUNTS["pylon_auth"] * EXPECTED_POOLS["pylon_auth"]["pool_size"]
            + REPLICA_COUNTS["pylon_main"] * EXPECTED_POOLS["pylon_main"]["pool_size"]
            + REPLICA_COUNTS["pylon_indexer"] * EXPECTED_POOLS["pylon_indexer"]["pool_size"]
        )
        assert total == 95

    def test_max_burst_connections_exact_value(self):
        """Verify burst math: 2×15 + 3×25 + 3×15 = 150"""
        total = (
            REPLICA_COUNTS["pylon_auth"] * (EXPECTED_POOLS["pylon_auth"]["pool_size"] + EXPECTED_POOLS["pylon_auth"]["max_overflow"])
            + REPLICA_COUNTS["pylon_main"] * (EXPECTED_POOLS["pylon_main"]["pool_size"] + EXPECTED_POOLS["pylon_main"]["max_overflow"])
            + REPLICA_COUNTS["pylon_indexer"] * (EXPECTED_POOLS["pylon_indexer"]["pool_size"] + EXPECTED_POOLS["pylon_indexer"]["max_overflow"])
        )
        assert total == 150

    def test_headroom_for_admin_connections(self):
        """Ensure at least 20 connections reserved for admin/maintenance (pg_dump, migrations)"""
        total_max = sum(
            (EXPECTED_POOLS[svc]["pool_size"] + EXPECTED_POOLS[svc]["max_overflow"]) * REPLICA_COUNTS[svc]
            for svc in EXPECTED_POOLS
        )
        headroom = POSTGRES_MAX_CONNECTIONS - total_max
        assert headroom >= 20, (
            f"Only {headroom} connections available for admin tasks (need >= 20)"
        )


class TestStagingPoolConfig:
    """Tests for staging (ArgoCD) pool configuration consistency."""

    @pytest.fixture
    def staging_main_config(self):
        filepath = os.path.join(ARGOCD_STAGING, "pylon-main.yaml")
        if not os.path.exists(filepath):
            pytest.skip("Staging config not available")
        return load_yaml(filepath)

    @pytest.fixture
    def staging_auth_config(self):
        filepath = os.path.join(ARGOCD_STAGING, "pylon-auth.yaml")
        if not os.path.exists(filepath):
            pytest.skip("Staging config not available")
        return load_yaml(filepath)

    @pytest.fixture
    def staging_indexer_config(self):
        filepath = os.path.join(ARGOCD_STAGING, "pylon-indexer.yaml")
        if not os.path.exists(filepath):
            pytest.skip("Staging config not available")
        return load_yaml(filepath)

    def test_staging_main_pool_size(self, staging_main_config):
        shared_yml = yaml.safe_load(staging_main_config["config"]["files"]["shared.yml"])
        engine_opts = shared_yml["settings"]["database_engine_options"]
        assert engine_opts["pool_size"] == EXPECTED_POOLS["pylon_main"]["pool_size"]

    def test_staging_main_max_overflow(self, staging_main_config):
        shared_yml = yaml.safe_load(staging_main_config["config"]["files"]["shared.yml"])
        engine_opts = shared_yml["settings"]["database_engine_options"]
        assert engine_opts["max_overflow"] == EXPECTED_POOLS["pylon_main"]["max_overflow"]

    def test_staging_main_pool_pre_ping(self, staging_main_config):
        shared_yml = yaml.safe_load(staging_main_config["config"]["files"]["shared.yml"])
        engine_opts = shared_yml["settings"]["database_engine_options"]
        assert engine_opts["pool_pre_ping"] is True

    def test_staging_auth_pool_size(self, staging_auth_config):
        auth_yml = yaml.safe_load(staging_auth_config["config"]["files"]["auth_core.yml"])
        db_opts = auth_yml["db_options"]
        assert db_opts["pool_size"] == EXPECTED_POOLS["pylon_auth"]["pool_size"]

    def test_staging_auth_max_overflow(self, staging_auth_config):
        auth_yml = yaml.safe_load(staging_auth_config["config"]["files"]["auth_core.yml"])
        db_opts = auth_yml["db_options"]
        assert db_opts["max_overflow"] == EXPECTED_POOLS["pylon_auth"]["max_overflow"]

    def test_staging_auth_pool_pre_ping(self, staging_auth_config):
        auth_yml = yaml.safe_load(staging_auth_config["config"]["files"]["auth_core.yml"])
        db_opts = auth_yml["db_options"]
        assert db_opts["pool_pre_ping"] is True

    def test_staging_indexer_pool_size(self, staging_indexer_config):
        shared_yml = yaml.safe_load(staging_indexer_config["config"]["files"]["shared.yml"])
        engine_opts = shared_yml["settings"]["database_engine_options"]
        assert engine_opts["pool_size"] == EXPECTED_POOLS["pylon_indexer"]["pool_size"]

    def test_staging_indexer_max_overflow(self, staging_indexer_config):
        shared_yml = yaml.safe_load(staging_indexer_config["config"]["files"]["shared.yml"])
        engine_opts = shared_yml["settings"]["database_engine_options"]
        assert engine_opts["max_overflow"] == EXPECTED_POOLS["pylon_indexer"]["max_overflow"]

    def test_staging_indexer_pool_pre_ping(self, staging_indexer_config):
        shared_yml = yaml.safe_load(staging_indexer_config["config"]["files"]["shared.yml"])
        engine_opts = shared_yml["settings"]["database_engine_options"]
        assert engine_opts["pool_pre_ping"] is True

    def test_staging_replica_counts(self, staging_main_config, staging_auth_config, staging_indexer_config):
        assert staging_main_config["replicaCount"] == REPLICA_COUNTS["pylon_main"]
        assert staging_auth_config["replicaCount"] == REPLICA_COUNTS["pylon_auth"]
        assert staging_indexer_config["replicaCount"] == REPLICA_COUNTS["pylon_indexer"]


class TestPoolConfigConsistency:
    """Ensures local dev and staging configs are consistent."""

    @pytest.fixture
    def staging_main_config(self):
        filepath = os.path.join(ARGOCD_STAGING, "pylon-main.yaml")
        if not os.path.exists(filepath):
            pytest.skip("Staging config not available")
        return load_yaml(filepath)

    @pytest.fixture
    def staging_auth_config(self):
        filepath = os.path.join(ARGOCD_STAGING, "pylon-auth.yaml")
        if not os.path.exists(filepath):
            pytest.skip("Staging config not available")
        return load_yaml(filepath)

    def test_main_pool_matches_staging(self, staging_main_config):
        local_config = load_yaml(os.path.join(CENTRY_ROOT, "pylon_main", "configs", "shared.yml"))
        local_opts = local_config["settings"]["database_engine_options"]

        staging_shared = yaml.safe_load(staging_main_config["config"]["files"]["shared.yml"])
        staging_opts = staging_shared["settings"]["database_engine_options"]

        assert local_opts["pool_size"] == staging_opts["pool_size"]
        assert local_opts["max_overflow"] == staging_opts["max_overflow"]
        assert local_opts["pool_pre_ping"] == staging_opts["pool_pre_ping"]

    def test_auth_pool_matches_staging(self, staging_auth_config):
        local_config = load_yaml(os.path.join(CENTRY_ROOT, "pylon_auth", "configs", "auth_core.yml"))
        local_opts = local_config["db_options"]

        staging_auth = yaml.safe_load(staging_auth_config["config"]["files"]["auth_core.yml"])
        staging_opts = staging_auth["db_options"]

        assert local_opts["pool_size"] == staging_opts["pool_size"]
        assert local_opts["max_overflow"] == staging_opts["max_overflow"]
        assert local_opts["pool_pre_ping"] == staging_opts["pool_pre_ping"]


class TestConfigDefaults:
    """Tests that the Config class defaults are overridden by explicit config."""

    def test_config_class_default_pool_size(self):
        """Config class has default pool_size=25, we override to 15 via shared.yml"""
        config = load_yaml(os.path.join(CENTRY_ROOT, "pylon_main", "configs", "shared.yml"))
        engine_opts = config["settings"]["database_engine_options"]
        assert engine_opts["pool_size"] != 25, "Should override the Config class default of 25"
        assert engine_opts["pool_size"] == 15

    def test_config_class_default_max_overflow(self):
        """Config class has default max_overflow=25, we override to 10 via shared.yml"""
        config = load_yaml(os.path.join(CENTRY_ROOT, "pylon_main", "configs", "shared.yml"))
        engine_opts = config["settings"]["database_engine_options"]
        assert engine_opts["max_overflow"] != 25, "Should override the Config class default of 25"
        assert engine_opts["max_overflow"] == 10
