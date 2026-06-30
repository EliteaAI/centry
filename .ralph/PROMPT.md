# Elitea Horizontal Scaling - Phase 1: Stateless Foundation

## Goal

Implement Phase 1 of horizontal scaling: eliminate all in-memory state from pylon services so they can scale horizontally without sticky sessions.

## Specifications

Read @HORIZONTAL_SCALING_PLAN_V2.md for the complete scaling plan.

## Current Task

Check @.ralph/ralph-tasks.md and find the next uncompleted task (`- [ ]`).

## Instructions

1. Read the task and all its subtasks
2. Study the relevant source code before making changes
3. Implement all subtasks within the current top-level task
4. Write unit tests achieving ≥85% coverage for new code
5. Run validation: `python .ralph/validate.py --phase phase-1`
6. Mark completed subtasks with `[x]` in ralph-tasks.md
7. Commit with conventional commit format: `feat(scaling): description`

## Key Repositories

| Repository | Path | Branch |
|-----------|------|--------|
| centry | `centry/` | feature/horizontal-scaling-phase-1 |
| elitea_core | `centry/pylon_main/plugins/elitea_core/` | feature/horizontal-scaling-phase-1 |
| ArgoCD | `../kharkevich/argocd-public/elitea-platform/` | feature/horizontal-scaling-staging |
| EliteaUI | `EliteaUI/` | feature/horizontal-scaling-e2e |

## Architecture Constraints

- **Redis** (Valkey): host=`redis`, port=6379, no auth (local dev)
- **PostgreSQL** (pgvector): host=`postgres`, port=5432, user=`centry`, db=`centry`
- **S3** (RustFS): host=`rustfs`, port=9000, access_key from env
- **Socket.IO**: python-socketio 5.x, RedisManager available in pylon framework
- **Staging hostname**: `elitea-staging.technicaldomain.xyz`
- **OIDC mock**: `https://oidc-mock.technicaldomain.xyz/`
- **Pylon chart**: `oci://ghcr.io/eliteaai/charts/pylon` v1.0.6

## Patterns

### Redis State Pattern
```python
from tools import redis_tools

def get_state(key: str) -> dict:
    client = redis_tools.get_client()
    data = client.hgetall(f"prefix:{key}")
    return {k.decode(): v.decode() for k, v in data.items()}

def set_state(key: str, state: dict, ttl: int = 3600):
    client = redis_tools.get_client()
    pipe = client.pipeline()
    pipe.hset(f"prefix:{key}", mapping=state)
    pipe.expire(f"prefix:{key}", ttl)
    pipe.execute()
```

### Health Check Pattern
```python
from pylon.core.tools import web

@web.route("/health/live")
def health_live():
    checks = {}
    checks["redis"] = check_redis()
    checks["postgres"] = check_postgres()
    status = "ok" if all(c["status"] == "ok" for c in checks.values()) else "unhealthy"
    return {"status": status, "checks": checks}, 200 if status == "ok" else 503
```

### Feature Flag Pattern
```python
import os
from tools import redis_tools

def is_enabled(flag: str, project_id: str = None) -> bool:
    # Check env var first (override)
    env_val = os.environ.get(f"FF_{flag}")
    if env_val is not None:
        return env_val.lower() in ("1", "true", "yes")
    # Check Redis
    client = redis_tools.get_client()
    if project_id:
        val = client.get(f"feature_flags:{project_id}:{flag}")
        if val is not None:
            return val.decode() == "1"
    val = client.get(f"feature_flags:global:{flag}")
    return val is not None and val.decode() == "1"
```

## Quality Requirements

- 85% test coverage for all new Python modules
- TypeScript for any frontend changes
- Conventional commits: `feat(scaling):`, `test(scaling):`, `fix(scaling):`
- No hardcoded secrets
- All Redis keys must have TTL (prevent memory leaks)

## Completion Signals

When current top-level task is complete: <promise>READY_FOR_NEXT_TASK</promise>
When ALL Phase 1 tasks are complete: <promise>COMPLETE</promise>
