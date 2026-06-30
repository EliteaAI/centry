"""Shared helpers for chaos tests.

These tests require a running Docker Compose environment (centry/).
They manipulate containers and network conditions to validate resilience.
"""

import os
import subprocess
import time

import requests


COMPOSE_DIR = os.path.join(os.path.dirname(__file__), "..", "..")
COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT_NAME", "centry")

REDIS_HOST = os.environ.get("CHAOS_REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("CHAOS_REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "changeme")

PYLON_MAIN_URL = os.environ.get("CHAOS_PYLON_MAIN_URL", "http://localhost:80")

DOCKER_COMPOSE_CMD = ["docker", "compose"]


def run_compose(*args, check=True, timeout=60):
    """Run a docker compose command in the centry directory."""
    cmd = DOCKER_COMPOSE_CMD + list(args)
    return subprocess.run(
        cmd,
        cwd=COMPOSE_DIR,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def wait_for_healthy(service, timeout=60):
    """Wait until a service container is healthy."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = run_compose(
            "ps", "--format", "{{.Health}}", service, check=False
        )
        status = result.stdout.strip().lower()
        if "healthy" in status:
            return True
        time.sleep(2)
    return False


def wait_for_http(url, timeout=30, expected_status=200):
    """Wait until an HTTP endpoint responds with expected status."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code == expected_status:
                return True
        except (requests.ConnectionError, requests.Timeout):
            pass
        time.sleep(1)
    return False


def exec_in_container(service, command, check=False, timeout=30):
    """Execute a command inside a running container."""
    cmd = DOCKER_COMPOSE_CMD + ["exec", "-T", service, "sh", "-c", command]
    return subprocess.run(
        cmd,
        cwd=COMPOSE_DIR,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )
