# SDK Compatibility with Horizontal Scaling Backend

## Summary

**No breaking changes.** The horizontal scaling changes are fully server-side
transparent. SDK v0.7.0+ continues to work without any modifications.

## Minimum SDK Version

| SDK Version | Compatible | Notes |
|-------------|-----------|-------|
| < 0.7.0 | No | May use /api/v1 paths (deprecated) |
| 0.7.0+ | Yes | Uses /api/v2 paths, stateless HTTP |
| Current (0.7.53) | Yes | Fully tested |

## Why It's Transparent

The SDK communicates with the backend exclusively via **stateless HTTP REST APIs**:

1. **No WebSocket/Socket.IO** — SDK uses `requests.get/post` directly
2. **Bearer token auth** — No server-side session affinity needed
3. **No cookies** — SDK doesn't parse or store Set-Cookie headers
4. **Self-contained requests** — Each request carries full auth context

## New Server Behaviors (Non-Breaking)

| Change | SDK Impact | Action Required |
|--------|-----------|-----------------|
| Redis session state | None (uses Bearer tokens) | None |
| Socket.IO Redis adapter | None (SDK doesn't use Socket.IO) | None |
| Distributed locks | May receive 409 Conflict | Handle gracefully |
| PgBouncer pooling | None (same wire protocol) | None |
| Redis Streams | None (internal event system) | None |
| Rate limiting (6.10) | May receive 429 Too Many Requests | Handle gracefully |

## New HTTP Response Codes to Handle

### 409 Conflict (Distributed Lock Contention)

When two SDK clients create the same conversation simultaneously:

```
HTTP 409 Conflict
Retry-After: 1
{"error": "Conflict", "message": "Resource locked, retry after 1s"}
```

**Recommended handling:** Retry after the indicated delay.

### 429 Too Many Requests (Rate Limiting)

When the global rate limit is exceeded:

```
HTTP 429 Too Many Requests
Retry-After: 60
{"error": "Rate limit exceeded", "retry_after": 60}
```

**Recommended handling:** Implement exponential backoff.

## Test Evidence

60 compatibility tests verify:
- API endpoint format unchanged (/api/v2/*)
- Authentication mechanism unchanged (Bearer token)
- No session/cookie dependencies
- All public methods preserved
- Error handling for new response codes
- Multi-instance safety (no global mutable state)

Run tests:
```bash
python3 -m pytest centry/tests/compat/test_sdk_versions.py -v
```

## Migration Guide

**No migration needed.** The SDK works as-is against the horizontally-scaled backend.

For SDK maintainers considering future changes:
- Do NOT add cookie-based session management
- Do NOT add WebSocket/Socket.IO client dependencies
- Keep using `requests.get/post` (not `requests.Session`)
- Continue sending all auth context per-request (Bearer token + X-SECRET)
