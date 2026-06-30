#!/usr/bin/env bash
#
# Simple implementation loop using Claude Code directly
# Avoids Ralph's model validation issues with Bedrock IDs
#
# Usage:
#   ./run-loop.sh                    # Default (Sonnet)
#   ./run-loop.sh --model opus       # Use Opus
#   ./run-loop.sh --max-iterations 50
#

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RALPH_DIR="$PROJECT_ROOT/.ralph"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Model mapping
get_model_id() {
    case "$1" in
        opus)   echo "eu.anthropic.claude-opus-4-5-20251101-v1:0" ;;
        sonnet) echo "eu.anthropic.claude-sonnet-4-5-20250929-v1:0" ;;
        haiku)  echo "eu.anthropic.claude-haiku-4-5-20251001-v1:0" ;;
        *)      echo "$1" ;;
    esac
}

# Defaults
MODEL="sonnet"
MAX_ITERATIONS=100
MIN_ITERATIONS=2
PHASE=""

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --model) MODEL="$2"; shift 2 ;;
        --max-iterations) MAX_ITERATIONS="$2"; shift 2 ;;
        --min-iterations) MIN_ITERATIONS="$2"; shift 2 ;;
        --phase) PHASE="$2"; shift 2 ;;
        --help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --model MODEL        Model: opus, sonnet, haiku (default: sonnet)"
            echo "  --max-iterations N   Maximum iterations (default: 100)"
            echo "  --min-iterations N   Minimum per task (default: 2)"
            echo "  --phase N            Run specific phase only (1-5)"
            echo ""
            echo "Models (Bedrock EU):"
            echo "  opus   -> eu.anthropic.claude-opus-4-5-20251101-v1:0"
            echo "  sonnet -> eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
            echo "  haiku  -> eu.anthropic.claude-haiku-4-5-20251001-v1:0"
            exit 0
            ;;
        *) shift ;;
    esac
done

cd "$PROJECT_ROOT"

FULL_MODEL_ID=$(get_model_id "$MODEL")

# Build prompt
PROMPT_FILE="$RALPH_DIR/PROMPT.md"
if [[ -n "$PHASE" ]]; then
    PROMPT_FILE="$RALPH_DIR/PROMPT_PHASE_${PHASE}.md"
    cat > "$PROMPT_FILE" << EOF
# Elitea LLM Gateway Implementation - Phase ${PHASE}

Read @LLM_GATEWAY_IMPLEMENTATION_PLAN.md for the full specification.

## Current Focus: Phase ${PHASE}

Check @.ralph/ralph-tasks.md and work on Phase ${PHASE} tasks only.

## Instructions

1. Find the next uncompleted top-level task in Phase ${PHASE}
2. Implement all subtasks within that task
3. Run validation: \`python .ralph/validate.py --phase phase-${PHASE}\`
4. Mark completed tasks with \`[x]\`
5. Commit changes

## Completion Signals

When current task is complete: <promise>READY_FOR_NEXT_TASK</promise>
When ALL Phase ${PHASE} tasks are complete: <promise>COMPLETE</promise>
EOF
fi

log_info "Starting implementation loop"
log_info "  Model:      $MODEL -> $FULL_MODEL_ID"
log_info "  Phase:      ${PHASE:-all}"
log_info "  Max Iters:  $MAX_ITERATIONS"
echo ""

iteration=0
task_iterations=0
completed=false

while [[ $iteration -lt $MAX_ITERATIONS ]] && [[ "$completed" != "true" ]]; do
    iteration=$((iteration + 1))
    task_iterations=$((task_iterations + 1))

    echo ""
    echo "═══════════════════════════════════════════════════════════════"
    log_info "Iteration $iteration/$MAX_ITERATIONS (task iteration: $task_iterations)"
    echo "═══════════════════════════════════════════════════════════════"
    echo ""

    # Run Claude Code with the prompt
    start_time=$(date +%s)

    output=$(claude --model "$FULL_MODEL_ID" --print --dangerously-skip-permissions \
        "$(cat "$PROMPT_FILE")" 2>&1) || true

    end_time=$(date +%s)
    duration=$((end_time - start_time))

    log_info "Iteration completed in ${duration}s"

    # Check for COMPLETE signal
    if echo "$output" | grep -q "<promise>COMPLETE</promise>"; then
        # Verify ALL validators actually pass before accepting completion
        log_info "COMPLETE signal detected - verifying all validators pass..."

        validation_output=$(python3 "$RALPH_DIR/validate.py" --dashboard 2>&1)

        if echo "$validation_output" | grep -q "100%.*38/38"; then
            log_success "All 38 validators pass - Implementation COMPLETE!"
            completed=true
            break
        else
            log_warn "COMPLETE signal rejected - not all validators pass!"
            log_warn "Agent must fix remaining validators before completion."
            echo "$validation_output"
            # Continue the loop - don't accept premature completion
            continue
        fi
    fi

    # Check for READY_FOR_NEXT_TASK signal
    if echo "$output" | grep -q "<promise>READY_FOR_NEXT_TASK</promise>"; then
        log_success "Task complete! Moving to next task..."
        task_iterations=0

        # Show progress
        python3 "$RALPH_DIR/validate.py" --dashboard 2>/dev/null || true
        continue
    fi

    # Check minimum iterations per task
    if [[ $task_iterations -lt $MIN_ITERATIONS ]]; then
        log_info "Continuing task (iteration $task_iterations/$MIN_ITERATIONS minimum)"
        continue
    fi

    # Check for errors
    if echo "$output" | grep -qi "error.*401\|authentication failed"; then
        log_error "Authentication error detected!"
        echo "$output" | tail -10
        break
    fi

    log_info "No completion signal, continuing..."
done

echo ""
echo "═══════════════════════════════════════════════════════════════"
if [[ "$completed" == "true" ]]; then
    log_success "Implementation finished!"
else
    log_warn "Stopped after $iteration iterations"
fi
echo "═══════════════════════════════════════════════════════════════"

# Final status
python3 "$RALPH_DIR/validate.py" --dashboard 2>/dev/null || true
