#!/usr/bin/env bash
# Update the Hugging Face Space (F6) from the current `main`.
#
# The Space tracks a dedicated `space-deploy` branch that carries three things NOT on the
# GitHub `main` showcase branch:
#   * a README with the HF Space front-matter (title/sdk/app_port/…) as its first lines,
#   * a `.gitattributes` that LFS-tracks the committed binaries (HF requires binaries via
#     LFS/Xet — a plain-git binary push is rejected),
#   * the self-contained bake image as the *literal* `Dockerfile` (HF ignores the
#     front-matter `dockerfile_path`, so the file it builds must be named `Dockerfile`).
# Keeping those off `main` means the GitHub repo stays LFS-free and its fixture a normal
# file (so `clone && pytest` needs no LFS).
#
# This script forwards the new `main` commits onto `space-deploy` (cherry-pick, since the
# two branches have intentionally unrelated history) and pushes to the Space remote.
#
# Prereqs (one-time):
#   git remote add space https://huggingface.co/spaces/JorgeEd/forge-pdm-mlops
#   git lfs install
# Usage (from the repo root, on an up-to-date `main`):
#   bash scripts/deploy_space.sh [<commit-ish>...]
# With no args, it cherry-picks every commit on `main` that `space-deploy` doesn't have.
set -euo pipefail

BRANCH="space-deploy"
REMOTE="space"

start_branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "${start_branch}" != "main" ]; then
  echo "warning: you are on '${start_branch}', not 'main' — deploying its commits." >&2
fi

git checkout "${BRANCH}"

if [ "$#" -gt 0 ]; then
  echo "[deploy] cherry-picking: $*"
  git cherry-pick "$@"
else
  echo "[deploy] cherry-picking every commit on main not yet on ${BRANCH}…"
  # --no-merges: linear history only; cherry-pick can't apply merge commits blindly.
  mapfile -t picks < <(git rev-list --reverse --no-merges "${BRANCH}..main")
  if [ "${#picks[@]}" -eq 0 ]; then
    echo "[deploy] nothing new to forward."
  else
    git cherry-pick "${picks[@]}"
  fi
fi

echo "[deploy] pushing ${BRANCH} → ${REMOTE}/main…"
git push "${REMOTE}" "${BRANCH}:main"

git checkout "${start_branch}"
echo "[deploy] done. Watch the build at https://huggingface.co/spaces/JorgeEd/forge-pdm-mlops"
echo "[deploy] then verify: curl -s https://jorgeed-forge-pdm-mlops.hf.space/health"
