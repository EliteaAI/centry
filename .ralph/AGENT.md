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

### 2026-06-29: Task 1.1 - Socket.IO Redis Adapter
- Config goes in `shared.yml` (NOT pylon.yml — that file doesn't exist in this project)
- The `socketio.redis` section under `settings:` key activates `RedisManager` in pylon
- Env var expansion happens BEFORE yaml.SafeLoader, so `${REDIS_SSL}` → `false` → boolean `False`
- Requirements already present: `python-socketio[client]==5.15.0`, `redis==7.1.0`, `aioredis==2.0.1`
- Test file: `centry/tests/unit/scaling/test_socketio_redis_adapter.py` (17 tests)
- Cannot import pylon directly in tests (dependency chain: pylon→arbiter→pika, socketio not installed locally)
- Solution: replicate URL construction logic for unit testing; test config integration via YAML parsing
- Features validator (`.ralph/features.json`) expected pylon.yml → fixed to shared.yml
- No Docker available in dev env, so Docker-level integration tests can't run locally

### 2026-06-29: Task 1.2 - RedisServersStorage for MCP State
- Existing in-memory `ServersStorage` at `utils/mcp_servers_storage.py` — uses two dicts: `project_id_to_server_name_to_server` and `sid_to_project_id`
- Created `RedisServersStorage` at same path prefix but separate file: `utils/redis_servers_storage.py`
- Redis key layout: `mcp_servers:{project_id}` (hash: server_name → JSON), `mcp_sid_to_project:{sid}` (string: project_id)
- Uses `HSETNX` for atomic registration (prevents duplicate registration race)
- Module accesses Redis via `self.get_redis_client()` method (from `methods/redis_client.py`)
- The `McpServer` Pydantic model has `model_dump_json()` / `model_validate_json()` for serialization
- Python 3.9 incompatibility: `models/mcp.py` and `utils/sio_utils.py` use `X | Y` union syntax (3.10+)
- Test strategy: define compatible Pydantic models in test file; load `redis_servers_storage.py` via `importlib.util.spec_from_file_location` bypassing `__init__.py` chain
- `validate_all()` uses `SCAN` with cursor to iterate `mcp_sid_to_project:*` keys
- Module initialization at `module.py:800-801`: `self.servers_storage = ServersStorage()` — will change to `RedisServersStorage(self.get_redis_client())` when feature flag is enabled
- Test file: `centry/tests/unit/scaling/test_redis_servers_storage.py` (37 tests, 98% coverage)

### 2026-06-29: Task 1.3 - Externalize ASR Session State to Redis
- ASR module at `sio/asr.py` uses a module-global dict `_sessions` for per-SID VAD state
- Two session types: "whisper" (VAD buffering + batch API calls) and "realtime" (streaming to indexer via event_node)
- Whisper sessions hold threading.Lock, bytearray buffer, flush timer — not directly serializable to Redis
- Design decision: **hybrid approach** — keep local dict for hot-path VAD processing (sub-ms latency needed), externalize config + recovery state to Redis
  - Local dict: lock, buffer (active frame accumulation), flush_timer, event_node, task_node references
  - Redis hash `asr_session:{sid}`: type, project_id, model_name, language, VAD state (speech_detected, silent_frames, call_in_flight)
  - Redis list `asr_buffer:{sid}`: base64-encoded PCM chunks (for recovery only, written at flush boundaries)
- Session recovery via `_try_recover_session()`: when audio arrives for a SID not in local dict but present in Redis, reconstruct the local session from Redis state
- Redis client uses `decode_responses=True` (existing pattern), so binary audio is base64-encoded for list storage
- `MAX_BUFFER_CHUNKS = 200` (LTRIM to bound memory ~60s audio at 300ms/chunk)
- Module initialization: `init_redis_store(redis_client)` called from module.py during plugin init
- `on_whisper_call_done()` persists call_in_flight=False to Redis after transcript response
- The `evict_stale_sessions()` in the store uses SCAN to find idle sessions (TTL handles true abandonment, eviction is proactive)
- `sio_utils.py` has Python 3.10+ syntax (`str | None`) — tests must mock SioEvents with a fake StrEnum instead of loading the real module
- Test file: `centry/tests/unit/scaling/test_redis_asr_store.py` (59 tests, 100% coverage on store)
- Validator pattern updated in features.json: checks for `RedisAsrSessionStore` and `redis_asr_store` imports (not `redis_tools`)
- event_node and task_node are pylon framework objects — cannot be stored in Redis, must be injected from the SIO handler context during recovery

### 2026-06-29: Task 1.4 - Move callback_tasks dict to Redis
- In-memory `callback_tasks` dict defined in `module.py:67` as `self.callback_tasks = {}`
- Used in 3 places: `module.py` (init), `api/v2/predict.py:104`, `api/v2/pipeline_run.py:86` (registration), `methods/task_callbacks.py:48,51` (consumption via `.pop()`)
- Synchronization mechanism: `not_starting_task_event` (threading.Event) handles the race where task completes before callback is registered (predict clears event, task_status_changed waits on it)
- For Redis version: synchronization race is solved differently — `pop_callback` retries with wait are still needed in `task_status_changed` because the predict API on another pod may not have written to Redis yet
- Design: simple Redis string per task_id (not hash) — `callback_tasks:{task_id}` → JSON string with url+headers
- Uses `GETDEL` (Redis 6.2+) for atomic pop — ensures exactly-once consumption when multiple pods race
- TTL: 24 hours (DEFAULT_TTL = 86400)
- `pipeline_run.py` has a `hasattr(self.module, "callback_tasks")` guard (defensive) — when switching to CallbackManager, same pattern applies
- Test file: `centry/tests/unit/scaling/test_callback_manager.py` (26 tests, 100% coverage)
- Coverage trick: use `--cov=centry.pylon_main.plugins.elitea_core.utils.callback_manager` (module path) not `--cov=/absolute/path` for dynamic-import test files

### 2026-06-29: Task 1.5 - Move task_logs cache to Redis
- Implementation file `task_logs_redis.py` already existed (written in a prior iteration)
- `self.task_logs = {}` in `module.py:71` is the in-memory dict to replace
- Actual task log caching is in `logging_hub` plugin's `room_cache` dict — separate concern
- `logging_hub/sio/logs.py` handles `task_logs_subscribe`/`task_logs_unsubscribe` Socket.IO events
- `logging_hub/methods/logs.py` populates `self.room_cache[room]` with log records, limited by `room_cache_size` (default 100)
- `TaskLogsRedis` uses Redis sorted set: score=timestamp, member=JSON(record)
- Methods: `append`, `append_batch`, `get_latest`, `get_all`, `get_since`, `clear`, `count`, `exists`, `set_ttl`
- TTL: 604800 (7 days), MAX_ENTRIES: 500 (enforced via `zremrangebyrank`)
- Test file: `centry/tests/unit/scaling/test_task_logs_redis.py` (48 tests, 100% coverage)
- Module loading pattern: same as task 1.4 — mock pylon.core.tools, use importlib.util.spec_from_file_location

### 2026-06-29: Task 1.6 - Implement User Icons Storage in S3
- Current icon system: local filesystem at `/data/static/application_icon/{project_id}/{uuid}.png`
- Served via Flask route `@web.route("/application_icon/<path:sub_path>")` in `routes/application_icon.py`
- Upload flow: `api/v2/upload_icon.py` → RPC `social_save_image` (PIL resize) → save to disk
- S3/MinIO infrastructure: `MinioClient` in `shared/tools/minio_client.py` (boto3-backed), bucket prefix: `p--{project_id}.`
- Artifacts plugin RPCs available: `artifacts_upload` (upload), `artifacts_get_file_data` (download) — NO delete/list RPCs
- For delete/list: must use `MinioClient` directly (has `remove_file`, `list_files` methods)
- `MinioClient.list_files()` has no prefix filter — must filter client-side
- Config: `STORAGE_ENGINE=libcloud` in shared.yml (local driver), but `MinioClient` uses direct boto3 → `MINIO_URL` env var
- Config values from `config_pydantic.py`: MINIO_URL=http://carrier-minio:9000, MINIO_REGION=us-east-1
- Icon bucket: `icons` (no project prefix needed since icons already contain `{project_id}/` in key)
- PIL: JPEG doesn't support RGBA mode; test images for JPEG must use RGB mode
- No presigned URL support in MinioClient — icons served via application route that proxies from S3
- Design: `IconStorage(rpc_caller, minio_client)` — RPC for upload/download, MinioClient for delete/list
- Test file: `centry/tests/unit/scaling/test_icon_storage.py` (48 tests, 100% coverage)
- Module has zero pylon dependencies (only PIL + stdlib) — can be imported directly in tests without mocking framework

---
*Last updated by Ralph iteration*
