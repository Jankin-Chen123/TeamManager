#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
if [ "$#" -ne 1 ]; then
  echo "Usage: sh scripts/update.sh ghcr.io/owner/teammanager:VERSION" >&2
  exit 1
fi
image="$1"
case "$image" in
  ghcr.io/*:*|ghcr.io/*@sha256:*) ;;
  *) echo "Use an explicit GHCR image version or digest" >&2; exit 1 ;;
esac
export TM_IMAGE="$image"
docker compose pull app
mkdir -p backups
chmod 700 backups
stamp=$(date -u +%Y%m%dT%H%M%SZ)
# Preserve the exact old image before stopping the current container.
docker inspect --format '{{.Image}}' "$(docker compose ps -q app)" > "backups/previous-image-$stamp.txt"
cp .env "backups/env-$stamp"
chmod 600 "backups/env-$stamp"
docker compose stop app
docker compose run --rm --no-deps app python -c 'import sqlite3,datetime; p="/app/backups/teammanager-"+datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")+".db"; s=sqlite3.connect("file:/app/data/teammanager.db?mode=ro",uri=True); d=sqlite3.connect(p); s.backup(d); assert d.execute("PRAGMA integrity_check").fetchone()[0]=="ok"; d.close(); s.close(); print(p)'
docker compose up -d --no-build app
count=0
until docker compose exec -T app python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8765/api/health",timeout=3)' >/dev/null 2>&1; do
  count=$((count + 1))
  if [ "$count" -ge 20 ]; then
    echo "Health check failed. Keep backups; follow docs/DEPLOYMENT.md to restore." >&2
    exit 1
  fi
  sleep 2
done
# Persist the selected image only after it starts successfully.
docker run --rm -i --user "$(id -u):$(id -g)" -v "$(pwd):/setup" -w /setup --entrypoint python "$image" - "$image" <<'PY'
from pathlib import Path
import sys
p = Path('.env')
lines = [s for s in p.read_text().splitlines() if not s.startswith('TM_IMAGE=')]
p.write_text('\n'.join(lines) + '\nTM_IMAGE=' + sys.argv[1] + '\n')
PY
echo "Updated to $image. Verify a node and a workspace from the management UI."
