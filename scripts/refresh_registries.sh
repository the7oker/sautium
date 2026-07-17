#!/usr/bin/env bash
# Refresh the gear measurement registries and reimport them.
#
# The AutoEq clone lives on the host (the container mounts it
# read-only), so git runs here; imports run inside the backend where
# the DB pool and guardrails live. Guardrails make this safe to run
# blindly: a broken upstream (zero files, schema change, shrunken
# dataset) aborts loudly BEFORE any write — stale data beats silently
# poisoned data.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== AutoEq: git pull (sparse checkout) =="
git -C data/registry/autoeq pull --ff-only

echo "== spinorama: fetch metadata.json =="
curl -fsSL --max-time 120 "https://www.spinorama.org/json/metadata.json" \
    -o data/registry/spinorama/metadata.json.new
# fetch_json-style floor: a 404 page or truncated body must not
# replace a good snapshot.
[ "$(wc -c < data/registry/spinorama/metadata.json.new)" -gt 100000 ] \
    || { echo "spinorama download too small — keeping old snapshot"; exit 1; }
mv data/registry/spinorama/metadata.json.new data/registry/spinorama/metadata.json

echo "== import (guardrails inside) =="
docker exec -e PYTHONPATH=/app -w /app sautium-backend \
    python gear_registry.py \
    --import-autoeq /app/registry/autoeq \
    --import-spinorama /app/registry/spinorama/metadata.json \
    --match --stats
