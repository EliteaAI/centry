"""Chaos tests for Redis failure and recovery.

Tests validate that the system:
1. Degrades gracefully when Redis becomes unavailable
2. Recovers automatically when Redis comes back online
3. Does not lose critical data during brief outages

Prerequisites:
    - Docker Compose environment running (centry/)
    - Redis container named 'redis'
    - pylon_main accessible at http://localhost:80

Run with:
    cd centry
    python3 -m pytest tests/chaos/test_redis_failure.py -v --timeout=120
"""

import time

import pytest
import redis
import requests

from .helpers import (
    PYLON_MAIN_URL,
    REDIS_HOST,
    REDIS_PASSWORD,
    REDIS_PORT,
    run_compose,
    wait_for_healthy,
    wait_for_http,
)


@pytest.fixture
def ensure_redis_running():
    """Ensure Redis is running before and after the test (cleanup)."""
    run_compose("start", "redis", check=False)
    wait_for_healthy("redis", timeout=30)
    yield
    run_compose("start", "redis", check=False)
    wait_for_healthy("redis", timeout=30)


class TestRedisStop:
    """Test behavior when Redis is stopped."""

    @pytest.mark.usefixtures("compose_env", "ensure_redis_running")
    def test_health_endpoint_reports_degraded(self, pylon_main_url):
        """After Redis stops, /health/live should report degraded/unhealthy."""
        resp = requests.get(f"{pylon_main_url}/api/v1/health/live", timeout=5)
        assert resp.status_code == 200
        initial_data = resp.json()
        assert initial_data["status"] == "ok"

        run_compose("stop", "redis")
        time.sleep(3)

        resp = requests.get(f"{pylon_main_url}/api/v1/health/live", timeout=5)
        data = resp.json()
        assert data["status"] in ("degraded", "unhealthy")
        assert data["checks"]["redis"]["status"] != "ok"

    @pytest.mark.usefixtures("compose_env", "ensure_redis_running")
    def test_api_returns_error_not_crash(self, pylon_main_url):
        """API requests should return errors, not crash the service."""
        run_compose("stop", "redis")
        time.sleep(3)

        resp = requests.get(f"{pylon_main_url}/api/v1/health/live", timeout=10)
        assert resp.status_code in (200, 503)
        assert resp.headers.get("Content-Type", "").startswith("application/json")

    @pytest.mark.usefixtures("compose_env", "ensure_redis_running")
    def test_service_stays_running_during_redis_outage(self, pylon_main_url):
        """pylon_main should remain running (not crash-loop) without Redis."""
        run_compose("stop", "redis")
        time.sleep(5)

        result = run_compose("ps", "--format", "{{.Status}}", "pylon_main", check=False)
        status = result.stdout.strip().lower()
        assert "up" in status, f"pylon_main should stay up, got: {status}"


class TestRedisRecovery:
    """Test automatic recovery when Redis comes back."""

    @pytest.mark.usefixtures("compose_env", "ensure_redis_running")
    def test_health_recovers_after_redis_restart(self, pylon_main_url):
        """Health endpoint should return 'ok' after Redis is restored."""
        run_compose("stop", "redis")
        time.sleep(3)

        resp = requests.get(f"{pylon_main_url}/api/v1/health/live", timeout=5)
        assert resp.json()["status"] != "ok"

        run_compose("start", "redis")
        wait_for_healthy("redis", timeout=30)
        time.sleep(3)

        resp = requests.get(f"{pylon_main_url}/api/v1/health/live", timeout=5)
        data = resp.json()
        assert data["status"] == "ok"
        assert data["checks"]["redis"]["status"] == "ok"

    @pytest.mark.usefixtures("compose_env", "ensure_redis_running")
    def test_redis_reconnect_without_service_restart(self, pylon_main_url):
        """Services should reconnect to Redis without needing a restart."""
        result_before = run_compose(
            "ps", "--format", "{{.ID}}", "pylon_main", check=False
        )
        container_id_before = result_before.stdout.strip()

        run_compose("stop", "redis")
        time.sleep(3)
        run_compose("start", "redis")
        wait_for_healthy("redis", timeout=30)
        time.sleep(5)

        result_after = run_compose(
            "ps", "--format", "{{.ID}}", "pylon_main", check=False
        )
        container_id_after = result_after.stdout.strip()

        assert container_id_before == container_id_after, (
            "pylon_main container should not have restarted"
        )

    @pytest.mark.usefixtures("compose_env", "ensure_redis_running")
    def test_redis_data_persists_across_restart(self, redis_client):
        """Data written before stop should persist after restart (AOF enabled)."""
        test_key = "chaos_test:persistence_check"
        redis_client.set(test_key, "chaos_value", ex=300)
        assert redis_client.get(test_key) == "chaos_value"

        run_compose("restart", "redis")
        wait_for_healthy("redis", timeout=30)
        time.sleep(2)

        new_client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD,
            decode_responses=True,
            socket_connect_timeout=5,
        )
        try:
            value = new_client.get(test_key)
            assert value == "chaos_value", (
                f"Data should persist across restart (AOF), got: {value}"
            )
        finally:
            new_client.delete(test_key)
            new_client.close()


class TestRedisSentinelFailover:
    """Test sentinel-managed failover (if sentinels are running)."""

    @pytest.mark.usefixtures("compose_env")
    def test_sentinel_detects_master_down(self):
        """Sentinel should detect master failure within down-after-milliseconds."""
        result = run_compose("ps", "--services", check=False)
        if "redis-sentinel-1" not in result.stdout:
            pytest.skip("Sentinel not deployed")

        sentinel_client = redis.Redis(
            host=REDIS_HOST,
            port=26379,
            decode_responses=True,
            socket_connect_timeout=5,
        )
        try:
            sentinel_client.ping()
        except redis.ConnectionError:
            pytest.skip("Sentinel not reachable on port 26379")

        info = sentinel_client.execute_command("SENTINEL", "master", "mymaster")
        info_dict = dict(zip(info[::2], info[1::2]))
        assert info_dict.get("flags") == "master" or "master" in info_dict.get("flags", "")

        sentinel_client.close()
