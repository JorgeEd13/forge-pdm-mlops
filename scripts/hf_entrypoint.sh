#!/usr/bin/env bash
# HF Spaces container entrypoint (F6).
#
# Bake the demo registry at *startup* (as the runtime user, at the runtime path) rather
# than at build time. This sidesteps two container-only failure modes a build-time bake
# hit: (1) the artifact locations MLflow writes are absolute, so they must be created by
# the same user, at the same path, that later serves them; (2) on HF the committed
# fixture is an LFS object that is only smudged into a real file in the running container,
# not necessarily in the build context. Baking here runs after both are settled.
#
# Idempotent: if a production model is already present (e.g. a warm restart with a
# persisted store), skip the bake and serve immediately.
set -euo pipefail

STORE_DIR="${MLFLOW_STORE_DIR:-/mlflow}"
DB="${STORE_DIR}/mlflow.db"
FIXTURE="data/sample_readings.parquet"

# Guard: on HF the fixture is an LFS object. If it wasn't smudged into a real file, it is
# a tiny text pointer starting with "version https://git-lfs…" — training on it would
# fail confusingly. Detect that explicitly and say so, rather than serve an empty registry.
if head -c 64 "${FIXTURE}" 2>/dev/null | grep -q "git-lfs"; then
  echo "[entrypoint] FATAL: ${FIXTURE} is an un-smudged Git LFS pointer, not a real" >&2
  echo "[entrypoint] parquet. The demo bake needs the real file. (Check LFS on the Space.)" >&2
  exit 1
fi

if [ ! -f "${DB}" ]; then
  echo "[entrypoint] no registry at ${DB} — baking the demo model (trains on the fixture)…"
  python scripts/seed_demo_registry.py --store-dir "${STORE_DIR}"
  echo "[entrypoint] bake done."
else
  echo "[entrypoint] registry present at ${DB} — skipping bake."
fi

echo "[entrypoint] starting serving on :${PORT:-8000}"
exec pdm serve --host 0.0.0.0 --port "${PORT:-8000}"
