# Chaos Testing Suite

Chaos tests verify that Elitea Pylon services degrade gracefully under failure conditions and recover automatically without data loss. All tests are idempotent and safe to run on a local Docker Compose environment.

## Prerequisites

- Docker Compose environment running (`cd centry && docker compose up -d`)
- Python 3.10+ with pytest installed
- Required packages: `pytest`, `redis`, `requests`
- Network partition tests require `iproute2` installed in target containers

## Running Tests

```bash
# From the centry directory
cd centry

# Run all chaos tests
python3 -m pytest tests/chaos/ -v --timeout=180

# Run specific test category
python3 -m pytest tests/chaos/test_redis_failure.py -v --timeout=120
python3 -m pytest tests/chaos/test_pod_kill.py -v --timeout=120
python3 -m pytest tests/chaos/test_network_partition.py -v --timeout=180
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CHAOS_REDIS_HOST` | `localhost` | Redis host for direct client connections |
| `CHAOS_REDIS_PORT` | `6379` | Redis port |
| `REDIS_PASSWORD` | `changeme` | Redis AUTH password |
| `CHAOS_PYLON_MAIN_URL` | `http://localhost:80` | pylon_main base URL |
| `COMPOSE_PROJECT_NAME` | `centry` | Docker Compose project name |

## Test Categories

### test_redis_failure.py

Tests Redis failure and recovery scenarios:

| Test | Scenario | Expected Behavior |
|------|----------|-------------------|
| `test_health_endpoint_reports_degraded` | Redis stopped | Health reports degraded/unhealthy |
| `test_api_returns_error_not_crash` | Redis stopped | API returns JSON error, no crash |
| `test_service_stays_running_during_redis_outage` | Redis stopped | Container remains Up |
| `test_health_recovers_after_redis_restart` | Redis stop → start | Health returns to ok |
| `test_redis_reconnect_without_service_restart` | Redis restart | Same container ID (no restart) |
| `test_redis_data_persists_across_restart` | Redis restart | AOF data survives |
| `test_sentinel_detects_master_down` | Sentinel check | Sentinel monitors master |

### test_pod_kill.py

Tests container kill and failover:

| Test | Scenario | Expected Behavior |
|------|----------|-------------------|
| `test_container_restarts_after_kill` | SIGKILL pylon_main | Auto-restart via compose |
| `test_service_recovers_within_timeout` | Kill → wait | Accessible within 60s |
| `test_graceful_stop_allows_request_completion` | SIGTERM (stop) | Clean exit |
| `test_redis_state_survives_pylon_kill` | Kill pylon_main | Redis data intact |
| `test_session_data_survives_restart` | Restart pylon_main | Session keys persist |
| `test_callback_tasks_survive_restart` | Kill pylon_main | Callback data intact |
| `test_service_stable_after_multiple_kills` | 3 rapid kills | Recovers and stabilizes |
| `test_no_zombie_processes_after_kill` | Kill → recover | No defunct processes |

### test_network_partition.py

Tests network delays, packet loss, and partitions:

| Test | Scenario | Expected Behavior |
|------|----------|-------------------|
| `test_service_handles_redis_latency` | 200ms Redis delay | Stays functional |
| `test_service_handles_db_latency` | 500ms PG delay | Stays functional |
| `test_high_latency_triggers_timeout` | 5s Redis delay | Timeout detected |
| `test_recovery_after_latency_removed` | Delay → remove | Immediate recovery |
| `test_service_tolerates_low_packet_loss` | 10% loss | ≥50% success rate |
| `test_high_packet_loss_degrades_gracefully` | 50% loss | No crash |
| `test_redis_partition_and_recovery` | 100% loss → heal | Full recovery |

## Enabling Network Partition Tests

Network delay/loss tests use `tc netem` which requires:

1. The `iproute2` package inside containers
2. `NET_ADMIN` capability on containers

To enable, add to your `docker-compose.override.yml`:

```yaml
services:
  pylon_main:
    cap_add:
      - NET_ADMIN
  redis:
    cap_add:
      - NET_ADMIN
  postgres:
    cap_add:
      - NET_ADMIN
```

For Alpine-based Redis images, install `iproute2`:

```bash
docker compose exec redis apk add --no-cache iproute2
```

## Safety

- All tests use cleanup fixtures to restore services to running state
- Tests skip gracefully if prerequisites are not met (no Docker, no `tc`)
- No permanent changes are made to volumes or configuration
- Redis test keys use `chaos_test:` prefix and short TTLs
- Tests are designed to be re-run without manual intervention

## Integration with CI

```yaml
# Example GitHub Actions step
- name: Run chaos tests
  run: |
    cd centry
    docker compose up -d
    sleep 30  # wait for services to initialize
    pip install pytest redis requests
    python3 -m pytest tests/chaos/ -v --timeout=180 --junitxml=chaos-results.xml
    docker compose down
```
