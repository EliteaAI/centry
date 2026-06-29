# Horizontal Scaling Implementation Tasks

## Phase 1: Stateless Foundation (Weeks 1-3)

### Week 1: Core State Externalization

- [ ] 1.1 Add Socket.IO Redis Adapter to pylon_main
  - [ ] Add `socketio.redis` section to `centry/pylon_main/configs/pylon.yml`
  - [ ] Add `python-socketio[asyncio_client]` to requirements if not present
  - [ ] Verify RedisManager is activated on pylon_main startup
  - [ ] Test: emit event from one process, verify delivery via Redis pub/sub
  - [ ] Document configuration in AGENT.md

- [ ] 1.2 Implement RedisServersStorage for MCP state
  - [ ] Create `centry/pylon_main/plugins/elitea_core/utils/redis_servers_storage.py`
  - [ ] Implement `get_server_state(project_id, server_name)` → dict
  - [ ] Implement `set_server_state(project_id, server_name, state)` with TTL (1h)
  - [ ] Implement `delete_server_state(project_id, server_name)`
  - [ ] Implement `list_active_servers(project_id)` → list
  - [ ] Write unit tests (≥85% coverage)

- [ ] 1.3 Externalize ASR session state to Redis
  - [ ] Modify `centry/pylon_main/plugins/elitea_core/sio/asr.py`
  - [ ] Store ASR buffer chunks in Redis list (key: `asr_session:{sid}`)
  - [ ] Store ASR config/state in Redis hash
  - [ ] Add TTL (5 minutes) for abandoned sessions
  - [ ] Implement session recovery on reconnect
  - [ ] Write unit tests (≥85% coverage)

- [ ] 1.4 Move callback_tasks dict to Redis
  - [ ] Create `centry/pylon_main/plugins/elitea_core/utils/callback_manager.py`
  - [ ] Replace in-memory dict with Redis hash (key: `callback_tasks:{task_id}`)
  - [ ] Add TTL (24h) for stale callbacks
  - [ ] Implement `register_callback(task_id, callback_info)`
  - [ ] Implement `get_callback(task_id)` and `remove_callback(task_id)`
  - [ ] Write unit tests (≥85% coverage)

- [ ] 1.5 Move task_logs cache to Redis
  - [ ] Create `centry/pylon_main/plugins/elitea_core/utils/task_logs_redis.py`
  - [ ] Replace in-memory cache with Redis sorted set (key: `task_logs:{task_id}`)
  - [ ] Add TTL (7 days) for old logs
  - [ ] Implement append, get_latest, clear operations
  - [ ] Write unit tests (≥85% coverage)

- [ ] 1.6 Implement user icons storage in S3
  - [ ] Create `centry/pylon_main/plugins/elitea_core/utils/icon_storage.py`
  - [ ] Use existing `storage_libcloud` driver from shared config
  - [ ] Implement `upload_icon(project_id, icon_data, filename)` → URL
  - [ ] Implement `get_icon_url(project_id, filename)` → presigned URL
  - [ ] Implement `delete_icon(project_id, filename)`
  - [ ] Write unit tests (≥85% coverage)

### Week 1-2: Infrastructure Configuration

- [ ] 1.7 Convert /tmp PVC to emptyDir in staging
  - [ ] Update staging pylon-main values: add emptyDir volume with 10Gi sizeLimit
  - [ ] Update staging pylon-indexer values: add emptyDir volume with 20Gi sizeLimit
  - [ ] Verify no persistent data written to /tmp that needs survival

- [ ] 1.8 Reduce database connection pools
  - [ ] pylon-auth staging: pool_size=10, max_overflow=5
  - [ ] pylon-main staging: pool_size=15, max_overflow=10
  - [ ] pylon-indexer staging: pool_size=10, max_overflow=5
  - [ ] Add pool_pre_ping=true to all
  - [ ] Document max connections math: 2×10 + 3×15 + 3×10 = 95 < 200

- [ ] 1.9 Implement migration lock with timeout
  - [ ] Create `centry/pylon_main/plugins/elitea_core/utils/migration_lock.py`
  - [ ] Use `pg_try_advisory_lock` with a fixed lock ID
  - [ ] Add 10-minute timeout for lock acquisition
  - [ ] Implement explicit unlock on completion
  - [ ] Log lock acquisition/release events
  - [ ] Write unit tests (≥85% coverage)

- [ ] 1.10 Add feature flags module
  - [ ] Create `centry/pylon_main/plugins/elitea_core/utils/feature_flags.py`
  - [ ] Implement `is_enabled(flag_name)` checking Redis and env vars
  - [ ] Define flags: REDIS_STATE_ENABLED, SOCKETIO_REDIS_ENABLED, REDIS_STREAMS_ENABLED
  - [ ] Support per-project override via Redis key
  - [ ] Write unit tests (≥85% coverage)

### Week 2: Health & Lifecycle

- [ ] 1.11 Implement /health/live and /health/ready endpoints
  - [ ] Create `centry/pylon_main/plugins/elitea_core/routes/health.py`
  - [ ] /health/live: check Redis ping, PostgreSQL SELECT 1
  - [ ] /health/ready: check all plugins initialized, check startup complete
  - [ ] Return JSON: `{"status": "ok"|"degraded"|"unhealthy", "checks": {...}}`
  - [ ] Add response time to each check
  - [ ] Write unit tests (≥85% coverage)

- [ ] 1.12 Configure graceful shutdown (preStop hooks)
  - [ ] Add preStop lifecycle hook: `exec: command: ["sh", "-c", "sleep 15"]`
  - [ ] Implement SIGTERM handler in pylon_main to drain connections
  - [ ] Close Socket.IO connections gracefully (send disconnect event)
  - [ ] Flush pending Redis operations
  - [ ] Log shutdown progress

- [ ] 1.13 Set terminationGracePeriodSeconds
  - [ ] pylon-main: 60s (WebSocket connections need time to drain)
  - [ ] pylon-indexer: 120s (long-running tasks need time to complete)
  - [ ] pylon-auth: 30s (stateless, quick shutdown)

- [ ] 1.14 Configure liveness/readiness probes
  - [ ] Liveness: GET /health/live, initialDelaySeconds=30, period=10, timeout=5
  - [ ] Readiness: GET /health/ready, initialDelaySeconds=15, period=5, timeout=3
  - [ ] Startup: GET /health/live, failureThreshold=30, period=10

### Week 2-3: Frontend & Testing

- [ ] 1.15 Update Socket.IO client with auto-reconnect
  - [ ] Find Socket.IO client initialization in EliteaUI
  - [ ] Configure: reconnection=true, reconnectionDelay=1000, reconnectionDelayMax=5000
  - [ ] Add reconnection attempt counter (max 10)
  - [ ] Add exponential backoff
  - [ ] Emit 'reconnected' event for UI update

- [ ] 1.16 Add connection state indicator to UI
  - [ ] Create ConnectionStatus component (React)
  - [ ] Show: connected (green), reconnecting (yellow), disconnected (red)
  - [ ] Position: bottom-right corner, non-intrusive
  - [ ] Auto-hide after 3s when connected
  - [ ] Show reconnection attempt count

- [ ] 1.17 Create staging ArgoCD overlay
  - [ ] Create values/staging/pylon-main.yaml (3 replicas, OIDC mock, Redis adapter)
  - [ ] Create values/staging/pylon-auth.yaml (2 replicas, OIDC mock)
  - [ ] Create values/staging/pylon-indexer.yaml (3 replicas, emptyDir)
  - [ ] Create apps/staging/ directory with ArgoCD Application definitions
  - [ ] Create manifests/staging/ with namespace and HTTPRoutes
  - [ ] Create staging-platform.yaml app-of-apps

### Week 3: E2E Testing

- [ ] 1.18 Set up Playwright test framework
  - [ ] Create `centry/tests/e2e/` directory structure
  - [ ] Write package.json with @playwright/test, @kubernetes/client-node, socket.io-client
  - [ ] Write playwright.config.ts (sequential, single worker, chromium+firefox)
  - [ ] Write tsconfig.json
  - [ ] Write .env.staging with BASE_URL and K8S_NAMESPACE

- [ ] 1.19 Write E2E scaling test specs
  - [ ] health-checks.spec.ts: verify /health/live and /health/ready for all pods
  - [ ] oidc-login.spec.ts: complete OIDC mock login flow
  - [ ] socket-io-scaling.spec.ts: cross-pod message delivery test
  - [ ] session-persistence.spec.ts: session survives pod restart
  - [ ] connection-resilience.spec.ts: auto-reconnect within 5s

- [ ] 1.20 Achieve 85% test coverage for E2E utilities
  - [ ] Write unit tests for utils/kubernetes.ts
  - [ ] Write unit tests for utils/api-client.ts
  - [ ] Write unit tests for utils/socket-client.ts
  - [ ] Write unit tests for pages/LoginPage.ts
  - [ ] Configure vitest with coverage reporter
  - [ ] Verify coverage ≥ 85%

---

## Phase 2: Session & Task State (Weeks 4-6)

- [ ] 2.1 Move auth_core sessions to Redis
- [ ] 2.2 Configure secure session cookies
- [ ] 2.3 Externalize toolkit_schemas to Redis
- [ ] 2.4 Externalize index_types to Redis
- [ ] 2.5 Externalize mcp_prebuilt_configs to Redis
- [ ] 2.6 Externalize provider health state to Redis
- [ ] 2.7 Change TaskNode result_transport to Redis
- [ ] 2.8 Implement startup state reconstruction
- [ ] 2.9 Add distributed lock library (Redlock)
- [ ] 2.10 Wrap conversation creation in lock
- [ ] 2.11 Implement Canvas version atomicity (MULTI/EXEC)
- [ ] 2.12 Change task claiming to SKIP LOCKED
- [ ] 2.13 Add disconnect cleanup via pub/sub

## Phase 3: Storage Optimization (Weeks 4-6, parallel with Phase 2)

- [ ] 3.1 Create model-cache init container image
- [ ] 3.2 Implement model cache manifest (JSON)
- [ ] 3.3 Add init container to pylon_indexer
- [ ] 3.4 Configure emptyDir for model caches (60Gi)
- [ ] 3.5 Add cache validation (MD5 checksums)
- [ ] 3.6 Implement cache versioning
- [ ] 3.7 Add cache metrics
- [ ] 3.8 Optimize /tmp size based on profiling
- [ ] 3.9 Add /tmp usage monitoring and cleanup
- [ ] 3.10 Document storage architecture

## Phase 4: Event System Hardening (Weeks 7-10)

- [ ] 4.1 Audit all Redis pub/sub event handlers
- [ ] 4.2 Classify events: broadcast vs work vs notification
- [ ] 4.3 Implement Redis Streams for work events
- [ ] 4.4 Migrate task distribution to Streams
- [ ] 4.5 Add event deduplication (SETNX pattern)
- [ ] 4.6 Implement idempotency keys
- [ ] 4.7 Implement dead letter queue
- [ ] 4.8 Add event replay capability
- [ ] 4.9 Implement event handler timeout
- [ ] 4.10 Add event metrics
- [ ] 4.11 Implement distributed cron (leader election)
- [ ] 4.12 Add event schema registry
- [ ] 4.13 Configure Streams retention (MAXLEN)
- [ ] 4.14 Add Streams monitoring

## Phase 5: Infrastructure Scaling (Weeks 11-14)

- [ ] 5.1 Deploy PgBouncer (session pooling)
- [ ] 5.2 Configure PgBouncer pools
- [ ] 5.3 Update services to use PgBouncer
- [ ] 5.4 Increase PostgreSQL max_connections
- [ ] 5.5 Deploy Redis Sentinel (3 nodes)
- [ ] 5.6 Configure Redis persistence (AOF + RDB)
- [ ] 5.7 Update services for Sentinel URLs
- [ ] 5.8 Add Redis backup to S3
- [ ] 5.9 Implement HPA (CPU target 70%)
- [ ] 5.10 Add custom HPA metrics
- [ ] 5.11 Configure resource requests/limits
- [ ] 5.12 Implement PodDisruptionBudget
- [ ] 5.13 Add node affinity (spread across AZs)
- [ ] 5.14 Deploy Prometheus + Grafana
- [ ] 5.15 Define SLOs and alerts
- [ ] 5.16 Create runbooks
- [ ] 5.17 Implement synthetic monitoring
- [ ] 5.18 Add chaos testing suite
- [ ] 5.19 Configure log aggregation
- [ ] 5.20 Implement distributed tracing

## Phase 6: Production Hardening (Weeks 15-17)

- [ ] 6.1 Enable Redis AUTH and TLS
- [ ] 6.2 Implement Network Policies
- [ ] 6.3 Migrate to Kubernetes Secrets
- [ ] 6.4 Add session cookie security flags
- [ ] 6.5 Implement audit logging
- [ ] 6.6 Add volume security (fsGroup, permissions)
- [ ] 6.7 SDK version compatibility testing
- [ ] 6.8 UI client resilience testing
- [ ] 6.9 Document dynamic webhook IPs
- [ ] 6.10 Implement global API rate limiting
- [ ] 6.11 Add feature flags for all changes
- [ ] 6.12 Document per-phase rollback procedures
- [ ] 6.13 Add validation gates (automated tests)
- [ ] 6.14 Implement Redis backup/restore testing
- [ ] 6.15 Implement Canvas auto-save (5-min interval)
- [ ] 6.16 Add disaster recovery plan
- [ ] 6.17 Implement PostgreSQL backup strategy
- [ ] 6.18 Add data consistency checks
- [ ] 6.19 Document operational procedures
- [ ] 6.20 Create incident response playbook
