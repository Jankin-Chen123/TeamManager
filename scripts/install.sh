#!/bin/sh
# Requires Docker Engine and Compose; no host Python or Node installation.
set -eu
cd "$(dirname "$0")/.."
docker compose version >/dev/null
if [ ! -f .env ]; then
  docker run --rm --user "$(id -u):$(id -g)" \
    -v "$(pwd):/setup" -w /setup python:3.12-slim-bookworm \
    python scripts/init_config.py
fi
docker compose up -d --build
count=0
until docker compose exec -T app python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8765/api/health",timeout=3)' >/dev/null 2>&1; do
  count=$((count + 1))
  if [ "$count" -ge 30 ]; then
    echo "Startup did not pass health check. Inspect: docker compose logs app" >&2
    exit 1
  fi
  sleep 2
done
echo "TeamManager is ready on 127.0.0.1:8765. Initial password: TM_ADMIN_PASSWORD in .env."
echo "Public redemption page: /join. Configure a workspace and generate CDKs in the admin console."
echo "For public access, configure HTTPS and TM_COOKIE_SECURE=true; see docs/DEPLOYMENT.md."
