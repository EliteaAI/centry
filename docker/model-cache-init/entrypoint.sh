#!/usr/bin/env bash
set -euo pipefail

MANIFEST_PATH="${MANIFEST_PATH:-/config/manifest.json}"
CACHE_DIR="${CACHE_DIR:-/cache}"
MAX_RETRIES="${MAX_RETRIES:-3}"
VERIFY_ONLY="${VERIFY_ONLY:-false}"

log() {
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $1"
}

die() {
    log "ERROR: $1"
    exit 1
}

if [ ! -f "$MANIFEST_PATH" ]; then
    die "Manifest not found at $MANIFEST_PATH"
fi

if ! jq empty "$MANIFEST_PATH" 2>/dev/null; then
    die "Manifest is not valid JSON: $MANIFEST_PATH"
fi

mkdir -p "$CACHE_DIR"

TOTAL=$(jq '.models | length' "$MANIFEST_PATH")
DOWNLOADED=0
SKIPPED=0
FAILED=0

log "Starting model cache sync: $TOTAL files to process"
log "Cache directory: $CACHE_DIR"
log "Verify only: $VERIFY_ONLY"

for i in $(seq 0 $((TOTAL - 1))); do
    NAME=$(jq -r ".models[$i].name" "$MANIFEST_PATH")
    URL=$(jq -r ".models[$i].url" "$MANIFEST_PATH")
    PATH_REL=$(jq -r ".models[$i].path" "$MANIFEST_PATH")
    EXPECTED_MD5=$(jq -r ".models[$i].md5" "$MANIFEST_PATH")
    SIZE_MB=$(jq -r ".models[$i].size_mb // \"unknown\"" "$MANIFEST_PATH")

    TARGET_PATH="${CACHE_DIR}/${PATH_REL}"
    TARGET_DIR=$(dirname "$TARGET_PATH")

    log "[$((i+1))/$TOTAL] Processing: $NAME (${SIZE_MB}MB)"

    if [ -f "$TARGET_PATH" ]; then
        if [ "$EXPECTED_MD5" != "null" ] && [ -n "$EXPECTED_MD5" ]; then
            ACTUAL_MD5=$(md5sum "$TARGET_PATH" | awk '{print $1}')
            if [ "$ACTUAL_MD5" = "$EXPECTED_MD5" ]; then
                log "  SKIP: already cached with correct MD5"
                SKIPPED=$((SKIPPED + 1))
                continue
            else
                log "  MISMATCH: expected=$EXPECTED_MD5 actual=$ACTUAL_MD5"
                rm -f "$TARGET_PATH"
            fi
        else
            log "  SKIP: file exists (no MD5 to verify)"
            SKIPPED=$((SKIPPED + 1))
            continue
        fi
    fi

    if [ "$VERIFY_ONLY" = "true" ]; then
        log "  MISSING: $TARGET_PATH (verify-only mode, not downloading)"
        FAILED=$((FAILED + 1))
        continue
    fi

    mkdir -p "$TARGET_DIR"

    DOWNLOAD_SUCCESS=false
    for attempt in $(seq 1 "$MAX_RETRIES"); do
        log "  Downloading (attempt $attempt/$MAX_RETRIES)..."

        DOWNLOAD_CMD=""
        case "$URL" in
            s3://*)
                BUCKET=$(echo "$URL" | sed 's|s3://||' | cut -d'/' -f1)
                KEY=$(echo "$URL" | sed "s|s3://${BUCKET}/||")
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
            if [ "$EXPECTED_MD5" != "null" ] && [ -n "$EXPECTED_MD5" ]; then
                ACTUAL_MD5=$(md5sum "$TARGET_PATH" | awk '{print $1}')
                if [ "$ACTUAL_MD5" = "$EXPECTED_MD5" ]; then
                    DOWNLOAD_SUCCESS=true
                    break
                else
                    log "  MD5 mismatch after download: expected=$EXPECTED_MD5 actual=$ACTUAL_MD5"
                    rm -f "$TARGET_PATH"
                fi
            else
                DOWNLOAD_SUCCESS=true
                break
            fi
        else
            log "  Download failed"
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
        log "  OK: downloaded successfully"
        #
        EXTRACT=$(jq -r ".models[$i].extract // false" "$MANIFEST_PATH")
        EXTRACT_TARGET=$(jq -r ".models[$i].extract_target // empty" "$MANIFEST_PATH")
        #
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
        log "  FAILED: all $MAX_RETRIES attempts exhausted"
    fi
done

log "---"
log "Summary: total=$TOTAL downloaded=$DOWNLOADED skipped=$SKIPPED failed=$FAILED"

if [ "$FAILED" -gt 0 ]; then
    die "Failed to download $FAILED files"
fi

log "Model cache sync complete"
exit 0
