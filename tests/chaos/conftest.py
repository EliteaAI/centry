"""Shared fixtures for chaos tests.

These tests require a running Docker Compose environment (centry/).
They manipulate containers and network conditions to validate resilience.
"""

import pytest
import redis
import requests

from .helpers import (
    PYLON_MAIN_URL,
    REDIS_HOST,
    REDIS_PASSWORD,
    REDIS_PORT,
    run_compose,
)


@pytest.fixture(scope="session")
def compose_env():
    """Verify Docker Compose environment is running."""
    result = run_compose("ps", "--services", check=False)
    if result.returncode != 0:
        pytest.skip("Docker Compose environment not available")
    services = result.stdout.strip().split("\n")
    required = {"redis", "pylon_main", "postgres"}
    running = set(services)
    missing = required - running
    if missing:
        pytest.skip(f"Required services not running: {missing}")
    return {"services": running}


@pytest.fixture
def redis_client():
    """Create a Redis client connected to the local Redis instance."""
    client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        decode_responses=True,
        socket_connect_timeout=5,
    )
    try:
        client.ping()
    except redis.ConnectionError:
        pytest.skip("Redis not reachable — is the Docker environment running?")
    yield client
    client.close()


@pytest.fixture
def pylon_main_url():
    """Return the pylon_main base URL and verify it's reachable."""
    try:
        requests.get(f"{PYLON_MAIN_URL}/api/v1/health/live", timeout=5)
    except (requests.ConnectionError, requests.Timeout):
        pytest.skip("pylon_main not reachable")
    return PYLON_MAIN_URL
