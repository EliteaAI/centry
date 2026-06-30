#!/usr/bin/env bash
# validate-manifest.sh — Validate the model cache manifest
#
# Checks:
#   1. JSON is valid
#   2. Required fields are present
#   3. URLs are reachable (HEAD request for http/https, aws s3 ls for s3://)
#
# Usage:
#   ./validate-manifest.sh [manifest.json]
#
# Exit codes:
#   0 — all checks pass
#   1 — validation failure

set -euo pipefail

MANIFEST="${1:-manifest.json}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

log_ok()   { echo -e "${GREEN}✓${NC} $1"; }
log_warn() { echo -e "${YELLOW}!${NC} $1"; }
log_fail() { echo -e "${RED}✗${NC} $1"; }

ERRORS=0
WARNINGS=0

# --- Check 1: File exists ---
if [ ! -f "$MANIFEST" ]; then
    log_fail "Manifest file not found: $MANIFEST"
    exit 1
fi
log_ok "Manifest file exists: $MANIFEST"

# --- Check 2: Valid JSON ---
if ! jq empty "$MANIFEST" 2>/dev/null; then
    log_fail "Invalid JSON in $MANIFEST"
    exit 1
fi
log_ok "Valid JSON"

# --- Check 3: Required top-level fields ---
VERSION=$(jq -r '.version // empty' "$MANIFEST")
if [ -z "$VERSION" ]; then
    log_fail "Missing required field: version"
    ERRORS=$((ERRORS + 1))
else
    log_ok "Version: $VERSION"
fi

MODELS_COUNT=$(jq '.models | length' "$MANIFEST")
if [ "$MODELS_COUNT" -eq 0 ]; then
    log_fail "Models array is empty"
    ERRORS=$((ERRORS + 1))
else
    log_ok "Models count: $MODELS_COUNT"
fi

# --- Check 4: Per-model validation ---
for i in $(seq 0 $((MODELS_COUNT - 1))); do
    NAME=$(jq -r ".models[$i].name" "$MANIFEST")
    URL=$(jq -r ".models[$i].url" "$MANIFEST")
    PATH_REL=$(jq -r ".models[$i].path" "$MANIFEST")
    MD5=$(jq -r ".models[$i].md5" "$MANIFEST")
    EXTRACT=$(jq -r ".models[$i].extract // false" "$MANIFEST")
    EXTRACT_TARGET=$(jq -r ".models[$i].extract_target // empty" "$MANIFEST")

    echo ""
    echo "--- [$((i+1))/$MODELS_COUNT] $NAME ---"

    # Required fields
    if [ -z "$NAME" ] || [ "$NAME" = "null" ]; then
        log_fail "  Missing 'name'"
        ERRORS=$((ERRORS + 1))
    fi

    if [ -z "$URL" ] || [ "$URL" = "null" ]; then
        log_fail "  Missing 'url'"
        ERRORS=$((ERRORS + 1))
        continue
    fi

    if [ -z "$PATH_REL" ] || [ "$PATH_REL" = "null" ]; then
        log_fail "  Missing 'path'"
        ERRORS=$((ERRORS + 1))
    fi

    # extract_target required when extract=true
    if [ "$EXTRACT" = "true" ] && [ -z "$EXTRACT_TARGET" ]; then
        log_fail "  extract=true but missing 'extract_target'"
        ERRORS=$((ERRORS + 1))
    fi

    # MD5 format check
    if [ "$MD5" != "null" ] && [ -n "$MD5" ]; then
        if echo "$MD5" | grep -qE '^[a-f0-9]{32}$'; then
            log_ok "  MD5: $MD5"
        else
            log_fail "  Invalid MD5 format: $MD5"
            ERRORS=$((ERRORS + 1))
        fi
    else
        log_warn "  No MD5 checksum (cannot verify integrity)"
        WARNINGS=$((WARNINGS + 1))
    fi

    # URL reachability check
    case "$URL" in
        s3://*)
            if command -v aws &>/dev/null; then
                BUCKET=$(echo "$URL" | sed 's|s3://||' | cut -d'/' -f1)
                KEY=$(echo "$URL" | sed "s|s3://${BUCKET}/||")
                if aws s3 ls "s3://${BUCKET}/${KEY}" --no-sign-request 2>/dev/null || \
                   aws s3 ls "s3://${BUCKET}/${KEY}" 2>/dev/null; then
                    log_ok "  URL reachable: $URL"
                else
                    log_warn "  URL not reachable (may need auth): $URL"
                    WARNINGS=$((WARNINGS + 1))
                fi
            else
                log_warn "  Cannot check S3 URL (aws CLI not installed)"
                WARNINGS=$((WARNINGS + 1))
            fi
            ;;
        http://*|https://*)
            if command -v curl &>/dev/null; then
                HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --head --max-time 10 "$URL" 2>/dev/null || echo "000")
                if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 400 ]; then
                    log_ok "  URL reachable: $URL (HTTP $HTTP_CODE)"
                elif [ "$HTTP_CODE" = "000" ]; then
                    log_warn "  URL unreachable (timeout/DNS): $URL"
                    WARNINGS=$((WARNINGS + 1))
                else
                    log_warn "  URL returned HTTP $HTTP_CODE: $URL"
                    WARNINGS=$((WARNINGS + 1))
                fi
            else
                log_warn "  Cannot check HTTP URL (curl not installed)"
                WARNINGS=$((WARNINGS + 1))
            fi
            ;;
        *)
            log_fail "  Unsupported URL scheme: $URL"
            ERRORS=$((ERRORS + 1))
            ;;
    esac
done

# --- Summary ---
echo ""
echo "=== Validation Summary ==="
echo "  Entries:  $MODELS_COUNT"
echo "  Errors:   $ERRORS"
echo "  Warnings: $WARNINGS"

if [ "$ERRORS" -gt 0 ]; then
    log_fail "Validation FAILED with $ERRORS error(s)"
    exit 1
fi

if [ "$WARNINGS" -gt 0 ]; then
    log_warn "Validation passed with $WARNINGS warning(s)"
else
    log_ok "Validation PASSED"
fi

exit 0
