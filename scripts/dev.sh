#!/usr/bin/env bash
# Starts backend (uvicorn, :8000) and frontend (vite, :5173) together.
# Ctrl-C stops both.
set -euo pipefail
cd "$(dirname "$0")/.."

trap 'kill 0' EXIT

uv run uvicorn demo_ui.backend.app:app --reload --port 8000 &
(cd demo_ui/frontend && npm run dev) &

wait
