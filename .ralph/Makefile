# Elitea Horizontal Scaling - Ralph Implementation Commands
# Run from .ralph directory: cd .ralph && make <target>

SHELL := /bin/bash
.PHONY: help bootstrap validate dashboard run run-opus run-haiku status clean context

help:
	@echo "Elitea Horizontal Scaling Implementation"
	@echo ""
	@echo "Quick Start:"
	@echo "  make bootstrap    - Create directory structure"
	@echo "  make run          - Start Ralph loop (Sonnet)"
	@echo "  make dashboard    - Show progress dashboard"
	@echo ""
	@echo "Run Commands:"
	@echo "  make run          - Run with Sonnet (balanced)"
	@echo "  make run-opus     - Run with Opus (complex tasks)"
	@echo "  make run-haiku    - Run with Haiku (simple/fast)"
	@echo "  make run-phase1   - Run Phase 1 only"
	@echo "  make run-phase2   - Run Phase 2 only"
	@echo "  make run-phase3   - Run Phase 3 only"
	@echo "  make run-phase4   - Run Phase 4 only"
	@echo "  make run-phase5   - Run Phase 5 only"
	@echo "  make run-phase6   - Run Phase 6 only"
	@echo ""
	@echo "Status Commands:"
	@echo "  make status       - Show Ralph status and tasks"
	@echo "  make dashboard    - Show progress dashboard"
	@echo "  make validate     - Run all feature validators"
	@echo ""
	@echo "Control Commands:"
	@echo "  make context      - Add context for next iteration"
	@echo "  make clean        - Reset history (keep progress)"

bootstrap:
	@./bootstrap.sh

validate:
	@python3 validate.py

dashboard:
	@python3 validate.py --dashboard || true

# Main run commands
run:
	@./run-loop.sh --model sonnet

run-opus:
	@./run-loop.sh --model opus

run-haiku:
	@./run-loop.sh --model haiku

run-phase1:
	@./run-loop.sh --model sonnet --phase 1

run-phase2:
	@./run-loop.sh --model sonnet --phase 2

run-phase3:
	@./run-loop.sh --model sonnet --phase 3

run-phase4:
	@./run-loop.sh --model opus --phase 4

run-phase5:
	@./run-loop.sh --model opus --phase 5

run-phase6:
	@./run-loop.sh --model sonnet --phase 6

# Alternative: use Ralph CLI
run-ralph:
	@./run-with-ralph.sh --model sonnet

# Status and control
status:
	@ralph --status --tasks 2>/dev/null || echo "No active Ralph loop"
	@echo ""
	@python3 validate.py --dashboard || true

context:
	@echo "Enter context to add (Ctrl+D when done):"
	@ralph --add-context "$$(cat)"

clean:
	@echo '{"iterations":[],"totalDurationMs":0,"struggleIndicators":{"repeatedErrors":{},"noProgressIterations":0,"shortIterations":0}}' > ralph-history.json
	@ralph --clear-context 2>/dev/null || true
	@echo "History and context reset."
