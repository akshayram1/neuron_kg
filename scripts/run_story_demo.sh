#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

docker compose -f compose.story.yml up -d --wait
npm --prefix demo_ui/frontend run build

export DATABASE_URL="${DATABASE_URL:-postgresql://neuron:neuron@localhost:55432/neuron}"
export VECTOR_BACKEND="${VECTOR_BACKEND:-postgres}"
export FALKOR_HOST="${FALKOR_HOST:-localhost}"
export FALKOR_PORT="${FALKOR_PORT:-6380}"
export STORY_RUN_LLM="${STORY_RUN_LLM:-true}"

exec uv run uvicorn demo_ui.backend.app:app --reload --port "${NEURON_DEMO_PORT:-8000}"
