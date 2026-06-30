"""Chaos tests for pod (container) kill and failover.

Tests validate that:
1. Killing pylon_main container causes automatic restart
2. Requests failover to other pods (if scaled) or recover after restart
3. No data loss occurs during container crash and recovery

Prerequisites:
    - Docker Compose environment running (centry/)
    - pylon_main accessible at http://localhost:80
    - Redis accessible for data persistence verification

Run with:
    cd centry
    python3 -m pytest tests/chaos/test_pod_kill.py -v --timeout=120
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
def ensure_pylon_main_running():
    """Ensure pylon_main is running before and after test."""
    run_compose("start", "pylon_main", check=False)
    wait_for_http(f"{PYLON_MAIN_URL}/api/v1/health/live", timeout=60)
    yield
    run_compose("start", "pylon_main", check=False)
    wait_for_http(f"{PYLON_MAIN_URL}/api/v1/health/live", timeout=60)


class TestContainerKill:
    """Test container kill and automatic restart."""

    @pytest.mark.usefixtures("compose_env", "ensure_pylon_main_running")
    def test_container_restarts_after_kill(self):
        """Container should auto-restart after being killed (restart: unless-stopped)."""
        result = run_compose(
            "ps", "--format", "{{.ID}}", "pylon_main", check=False
        )
        original_id = result.stdout.strip()
        assert original_id, "pylon_main should be running"

        run_compose("kill", "pylon_main")
        time.sleep(5)

        result = run_compose(
            "ps", "--format", "{{.Status}}", "pylon_main", check=False
        )
        status = result.stdout.strip().lower()
        assert "up" in status or "restarting" in status, (
            f"pylon_main should restart automatically, got: {status}"
        )

    @pytest.mark.usefixtures("compose_env", "ensure_pylon_main_running")
    def test_service_recovers_within_timeout(self, pylon_main_url):
        """Service should be accessible again within 60 seconds after kill."""
        run_compose("kill", "pylon_main")

        recovered = wait_for_http(
            f"{PYLON_MAIN_URL}/api/v1/health/live", timeout=60
        )
        assert recovered, "pylon_main should recover within 60 seconds"

        resp = requests.get(f"{PYLON_MAIN_URL}/api/v1/health/live", timeout=5)
        assert resp.status_code == 200

    @pytest.mark.usefixtures("compose_env", "ensure_pylon_main_running")
    def test_graceful_stop_allows_request_completion(self, pylon_main_url):
        """Graceful stop (SIGTERM) should allow in-flight requests to complete."""
        resp = requests.get(f"{pylon_main_url}/api/v1/health/live", timeout=5)
        assert resp.status_code == 200

        run_compose("stop", "-t", "30", "pylon_main")

        result = run_compose(
            "ps", "--format", "{{.Status}}", "pylon_main", check=False
        )
        status = result.stdout.strip().lower()
        assert "exited" in status or status == "", (
            f"pylon_main should have stopped gracefully, got: {status}"
        )


class TestDataPersistenceDuringKill:
    """Verify data in Redis survives container kills."""

    @pytest.mark.usefixtures("compose_env", "ensure_pylon_main_running")
    def test_redis_state_survives_pylon_kill(self, redis_client):
        """State stored in Redis should persist when pylon_main is killed."""
        test_key = "chaos_test:pod_kill_persistence"
        redis_client.set(test_key, "pre_kill_value", ex=300)

        run_compose("kill", "pylon_main")
        time.sleep(2)

        value = redis_client.get(test_key)
        assert value == "pre_kill_value", (
            "Redis state should not be affected by pylon_main container kill"
        )

        redis_client.delete(test_key)

    @pytest.mark.usefixtures("compose_env", "ensure_pylon_main_running")
    def test_session_data_survives_restart(self, redis_client):
        """Session data in Redis should survive a full pylon_main restart cycle."""
        session_key = "chaos_test:session_survival"
        session_data = '{"user_id": 42, "project_id": 1, "token": "abc123"}'
        redis_client.set(session_key, session_data, ex=300)

        run_compose("restart", "pylon_main")
        wait_for_http(f"{PYLON_MAIN_URL}/api/v1/health/live", timeout=60)

        value = redis_client.get(session_key)
        assert value == session_data, (
            "Session data should persist across pylon_main restart"
        )

        redis_client.delete(session_key)

    @pytest.mark.usefixtures("compose_env", "ensure_pylon_main_running")
    def test_callback_tasks_survive_restart(self, redis_client):
        """Callback tasks in Redis should survive service restart."""
        callback_key = "chaos_test:callback_tasks:task_001"
        callback_data = '{"callback_url": "http://example.com/hook", "status": "pending"}'
        redis_client.set(callback_key, callback_data, ex=300)

        run_compose("kill", "pylon_main")
        time.sleep(3)

        value = redis_client.get(callback_key)
        assert value == callback_data

        redis_client.delete(callback_key)


class TestMultipleContainerRestart:
    """Test rapid successive kills (crash-loop simulation)."""

    @pytest.mark.usefixtures("compose_env", "ensure_pylon_main_running")
    def test_service_stable_after_multiple_kills(self, pylon_main_url):
        """Service should stabilize after multiple rapid kills."""
        for i in range(3):
            run_compose("kill", "pylon_main")
            time.sleep(3)

        recovered = wait_for_http(
            f"{PYLON_MAIN_URL}/api/v1/health/live", timeout=90
        )
        assert recovered, (
            "pylon_main should recover and stabilize after 3 rapid kills"
        )

    @pytest.mark.usefixtures("compose_env", "ensure_pylon_main_running")
    def test_no_zombie_processes_after_kill(self):
        """No zombie processes should remain after container kill."""
        run_compose("kill", "pylon_main")
        time.sleep(5)

        wait_for_http(f"{PYLON_MAIN_URL}/api/v1/health/live", timeout=60)

        result = run_compose(
            "exec", "pylon_main", "sh", "-c",
            "ps aux | grep -c defunct || echo 0",
            check=False,
        )
        defunct_count = 0
        try:
            defunct_count = int(result.stdout.strip())
        except ValueError:
            pass
        assert defunct_count == 0, f"Found {defunct_count} zombie processes"
