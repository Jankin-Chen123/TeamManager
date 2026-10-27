#!/usr/bin/env sh
# Called only over private SSH after CI has published an immutable image digest.
set -eu
cd "$(dirname "$0")/.."
if [ "$#" -ne 1 ]; then
  echo 'Usage: sh scripts/deploy.sh ghcr.io/owner/repo@sha256:DIGEST' >&2
  exit 1
fi
image="$1"
echo "$image" | grep -Eq '^ghcr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$' || exit 1
test -s .env || { echo 'Initialize server .env before enabling deployment.' >&2; exit 1; }
export TM_IMAGE="$image"
if [ -n "$(docker compose ps -aq app)" ]; then
  exec sh scripts/update.sh "$image"
fi
docker compose pull app
docker compose up -d --no-build --wait --wait-timeout 120 app
# Keep the digest for later manual restarts. Secrets stay on the server.
sed -i '/^TM_IMAGE=/d' .env
printf '\nTM_IMAGE=%s\n' "$image" >> .env
echo "First deployment healthy: $image"
