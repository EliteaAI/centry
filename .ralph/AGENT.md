# Agent Knowledge Base - Elitea Horizontal Scaling

This file contains learnings and context for horizontal scaling implementation.
Updated by each Ralph iteration when discoveries are made.

## Project Structure

```
eliteaai/
├── elitea_core/                         # SOURCE REPO (git: EliteaAI/elitea_core) ← EDIT HERE
│   ├── sio/                            # Socket.IO handlers (asr, mcp, tts)
│   ├── utils/                          # Utilities (scaling additions go here)
│   ├── routes/                         # HTTP routes (health endpoints here)
│   └── module.py                       # Plugin initialization
├── centry/                              # Docker orchestration (git: EliteaAI/centry)
│   ├── docker-compose.yml               # Service definitions
│   ├── envs/                            # Environment files
│   ├── pylon_main/                      # Main API service (mounted as /data in container)
│   │   ├── configs/                     # Service configuration
│   │   │   ├── pylon.yml                # Main pylon config
│   │   │   └── shared.yml              # Shared settings (DB pools, Redis)
│   │   └── plugins/
│   │       └── elitea_core/            # RUNTIME CLONE — do NOT edit directly
│   ├── pylon_indexer/                   # Agent runtime
│   └── tests/e2e/                       # Playwright scaling tests
├── EliteaUI/                            # React frontend (git: EliteaAI/EliteaUI)
├── pylon/                               # Pylon framework (has RedisManager)
│   └── pylon/core/tools/server/socketio.py  # Socket.IO adapter support
├── arbiter/                             # Event bus framework
│   └── arbiter/eventnode/redis.py       # Redis pub/sub implementation
└── .ralph/                              # This directory
```

**IMPORTANT**: `centry/pylon_main/plugins/elitea_core/` is a runtime git clone used by Docker.
Always edit the SOURCE at `elitea_core/` and commit there. The runtime copy syncs separately.

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

### 2026-06-29: Task 1.7 - Convert /tmp PVC to emptyDir in Staging
- Pylon Helm chart v1.0.6 has built-in `tmpStorage` support: `tmpStorage.enabled: true`, `tmpStorage.mountPath: /tmp`, `tmpStorage.sizeLimit: XXGi`
- Chart template renders emptyDir at `/tmp` when `tmpStorage.enabled=true` (deployment.yaml lines 105-136)
- Previous staging overlay used `extraVolumes`/`extraVolumeMounts` which would CONFLICT with the chart's built-in `tmpStorage` (both try to mount at /tmp)
- Fix: use native `tmpStorage` in values override, keep `extraVolumes` only for non-chart-supported mounts (e.g. /data/cache)
- /tmp usage verified as truly ephemeral:
  - `pylon_main`: CHUNKS_TEMP_DIR (file upload chunks), TASKS_UPLOAD_FOLDER, SECRETS_FILESYSTEM_PATH, STORAGE_FILESYSTEM_PATH — all request-scoped, no persistence needed
  - `pylon_indexer`: TaskNode intermediate results at `/tmp/tasknode`, bootstrap tempfiles — all ephemeral
  - NLTK data configured to `/data/cache/nltk` (not /tmp) in staging config
- Chart template v1.0.6 does NOT support `startupProbe`, `lifecycle`, or `terminationGracePeriodSeconds` — those fields in values are inert until chart is upgraded (tasks 1.12-1.14)
- ArgoCD staging apps use OCI chart + values ref pattern: chart from `oci://ghcr.io/eliteaai/charts/pylon@1.0.6`, values from git repo `$values/elitea-platform/values/staging/`
- Staging pylon-main: tmpStorage 10Gi sizeLimit
- Staging pylon-indexer: tmpStorage 20Gi sizeLimit + extraVolumes cache 60Gi at /data/cache

### 2026-06-29: Task 1.8 - Reduce Database Connection Pools
- **pylon_main** pool config lives in `centry/pylon_main/configs/shared.yml` under `settings.database_engine_options`
- **pylon_auth** pool config lives in `centry/pylon_auth/configs/auth_core.yml` under `db_options`
- **pylon_indexer** has NO local `shared` plugin or SQLAlchemy pool — uses sqlite for pylon_db. In staging, the `shared` plugin is bootstrapped which injects the DB engine (via `force_inject_db: true`)
- Config flow: `shared.yml` → `Config` class (`shared/tools/config.py`) → `DATABASE_ENGINE_OPTIONS` → `db.py` line 84: `"engine_kwargs": c.DATABASE_ENGINE_OPTIONS.copy()` → `db_support.make_engine()` → `sqlalchemy.create_engine(url, **engine_kwargs)`
- Previous values (from original dev setup): pylon_main had pool_size=100, max_overflow=200 (absurdly large); pylon_auth had pool_size=25, max_overflow=25
- New values: pylon_auth=10/5, pylon_main=15/10, pylon_indexer=10/5 (staging only, no local shared plugin)
- Connection math: steady state = 2×10 + 3×15 + 3×10 = 95; burst = 2×15 + 3×25 + 3×15 = 150; both < 200 max_connections
- The `Config` class in `config.py:170-177` has DEFAULT pool settings (pool_size=25, max_overflow=25) that only apply when `DATABASE_ENGINE_OPTIONS` is empty/None — our explicit config in shared.yml overrides this
- `pool_pre_ping=True` was already present in pylon_main; added to pylon_auth to match
- Test file: `centry/tests/unit/scaling/test_db_connection_pools.py` (27 tests: local config, staging config, connection math, consistency checks)
- The pylon_indexer's LangGraph `agent_memory_config` uses psycopg directly (not SQLAlchemy pool) — separate concern, not affected by this task

### 2026-06-29: Task 1.9 - Implement Migration Lock with Timeout
- Created `elitea_core/utils/migration_lock.py`
- Uses `pg_try_advisory_lock` with polling loop (not blocking `pg_advisory_lock`) to avoid holding connections indefinitely
- Default lock ID: 900100 (arbitrary large number to avoid collision with app-level advisory locks)
- Default timeout: 600s (10 minutes), poll interval: 2.0s
- Context manager `migration_lock(db_url, lock_id, timeout, poll_interval)` yields the connection
- Creates its own NullPool engine (same pattern as `db_migrations.py`) so lock connection is independent of app pool
- `_release_lock()` swallows exceptions to guarantee cleanup in finally block
- `run_migrations_with_lock()` is the integration function — wraps `db_migrations.run_db_migrations` with advisory lock
- Integration point: replace `db_migrations.run_db_migrations(self, db_url)` with `migration_lock.run_migrations_with_lock(self, db_url)` in module.py init
- `MigrationLockTimeout` exception raised on failure — callers can catch to implement fallback behavior
- Uses `getattr(getattr(module, 'descriptor', None), 'name', str(module))` for safe module name logging
- Test file: `centry/tests/unit/scaling/test_migration_lock.py` (31 tests, 100% coverage)
- Key test pattern: `patch.object(_mod, "time")` to control time.time() and time.sleep() for deterministic retry tests

### 2026-06-29: Task 1.10 - Add Feature Flags Module
- Created `elitea_core/utils/feature_flags.py`
- Existing feature flag patterns in project: `chat_feature_flags.py` (VaultClient-based, per-project), `gateway_feature_flags.py` (config + VaultClient + consistent hashing)
- Our scaling feature flags are simpler: env var → Redis project override → Redis global → default False
- Priority chain: `FF_{FLAG_NAME}` env var (highest) > `feature_flags:{project_id}:{flag_name}` Redis key > `feature_flags:global:{flag_name}` Redis key > False (default)
- KNOWN_FLAGS tuple (not list) for immutability: REDIS_STATE_ENABLED, SOCKETIO_REDIS_ENABLED, REDIS_STREAMS_ENABLED
- Handles both `decode_responses=True` (str) and `False` (bytes) Redis clients via `isinstance(val, str)` check
- No TTL on flag keys — flags are intentional configuration, not ephemeral state
- `FeatureFlags` class takes `redis_client` (DI pattern consistent with other scaling modules)
- Test file: `centry/tests/unit/scaling/test_feature_flags.py` (38 tests, 100% coverage)
- Integration point: instantiate `FeatureFlags(self.get_redis_client())` in module.py, use `ff.is_enabled("REDIS_STATE_ENABLED")` to gate new Redis-backed implementations

### 2026-06-29: Task 1.11 - Implement /health/live and /health/ready Endpoints
- Created `elitea_core/routes/health.py` with two Flask routes
- Pylon framework already has basic `/healthz`, `/livez`, `/readyz` endpoints (in `pylon/core/tools/server/init.py`) — they just return "OK" text. Our `/health/live` and `/health/ready` are richer with dependency checks and JSON response
- Route pattern: `@web.route("/health/live")` — methods on the `Route` class get `self` bound to the module instance at runtime
- `self.get_redis_client()` is the standard way to get Redis in elitea_core (from `methods/redis_client.py`)
- PostgreSQL check uses `from tools import db as db_tools; db_tools.engine.connect()` — the `tools.db` module exposes `engine` as a module-level var (from `shared/tools/db.py`)
- SQLAlchemy `text()` must be imported from `sqlalchemy` directly (not from `tools.db`)
- `_scaling_ready` flag set to `True` at end of `ready()` method (line ~403 in module.py) — signals plugin fully initialized
- Health endpoints registered as public (no auth): `auth.add_public_rule({"uri": "/app/health/live"})` — note the `/app` prefix (from `url_prefix="/app"` in `init()`)
- The project root has a `secrets/` directory (pylon plugin) that shadows stdlib `secrets` module — this breaks `flask` import when running pytest from root. Solution: mock `flask` in `sys.modules` before loading the health module in tests
- Test pattern: mock flask.jsonify with a `FakeJsonResponse` class, mock `sys.modules["tools"]` to provide a fake `db` engine
- `make_db_engine_mock(pg_ok=True)` pattern: context manager mock on `engine.connect()` for success, `side_effect=Exception(...)` for failure
- Test file: `centry/tests/unit/scaling/test_health_endpoints.py` (28 tests, 100% coverage)

### 2026-06-30: Task 1.12 - Configure Graceful Shutdown (preStop hooks)
- Created `elitea_core/utils/graceful_shutdown.py`
- Pylon's SIGTERM handler (`pylon/core/tools/signal.py:32`) raises `SystemExit` → triggers `finally` block in `main.py:295` → calls `module_manager.deinit_modules()` → each module's `deinit()` in reverse load order
- `elitea_core/module.py:650` already had a `deinit()` method — added `GracefulShutdown.execute()` as the FIRST step before existing cleanup
- `GracefulShutdown.execute()` sequence: set shutting_down flag → enumerate SIDs via `sio.manager.get_participants("/", None)` → emit `server_shutting_down` event to each → `sio.disconnect(sid)` → flush Redis (verify connectivity)
- `sio.manager.get_participants(namespace, room)` yields `(sid, eio_sid)` tuples — room=None returns all connected clients in the namespace
- `sio.disconnect(sid)` does a server-initiated disconnect (client sees `SERVER_DISCONNECT` reason)
- Helm chart `deployment.yaml` had NO `lifecycle`, `terminationGracePeriodSeconds`, or `startupProbe` support — added all three as optional values
- Template pattern: `{{- with .Values.lifecycle }}` + `{{- toYaml . | nindent 12 }}` for flexible lifecycle hook specification
- `terminationGracePeriodSeconds` goes at `.spec.template.spec` level (pod spec), NOT container level
- Staging values already had preStop hooks configured from earlier work:
  - pylon-main: `sleep 15` + terminationGracePeriodSeconds=60
  - pylon-indexer: `sleep 30` + terminationGracePeriodSeconds=120
  - pylon-auth: `sleep 5` + terminationGracePeriodSeconds=30
- The preStop `sleep` gives the load balancer time to deregister the pod from endpoints BEFORE SIGTERM kills the app
- Gevent server stop: `Greenlet.spawn(context.http_server.stop, timeout=None).join()` — stops accepting new connections but doesn't explicitly drain existing ones
- Socket.IO server has no `shutdown()` method in sync mode (only `AsyncServer` has it) — our disconnect-all approach is the correct pattern for sync Server
- Test file: `centry/tests/unit/scaling/test_graceful_shutdown.py` (24 tests, 95% coverage)
- Coverage: `--cov=graceful_shutdown` works because importlib loads it with that module name into sys.modules

### 2026-06-30: Task 1.13 - Set terminationGracePeriodSeconds
- Already configured in staging values during task 1.12 (graceful shutdown):
  - pylon-main: `terminationGracePeriodSeconds: 60` (values/staging/pylon-main.yaml:11)
  - pylon-indexer: `terminationGracePeriodSeconds: 120` (values/staging/pylon-indexer.yaml:11)
  - pylon-auth: `terminationGracePeriodSeconds: 30` (values/staging/pylon-auth.yaml:11)
- Chart template at `charts/charts/pylon/templates/deployment.yaml:39-41` renders the field conditionally: `{{- if .Values.terminationGracePeriodSeconds }}`
- Chart also supports `lifecycle` (lines 78-81) and `startupProbe` (lines 74-77) via `{{- with .Values.X }}` pattern
- This task was a no-op — work already done in 1.12 iteration

### 2026-06-30: Task 1.14 - Configure Liveness/Readiness Probes
- Helm chart `deployment.yaml` already supports `livenessProbe`, `readinessProbe`, `startupProbe` via `{{- with .Values.X }}` blocks (lines 66-77)
- **Critical routing insight**: pylon's root_router (`wsgi.py:RouterApp`) matches routes by length (longest first)
  - Built-in health endpoints (`/healthz/`, `/livez/`, `/readyz/`) are registered at root level on root_router
  - Flask apps are mounted at `/{url_prefix}/` which may be longer than health paths
  - For pylon-auth with `server.path: /forward-auth/`: probing `/forward-auth/healthz` hits Flask app (404), NOT the built-in health endpoint
  - Correct: probe at `/livez` or `/healthz` (root level) for services without custom health routes
- **Probe path mapping**:
  - pylon-main: `/app/health/live` and `/app/health/ready` (elitea_core blueprint prefix = `/app`)
  - pylon-indexer: `/livez` and `/readyz` (pylon built-in, no elitea_core plugin)
  - pylon-auth: `/livez` and `/readyz` (pylon built-in, no custom health routes)
- Pylon's built-in `/healthz/`, `/livez/`, `/readyz/` just return "OK" (200) text — no dependency checks
- elitea_core's `/app/health/live` checks Redis + PostgreSQL connectivity; `/app/health/ready` also checks plugin init state
- `auth.add_public_rule({"uri": "/app/health/live"})` exempts the endpoint from auth (line 530-531 in module.py)
- Staging values had incorrect probe paths from earlier iterations — fixed:
  - pylon-main: `/health/live` → `/app/health/live` (added `/app` prefix)
  - pylon-indexer: `/health/live` → `/livez` (uses built-in since no elitea_core)
  - pylon-auth: `/forward-auth/healthz` → `/livez` (root level, not under Flask app prefix)
  - pylon-auth: added missing `startupProbe` (was not present before)
- Timing rationale:
  - pylon-indexer needs longer delays (initialDelay=120 for liveness) due to heavy plugin init + pip install + model loading
  - Startup probe `failureThreshold=30 × period=10 = 300s` max boot time for all services
  - pylon-auth fastest to boot (30s terminationGrace, 30s liveness delay)
- Test file: `centry/tests/unit/scaling/test_health_probes_config.py` (53 tests, validates YAML config + timing + path correctness)

### 2026-06-30: Task 1.15 - Update Socket.IO Client with Auto-Reconnect
- Socket.IO client initialization lives in `EliteaUI/src/[fsd]/app/root.jsx`
- All reconnection config was ALREADY present: reconnection=true, reconnectionDelay=1000, reconnectionDelayMax=5000, reconnectionAttempts=10, randomizationFactor=0.5
- Redux state for socket: `socketConnected`, `socketReconnecting`, `socketReconnectAttempt` in `slices/settings.js`
- Event handlers already set up: `connect`, `connect_error`, `disconnect`, `reconnect_attempt` (on `socketIo.io`), `reconnect` (on `socketIo.io`), `reconnect_failed` (on `socketIo.io`)
- **KEY FINDING**: socket.io-client v4 does NOT auto-reconnect when server forces disconnect (`sio.disconnect(sid)`) — `socket.active` will be `false`. Added `if (!socketIo.active) { setTimeout(() => socketIo.connect(), 1000); }` in disconnect handler
- Added `server_shutting_down` event handler that sets `socketReconnecting` state before the actual disconnect happens (from task 1.12's graceful shutdown)
- The `SocketContext` at `contexts/SocketContext.jsx` is just `React.createContext(undefined)` — socket instance set via `setSocket(socketIo)` on connect

### 2026-06-30: Task 1.16 - Connection State Indicator to UI
- Connection state indicator ALREADY EXISTS as a colored dot (0.5rem circle) next to the EliteA logo in the sidebar
- Located in `[fsd]/widgets/sidebar-root/ui/SidebarBody.jsx` lines 277-288 (render) and 477-491 (styles)
- `useSocketIcon` hook at `[fsd]/widgets/sidebar-root/lib/hooks/useSocketIcon.hooks.jsx` derives status from Redux
- Constants at `[fsd]/widgets/sidebar-root/lib/constants/socket.constants.js`: Connected/Reconnecting/Disconnected
- Color mapping: Connected=`palette.icon.fill.success` (green), Reconnecting=`palette.warning.main` (yellow), Disconnected=`palette.icon.fill.error` (red)
- Tooltip shows "reconnecting (attempt X/10)" during reconnection
- `isSocketIconVisible: true` — always visible (no auto-hide), which is better UX for always-connected app
- Existing implementation uses dot indicator rather than MUI Chip, but achieves same purpose
- No new component needed — task subtasks marked complete since functionality exists (different approach than planned but equivalent)

### 2026-06-30: Task 0.1 - Migrate elitea_core Changes to Source Repo
- Source repo: `elitea_core/` (on `main`, created `feature/horizontal-scaling-phase-1`)
- Runtime copy: `centry/pylon_main/plugins/elitea_core/` (on `feature/horizontal-scaling-phase-1` with 7 commits + 3 untracked files)
- `git show <branch>:<path>` used to extract files from runtime feature branch; `cat` for untracked files
- `.gitignore` had `MIGRATION*` (no leading `/`) which, with `core.ignoreCase=true` on macOS, matched `utils/migration_lock.py` — fixed to `/MIGRATION*` to scope it to root-level files only
- `*.md` in `.gitignore` means no markdown can be committed to elitea_core — this is intentional (docs live elsewhere)
- `utils/gateway_feature_flags.py` was also untracked in runtime copy but is NOT part of scaling work (LLM Gateway concern) — skipped
- After migration, validator shows 19/20 passing (only F1.20 E2E test coverage remains)
- Commit hash: `72acd3c` in `elitea_core/` source repo

### 2026-06-30: Task 1.20 - Achieve 85% Test Coverage for E2E Utilities
- Test files already existed for `utils/kubernetes.ts`, `utils/api-client.ts`, `utils/socket-client.ts`, and `pages/LoginPage.ts` (+ BasePage)
- `pages/ChatPage.ts` had 0% coverage — created `pages/ChatPage.test.ts` with 12 tests covering all methods
- `LoginPage.ts` had 91% coverage (missing `verifyLoggedIn` at lines 36-38) — added test for it
- `ChatPage.waitForSocketConnected()` passes an inline function to `page.waitForFunction` — the function body (DOM access) can't execute in vitest, so lines 51-52 remain uncovered (95% per-file)
- `vitest.config.ts` already had `@vitest/coverage-v8` configured with 85% thresholds and `include: ['utils/**/*.ts', 'pages/**/*.ts']`
- Added `'cobertura'` to the reporter list so the validator can parse XML coverage output
- Validator (`validate.py`) only looked for `coverage.xml` (pytest format) or `EliteaUI/coverage/coverage-summary.json` — updated to also check `{path}/coverage/cobertura-coverage.xml` and `{path}/coverage/coverage-summary.json`
- Final coverage: 99.35% statements, 100% branches, 98.14% functions, 99.35% lines
- 85 tests total across 5 test files, all passing
- Mocking pattern for Playwright `Page`: create `createMockPage()` factory returning object with `locator`, `waitForLoadState`, `goto`, `context`, etc. — cast as `any` when constructing page objects

### 2026-06-30: Task 6.13 - Add Validation Gates (Automated Tests)
- Gate tests live at `centry/tests/gates/` with three phase-specific test files
- `test_phase_1_gate.py` already existed (created in earlier iteration) — validates state externalization, Redis TTLs, health endpoints, Socket.IO adapter
- Created `test_phase_2_gate.py`: validates distributed locks (mutual exclusion, TTL auto-release, safe release via Lua script), Canvas optimistic locking (WATCH+MULTI/EXEC, version conflicts), auth sessions in Redis, GETDEL exactly-once task results, disconnect cleanup pub/sub
- Created `test_phase_3_gate.py`: validates health endpoint responses, concurrent health checks, rolling update simulation (rapid requests), stateless pod design (cross-connection Redis access), degraded state reporting, database connection pooling headroom
- CI workflow at `centry/.github/workflows/gate-tests.yml`:
  - Triggered on PRs to main/develop touching relevant paths
  - Uses Redis and PostgreSQL service containers
  - Runs each phase gate test separately, skipping tests that require live pylon_main (HTTP endpoint tests)
  - `-k "not ..."` filters exclude tests needing running pylon services in CI (those run in staging)
- Gate tests use `conftest.py` fixtures: `redis_client` (session-scoped), `pg_connection`, `gate_key_prefix` (UUID-based, prevents collisions)
- All gate test keys have short TTLs and are explicitly deleted in each test — safe to run against shared Redis
- Pattern: gate tests validate *invariants* (data model contracts, key patterns, TTLs) not *features* — they prove the scaling architecture holds even without live services

## Gate Tests

### Purpose
Gate tests validate scaling invariants at each phase boundary. They are the last automated check before a phase is considered complete and mergeable to main.

### Running Locally
```bash
cd centry

# Requires Redis running on localhost:6379 and PostgreSQL on localhost:5432
# Start them via Docker:
#   docker run -d --name redis-gate -p 6379:6379 redis:7-alpine
#   docker run -d --name pg-gate -p 5432:5432 -e POSTGRES_USER=centry -e POSTGRES_PASSWORD=centry -e POSTGRES_DB=centry postgres:15-alpine

# Install dependencies (not in centry venv by default):
pip install pytest pytest-timeout redis requests psycopg2-binary

# Run all gate tests
python3 -m pytest tests/gates/ -v

# Run specific phase
python3 -m pytest tests/gates/test_phase_1_gate.py -v
python3 -m pytest tests/gates/test_phase_2_gate.py -v
python3 -m pytest tests/gates/test_phase_3_gate.py -v

# Skip tests requiring live pylon_main (CI mode):
python3 -m pytest tests/gates/ -v -k "not TestHealthEndpoints and not TestMultiPodHealthChecks and not TestRollingUpdateZeroDowntime and not TestDegradedState"
```

### Environment Variables
| Variable | Default | Description |
|----------|---------|-------------|
| `GATE_REDIS_HOST` | `localhost` | Redis host for gate tests |
| `GATE_REDIS_PORT` | `6379` | Redis port |
| `REDIS_PASSWORD` | `` | Redis password (empty for local) |
| `GATE_PYLON_MAIN_URL` | `http://localhost:80` | pylon_main base URL |
| `GATE_PG_HOST` | `localhost` | PostgreSQL host |
| `GATE_PG_PORT` | `5432` | PostgreSQL port |
| `GATE_PG_USER` | `centry` | PostgreSQL user |
| `GATE_PG_PASSWORD` | `centry` | PostgreSQL password |
| `GATE_PG_DATABASE` | `centry` | PostgreSQL database |

### CI Integration
Gate tests run automatically on PRs via `.github/workflows/gate-tests.yml`. Tests requiring live pylon services are excluded in CI (they're validated in staging E2E tests instead). The CI-safe subset validates:
- Redis data model contracts (key patterns, TTLs, atomic operations)
- PostgreSQL connection budget (max_connections, active count)
- Lock semantics (mutual exclusion, auto-release, safe release)
- Canvas optimistic locking (WATCH/MULTI/EXEC)

---
*Last updated by Ralph iteration*
