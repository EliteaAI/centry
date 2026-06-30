#!/usr/bin/env bash
# Test suite for entrypoint.sh MD5 validation and --verify-only flag.
# Runs without Docker — uses a local temp directory and a mock HTTP server.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENTRYPOINT="$SCRIPT_DIR/entrypoint.sh"
TEST_DIR=""
PASS=0
FAIL=0

setup() {
    TEST_DIR=$(mktemp -d)
    export CACHE_DIR="$TEST_DIR/cache"
    export MANIFEST_PATH="$TEST_DIR/manifest.json"
    export MAX_RETRIES=2
    export VERIFY_ONLY=false
    mkdir -p "$CACHE_DIR"
}

teardown() {
    [ -n "$TEST_DIR" ] && rm -rf "$TEST_DIR"
}

assert_exit() {
    local expected=$1
    shift
    local output
    output=$("$@" 2>&1) || true
    local actual=$?
    # Re-run to capture exit code correctly
    set +e
    "$@" >/dev/null 2>&1
    actual=$?
    set -e
    if [ "$actual" -ne "$expected" ]; then
        FAIL=$((FAIL + 1))
        echo "FAIL: expected exit=$expected got exit=$actual"
        echo "  CMD: $*"
        echo "  OUT: $output"
        return 1
    fi
    PASS=$((PASS + 1))
    return 0
}

assert_contains() {
    local needle="$1"
    local haystack="$2"
    if echo "$haystack" | grep -q "$needle"; then
        PASS=$((PASS + 1))
        return 0
    fi
    FAIL=$((FAIL + 1))
    echo "FAIL: output does not contain '$needle'"
    echo "  GOT: $haystack"
    return 1
}

assert_file_exists() {
    if [ -f "$1" ]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        echo "FAIL: file does not exist: $1"
    fi
}

assert_file_not_exists() {
    if [ ! -f "$1" ]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        echo "FAIL: file should not exist: $1"
    fi
}

# ---------- Test: Missing manifest ----------
test_missing_manifest() {
    echo "--- test_missing_manifest ---"
    setup
    export MANIFEST_PATH="$TEST_DIR/nonexistent.json"

    set +e
    output=$("$ENTRYPOINT" 2>&1)
    rc=$?
    set -e

    if [ "$rc" -ne 1 ]; then
        FAIL=$((FAIL + 1))
        echo "FAIL: expected exit 1, got $rc"
    else
        PASS=$((PASS + 1))
    fi
    assert_contains "Manifest not found" "$output"
    teardown
}

# ---------- Test: Invalid JSON manifest ----------
test_invalid_json_manifest() {
    echo "--- test_invalid_json_manifest ---"
    setup
    echo "not json" > "$MANIFEST_PATH"

    set +e
    output=$("$ENTRYPOINT" 2>&1)
    rc=$?
    set -e

    if [ "$rc" -ne 1 ]; then
        FAIL=$((FAIL + 1))
        echo "FAIL: expected exit 1, got $rc"
    else
        PASS=$((PASS + 1))
    fi
    assert_contains "not valid JSON" "$output"
    teardown
}

# ---------- Test: File with correct MD5 is skipped ----------
test_correct_md5_skipped() {
    echo "--- test_correct_md5_skipped ---"
    setup

    echo -n "hello world" > "$CACHE_DIR/test.txt"
    local expected_md5
    expected_md5=$(md5sum "$CACHE_DIR/test.txt" | awk '{print $1}')

    cat > "$MANIFEST_PATH" <<EOF
{
  "models": [
    {
      "name": "test-file",
      "url": "http://localhost:9999/test.txt",
      "path": "test.txt",
      "md5": "$expected_md5",
      "size_mb": 0
    }
  ]
}
EOF

    set +e
    output=$("$ENTRYPOINT" 2>&1)
    rc=$?
    set -e

    if [ "$rc" -ne 0 ]; then
        FAIL=$((FAIL + 1))
        echo "FAIL: expected exit 0, got $rc"
    else
        PASS=$((PASS + 1))
    fi
    assert_contains "already cached with correct MD5" "$output"
    assert_contains "skipped=1" "$output"
    teardown
}

# ---------- Test: File with wrong MD5 is deleted (and download fails → exit 1) ----------
test_wrong_md5_deleted() {
    echo "--- test_wrong_md5_deleted ---"
    setup

    echo -n "corrupt data" > "$CACHE_DIR/test.txt"

    cat > "$MANIFEST_PATH" <<EOF
{
  "models": [
    {
      "name": "test-file",
      "url": "http://localhost:9999/not-a-real-server.txt",
      "path": "test.txt",
      "md5": "0000000000000000000000000000dead",
      "size_mb": 0
    }
  ]
}
EOF

    set +e
    output=$("$ENTRYPOINT" 2>&1)
    rc=$?
    set -e

    if [ "$rc" -ne 1 ]; then
        FAIL=$((FAIL + 1))
        echo "FAIL: expected exit 1 (download fails), got $rc"
    else
        PASS=$((PASS + 1))
    fi
    assert_contains "MD5 MISMATCH" "$output"
    assert_contains "Deleting corrupt file" "$output"
    assert_file_not_exists "$CACHE_DIR/test.txt"
    teardown
}

# ---------- Test: --verify-only with valid file ----------
test_verify_only_valid() {
    echo "--- test_verify_only_valid ---"
    setup

    echo -n "verify me" > "$CACHE_DIR/data.bin"
    local expected_md5
    expected_md5=$(md5sum "$CACHE_DIR/data.bin" | awk '{print $1}')

    cat > "$MANIFEST_PATH" <<EOF
{
  "models": [
    {
      "name": "data-bundle",
      "url": "s3://bucket/data.bin",
      "path": "data.bin",
      "md5": "$expected_md5",
      "size_mb": 1
    }
  ]
}
EOF

    set +e
    output=$("$ENTRYPOINT" --verify-only 2>&1)
    rc=$?
    set -e

    if [ "$rc" -ne 0 ]; then
        FAIL=$((FAIL + 1))
        echo "FAIL: expected exit 0 (valid), got $rc"
    else
        PASS=$((PASS + 1))
    fi
    assert_contains "VALID" "$output"
    assert_contains "valid=1" "$output"
    assert_file_exists "$CACHE_DIR/data.bin"
    teardown
}

# ---------- Test: --verify-only with invalid file (does NOT delete) ----------
test_verify_only_invalid_no_delete() {
    echo "--- test_verify_only_invalid_no_delete ---"
    setup

    echo -n "bad content" > "$CACHE_DIR/data.bin"

    cat > "$MANIFEST_PATH" <<EOF
{
  "models": [
    {
      "name": "data-bundle",
      "url": "s3://bucket/data.bin",
      "path": "data.bin",
      "md5": "0000000000000000000000000000dead",
      "size_mb": 1
    }
  ]
}
EOF

    set +e
    output=$("$ENTRYPOINT" --verify-only 2>&1)
    rc=$?
    set -e

    if [ "$rc" -ne 1 ]; then
        FAIL=$((FAIL + 1))
        echo "FAIL: expected exit 1 (invalid), got $rc"
    else
        PASS=$((PASS + 1))
    fi
    assert_contains "MD5 MISMATCH" "$output"
    assert_contains "invalid=1" "$output"
    # Critically: verify-only does NOT delete the file
    assert_file_exists "$CACHE_DIR/data.bin"
    teardown
}

# ---------- Test: --verify-only with missing file ----------
test_verify_only_missing() {
    echo "--- test_verify_only_missing ---"
    setup

    cat > "$MANIFEST_PATH" <<EOF
{
  "models": [
    {
      "name": "absent-file",
      "url": "s3://bucket/absent.bin",
      "path": "absent.bin",
      "md5": "abc123",
      "size_mb": 10
    }
  ]
}
EOF

    set +e
    output=$("$ENTRYPOINT" --verify-only 2>&1)
    rc=$?
    set -e

    if [ "$rc" -ne 1 ]; then
        FAIL=$((FAIL + 1))
        echo "FAIL: expected exit 1 (missing), got $rc"
    else
        PASS=$((PASS + 1))
    fi
    assert_contains "MISSING" "$output"
    assert_contains "not found in cache" "$output"
    teardown
}

# ---------- Test: File with no MD5 (null) is skipped if exists ----------
test_null_md5_skipped() {
    echo "--- test_null_md5_skipped ---"
    setup

    echo -n "any content" > "$CACHE_DIR/nomd5.txt"

    cat > "$MANIFEST_PATH" <<EOF
{
  "models": [
    {
      "name": "no-md5",
      "url": "http://example.com/nomd5.txt",
      "path": "nomd5.txt",
      "md5": null,
      "size_mb": 0
    }
  ]
}
EOF

    set +e
    output=$("$ENTRYPOINT" 2>&1)
    rc=$?
    set -e

    if [ "$rc" -ne 0 ]; then
        FAIL=$((FAIL + 1))
        echo "FAIL: expected exit 0, got $rc"
    else
        PASS=$((PASS + 1))
    fi
    assert_contains "exists (no MD5 to verify)" "$output"
    assert_contains "skipped=1" "$output"
    teardown
}

# ---------- Test: --verify-only via env var ----------
test_verify_only_env_var() {
    echo "--- test_verify_only_env_var ---"
    setup
    export VERIFY_ONLY=true

    echo -n "content" > "$CACHE_DIR/file.dat"
    local expected_md5
    expected_md5=$(md5sum "$CACHE_DIR/file.dat" | awk '{print $1}')

    cat > "$MANIFEST_PATH" <<EOF
{
  "models": [
    {
      "name": "env-test",
      "url": "http://example.com/file.dat",
      "path": "file.dat",
      "md5": "$expected_md5",
      "size_mb": 0
    }
  ]
}
EOF

    set +e
    output=$("$ENTRYPOINT" 2>&1)
    rc=$?
    set -e

    if [ "$rc" -ne 0 ]; then
        FAIL=$((FAIL + 1))
        echo "FAIL: expected exit 0 (env var verify-only), got $rc"
    else
        PASS=$((PASS + 1))
    fi
    assert_contains "Verify only: true" "$output"
    assert_contains "VALID" "$output"
    teardown
}

# ---------- Test: Logging includes filename in MD5 messages ----------
test_log_includes_filename() {
    echo "--- test_log_includes_filename ---"
    setup

    echo -n "some data" > "$CACHE_DIR/myfile.bin"

    cat > "$MANIFEST_PATH" <<EOF
{
  "models": [
    {
      "name": "my-model",
      "url": "http://localhost:9999/myfile.bin",
      "path": "myfile.bin",
      "md5": "deadbeefdeadbeefdeadbeefdeadbeef",
      "size_mb": 5
    }
  ]
}
EOF

    set +e
    output=$("$ENTRYPOINT" 2>&1)
    rc=$?
    set -e

    # Should contain filename in the mismatch message
    assert_contains "file=myfile.bin" "$output"
    teardown
}

# ---------- Test: Empty models array is a success ----------
test_empty_models() {
    echo "--- test_empty_models ---"
    setup

    cat > "$MANIFEST_PATH" <<EOF
{
  "models": []
}
EOF

    set +e
    output=$("$ENTRYPOINT" 2>&1)
    rc=$?
    set -e

    if [ "$rc" -ne 0 ]; then
        FAIL=$((FAIL + 1))
        echo "FAIL: expected exit 0 (empty models), got $rc"
    else
        PASS=$((PASS + 1))
    fi
    assert_contains "0 files to process" "$output"
    assert_contains "Model cache sync complete" "$output"
    teardown
}

# --- Run all tests ---
echo "=== Running entrypoint.sh tests ==="
echo ""

test_missing_manifest
test_invalid_json_manifest
test_correct_md5_skipped
test_wrong_md5_deleted
test_verify_only_valid
test_verify_only_invalid_no_delete
test_verify_only_missing
test_null_md5_skipped
test_verify_only_env_var
test_log_includes_filename
test_empty_models

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
