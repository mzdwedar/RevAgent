#!/usr/bin/env bash
# Bring the dev substrate up and migrate it. One command, as T1 requires.
#
# The test suite does not skip when Postgres is missing - it fails and points here.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "----- postgres"
docker compose up --wait

echo "----- migrations"
uv run agentstack-migrate up

echo
echo "ready. DATABASE_URL defaults to the compose service on port 5433."
