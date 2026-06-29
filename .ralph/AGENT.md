# Agent Knowledge Base - Elitea Horizontal Scaling

This file contains learnings and context for horizontal scaling implementation.
Updated by each Ralph iteration when discoveries are made.

## Project Structure

```
eliteaai/
├── centry/                              # Docker orchestration (git: EliteaAI/centry)
│   ├── docker-compose.yml               # Service definitions
│   ├── envs/                            # Environment files
│   ├── pylon_main/                      # Main API service
│   │   ├── configs/                     # Service configuration
│   │   │   ├── pylon.yml                # Main pylon config
│   │   │   └── shared.yml              # Shared settings (DB pools, Redis)
│   │   └── plugins/
│   │       └── elitea_core/            # Core plugin (git: EliteaAI/elitea_core)
│   │           ├── sio/                # Socket.IO handlers (asr, mcp, tts)
│   │           ├── utils/              # Utilities (scaling additions go here)
│   │           ├── routes/             # HTTP routes (health endpoints here)
│   │           └── module.py           # Plugin initialization
│   ├── pylon_indexer/                   # Agent runtime
│   └── tests/e2e/                       # NEW: Playwright scaling tests
├── EliteaUI/                            # React frontend (git: EliteaAI/EliteaUI)
├── pylon/                               # Pylon framework (has RedisManager)
│   └── pylon/core/tools/server/socketio.py  # Socket.IO adapter support
├── arbiter/                             # Event bus framework
│   └── arbiter/eventnode/redis.py       # Redis pub/sub implementation
└── .ralph/                              # This directory
```

## Key Patterns

### Socket.IO Redis Adapter (Pylon Framework)

The pylon framework already supports Redis-backed Socket.IO via `RedisManager`:

```python
# In pylon/pylon/core/tools/server/socketio.py
# Three adapters available:
# 1. RedisManager (for horizontal scaling)
# 2. EventNodeManager (custom arbiter-based)
# 3. KombuManager (RabbitMQ)

# Configuration in pylon.yml:
socketio:
  redis:
    host: redis-host
    port: 6379
    password: ""
    use_ssl: false
```

### Redis State Externalization

```python
from tools import redis_tools

class RedisStateStore:
    def __init__(self, prefix: str, ttl: int = 3600):
        self.prefix = prefix
        self.ttl = ttl
        self.client = redis_tools.get_client()

    def get(self, key: str) -> dict:
        data = self.client.hgetall(f"{self.prefix}:{key}")
        return {k.decode(): v.decode() for k, v in data.items()} if data else {}

    def set(self, key: str, state: dict):
        pipe = self.client.pipeline()
        pipe.hset(f"{self.prefix}:{key}", mapping=state)
        pipe.expire(f"{self.prefix}:{key}", self.ttl)
        pipe.execute()

    def delete(self, key: str):
        self.client.delete(f"{self.prefix}:{key}")

    def list_keys(self) -> list:
        keys = self.client.keys(f"{self.prefix}:*")
        return [k.decode().split(":", 1)[1] for k in keys]
```

### Health Endpoint Pattern

```python
from pylon.core.tools import web
from flask import jsonify
import time

@web.route("/health/live")
def health_live(self):
    checks = {}
    start = time.time()

    # Redis check
    try:
        redis_tools.get_client().ping()
        checks["redis"] = {"status": "ok", "latency_ms": round((time.time() - start) * 1000)}
    except Exception as e:
        checks["redis"] = {"status": "unhealthy", "error": str(e)}

    # PostgreSQL check
    try:
        with db_tools.get_session() as session:
            session.execute("SELECT 1")
        checks["postgres"] = {"status": "ok"}
    except Exception as e:
        checks["postgres"] = {"status": "unhealthy", "error": str(e)}

    status = "ok" if all(c["status"] == "ok" for c in checks.values()) else "unhealthy"
    code = 200 if status == "ok" else 503
    return jsonify({"status": status, "checks": checks}), code
```

### Migration Lock Pattern

```python
import contextlib
from sqlalchemy import text

@contextlib.contextmanager
def migration_lock(session, lock_id: int = 12345, timeout_seconds: int = 600):
    """Acquire advisory lock for migrations. Only one pod runs migrations."""
    acquired = session.execute(
        text(f"SELECT pg_try_advisory_lock(:lock_id)"),
        {"lock_id": lock_id}
    ).scalar()

    if not acquired:
        raise RuntimeError(f"Could not acquire migration lock {lock_id}")

    try:
        yield
    finally:
        session.execute(
            text(f"SELECT pg_advisory_unlock(:lock_id)"),
            {"lock_id": lock_id}
        )
```

### Feature Flags Pattern

```python
import os
from tools import redis_tools

KNOWN_FLAGS = [
    "REDIS_STATE_ENABLED",
    "SOCKETIO_REDIS_ENABLED",
    "REDIS_STREAMS_ENABLED",
]

def is_enabled(flag: str, project_id: str = None) -> bool:
    # Environment variable override (highest priority)
    env_val = os.environ.get(f"FF_{flag}")
    if env_val is not None:
        return env_val.lower() in ("1", "true", "yes")

    # Redis per-project flag
    client = redis_tools.get_client()
    if project_id:
        val = client.get(f"feature_flags:{project_id}:{flag}")
        if val is not None:
            return val.decode() == "1"

    # Redis global flag
    val = client.get(f"feature_flags:global:{flag}")
    return val is not None and val.decode() == "1"
```

### Pylon Plugin Pattern

Every plugin follows this structure:
```
plugin_name/
├── __init__.py          # Must have PLUGIN_NAME constant
├── module.py            # Module class with init(), deinit()
├── metadata.json        # Plugin metadata (version, requirements)
└── requirements.txt     # Python dependencies
```

### ArgoCD Staging Overlay

Staging deployment uses same Helm chart with different values:
- Namespace: `elitea-staging`
- Hostname: `elitea-staging.technicaldomain.xyz`
- OIDC: `oidc-mock.technicaldomain.xyz` (mock provider)
- Replicas: main=3, indexer=3, auth=2
- Redis adapter: enabled
- DB pools: reduced for multi-replica (main=15+10, indexer=10+5, auth=10+5)

## Testing Patterns

### Python Tests (pytest)

```python
import pytest
from unittest.mock import MagicMock, patch

@pytest.fixture
def mock_redis():
    with patch('tools.redis_tools.get_client') as mock:
        client = MagicMock()
        mock.return_value = client
        yield client

def test_state_store_get(mock_redis):
    mock_redis.hgetall.return_value = {b"key": b"value"}
    store = RedisStateStore("test")
    result = store.get("id1")
    assert result == {"key": "value"}
    mock_redis.hgetall.assert_called_once_with("test:id1")
```

### Playwright E2E Tests

```typescript
import { test, expect } from '@playwright/test';

test('health check returns ok for all pods', async ({ request }) => {
  const response = await request.get('/health/live');
  expect(response.ok()).toBeTruthy();
  const body = await response.json();
  expect(body.status).toBe('ok');
});
```

## Environment Details

### Local Development (Docker Compose)
- Redis: `redis:6379` (no auth)
- PostgreSQL: `postgres:5432` (user: centry, db: centry)
- RustFS: `rustfs:9000`
- pylon_main: port 8080
- pylon_auth: port 8080
- pylon_indexer: port 8080

### Staging (Kubernetes)
- Valkey: `elitea-staging-valkey:6379` (no auth)
- PostgreSQL: `elitea-staging-postgres-cluster-rw:5432` (CNPG)
- RustFS: `elitea-staging-rustfs-svc:9000`
- Ingress: Traefik Gateway API HTTPRoutes

## Learnings Log

### 2026-06-29: Horizontal Scaling Setup
- Pylon framework already has RedisManager for Socket.IO (python-socketio 5.15.0)
- Socket.IO sio handlers live in `elitea_core/sio/` (asr.py, mcp.py, tts.py)
- Current DB pools are oversized for multi-replica (main: 100+200)
- Health endpoints exist at /healthz, /livez, /readyz (basic)
- Need richer /health/live and /health/ready with dependency checks
- ArgoCD uses app-of-apps with OCI Helm charts (pylon v1.0.6)
- Gateway API HTTPRoutes (not Ingress) for routing
- OIDC mock at oidc-mock.technicaldomain.xyz supports authorization_code flow

---
*Last updated by Ralph iteration*
