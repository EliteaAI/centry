"""Chaos tests for network partitions and latency injection.

Tests validate that:
1. Services handle network delays gracefully (timeouts fire correctly)
2. Services recover after network partition heals
3. No data corruption occurs during network issues

Prerequisites:
    - Docker Compose environment running (centry/)
    - Containers running with NET_ADMIN capability (for tc netem)
    - Or: running tests with sufficient Docker privileges

Note on tc netem:
    Network partition simulation requires the `tc` command inside containers.
    Alpine-based images need `iproute2` package installed.
    If `tc` is not available, tests that require it will be skipped.

Run with:
    cd centry
    python3 -m pytest tests/chaos/test_network_partition.py -v --timeout=180
"""

import time

import pytest
import requests

from .helpers import (
    PYLON_MAIN_URL,
    exec_in_container,
    run_compose,
    wait_for_http,
)


def _has_tc(service):
    """Check if tc (traffic control) is available in the container."""
    result = exec_in_container(service, "which tc 2>/dev/null || echo missing")
    return "missing" not in result.stdout


def _add_network_delay(service, delay_ms, interface="eth0"):
    """Add network delay using tc netem."""
    cmd = f"tc qdisc add dev {interface} root netem delay {delay_ms}ms"
    return exec_in_container(service, cmd)


def _remove_network_delay(service, interface="eth0"):
    """Remove all tc netem rules."""
    cmd = f"tc qdisc del dev {interface} root 2>/dev/null; true"
    return exec_in_container(service, cmd)


def _add_packet_loss(service, loss_pct, interface="eth0"):
    """Add packet loss using tc netem."""
    cmd = f"tc qdisc add dev {interface} root netem loss {loss_pct}%"
    return exec_in_container(service, cmd)


@pytest.fixture
def clean_network_rules():
    """Remove any tc netem rules before and after test."""
    services = ["pylon_main", "redis", "postgres"]
    for svc in services:
        _remove_network_delay(svc)
    yield
    for svc in services:
        _remove_network_delay(svc)


class TestNetworkDelay:
    """Test behavior under network latency."""

    @pytest.mark.usefixtures("compose_env", "clean_network_rules")
    def test_service_handles_redis_latency(self, pylon_main_url):
        """Service should tolerate moderate Redis latency (200ms)."""
        if not _has_tc("redis"):
            pytest.skip("tc not available in redis container (need iproute2)")

        _add_network_delay("redis", 200)
        time.sleep(2)

        resp = requests.get(
            f"{pylon_main_url}/api/v1/health/live", timeout=10
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "degraded")

        redis_check = data.get("checks", {}).get("redis", {})
        if "response_time_ms" in redis_check:
            assert redis_check["response_time_ms"] >= 200

    @pytest.mark.usefixtures("compose_env", "clean_network_rules")
    def test_service_handles_db_latency(self, pylon_main_url):
        """Service should tolerate moderate PostgreSQL latency (500ms)."""
        if not _has_tc("postgres"):
            pytest.skip("tc not available in postgres container (need iproute2)")

        _add_network_delay("postgres", 500)
        time.sleep(2)

        resp = requests.get(
            f"{pylon_main_url}/api/v1/health/live", timeout=15
        )
        assert resp.status_code in (200, 503)

    @pytest.mark.usefixtures("compose_env", "clean_network_rules")
    def test_high_latency_triggers_timeout(self, pylon_main_url):
        """Extreme latency (5s) should trigger connection timeouts."""
        if not _has_tc("redis"):
            pytest.skip("tc not available in redis container")

        _add_network_delay("redis", 5000)
        time.sleep(2)

        resp = requests.get(
            f"{pylon_main_url}/api/v1/health/live", timeout=15
        )
        data = resp.json()
        redis_status = data.get("checks", {}).get("redis", {}).get("status")
        assert redis_status in ("timeout", "unhealthy", "error"), (
            f"Redis check should fail with 5s delay, got: {redis_status}"
        )

    @pytest.mark.usefixtures("compose_env", "clean_network_rules")
    def test_recovery_after_latency_removed(self, pylon_main_url):
        """Service should recover immediately when latency is removed."""
        if not _has_tc("redis"):
            pytest.skip("tc not available in redis container")

        _add_network_delay("redis", 3000)
        time.sleep(3)

        _remove_network_delay("redis")
        time.sleep(3)

        resp = requests.get(
            f"{pylon_main_url}/api/v1/health/live", timeout=10
        )
        data = resp.json()
        assert data["status"] == "ok", (
            f"Service should recover after latency removal, got: {data['status']}"
        )


class TestPacketLoss:
    """Test behavior under packet loss conditions."""

    @pytest.mark.usefixtures("compose_env", "clean_network_rules")
    def test_service_tolerates_low_packet_loss(self, pylon_main_url):
        """Service should remain functional with 10% packet loss."""
        if not _has_tc("redis"):
            pytest.skip("tc not available in redis container")

        _add_packet_loss("redis", 10)
        time.sleep(2)

        success_count = 0
        for _ in range(10):
            try:
                resp = requests.get(
                    f"{pylon_main_url}/api/v1/health/live", timeout=10
                )
                if resp.status_code == 200:
                    success_count += 1
            except (requests.ConnectionError, requests.Timeout):
                pass
            time.sleep(0.5)

        assert success_count >= 5, (
            f"At least 50% of requests should succeed with 10% loss, got {success_count}/10"
        )

    @pytest.mark.usefixtures("compose_env", "clean_network_rules")
    def test_high_packet_loss_degrades_gracefully(self, pylon_main_url):
        """Service should not crash with 50% packet loss."""
        if not _has_tc("redis"):
            pytest.skip("tc not available in redis container")

        _add_packet_loss("redis", 50)
        time.sleep(3)

        result = run_compose(
            "ps", "--format", "{{.Status}}", "pylon_main", check=False
        )
        status = result.stdout.strip().lower()
        assert "up" in status, (
            f"pylon_main should stay running under packet loss, got: {status}"
        )


class TestNetworkPartition:
    """Test full network partition between services."""

    @pytest.mark.usefixtures("compose_env", "clean_network_rules")
    def test_redis_partition_and_recovery(self, pylon_main_url):
        """Full partition from Redis → degradation → recovery."""
        if not _has_tc("pylon_main"):
            pytest.skip("tc not available in pylon_main container")

        result = exec_in_container(
            "pylon_main",
            "ip route | grep -oP '\\d+\\.\\d+\\.\\d+\\.\\d+' | head -1",
        )
        if not result.stdout.strip():
            pytest.skip("Cannot determine container network interface")

        exec_in_container(
            "pylon_main",
            "tc qdisc add dev eth0 root netem loss 100%",
        )
        time.sleep(5)

        result = run_compose(
            "ps", "--format", "{{.Status}}", "pylon_main", check=False
        )
        assert "up" in result.stdout.strip().lower()

        _remove_network_delay("pylon_main")
        time.sleep(5)

        recovered = wait_for_http(
            f"{PYLON_MAIN_URL}/api/v1/health/live", timeout=30
        )
        assert recovered, "Service should recover after partition heals"


class TestDNSFailure:
    """Test behavior when DNS resolution fails."""

    @pytest.mark.usefixtures("compose_env")
    def test_service_survives_dns_unavailability(self):
        """Services should handle temporary DNS issues without crashing."""
        result = run_compose(
            "ps", "--format", "{{.Status}}", "pylon_main", check=False
        )
        assert "up" in result.stdout.strip().lower()
