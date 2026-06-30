# Elitea Horizontal Scaling - Ralph Implementation

This directory contains the Ralph Wiggum autonomous implementation framework for horizontal scaling of Elitea Pylon services.

## Overview

The implementation follows the [HORIZONTAL_SCALING_PLAN_V2.md](../HORIZONTAL_SCALING_PLAN_V2.md) and is structured into 6 phases:

| Phase | Name | Duration | Key Deliverables |
|-------|------|----------|-----------------|
| 1 | Stateless Foundation | 2-3 weeks | Redis adapter, state externalization, staging overlay, E2E tests |
| 2 | Session & Task State | 2-3 weeks | Auth sessions, distributed locks, task claiming |
| 3 | Storage Optimization | 2-3 weeks | Init containers, emptyDir, model cache |
| 4 | Event System Hardening | 3-4 weeks | Redis Streams, deduplication, dead letter queue |
| 5 | Infrastructure Scaling | 3-4 weeks | PgBouncer, Sentinel, HPA, monitoring |
| 6 | Production Hardening | 2-3 weeks | Security, network policies, rate limiting |

## Files

| File | Purpose |
|------|---------|
| `PROMPT.md` | Main prompt sent to Ralph each iteration |
| `ralph-tasks.md` | Task checklist with subtasks (6 phases) |
| `features.json` | Feature definitions with validators (37 features) |
| `validate.py` | Validate feature completion |
| `run-loop.sh` | Run Ralph loop with Claude Code |
| `run-with-ralph.sh` | Alternative: run via Ralph CLI |
| `bootstrap.sh` | Create initial directory structure |
| `Makefile` | Quick commands |
| `AGENT.md` | Accumulated learnings and patterns |

## Quick Start

### Prerequisites

1. Claude Code installed and configured
2. Feature branches created:
   - `centry`: `feature/horizontal-scaling-phase-1`
   - `elitea_core`: `feature/horizontal-scaling-phase-1`
   - `argocd-public`: `feature/horizontal-scaling-staging`
   - `EliteaUI`: `feature/horizontal-scaling-e2e`

### Running the Implementation

```bash
# Bootstrap directories
cd .ralph && make bootstrap

# Run with Sonnet (recommended for most work)
make run

# Run with Opus for complex architecture tasks
make run-opus

# Run specific phase only
make run-phase1

# Check progress
make dashboard
make validate
```

### Model Selection Guide

| Model | Best For |
|-------|----------|
| **Haiku** | Simple file moves, config changes |
| **Sonnet** | General implementation, tests, most Phase 1 work |
| **Opus** | Complex state management, event system design (Phase 4-5) |

## Validation

Each feature has a concrete validator that checks for:
- Directory existence
- File existence
- Pattern presence in files
- Shell command success
- Test coverage thresholds

```bash
# Validate all
python3 validate.py

# Validate Phase 1 only
python3 validate.py --phase phase-1

# Show dashboard
python3 validate.py --dashboard

# Update features.json
python3 validate.py --update
```

## Completion Criteria

Phase 1 is complete when:
- [ ] All 20 Phase 1 validators pass
- [ ] Socket.IO events delivered across 3+ pods
- [ ] Sessions survive pod restart
- [ ] DB connections < 60 total with 8 pods
- [ ] Zero sticky session annotations
- [ ] Playwright E2E tests pass on staging
- [ ] 85% test coverage for new code

## Architecture

See `AGENT.md` for detailed patterns and code examples.

## References

- Scaling Plan: `../HORIZONTAL_SCALING_PLAN_V2.md`
- ArgoCD Staging: `../kharkevich/argocd-public/elitea-platform/values/staging/`
- Pylon Chart: `oci://ghcr.io/eliteaai/charts/pylon` v1.0.6
- OIDC Mock: `https://oidc-mock.technicaldomain.xyz/`
- Ralph Wiggum Technique: https://ghuntley.com/ralph/
