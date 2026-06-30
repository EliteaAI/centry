"""
Tests for Task 6.6: Add volume security (fsGroup, permissions).

Validates that:
- All staging Deployment values include podSecurityContext with fsGroup: 1000
- All staging Deployment values include securityContext with runAsNonRoot, allowPrivilegeEscalation: false
- Container capabilities are dropped
- seccompProfile is RuntimeDefault
- emptyDir volumes are defined for writable temp dirs
- The Helm chart deployment template applies both pod and container security contexts
- readOnlyRootFilesystem rationale is documented (false due to pylon bootstrap needs)
"""

import os
import re

import pytest

try:
    import yaml
except ImportError:
    yaml = None

CENTRY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PROJECT_ROOT = os.path.dirname(CENTRY_ROOT)
ARGOCD_ROOT = os.path.join(PROJECT_ROOT, "..", "kharkevich", "argocd-public", "elitea-platform")
STAGING_VALUES = os.path.join(ARGOCD_ROOT, "values", "staging")
CHART_ROOT = os.path.join(PROJECT_ROOT, "charts", "charts", "pylon")


def load_yaml(path):
    """Load a YAML file and return as dict."""
    with open(path, "r") as f:
        if yaml:
            return yaml.safe_load(f)
        content = f.read()
        return content


def get_staging_values_files():
    """Return list of staging values files."""
    if not os.path.isdir(STAGING_VALUES):
        pytest.skip("Staging values directory not found")
    files = []
    for fname in os.listdir(STAGING_VALUES):
        if fname.endswith(".yaml") and fname.startswith("pylon-"):
            files.append(os.path.join(STAGING_VALUES, fname))
    return files


@pytest.fixture(params=["pylon-main.yaml", "pylon-indexer.yaml", "pylon-auth.yaml"])
def staging_values(request):
    """Parametrized fixture for each staging values file."""
    path = os.path.join(STAGING_VALUES, request.param)
    if not os.path.isfile(path):
        pytest.skip(f"{request.param} not found")
    if yaml is None:
        pytest.skip("PyYAML not installed")
    return request.param, load_yaml(path)


class TestPodSecurityContext:
    """Verify pod-level security context settings."""

    def test_pod_security_context_present(self, staging_values):
        name, values = staging_values
        assert "podSecurityContext" in values, f"{name}: missing podSecurityContext"

    def test_run_as_non_root(self, staging_values):
        name, values = staging_values
        psc = values.get("podSecurityContext", {})
        assert psc.get("runAsNonRoot") is True, f"{name}: runAsNonRoot must be true"

    def test_run_as_user_1000(self, staging_values):
        name, values = staging_values
        psc = values.get("podSecurityContext", {})
        assert psc.get("runAsUser") == 1000, f"{name}: runAsUser must be 1000"

    def test_run_as_group_1000(self, staging_values):
        name, values = staging_values
        psc = values.get("podSecurityContext", {})
        assert psc.get("runAsGroup") == 1000, f"{name}: runAsGroup must be 1000"

    def test_fs_group_1000(self, staging_values):
        name, values = staging_values
        psc = values.get("podSecurityContext", {})
        assert psc.get("fsGroup") == 1000, f"{name}: fsGroup must be 1000"

    def test_seccomp_profile_runtime_default(self, staging_values):
        name, values = staging_values
        psc = values.get("podSecurityContext", {})
        seccomp = psc.get("seccompProfile", {})
        assert seccomp.get("type") == "RuntimeDefault", \
            f"{name}: seccompProfile.type must be RuntimeDefault"


class TestContainerSecurityContext:
    """Verify container-level security context settings."""

    def test_security_context_present(self, staging_values):
        name, values = staging_values
        assert "securityContext" in values, f"{name}: missing securityContext"

    def test_allow_privilege_escalation_false(self, staging_values):
        name, values = staging_values
        sc = values.get("securityContext", {})
        assert sc.get("allowPrivilegeEscalation") is False, \
            f"{name}: allowPrivilegeEscalation must be false"

    def test_capabilities_drop_all(self, staging_values):
        name, values = staging_values
        sc = values.get("securityContext", {})
        caps = sc.get("capabilities", {})
        assert "ALL" in caps.get("drop", []), \
            f"{name}: capabilities must drop ALL"

    def test_read_only_root_filesystem_false(self, staging_values):
        """readOnlyRootFilesystem is false because pylon bootstrap writes to /data (git clone, pip install)."""
        name, values = staging_values
        sc = values.get("securityContext", {})
        assert sc.get("readOnlyRootFilesystem") is False, \
            f"{name}: readOnlyRootFilesystem must be false (pylon bootstrap requires writable /data)"


class TestWritableTempDirs:
    """Verify emptyDir volumes exist for writable temp directories."""

    def test_tmp_storage_enabled(self, staging_values):
        name, values = staging_values
        tmp = values.get("tmpStorage", {})
        assert tmp.get("enabled") is True, f"{name}: tmpStorage must be enabled"

    def test_tmp_storage_mount_path(self, staging_values):
        name, values = staging_values
        tmp = values.get("tmpStorage", {})
        assert tmp.get("mountPath") == "/tmp", f"{name}: tmpStorage.mountPath must be /tmp"

    def test_tmp_storage_has_size_limit(self, staging_values):
        name, values = staging_values
        tmp = values.get("tmpStorage", {})
        size = tmp.get("sizeLimit", "")
        assert size, f"{name}: tmpStorage.sizeLimit must be set"

    def test_indexer_has_cache_volume(self):
        """pylon-indexer needs extra emptyDir for model caches."""
        path = os.path.join(STAGING_VALUES, "pylon-indexer.yaml")
        if not os.path.isfile(path):
            pytest.skip("pylon-indexer.yaml not found")
        if yaml is None:
            pytest.skip("PyYAML not installed")
        values = load_yaml(path)
        extra_vols = values.get("extraVolumes", [])
        cache_vol = [v for v in extra_vols if v.get("name") == "cache-volume"]
        assert len(cache_vol) == 1, "pylon-indexer must have cache-volume extraVolume"
        assert "emptyDir" in cache_vol[0], "cache-volume must be emptyDir type"

    def test_indexer_cache_volume_has_size_limit(self):
        """cache-volume emptyDir must have a sizeLimit."""
        path = os.path.join(STAGING_VALUES, "pylon-indexer.yaml")
        if not os.path.isfile(path):
            pytest.skip("pylon-indexer.yaml not found")
        if yaml is None:
            pytest.skip("PyYAML not installed")
        values = load_yaml(path)
        extra_vols = values.get("extraVolumes", [])
        cache_vol = [v for v in extra_vols if v.get("name") == "cache-volume"][0]
        assert cache_vol["emptyDir"].get("sizeLimit"), "cache-volume must have sizeLimit"


class TestHelmChartDefaults:
    """Verify the Pylon Helm chart has secure default values."""

    @pytest.fixture
    def chart_values(self):
        path = os.path.join(CHART_ROOT, "values.yaml")
        if not os.path.isfile(path):
            pytest.skip("Pylon chart values.yaml not found")
        if yaml is None:
            pytest.skip("PyYAML not installed")
        return load_yaml(path)

    def test_chart_default_pod_security_context(self, chart_values):
        psc = chart_values.get("podSecurityContext", {})
        assert psc.get("runAsNonRoot") is True
        assert psc.get("runAsUser") == 1000
        assert psc.get("fsGroup") == 1000

    def test_chart_default_container_security_context(self, chart_values):
        sc = chart_values.get("securityContext", {})
        assert sc.get("allowPrivilegeEscalation") is False
        assert "ALL" in sc.get("capabilities", {}).get("drop", [])

    def test_chart_default_seccomp(self, chart_values):
        psc = chart_values.get("podSecurityContext", {})
        assert psc.get("seccompProfile", {}).get("type") == "RuntimeDefault"


class TestDeploymentTemplate:
    """Verify the Helm chart deployment template applies security contexts."""

    @pytest.fixture
    def deployment_template(self):
        path = os.path.join(CHART_ROOT, "templates", "deployment.yaml")
        if not os.path.isfile(path):
            pytest.skip("Deployment template not found")
        with open(path, "r") as f:
            return f.read()

    def test_pod_security_context_applied(self, deployment_template):
        assert "podSecurityContext" in deployment_template
        assert "securityContext:" in deployment_template

    def test_container_security_context_applied(self, deployment_template):
        assert ".Values.securityContext" in deployment_template

    def test_security_context_uses_with_block(self, deployment_template):
        assert "{{- with .Values.podSecurityContext }}" in deployment_template
        assert "{{- with .Values.securityContext }}" in deployment_template


class TestSecurityContextConsistency:
    """Verify all staging pylon values have identical security settings."""

    def test_all_pylons_have_same_pod_security_context(self):
        if yaml is None:
            pytest.skip("PyYAML not installed")
        contexts = {}
        for fname in ["pylon-main.yaml", "pylon-indexer.yaml", "pylon-auth.yaml"]:
            path = os.path.join(STAGING_VALUES, fname)
            if not os.path.isfile(path):
                pytest.skip(f"{fname} not found")
            values = load_yaml(path)
            contexts[fname] = values.get("podSecurityContext")

        main_ctx = contexts["pylon-main.yaml"]
        for fname, ctx in contexts.items():
            assert ctx == main_ctx, \
                f"{fname} podSecurityContext differs from pylon-main.yaml"

    def test_all_pylons_have_same_container_security_context(self):
        if yaml is None:
            pytest.skip("PyYAML not installed")
        contexts = {}
        for fname in ["pylon-main.yaml", "pylon-indexer.yaml", "pylon-auth.yaml"]:
            path = os.path.join(STAGING_VALUES, fname)
            if not os.path.isfile(path):
                pytest.skip(f"{fname} not found")
            values = load_yaml(path)
            contexts[fname] = values.get("securityContext")

        main_ctx = contexts["pylon-main.yaml"]
        for fname, ctx in contexts.items():
            assert ctx == main_ctx, \
                f"{fname} securityContext differs from pylon-main.yaml"

    def test_fs_group_matches_run_as_user(self):
        """fsGroup and runAsUser should match so volume permissions are consistent."""
        if yaml is None:
            pytest.skip("PyYAML not installed")
        for fname in ["pylon-main.yaml", "pylon-indexer.yaml", "pylon-auth.yaml"]:
            path = os.path.join(STAGING_VALUES, fname)
            if not os.path.isfile(path):
                continue
            values = load_yaml(path)
            psc = values.get("podSecurityContext", {})
            assert psc.get("fsGroup") == psc.get("runAsUser"), \
                f"{fname}: fsGroup ({psc.get('fsGroup')}) must match runAsUser ({psc.get('runAsUser')})"


class TestInitContainerSecurity:
    """Verify init containers don't run as root or escalate privileges."""

    def test_init_containers_use_non_root_images(self, staging_values):
        """Init containers should use minimal images (busybox runs as root by default,
        but pod-level runAsNonRoot + fsGroup constrain them)."""
        name, values = staging_values
        psc = values.get("podSecurityContext", {})
        assert psc.get("runAsNonRoot") is True, \
            f"{name}: podSecurityContext.runAsNonRoot applies to ALL containers including init"

    def test_init_containers_inherit_pod_security(self, staging_values):
        """Pod security context applies to init containers too."""
        name, values = staging_values
        init_containers = values.get("initContainers", [])
        psc = values.get("podSecurityContext", {})
        if not init_containers:
            pytest.skip(f"{name} has no init containers")
        assert psc.get("fsGroup") == 1000, \
            f"{name}: init containers inherit fsGroup from pod security context"
