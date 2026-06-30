#!/usr/bin/env bash
# Writes Prometheus textfile-format metrics about model cache state.
# Called by entrypoint.sh after sync completes.
# Metrics file at $CACHE_DIR/.metrics can be exposed via node-exporter textfile collector.

set -euo pipefail

CACHE_DIR="${CACHE_DIR:-/cache}"
METRICS_FILE="${CACHE_DIR}/.metrics"
MANIFEST_PATH="${MANIFEST_PATH:-/config/manifest.json}"

# Arguments passed from entrypoint:
#   $1 = duration_seconds (total sync wall time)
#   $2 = downloaded count
#   $3 = skipped count
#   $4 = failed count
#   $5 = total count
DURATION="${1:-0}"
DOWNLOADED="${2:-0}"
SKIPPED="${3:-0}"
FAILED="${4:-0}"
TOTAL="${5:-0}"

# Calculate total cache size on disk
CACHE_SIZE_BYTES=0
if [ -d "$CACHE_DIR" ]; then
    CACHE_SIZE_BYTES=$(du -sb "$CACHE_DIR" 2>/dev/null | awk '{print $1}' || echo "0")
fi

# Count total files (excluding dotfiles like .metrics, .manifest-version)
FILES_TOTAL=0
if [ -d "$CACHE_DIR" ]; then
    FILES_TOTAL=$(find "$CACHE_DIR" -type f -not -name '.*' | wc -l | tr -d ' ')
fi

# Write Prometheus textfile format
cat > "$METRICS_FILE" <<EOF
# HELP model_cache_download_duration_seconds Total time spent downloading model cache files.
# TYPE model_cache_download_duration_seconds gauge
model_cache_download_duration_seconds ${DURATION}

# HELP model_cache_size_bytes Total size of the model cache directory in bytes.
# TYPE model_cache_size_bytes gauge
model_cache_size_bytes ${CACHE_SIZE_BYTES}

# HELP model_cache_files_total Total number of cached model files on disk.
# TYPE model_cache_files_total gauge
model_cache_files_total ${FILES_TOTAL}

# HELP model_cache_files_downloaded Number of files downloaded in the last sync.
# TYPE model_cache_files_downloaded gauge
model_cache_files_downloaded ${DOWNLOADED}

# HELP model_cache_files_skipped Number of files skipped (already cached) in the last sync.
# TYPE model_cache_files_skipped gauge
model_cache_files_skipped ${SKIPPED}

# HELP model_cache_errors_total Number of files that failed to download in the last sync.
# TYPE model_cache_errors_total gauge
model_cache_errors_total ${FAILED}

# HELP model_cache_manifest_files_total Total number of files defined in the manifest.
# TYPE model_cache_manifest_files_total gauge
model_cache_manifest_files_total ${TOTAL}

# HELP model_cache_last_sync_timestamp_seconds Unix timestamp of the last successful cache sync.
# TYPE model_cache_last_sync_timestamp_seconds gauge
model_cache_last_sync_timestamp_seconds $(date +%s)
EOF

echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] Metrics written to $METRICS_FILE"
