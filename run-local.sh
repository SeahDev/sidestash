#!/usr/bin/env bash
# Run Sidestash on your Mac without Docker.  Needs uv:  brew install uv
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env. Set PUBLIC_URL and ADMIN_PASSWORD in it, then run ./run-local.sh again."
  exit 1
fi
set -a; source .env; set +a
export DATA_DIR="$PWD/data" TMPDIR="$PWD/data/tmp"
mkdir -p "$TMPDIR"
echo "Dashboard: http://localhost:8686/admin    Source: ${PUBLIC_URL%/}/source.json"
exec uv run --python 3.12 --with-requirements requirements.txt \
  uvicorn app.main:app --host 0.0.0.0 --port 8686
