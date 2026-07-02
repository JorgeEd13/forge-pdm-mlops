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

echo "[entrypoint] ==== forge-pdm-mlops HF entrypoint starting ===="

STORE_DIR="${MLFLOW_STORE_DIR:-/mlflow}"
FIXTURE="data/sample_readings.parquet"

# Guard: on HF the fixture is an LFS object. If it wasn't smudged into a real file, it is
# a tiny text pointer starting with "version https://git-lfs…" — training on it would
# fail confusingly. Detect that explicitly and say so, rather than serve an empty registry.
if head -c 64 "${FIXTURE}" 2>/dev/null | grep -q "git-lfs"; then
  echo "[entrypoint] FATAL: ${FIXTURE} is an un-smudged Git LFS pointer, not a real" >&2
  echo "[entrypoint] parquet. The demo bake needs the real file. (Check LFS on the Space.)" >&2
  exit 1
fi
echo "[entrypoint] fixture is a real file — OK."

# (Re)bake the demo model unless a production model is ALREADY promoted in this store
# (checked via the alias, not just the DB file — a leftover empty DB must not make us skip).
# seed_demo_registry --skip-if-promoted is idempotent: it retrains only when needed.
echo "[entrypoint] ensuring a promoted demo model in ${STORE_DIR}…"
mkdir -p "${STORE_DIR}"
python scripts/seed_demo_registry.py --store-dir "${STORE_DIR}" --skip-if-promoted
echo "[entrypoint] demo model ready."

echo "[entrypoint] starting serving on :${PORT:-8000}"
exec pdm serve --host 0.0.0.0 --port "${PORT:-8000}"
