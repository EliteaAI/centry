"""
Tests for Task 1.14: Liveness/readiness/startup probe configuration.

Validates that:
- All services have liveness, readiness, and startup probes configured
- Probe paths match the correct endpoints for each service type
- Probe timing parameters are within acceptable ranges
- Helm chart template supports all probe types
"""

import os
import yaml
import pytest


CENTRY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
ARGOCD_STAGING = os.path.join(
    os.path.dirname(os.path.dirname(CENTRY_ROOT)),
    "kharkevich", "argocd-public", "elitea-platform", "values", "staging"
)
CHARTS_ROOT = os.path.join(os.path.dirname(CENTRY_ROOT), "charts", "charts", "pylon")


def load_yaml(filepath):
    with open(filepath, "r") as f:
        return yaml.safe_load(f)


@pytest.fixture
def pylon_main_values():
    return load_yaml(os.path.join(ARGOCD_STAGING, "pylon-main.yaml"))


@pytest.fixture
def pylon_indexer_values():
    return load_yaml(os.path.join(ARGOCD_STAGING, "pylon-indexer.yaml"))


@pytest.fixture
def pylon_auth_values():
    return load_yaml(os.path.join(ARGOCD_STAGING, "pylon-auth.yaml"))


@pytest.fixture
def deployment_template():
    path = os.path.join(CHARTS_ROOT, "templates", "deployment.yaml")
    with open(path, "r") as f:
        return f.read()


class TestChartTemplateSupport:
    """Verify Helm chart template renders all probe types."""

    def test_liveness_probe_template(self, deployment_template):
        assert "{{- with .Values.livenessProbe }}" in deployment_template

    def test_readiness_probe_template(self, deployment_template):
        assert "{{- with .Values.readinessProbe }}" in deployment_template

    def test_startup_probe_template(self, deployment_template):
        assert "{{- with .Values.startupProbe }}" in deployment_template

    def test_probes_are_in_container_spec(self, deployment_template):
        liveness_idx = deployment_template.index("livenessProbe")
        readiness_idx = deployment_template.index("readinessProbe")
        startup_idx = deployment_template.index("startupProbe")
        container_idx = deployment_template.index("containers:")
        assert liveness_idx > container_idx
        assert readiness_idx > container_idx
        assert startup_idx > container_idx


class TestPylonMainProbes:
    """pylon-main uses custom /app/health/* endpoints from elitea_core."""

    def test_has_liveness_probe(self, pylon_main_values):
        assert "livenessProbe" in pylon_main_values

    def test_has_readiness_probe(self, pylon_main_values):
        assert "readinessProbe" in pylon_main_values

    def test_has_startup_probe(self, pylon_main_values):
        assert "startupProbe" in pylon_main_values

    def test_liveness_path(self, pylon_main_values):
        path = pylon_main_values["livenessProbe"]["httpGet"]["path"]
        assert path == "/app/health/live"

    def test_readiness_path(self, pylon_main_values):
        path = pylon_main_values["readinessProbe"]["httpGet"]["path"]
        assert path == "/app/health/ready"

    def test_startup_path(self, pylon_main_values):
        path = pylon_main_values["startupProbe"]["httpGet"]["path"]
        assert path == "/app/health/live"

    def test_liveness_port(self, pylon_main_values):
        assert pylon_main_values["livenessProbe"]["httpGet"]["port"] == 8080

    def test_readiness_port(self, pylon_main_values):
        assert pylon_main_values["readinessProbe"]["httpGet"]["port"] == 8080

    def test_startup_port(self, pylon_main_values):
        assert pylon_main_values["startupProbe"]["httpGet"]["port"] == 8080

    def test_liveness_initial_delay(self, pylon_main_values):
        delay = pylon_main_values["livenessProbe"]["initialDelaySeconds"]
        assert delay == 30

    def test_readiness_initial_delay(self, pylon_main_values):
        delay = pylon_main_values["readinessProbe"]["initialDelaySeconds"]
        assert delay == 15

    def test_startup_failure_threshold(self, pylon_main_values):
        threshold = pylon_main_values["startupProbe"]["failureThreshold"]
        assert threshold == 30

    def test_liveness_period(self, pylon_main_values):
        assert pylon_main_values["livenessProbe"]["periodSeconds"] == 10

    def test_readiness_period(self, pylon_main_values):
        assert pylon_main_values["readinessProbe"]["periodSeconds"] == 5

    def test_liveness_timeout(self, pylon_main_values):
        assert pylon_main_values["livenessProbe"]["timeoutSeconds"] == 5

    def test_readiness_timeout(self, pylon_main_values):
        assert pylon_main_values["readinessProbe"]["timeoutSeconds"] == 3


class TestPylonIndexerProbes:
    """pylon-indexer uses built-in pylon /livez and /readyz endpoints."""

    def test_has_liveness_probe(self, pylon_indexer_values):
        assert "livenessProbe" in pylon_indexer_values

    def test_has_readiness_probe(self, pylon_indexer_values):
        assert "readinessProbe" in pylon_indexer_values

    def test_has_startup_probe(self, pylon_indexer_values):
        assert "startupProbe" in pylon_indexer_values

    def test_liveness_path(self, pylon_indexer_values):
        path = pylon_indexer_values["livenessProbe"]["httpGet"]["path"]
        assert path == "/livez"

    def test_readiness_path(self, pylon_indexer_values):
        path = pylon_indexer_values["readinessProbe"]["httpGet"]["path"]
        assert path == "/readyz"

    def test_startup_path(self, pylon_indexer_values):
        path = pylon_indexer_values["startupProbe"]["httpGet"]["path"]
        assert path == "/livez"

    def test_liveness_port(self, pylon_indexer_values):
        assert pylon_indexer_values["livenessProbe"]["httpGet"]["port"] == 8080

    def test_liveness_initial_delay_higher_than_main(self, pylon_indexer_values):
        delay = pylon_indexer_values["livenessProbe"]["initialDelaySeconds"]
        assert delay >= 120

    def test_readiness_initial_delay_higher_than_main(self, pylon_indexer_values):
        delay = pylon_indexer_values["readinessProbe"]["initialDelaySeconds"]
        assert delay >= 90

    def test_startup_failure_threshold(self, pylon_indexer_values):
        threshold = pylon_indexer_values["startupProbe"]["failureThreshold"]
        assert threshold == 30

    def test_liveness_period(self, pylon_indexer_values):
        assert pylon_indexer_values["livenessProbe"]["periodSeconds"] == 30

    def test_readiness_period(self, pylon_indexer_values):
        assert pylon_indexer_values["readinessProbe"]["periodSeconds"] == 15

    def test_liveness_failure_threshold_higher(self, pylon_indexer_values):
        threshold = pylon_indexer_values["livenessProbe"]["failureThreshold"]
        assert threshold >= 6


class TestPylonAuthProbes:
    """pylon-auth uses built-in pylon /livez and /readyz endpoints."""

    def test_has_liveness_probe(self, pylon_auth_values):
        assert "livenessProbe" in pylon_auth_values

    def test_has_readiness_probe(self, pylon_auth_values):
        assert "readinessProbe" in pylon_auth_values

    def test_has_startup_probe(self, pylon_auth_values):
        assert "startupProbe" in pylon_auth_values

    def test_liveness_path(self, pylon_auth_values):
        path = pylon_auth_values["livenessProbe"]["httpGet"]["path"]
        assert path == "/livez"

    def test_readiness_path(self, pylon_auth_values):
        path = pylon_auth_values["readinessProbe"]["httpGet"]["path"]
        assert path == "/readyz"

    def test_startup_path(self, pylon_auth_values):
        path = pylon_auth_values["startupProbe"]["httpGet"]["path"]
        assert path == "/livez"

    def test_liveness_port(self, pylon_auth_values):
        assert pylon_auth_values["livenessProbe"]["httpGet"]["port"] == 8080

    def test_liveness_initial_delay(self, pylon_auth_values):
        delay = pylon_auth_values["livenessProbe"]["initialDelaySeconds"]
        assert delay == 30

    def test_readiness_initial_delay(self, pylon_auth_values):
        delay = pylon_auth_values["readinessProbe"]["initialDelaySeconds"]
        assert delay == 15

    def test_startup_failure_threshold(self, pylon_auth_values):
        threshold = pylon_auth_values["startupProbe"]["failureThreshold"]
        assert threshold == 30


class TestProbeTimingConsistency:
    """Cross-service probe timing validation."""

    def test_startup_covers_boot_time(self, pylon_main_values, pylon_indexer_values, pylon_auth_values):
        """Startup probe must allow enough time for full boot."""
        for name, values in [
            ("main", pylon_main_values),
            ("indexer", pylon_indexer_values),
            ("auth", pylon_auth_values),
        ]:
            startup = values["startupProbe"]
            max_boot_time = startup["initialDelaySeconds"] + (
                startup["periodSeconds"] * startup["failureThreshold"]
            )
            assert max_boot_time >= 120, (
                f"pylon-{name}: startup probe allows only {max_boot_time}s, "
                f"need at least 120s for plugin initialization"
            )

    def test_liveness_not_before_startup(self, pylon_main_values, pylon_indexer_values, pylon_auth_values):
        """Liveness initialDelay >= startup initialDelay (startup runs first)."""
        for name, values in [
            ("main", pylon_main_values),
            ("indexer", pylon_indexer_values),
            ("auth", pylon_auth_values),
        ]:
            liveness_delay = values["livenessProbe"]["initialDelaySeconds"]
            startup_delay = values["startupProbe"]["initialDelaySeconds"]
            assert liveness_delay >= startup_delay, (
                f"pylon-{name}: liveness initialDelay ({liveness_delay}s) < "
                f"startup initialDelay ({startup_delay}s)"
            )

    def test_readiness_before_liveness(self, pylon_main_values, pylon_indexer_values, pylon_auth_values):
        """Readiness should start checking before liveness."""
        for name, values in [
            ("main", pylon_main_values),
            ("indexer", pylon_indexer_values),
            ("auth", pylon_auth_values),
        ]:
            readiness_delay = values["readinessProbe"]["initialDelaySeconds"]
            liveness_delay = values["livenessProbe"]["initialDelaySeconds"]
            assert readiness_delay <= liveness_delay, (
                f"pylon-{name}: readiness delay ({readiness_delay}s) > "
                f"liveness delay ({liveness_delay}s)"
            )

    def test_timeout_less_than_period(self, pylon_main_values, pylon_indexer_values, pylon_auth_values):
        """Timeout must be less than period to avoid overlapping probes."""
        for name, values in [
            ("main", pylon_main_values),
            ("indexer", pylon_indexer_values),
            ("auth", pylon_auth_values),
        ]:
            for probe_name in ["livenessProbe", "readinessProbe"]:
                probe = values[probe_name]
                timeout = probe.get("timeoutSeconds", 1)
                period = probe.get("periodSeconds", 10)
                assert timeout < period, (
                    f"pylon-{name} {probe_name}: timeout ({timeout}s) >= "
                    f"period ({period}s)"
                )

    def test_indexer_has_longer_tolerances(self, pylon_main_values, pylon_indexer_values):
        """Indexer handles long-running tasks, needs more tolerant probes."""
        main_liveness_delay = pylon_main_values["livenessProbe"]["initialDelaySeconds"]
        indexer_liveness_delay = pylon_indexer_values["livenessProbe"]["initialDelaySeconds"]
        assert indexer_liveness_delay > main_liveness_delay


class TestProbeEndpointPaths:
    """Validate probe paths match the routing architecture."""

    def test_main_uses_elitea_core_health_endpoints(self, pylon_main_values):
        """pylon-main has elitea_core plugin with /app/health/* routes."""
        liveness_path = pylon_main_values["livenessProbe"]["httpGet"]["path"]
        readiness_path = pylon_main_values["readinessProbe"]["httpGet"]["path"]
        assert liveness_path.startswith("/app/health/")
        assert readiness_path.startswith("/app/health/")

    def test_indexer_uses_builtin_pylon_endpoints(self, pylon_indexer_values):
        """pylon-indexer has no elitea_core, uses pylon's built-in /livez /readyz."""
        liveness_path = pylon_indexer_values["livenessProbe"]["httpGet"]["path"]
        readiness_path = pylon_indexer_values["readinessProbe"]["httpGet"]["path"]
        assert liveness_path in ("/healthz", "/livez")
        assert readiness_path in ("/healthz", "/readyz")

    def test_auth_uses_builtin_pylon_endpoints(self, pylon_auth_values):
        """pylon-auth has no elitea_core, uses pylon's built-in /livez /readyz."""
        liveness_path = pylon_auth_values["livenessProbe"]["httpGet"]["path"]
        readiness_path = pylon_auth_values["readinessProbe"]["httpGet"]["path"]
        assert liveness_path in ("/healthz", "/livez")
        assert readiness_path in ("/healthz", "/readyz")

    def test_auth_probes_not_under_forward_auth_prefix(self, pylon_auth_values):
        """Probes must NOT use /forward-auth/ prefix (root_router won't route correctly)."""
        for probe_name in ["livenessProbe", "readinessProbe", "startupProbe"]:
            path = pylon_auth_values[probe_name]["httpGet"]["path"]
            assert not path.startswith("/forward-auth"), (
                f"{probe_name} path '{path}' is under /forward-auth/ "
                f"which routes to Flask app (not pylon's built-in health)"
            )

    def test_pylon_yml_health_enabled(self, pylon_main_values, pylon_indexer_values, pylon_auth_values):
        """All services must have health endpoints enabled in pylon.yml config."""
        for name, values in [
            ("main", pylon_main_values),
            ("indexer", pylon_indexer_values),
            ("auth", pylon_auth_values),
        ]:
            pylon_yml_content = values["config"]["files"]["pylon.yml"]
            pylon_config = yaml.safe_load(pylon_yml_content)
            health_config = pylon_config.get("server", {}).get("health", {})
            assert health_config.get("healthz", False) is True, (
                f"pylon-{name}: healthz not enabled in pylon.yml"
            )
            assert health_config.get("livez", False) is True, (
                f"pylon-{name}: livez not enabled in pylon.yml"
            )
            assert health_config.get("readyz", False) is True, (
                f"pylon-{name}: readyz not enabled in pylon.yml"
            )
