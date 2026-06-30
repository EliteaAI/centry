#!/usr/bin/env bash
#
# Run LLM Gateway implementation using Ralph Wiggum with Claude Code agent
#
# Usage:
#   ./run-with-ralph.sh                    # Default (Sonnet, all phases)
#   ./run-with-ralph.sh --model opus       # Use Opus for complex work
#   ./run-with-ralph.sh --phase 1          # Specific phase only
#   ./run-with-ralph.sh --max-iterations 50
#

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RALPH_DIR="$PROJECT_ROOT/.ralph"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }

# Model mapping - MUST use full Bedrock IDs for ELITEA proxy
get_model_id() {
    case "$1" in
        opus)   echo "eu.anthropic.claude-opus-4-5-20251101-v1:0" ;;
        sonnet) echo "eu.anthropic.claude-sonnet-4-5-20250929-v1:0" ;;
        haiku)  echo "eu.anthropic.claude-haiku-4-5-20251001-v1:0" ;;
        *)      echo "$1" ;;  # Pass through if already full ID
    esac
}

# Defaults
MODEL="sonnet"
PHASE=""
MAX_ITERATIONS="100"
MIN_ITERATIONS="2"

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --model)
            MODEL="$2"
            shift 2
            ;;
        --phase) PHASE="$2"; shift 2 ;;
        --max-iterations) MAX_ITERATIONS="$2"; shift 2 ;;
        --min-iterations) MIN_ITERATIONS="$2"; shift 2 ;;
        --status) ralph --status --tasks; exit 0 ;;
        --help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --model MODEL        Model: opus, sonnet, haiku (default: sonnet)"
            echo "  --phase N            Run specific phase only (1-5)"
            echo "  --max-iterations N   Maximum iterations (default: 100)"
            echo "  --min-iterations N   Minimum iterations per task (default: 2)"
            echo "  --status             Show Ralph status and tasks"
            echo ""
            echo "Models (Bedrock EU):"
            echo "  opus   -> eu.anthropic.claude-opus-4-5-20251101-v1:0"
            echo "  sonnet -> eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
            echo "  haiku  -> eu.anthropic.claude-haiku-4-5-20251001-v1:0"
            echo ""
            echo "Examples:"
            echo "  $0                           # Run with Sonnet (default)"
            echo "  $0 --model opus              # Use Opus for complex work"
            echo "  $0 --phase 1 --model haiku   # Phase 1 with Haiku"
            exit 0
            ;;
        *) shift ;;
    esac
done

cd "$PROJECT_ROOT"

# Build prompt file path
PROMPT_FILE="$RALPH_DIR/PROMPT.md"

if [[ -n "$PHASE" ]]; then
    # Create phase-specific prompt
    PROMPT_FILE="$RALPH_DIR/PROMPT_PHASE_${PHASE}.md"
    cat > "$PROMPT_FILE" << EOF
# Elitea LLM Gateway Implementation - Phase ${PHASE}

Read @LLM_GATEWAY_IMPLEMENTATION_PLAN.md for the full specification.

## Current Focus: Phase ${PHASE}

Check @.ralph/ralph-tasks.md and work on Phase ${PHASE} tasks only.

## Instructions

1. Find the next uncompleted top-level task in Phase ${PHASE} (first \`- [ ]\`)
2. Implement all subtasks within that task
3. Run validation: \`python .ralph/validate.py --phase phase-${PHASE}\`
4. Mark completed tasks with \`[x]\`
5. Commit changes

## Quality Requirements

- 85% test coverage for new code
- TypeScript for UI components
- Follow existing Pylon plugin patterns

## Completion Signals

When current task is complete: <promise>READY_FOR_NEXT_TASK</promise>
When ALL Phase ${PHASE} tasks are complete: <promise>COMPLETE</promise>
EOF
    log_info "Created phase-specific prompt: $PROMPT_FILE"
fi

# Resolve model shortcut to full Bedrock ID
FULL_MODEL_ID=$(get_model_id "$MODEL")

log_info "Starting Ralph Wiggum loop"
log_info "  Agent:      claude-code"
log_info "  Model:      $MODEL -> $FULL_MODEL_ID"
log_info "  Phase:      ${PHASE:-all}"
log_info "  Max Iters:  $MAX_ITERATIONS"
log_info "  Min Iters:  $MIN_ITERATIONS"
echo ""

# Run Ralph with claude-code agent
# Pass model via -- to Claude Code directly to avoid Ralph's model validation
ralph --prompt-file "$PROMPT_FILE" \
    --agent claude-code \
    --max-iterations "$MAX_ITERATIONS" \
    --min-iterations "$MIN_ITERATIONS" \
    --tasks \
    --task-promise "READY_FOR_NEXT_TASK" \
    --completion-promise "COMPLETE" \
    -- --model "$FULL_MODEL_ID"
