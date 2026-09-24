#!/usr/bin/env bash
# Bring the dev substrate up and migrate it. One command, as T1 requires.
#
# The test suite does not skip when Postgres or Temporal is missing - it fails and
# points here.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "----- postgres + temporal"
docker compose up --wait

echo "----- migrations"
uv run agentstack-migrate up

echo "----- temporal namespace"
# Seven days, long enough to debug a failed run (tasks/plan.md, Phase 8). Nothing
# depends on it: trigger dedupe is the Postgres cycle claim, never workflow-id reuse
# within retention (SPEC-durable-runtime, criterion 30). Run inside the container so
# the host needs no Temporal CLI.
docker compose exec -T temporal \
    temporal operator namespace update --namespace default --retention 168h \
    --address localhost:7233

echo
echo "ready. DATABASE_URL defaults to the compose service on port 5433;"
echo "TEMPORAL_ADDRESS defaults to localhost:7233 (UI on http://localhost:8233)."
