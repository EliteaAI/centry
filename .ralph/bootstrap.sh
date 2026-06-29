#!/bin/bash
#
# Bootstrap directory structure for horizontal scaling implementation
# Run this once to create all directories before starting implementation
#

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "Creating Horizontal Scaling directory structure..."

# Phase 1: State externalization utilities
mkdir -p centry/pylon_main/plugins/elitea_core/utils
mkdir -p centry/pylon_main/plugins/elitea_core/routes
mkdir -p centry/pylon_main/plugins/elitea_core/tests

# Phase 1: E2E test framework
mkdir -p centry/tests/e2e/{specs,pages,fixtures,utils}

# Phase 3: Model cache init container
mkdir -p centry/docker/model-cache-init

# Phase 5: Monitoring
mkdir -p centry/monitoring/alerts
mkdir -p centry/monitoring/grafana/dashboards

# ArgoCD staging overlay
ARGOCD_DIR="../kharkevich/argocd-public/elitea-platform"
if [ -d "$ARGOCD_DIR" ]; then
    mkdir -p "$ARGOCD_DIR/values/staging"
    mkdir -p "$ARGOCD_DIR/apps/staging"
    mkdir -p "$ARGOCD_DIR/manifests/staging"
    echo "ArgoCD staging directories created."
else
    echo "WARNING: ArgoCD directory not found at $ARGOCD_DIR"
    echo "  Create staging directories manually when available."
fi

echo ""
echo "Directory structure created!"
echo ""
echo "Run validation to see updated status:"
echo "  python3 .ralph/validate.py --phase phase-1"
