#!/usr/bin/env bash
set -euo pipefail

MANIFEST_PATH="${MANIFEST_PATH:-/config/manifest.json}"
CACHE_DIR="${CACHE_DIR:-/cache}"
MAX_RETRIES="${MAX_RETRIES:-3}"
VERIFY_ONLY="${VERIFY_ONLY:-false}"

# Parse CLI flags
for arg in "$@"; do
    case "$arg" in
        --verify-only)
            VERIFY_ONLY="true"
            ;;
        --manifest=*)
            MANIFEST_PATH="${arg#*=}"
            ;;
        --cache-dir=*)
            CACHE_DIR="${arg#*=}"
            ;;
    esac
done

log() {
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $1"
}

die() {
    log "ERROR: $1"
    exit 1
}

# Validate MD5 of a file against expected checksum.
# Returns 0 if valid, 1 if mismatch, 2 if no MD5 to check.
validate_md5() {
    local file_path="$1"
    local expected_md5="$2"
    local file_name="$3"

    if [ "$expected_md5" = "null" ] || [ -z "$expected_md5" ]; then
        return 2
    fi

    local actual_md5
    actual_md5=$(md5sum "$file_path" | awk '{print $1}')

    if [ "$actual_md5" = "$expected_md5" ]; then
        return 0
    else
        log "  MD5 MISMATCH: file=$file_name expected=$expected_md5 actual=$actual_md5"
        return 1
    fi
}

if [ ! -f "$MANIFEST_PATH" ]; then
    die "Manifest not found at $MANIFEST_PATH"
fi

if ! jq empty "$MANIFEST_PATH" 2>/dev/null; then
    die "Manifest is not valid JSON: $MANIFEST_PATH"
fi

mkdir -p "$CACHE_DIR"

# Record start time for duration metric
SYNC_START_TIME=$(date +%s)

# --- Cache versioning: compare manifest version with cached version ---
MANIFEST_VERSION=$(jq -r '.version // empty' "$MANIFEST_PATH")
VERSION_FILE="${CACHE_DIR}/.manifest-version"

if [ -n "$MANIFEST_VERSION" ] && [ "$VERIFY_ONLY" = "false" ]; then
    if [ -f "$VERSION_FILE" ]; then
        CACHED_VERSION=$(cat "$VERSION_FILE")
        if [ "$CACHED_VERSION" != "$MANIFEST_VERSION" ]; then
            log "Cache version mismatch: cached=$CACHED_VERSION manifest=$MANIFEST_VERSION"
            log "Clearing cache for clean re-download..."
            find "$CACHE_DIR" -mindepth 1 -not -name '.manifest-version' -delete 2>/dev/null || true
        else
            log "Cache version matches: $MANIFEST_VERSION (incremental sync)"
        fi
    else
        log "No cached version found, performing full download (version=$MANIFEST_VERSION)"
    fi
fi

TOTAL=$(jq '.models | length' "$MANIFEST_PATH")
DOWNLOADED=0
SKIPPED=0
FAILED=0
VALID=0
INVALID=0

log "Starting model cache sync: $TOTAL files to process"
log "Cache directory: $CACHE_DIR"
log "Verify only: $VERIFY_ONLY"

if [ "$TOTAL" -eq 0 ]; then
    log "No models in manifest, nothing to do"
    log "Model cache sync complete"
    exit 0
fi

for i in $(seq 0 $((TOTAL - 1))); do
    NAME=$(jq -r ".models[$i].name" "$MANIFEST_PATH")
    URL=$(jq -r ".models[$i].url" "$MANIFEST_PATH")
    PATH_REL=$(jq -r ".models[$i].path" "$MANIFEST_PATH")
    EXPECTED_MD5=$(jq -r ".models[$i].md5" "$MANIFEST_PATH")
    SIZE_MB=$(jq -r ".models[$i].size_mb // \"unknown\"" "$MANIFEST_PATH")

    TARGET_PATH="${CACHE_DIR}/${PATH_REL}"
    TARGET_DIR=$(dirname "$TARGET_PATH")

    log "[$((i+1))/$TOTAL] Processing: $NAME (${SIZE_MB}MB)"

    # --- Verify-only mode: check file presence and integrity, never download/delete ---
    if [ "$VERIFY_ONLY" = "true" ]; then
        if [ ! -f "$TARGET_PATH" ]; then
            log "  MISSING: file=$PATH_REL (not found in cache)"
            FAILED=$((FAILED + 1))
        else
            validate_md5 "$TARGET_PATH" "$EXPECTED_MD5" "$PATH_REL" && md5_rc=0 || md5_rc=$?
            if [ "$md5_rc" -eq 0 ]; then
                log "  VALID: file=$PATH_REL MD5 matches"
                VALID=$((VALID + 1))
            elif [ "$md5_rc" -eq 2 ]; then
                log "  VALID: file=$PATH_REL (no MD5 configured, file exists)"
                VALID=$((VALID + 1))
            else
                INVALID=$((INVALID + 1))
                FAILED=$((FAILED + 1))
            fi
        fi
        continue
    fi

    # --- Normal mode: check cache, download if needed ---
    if [ -f "$TARGET_PATH" ]; then
        validate_md5 "$TARGET_PATH" "$EXPECTED_MD5" "$PATH_REL" && md5_rc=0 || md5_rc=$?
        if [ "$md5_rc" -eq 0 ]; then
            log "  SKIP: file=$PATH_REL already cached with correct MD5"
            SKIPPED=$((SKIPPED + 1))
            continue
        elif [ "$md5_rc" -eq 2 ]; then
            log "  SKIP: file=$PATH_REL exists (no MD5 to verify)"
            SKIPPED=$((SKIPPED + 1))
            continue
        else
            log "  Deleting corrupt file: $PATH_REL"
            rm -f "$TARGET_PATH"
        fi
    fi

    mkdir -p "$TARGET_DIR"

    DOWNLOAD_SUCCESS=false
    for attempt in $(seq 1 "$MAX_RETRIES"); do
        log "  Downloading (attempt $attempt/$MAX_RETRIES)..."

        DOWNLOAD_CMD=""
        case "$URL" in
            s3://*)
                DOWNLOAD_CMD="aws s3 cp \"$URL\" \"$TARGET_PATH\" --no-progress"
                ;;
            http://*|https://*)
                DOWNLOAD_CMD="wget -q -O \"$TARGET_PATH\" \"$URL\""
                ;;
            *)
                log "  ERROR: Unsupported URL scheme: $URL"
                break
                ;;
        esac

        if eval "$DOWNLOAD_CMD" 2>/dev/null; then
            validate_md5 "$TARGET_PATH" "$EXPECTED_MD5" "$PATH_REL" && md5_rc=0 || md5_rc=$?
            if [ "$md5_rc" -eq 0 ] || [ "$md5_rc" -eq 2 ]; then
                DOWNLOAD_SUCCESS=true
                break
            else
                log "  Deleting corrupt download: $PATH_REL"
                rm -f "$TARGET_PATH"
            fi
        else
            log "  Download failed for: $PATH_REL"
            rm -f "$TARGET_PATH"
        fi

        if [ "$attempt" -lt "$MAX_RETRIES" ]; then
            SLEEP_TIME=$((attempt * 2))
            log "  Retrying in ${SLEEP_TIME}s..."
            sleep "$SLEEP_TIME"
        fi
    done

    if [ "$DOWNLOAD_SUCCESS" = "true" ]; then
        DOWNLOADED=$((DOWNLOADED + 1))
        log "  OK: downloaded successfully file=$PATH_REL"

        EXTRACT=$(jq -r ".models[$i].extract // false" "$MANIFEST_PATH")
        EXTRACT_TARGET=$(jq -r ".models[$i].extract_target // empty" "$MANIFEST_PATH")

        if [ "$EXTRACT" = "true" ] && [ -n "$EXTRACT_TARGET" ]; then
            EXTRACT_DIR="${CACHE_DIR}/${EXTRACT_TARGET}"
            mkdir -p "$EXTRACT_DIR"
            log "  Extracting to: $EXTRACT_DIR"
            if tar -xzf "$TARGET_PATH" -C "$EXTRACT_DIR" 2>/dev/null; then
                log "  OK: extracted successfully"
                rm -f "$TARGET_PATH"
            else
                log "  WARNING: extraction failed, keeping archive"
            fi
        fi
    else
        FAILED=$((FAILED + 1))
        log "  FAILED: file=$PATH_REL all $MAX_RETRIES attempts exhausted"
    fi
done

log "---"
if [ "$VERIFY_ONLY" = "true" ]; then
    log "Verification summary: total=$TOTAL valid=$VALID invalid=$INVALID missing=$((FAILED - INVALID))"
else
    log "Summary: total=$TOTAL downloaded=$DOWNLOADED skipped=$SKIPPED failed=$FAILED"
fi

if [ "$FAILED" -gt 0 ]; then
    if [ "$VERIFY_ONLY" = "true" ]; then
        die "Verification failed: $FAILED files missing or corrupt"
    else
        die "Failed to download $FAILED files"
    fi
fi

# Write manifest version to cache after successful sync
if [ -n "$MANIFEST_VERSION" ] && [ "$VERIFY_ONLY" = "false" ]; then
    echo "$MANIFEST_VERSION" > "$VERSION_FILE"
    log "Wrote cache version: $MANIFEST_VERSION"
fi

# Write Prometheus metrics
SYNC_END_TIME=$(date +%s)
SYNC_DURATION=$((SYNC_END_TIME - SYNC_START_TIME))
METRICS_SCRIPT="$(dirname "$0")/metrics.sh"
if [ -f "$METRICS_SCRIPT" ]; then
    bash "$METRICS_SCRIPT" "$SYNC_DURATION" "$DOWNLOADED" "$SKIPPED" "$FAILED" "$TOTAL"
fi

log "Model cache sync complete"
exit 0
