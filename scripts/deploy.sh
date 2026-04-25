#!/bin/bash
# scripts/deploy.sh
# ==============================================================================
# Automates the Helm Monorepo deployment pipeline:
# 1. Generates Helm charts for a specific project and environment.
# 2. Performs 'helm lint' only on charts that have changed.
# 3. Checks for Git changes in the generated charts.
# 4. Commits and pushes changes back to the repository (Write-back GitOps).
#
# Usage: ./deploy.sh --project <name> --env <dev|stg|prod> [--dry-run]
# ==============================================================================

# SEC-02 FIX: Enable strict mode.
#   -e: exit on any error
#   -u: error on unbound variables (catches typos like $PORJECT)
#   -o pipefail: catch failures inside pipes (e.g. vault kv get | jq)
set -euo pipefail

# Default values
PROJECT=""
ENV="dev"
TEAM=""
DRY_RUN=false
IMAGE_TAG=""
GIT_USER="bot-generator"
GIT_EMAIL="bot@devops.vn"

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --team) TEAM="$2"; shift ;;
        --project) PROJECT="$2"; shift ;;
        --env) ENV="$2"; shift ;;
        --image-tag) IMAGE_TAG="$2"; shift ;;
        --dry-run) DRY_RUN=true ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

if [ -z "$PROJECT" ]; then
    echo "ERROR: --project is required."
    exit 1
fi
if [ -z "$TEAM" ]; then
    echo "ERROR: --team is required."
    exit 1
fi

# SEC-H1 FIX: Validate PROJECT, ENV, and TEAM to prevent path traversal and injection.
# Uses the same regex as generator.py for consistency.
# Must be lowercase alphanumeric with dashes (valid K8s namespace name).
if ! echo "$PROJECT" | grep -qE '^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$'; then
    echo "ERROR: --project '${PROJECT}' must be a valid lowercase identifier (e.g. 'ecommerce', 'my-app')."
    exit 1
fi
if ! echo "$ENV" | grep -qE '^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$'; then
    echo "ERROR: --env '${ENV}' must be a valid lowercase identifier (e.g. 'dev', 'stg', 'prod')."
    exit 1
fi
if ! echo "$TEAM" | grep -qE '^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$'; then
    echo "ERROR: --team '${TEAM}' must be a valid lowercase identifier (e.g. 'ops-team', 'backend')."
    exit 1
fi

echo "=== Deployment Orchestrator ==="
echo "Team    : ${TEAM}"
echo "Project : ${PROJECT}"
echo "Env     : ${ENV}"
echo "Dry Run : ${DRY_RUN}"
echo "-------------------------------"

# 1. Run the Generator
echo "▶ Running Generator..."
PYTHON_BIN="python3"
if [ -f "./.venv/bin/python3" ]; then
    PYTHON_BIN="./.venv/bin/python3"
    echo "  - Using virtual environment: .venv"
fi

# SEC-03 FIX: Use an array for the command to prevent word splitting and injection.
# Never use: CMD="$BIN args..." ; $CMD  (unquoted → word splitting on spaces/IFS)
# Always use: CMD=("$BIN" "arg1" "arg2") ; "${CMD[@]}"  (safe, handles spaces in args)
GEN_CMD=("${PYTHON_BIN}" "scripts/generator.py" "--team" "${TEAM}" "--project" "${PROJECT}" "--env" "${ENV}")
if [[ -n "${IMAGE_TAG}" ]]; then
    GEN_CMD+=("--image-tag" "${IMAGE_TAG}")
fi
"${GEN_CMD[@]}"

# 2. Validate with Helm Lint (Optimized: Only changed charts)
TARGET_DIR="projects/${TEAM}/${PROJECT}/${PROJECT}-${ENV}"
if [ ! -d "$TARGET_DIR" ]; then
    echo "ERROR: Target directory ${TARGET_DIR} does not exist after generation."
    exit 1
fi

echo "▶ Detecting changed charts for validation..."
# Get a list of directories within TARGET_DIR that have git changes
CHANGED_CHARTS=$(git status --porcelain "$TARGET_DIR" | awk '{print $2}' | cut -d/ -f1-5 | sort -u)

if [ -z "$CHANGED_CHARTS" ]; then
    echo "  - No changes in charts detected. Skipping linting."
else
    for chart_path in $CHANGED_CHARTS; do
        if [ -d "$chart_path" ] && [ -f "$chart_path/Chart.yaml" ]; then
            chart_name=$(basename "$chart_path")
            echo "  - Validating chart: $chart_name"
            echo "    - Updating dependencies..."
            helm dependency update "$chart_path" > /dev/null
            echo "    - Running helm lint..."
            helm lint "$chart_path"
        fi
    done
fi

# 3. Check for Git Changes (General for the whole project)
CHANGES=$(git status --porcelain "$TARGET_DIR")

if [ -z "$CHANGES" ]; then
    echo "✅ No changes detected in ${TARGET_DIR}. Skip commit."
    exit 0
fi

echo "▶ Changes detected in ${TARGET_DIR}:"
echo "${CHANGES}"

# 4. Commit and Push (Write-back)
if [ "$DRY_RUN" = true ]; then
    echo "⚠️  Dry-run mode: Skipping Git commit and push."
    exit 0
fi

echo "▶ Committing changes..."
git config user.name "${GIT_USER}"
git config user.email "${GIT_EMAIL}"

git add "$TARGET_DIR"
if git diff-index --quiet HEAD --; then
    echo "ℹ No changes detected in charts. Skipping commit."
    SUCCESS=true
else
    git commit -m "chore(ops): update generated charts for ${PROJECT} (${ENV}) [skip ci]"
    echo "▶ Pushing changes to origin (with retry logic)..."
    MAX_RETRIES=5
    RETRY_COUNT=0
    SUCCESS=false
    while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
        if git pull --rebase origin HEAD; then
            if git push origin HEAD; then
                SUCCESS=true
                break
            fi
        fi
        RETRY_COUNT=$((RETRY_COUNT + 1))
        echo "⚠️  Push failed. Retrying in 5s... ($RETRY_COUNT/$MAX_RETRIES)"
        sleep 5
    done
fi

if [ "$SUCCESS" = true ]; then
    echo "✅ Deployment manifests successfully updated in Git."
else
    echo "❌ ERROR: Failed to push changes after $MAX_RETRIES attempts."
    exit 1
fi
