"""
Tests for Task 5.14: Deploy Prometheus + Grafana.

Validates that:
- Prometheus configuration is valid with correct scrape targets
- Grafana datasource provisioning is correctly configured
- Grafana dashboard provisioning is correctly configured
- Dashboard JSON is valid with expected panels
- Docker-compose services are defined with correct images and mounts
- All volume mounts reference existing config files
- Network configuration is consistent
"""

import json
import os

import pytest
import yaml


CENTRY_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)


def load_yaml(filepath):
    with open(filepath, "r") as f:
        return yaml.safe_load(f)


def load_json(filepath):
    with open(filepath, "r") as f:
        return json.load(f)


class TestPrometheusConfig:
    """Tests for Prometheus configuration."""

    @pytest.fixture
    def config(self):
        return load_yaml(os.path.join(CENTRY_ROOT, "prometheus", "prometheus.yml"))

    def test_config_file_exists(self):
        path = os.path.join(CENTRY_ROOT, "prometheus", "prometheus.yml")
        assert os.path.isfile(path)

    def test_global_scrape_interval(self, config):
        assert config["global"]["scrape_interval"] == "15s"

    def test_global_evaluation_interval(self, config):
        assert config["global"]["evaluation_interval"] == "15s"

    def test_scrape_configs_present(self, config):
        assert "scrape_configs" in config
        assert len(config["scrape_configs"]) >= 3

    def test_pylon_main_job(self, config):
        jobs = {j["job_name"]: j for j in config["scrape_configs"]}
        assert "pylon_main" in jobs
        job = jobs["pylon_main"]
        assert job["metrics_path"] == "/metrics"
        targets = job["static_configs"][0]["targets"]
        assert "pylon_main:8080" in targets

    def test_pylon_main_label(self, config):
        jobs = {j["job_name"]: j for j in config["scrape_configs"]}
        labels = jobs["pylon_main"]["static_configs"][0]["labels"]
        assert labels["service"] == "pylon-main"

    def test_pgbouncer_job(self, config):
        jobs = {j["job_name"]: j for j in config["scrape_configs"]}
        assert "pgbouncer" in jobs
        job = jobs["pgbouncer"]
        targets = job["static_configs"][0]["targets"]
        assert "pgbouncer-exporter:9127" in targets

    def test_redis_job(self, config):
        jobs = {j["job_name"]: j for j in config["scrape_configs"]}
        assert "redis" in jobs
        job = jobs["redis"]
        targets = job["static_configs"][0]["targets"]
        assert "redis-exporter:9121" in targets

    def test_all_jobs_have_metrics_path(self, config):
        for job in config["scrape_configs"]:
            assert "metrics_path" in job, f"Job {job['job_name']} missing metrics_path"

    def test_all_jobs_have_labels(self, config):
        for job in config["scrape_configs"]:
            labels = job["static_configs"][0].get("labels", {})
            assert "service" in labels, f"Job {job['job_name']} missing service label"


class TestGrafanaDatasource:
    """Tests for Grafana datasource provisioning."""

    @pytest.fixture
    def config(self):
        return load_yaml(
            os.path.join(CENTRY_ROOT, "grafana", "provisioning", "datasources", "prometheus.yaml")
        )

    def test_datasource_file_exists(self):
        path = os.path.join(
            CENTRY_ROOT, "grafana", "provisioning", "datasources", "prometheus.yaml"
        )
        assert os.path.isfile(path)

    def test_api_version(self, config):
        assert config["apiVersion"] == 1

    def test_datasource_name(self, config):
        ds = config["datasources"][0]
        assert ds["name"] == "Prometheus"

    def test_datasource_type(self, config):
        ds = config["datasources"][0]
        assert ds["type"] == "prometheus"

    def test_datasource_url(self, config):
        ds = config["datasources"][0]
        assert ds["url"] == "http://prometheus:9090"

    def test_datasource_is_default(self, config):
        ds = config["datasources"][0]
        assert ds["isDefault"] is True

    def test_datasource_access_mode(self, config):
        ds = config["datasources"][0]
        assert ds["access"] == "proxy"


class TestGrafanaDashboardProvisioning:
    """Tests for Grafana dashboard provisioning config."""

    @pytest.fixture
    def config(self):
        return load_yaml(
            os.path.join(CENTRY_ROOT, "grafana", "provisioning", "dashboards", "default.yaml")
        )

    def test_provisioning_file_exists(self):
        path = os.path.join(
            CENTRY_ROOT, "grafana", "provisioning", "dashboards", "default.yaml"
        )
        assert os.path.isfile(path)

    def test_api_version(self, config):
        assert config["apiVersion"] == 1

    def test_provider_type(self, config):
        provider = config["providers"][0]
        assert provider["type"] == "file"

    def test_provider_path(self, config):
        provider = config["providers"][0]
        assert provider["options"]["path"] == "/var/lib/grafana/dashboards"


class TestGrafanaDashboard:
    """Tests for the Elitea overview dashboard JSON."""

    @pytest.fixture
    def dashboard(self):
        return load_json(
            os.path.join(CENTRY_ROOT, "grafana", "dashboards", "elitea-overview.json")
        )

    def test_dashboard_file_exists(self):
        path = os.path.join(CENTRY_ROOT, "grafana", "dashboards", "elitea-overview.json")
        assert os.path.isfile(path)

    def test_dashboard_title(self, dashboard):
        assert dashboard["title"] == "Elitea Platform Overview"

    def test_dashboard_uid(self, dashboard):
        assert dashboard["uid"] == "elitea-overview"

    def test_dashboard_has_panels(self, dashboard):
        assert len(dashboard["panels"]) >= 6

    def test_connections_panel_exists(self, dashboard):
        titles = [p["title"] for p in dashboard["panels"]]
        assert "Active Socket.IO Connections" in titles

    def test_task_queue_panel_exists(self, dashboard):
        titles = [p["title"] for p in dashboard["panels"]]
        assert "Task Queue Depth" in titles

    def test_redis_clients_panel_exists(self, dashboard):
        titles = [p["title"] for p in dashboard["panels"]]
        assert "Redis Connected Clients" in titles

    def test_redis_memory_panel_exists(self, dashboard):
        titles = [p["title"] for p in dashboard["panels"]]
        assert "Redis Memory Usage" in titles

    def test_pgbouncer_panel_exists(self, dashboard):
        titles = [p["title"] for p in dashboard["panels"]]
        assert "PgBouncer Active Connections" in titles

    def test_event_stream_lag_panel_exists(self, dashboard):
        titles = [p["title"] for p in dashboard["panels"]]
        assert "Event Stream Lag" in titles

    def test_all_panels_have_targets(self, dashboard):
        for panel in dashboard["panels"]:
            assert "targets" in panel, f"Panel '{panel['title']}' missing targets"
            assert len(panel["targets"]) > 0

    def test_all_panels_have_grid_pos(self, dashboard):
        for panel in dashboard["panels"]:
            assert "gridPos" in panel, f"Panel '{panel['title']}' missing gridPos"

    def test_schema_version(self, dashboard):
        assert dashboard["schemaVersion"] >= 30

    def test_tags_include_scaling(self, dashboard):
        assert "horizontal-scaling" in dashboard["tags"]


class TestDockerComposeServices:
    """Tests for Prometheus/Grafana services in docker-compose."""

    @pytest.fixture
    def compose(self):
        return load_yaml(os.path.join(CENTRY_ROOT, "docker-compose.yml"))

    def test_prometheus_service_exists(self, compose):
        assert "prometheus" in compose["services"]

    def test_prometheus_image(self, compose):
        svc = compose["services"]["prometheus"]
        assert "prom/prometheus" in svc["image"]

    def test_prometheus_port(self, compose):
        svc = compose["services"]["prometheus"]
        assert "9090:9090" in svc["ports"]

    def test_prometheus_config_mount(self, compose):
        svc = compose["services"]["prometheus"]
        volume_strs = svc["volumes"]
        config_mounted = any("prometheus.yml" in v for v in volume_strs)
        assert config_mounted

    def test_prometheus_data_volume(self, compose):
        svc = compose["services"]["prometheus"]
        volume_strs = svc["volumes"]
        data_mounted = any("prometheus-data" in v for v in volume_strs)
        assert data_mounted

    def test_prometheus_retention(self, compose):
        svc = compose["services"]["prometheus"]
        cmd = svc["command"]
        assert "--storage.tsdb.retention.time=7d" in cmd

    def test_grafana_service_exists(self, compose):
        assert "grafana" in compose["services"]

    def test_grafana_image(self, compose):
        svc = compose["services"]["grafana"]
        assert "grafana/grafana" in svc["image"]

    def test_grafana_port(self, compose):
        svc = compose["services"]["grafana"]
        assert "3000:3000" in svc["ports"]

    def test_grafana_admin_credentials(self, compose):
        svc = compose["services"]["grafana"]
        env = svc["environment"]
        assert "GF_SECURITY_ADMIN_USER=admin" in env
        assert "GF_SECURITY_ADMIN_PASSWORD=admin" in env

    def test_grafana_provisioning_mount(self, compose):
        svc = compose["services"]["grafana"]
        volume_strs = svc["volumes"]
        prov_mounted = any("provisioning" in v for v in volume_strs)
        assert prov_mounted

    def test_grafana_dashboards_mount(self, compose):
        svc = compose["services"]["grafana"]
        volume_strs = svc["volumes"]
        dash_mounted = any("dashboards" in v for v in volume_strs)
        assert dash_mounted

    def test_grafana_depends_on_prometheus(self, compose):
        svc = compose["services"]["grafana"]
        assert "prometheus" in svc["depends_on"]

    def test_redis_exporter_service_exists(self, compose):
        assert "redis-exporter" in compose["services"]

    def test_redis_exporter_image(self, compose):
        svc = compose["services"]["redis-exporter"]
        assert "redis_exporter" in svc["image"]

    def test_redis_exporter_depends_on_redis(self, compose):
        svc = compose["services"]["redis-exporter"]
        assert "redis" in svc["depends_on"]

    def test_redis_exporter_connects_to_redis(self, compose):
        svc = compose["services"]["redis-exporter"]
        env = svc["environment"]
        assert any("redis://redis:6379" in e for e in env)

    def test_pgbouncer_exporter_service_exists(self, compose):
        assert "pgbouncer-exporter" in compose["services"]

    def test_pgbouncer_exporter_depends_on_pgbouncer(self, compose):
        svc = compose["services"]["pgbouncer-exporter"]
        assert "pgbouncer" in svc["depends_on"]

    def test_pgbouncer_exporter_host(self, compose):
        svc = compose["services"]["pgbouncer-exporter"]
        env = svc["environment"]
        assert any("pgbouncer" in e and "HOST" in e for e in env)

    def test_all_monitoring_services_on_centry_network(self, compose):
        for name in ["prometheus", "grafana", "redis-exporter", "pgbouncer-exporter"]:
            svc = compose["services"][name]
            assert "centry" in svc["networks"]

    def test_prometheus_volume_defined(self, compose):
        assert "prometheus-data" in compose["volumes"]

    def test_grafana_volume_defined(self, compose):
        assert "grafana-data" in compose["volumes"]

    def test_all_monitoring_services_have_logging(self, compose):
        for name in ["prometheus", "grafana", "redis-exporter", "pgbouncer-exporter"]:
            svc = compose["services"][name]
            assert "logging" in svc, f"{name} missing logging config"
            assert svc["logging"]["driver"] == "json-file"


class TestConfigFileConsistency:
    """Tests for consistency between config files and docker-compose mounts."""

    def test_prometheus_config_exists_at_mount_source(self):
        path = os.path.join(CENTRY_ROOT, "prometheus", "prometheus.yml")
        assert os.path.isfile(path)

    def test_grafana_datasource_exists_at_mount_source(self):
        path = os.path.join(
            CENTRY_ROOT, "grafana", "provisioning", "datasources", "prometheus.yaml"
        )
        assert os.path.isfile(path)

    def test_grafana_dashboard_provisioning_exists_at_mount_source(self):
        path = os.path.join(
            CENTRY_ROOT, "grafana", "provisioning", "dashboards", "default.yaml"
        )
        assert os.path.isfile(path)

    def test_grafana_dashboard_json_exists_at_mount_source(self):
        path = os.path.join(CENTRY_ROOT, "grafana", "dashboards", "elitea-overview.json")
        assert os.path.isfile(path)

    def test_prometheus_scrape_target_matches_compose_service(self):
        compose = load_yaml(os.path.join(CENTRY_ROOT, "docker-compose.yml"))
        prom_config = load_yaml(os.path.join(CENTRY_ROOT, "prometheus", "prometheus.yml"))
        compose_services = set(compose["services"].keys())
        for job in prom_config["scrape_configs"]:
            for sc in job["static_configs"]:
                for target in sc["targets"]:
                    host = target.split(":")[0]
                    assert host in compose_services or host.replace("-", "_") in compose_services, (
                        f"Prometheus target host '{host}' not found in docker-compose services"
                    )

    def test_grafana_datasource_url_points_to_compose_service(self):
        compose = load_yaml(os.path.join(CENTRY_ROOT, "docker-compose.yml"))
        ds_config = load_yaml(
            os.path.join(CENTRY_ROOT, "grafana", "provisioning", "datasources", "prometheus.yaml")
        )
        url = ds_config["datasources"][0]["url"]
        assert "prometheus" in url
        assert "prometheus" in compose["services"]
