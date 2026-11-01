#!/usr/bin/env bash
# Boots a local API on :$E2E_API_PORT (default 8000) backed by SQLite, no OAuth wall, seeded + a minted
# session (frontend/e2e/.auth/session.txt). Run by Playwright's webServer.
set -e
cd "$(dirname "$0")/../.."   # repo root
# Postgres, like production, whenever it can be had: a runner's writes and the
# browser's reads overlap, which SQLite answers with "database is locked" and
# Postgres with the row locks the code is written for. E2E_DATABASE_URL wins;
# else a throwaway container when Docker is up; else SQLite.
E2E_PG_PORT="${E2E_PG_PORT:-55432}"
if [ -n "$E2E_DATABASE_URL" ]; then
  export DATABASE_URL="$E2E_DATABASE_URL"
elif docker info >/dev/null 2>&1; then
  docker rm -f "canopy-e2e-pg-$(id -un)" >/dev/null 2>&1 || true
  docker run -d --rm --name "canopy-e2e-pg-$(id -un)" -p "$E2E_PG_PORT:5432" \
    -e POSTGRES_USER=e2e -e POSTGRES_PASSWORD=e2e -e POSTGRES_DB=canopy_e2e pgvector/pgvector:pg16 >/dev/null
  until docker exec "canopy-e2e-pg-$(id -un)" pg_isready -U e2e -d canopy_e2e >/dev/null 2>&1; do sleep 0.5; done
  export DATABASE_URL="postgres://e2e:e2e@127.0.0.1:$E2E_PG_PORT/canopy_e2e"
else
  export DATABASE_URL="sqlite:///$(pwd)/e2e.sqlite3"
fi
export REQUIRE_AUTH=False
export DEBUG=True
export SECRET_KEY="e2e-secret-key"
export ALLOWED_HOSTS="localhost,127.0.0.1"
# Chat sends queue real turns for a runner, as in production, instead of the
# inline stub reply: the fake runner (e2e/fake_runner.py) claims and answers them.
export CHAT_STUB_EXECUTOR=False
# Port is overridable: on a shared machine :8000 is often already taken by
# another account's dev server, and a hardcoded port makes the suite simply
# unrunnable there rather than failing with anything actionable.
E2E_API_PORT="${E2E_API_PORT:-8000}"
rm -rf "${TMPDIR:-/tmp}/canopy-e2e-runner"
rm -f e2e.sqlite3 frontend/e2e/.auth/session.txt frontend/e2e/.auth/session2.txt \
      frontend/e2e/.auth/mp-session-id.txt
# Per-user log for the same shared-Mac reason as the port: a fixed /tmp path
# left by the other account is not writable, and `set -e` then kills the suite.
uv run python manage.py migrate --noinput >"${TMPDIR:-/tmp}/e2e-migrate-$(id -un).log" 2>&1
uv run python manage.py shell -c "exec(open('frontend/e2e/seed.py').read())"
uv run python manage.py shell -c "exec(open('frontend/e2e/seed_runner.py').read())"
# The fake runner starts once the API answers, and dies with this script.
(
  until curl -sf "http://127.0.0.1:$E2E_API_PORT/health/" >/dev/null; do sleep 0.5; done
  E2E_API_BASE="http://127.0.0.1:$E2E_API_PORT" E2E_RUNNER_HOME="${TMPDIR:-/tmp}/canopy-e2e-runner" \
    exec uv run python frontend/e2e/fake_runner.py
) >"${TMPDIR:-/tmp}/e2e-fake-runner-$(id -un).log" 2>&1 &
RUNNER_PID=$!
uv run uvicorn config.asgi:application --host 127.0.0.1 --port "$E2E_API_PORT" &
API_PID=$!
trap 'kill $RUNNER_PID $API_PID 2>/dev/null; pkill -f "frontend/e2e/fake_runner.py" 2>/dev/null; docker rm -f "canopy-e2e-pg-$(id -un)" >/dev/null 2>&1' EXIT INT TERM
wait $API_PID
