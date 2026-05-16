#!/usr/bin/env bash
# CutFlow•CAD — VPS deploy / refresh.
#
# Usage (on the VPS):
#   bash deploy/vps-deploy.sh           # default: /opt/cutflow-cad
#   CUTFLOW_HOME=/srv/cad bash ...      # override target directory
#
# Workflow
#   1. git pull         — fast-forward the working tree to origin/main.
#   2. compose down     — stop running containers cleanly.
#   3. compose up --build -d — rebuild image and bring everything back up.
#
# We intentionally do NOT prune images — the second-to-last build is the
# rollback target if --build fails partway. Run `docker system prune` by
# hand when disk pressure hits.

set -euo pipefail

CUTFLOW_HOME=${CUTFLOW_HOME:-/opt/cutflow-cad}

if [[ ! -d "$CUTFLOW_HOME" ]]; then
  echo "error: $CUTFLOW_HOME does not exist" >&2
  echo "  clone the repo there first: git clone <url> $CUTFLOW_HOME" >&2
  exit 1
fi

cd "$CUTFLOW_HOME"

echo "==> git pull"
git pull --ff-only

# docker compose v2 (no hyphen) is the canonical command on modern hosts;
# fall back to the legacy hyphenated form on older boxes.
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
else
  DC=(docker-compose)
fi

echo "==> ${DC[*]} down"
"${DC[@]}" down

echo "==> ${DC[*]} up -d --build"
"${DC[@]}" up -d --build

echo ""
echo "deploy complete. status:"
"${DC[@]}" ps
