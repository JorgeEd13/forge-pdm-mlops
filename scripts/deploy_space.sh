#!/usr/bin/env bash
# Update the Hugging Face Space from the current `main`.
#
# The Space tracks a dedicated `space-deploy` branch whose history is **intentionally
# unrelated** to `main`, because it carries three things that must NOT exist on the GitHub
# showcase branch:
#   * `README.md`   — the HF Space front-matter (title/sdk/app_port/…) as its first lines,
#   * `.gitattributes` — LFS tracking for the committed binaries (HF *requires* binaries via
#     LFS/Xet; a plain-git binary push is rejected),
#   * `Dockerfile`  — the self-contained bake image under the DEFAULT name, because HF builds
#     a literal `Dockerfile` and does not reliably honour the front-matter `dockerfile_path`
#     (F6 bug #1, ADR-014). On `main` that name belongs to the F4 mounted-volume image.
# Keeping those off `main` means the GitHub repo stays LFS-free and its fixture a normal file
# (so `clone && pytest` needs no LFS).
#
# ## Why this script SYNCS PATHS and does not cherry-pick (rewritten 2026-07-14)
#
# It used to cherry-pick "every commit on main that space-deploy doesn't have". That is
# **broken by construction**: the histories are unrelated, so `space-deploy..main` is EVERY
# commit on main — running it with no arguments replays the repo from its first commit (F0)
# and drowns in add/add conflicts. It only ever appeared to work because it was always called
# with explicit commit-ish arguments.
#
# What the deploy actually *is* is a content sync: take main's code and docs, leave the three
# Space-specific files alone. So that is what it now does — one honest commit on
# `space-deploy`, no conflict theatre.
#
# ## The LFS trap (bit us 2026-07-14)
#
# The LFS objects for `space-deploy` exist ONLY on the HF remote — `main` is deliberately
# LFS-free, so they were never pushed to GitHub. But git-lfs resolves objects against the
# DEFAULT remote (`origin` = GitHub), which correctly answers "I do not have that object" →
# `smudge filter lfs failed` on checkout, and a half-applied checkout leaves the working tree
# contaminated with space-deploy content under a `main` HEAD. Pinning `lfs.url` at the Space
# is the fix; it is inert on `main`, which LFS-tracks nothing.
#
# Prereqs (one-time):
#   git remote add space https://huggingface.co/spaces/JorgeEd/forge-pdm-mlops
#   git lfs install
# Usage (from the repo root, on an up-to-date + COMMITTED `main`):
#   bash scripts/deploy_space.sh
set -euo pipefail

BRANCH="space-deploy"
REMOTE="space"
LFS_URL="https://huggingface.co/spaces/JorgeEd/forge-pdm-mlops.git/info/lfs"

# Text/code paths synced from `main`. Everything the app and its docs need — and NOTHING that
# is Space-specific (Dockerfile / README.md / .gitattributes are owned by this branch, above).
SYNC_PATHS=(
  src tests docs scripts configs .github
  CLAUDE.md PLAN.md pyproject.toml docker-compose.yml
  Dockerfile.hf Dockerfile.worker .gcloudignore .gitignore
)

# Binary paths that this branch tracks with **LFS** while `main` keeps them as plain files.
# They CANNOT be synced with `git checkout main -- <path>`: that stages main's blob verbatim,
# **bypassing the LFS clean filter**, and the HF pre-receive hook then rejects the whole push
# ("Your push was rejected because it contains binary files"). They must be written into the
# worktree and `git add`-ed, so the filter runs and the committed blob is a POINTER.
LFS_PATHS=(
  data/sample_readings.parquet
  assets/logo.png
)

start_branch="$(git rev-parse --abbrev-ref HEAD)"

# A dirty tree here is how you lose work: the checkout below would carry it across branches.
if [ -n "$(git status --porcelain)" ]; then
  echo "error: working tree is dirty — commit or stash before deploying the Space." >&2
  exit 1
fi
if [ "${start_branch}" != "main" ]; then
  echo "warning: you are on '${start_branch}', not 'main' — syncing ITS content." >&2
fi

# Point git-lfs at the Space (see "The LFS trap" above). Idempotent.
git config lfs.url "${LFS_URL}"

cleanup() { git checkout -q "${start_branch}" 2>/dev/null || true; }
trap cleanup EXIT

git checkout "${BRANCH}"

echo "[deploy] syncing app + docs from ${start_branch} → ${BRANCH}…"
git checkout "${start_branch}" -- "${SYNC_PATHS[@]}"

# The LFS-tracked binaries: write the bytes, then `git add` so the clean filter turns them
# into pointers (see LFS_PATHS above for why `git checkout main --` is wrong here).
for path in "${LFS_PATHS[@]}"; do
  if git cat-file -e "${start_branch}:${path}" 2>/dev/null; then
    mkdir -p "$(dirname "${path}")"
    git show "${start_branch}:${path}" > "${path}"
    git add "${path}"
  fi
done

# Guard: every staged blob under an LFS path must be a POINTER, never the raw bytes. If this
# trips, the push would be rejected by HF anyway — fail here, where the message is legible.
for path in "${LFS_PATHS[@]}"; do
  if git diff --cached --name-only | grep -qxF "${path}"; then
    if ! git cat-file -p ":${path}" | head -1 | grep -q '^version https://git-lfs'; then
      echo "error: ${path} is staged as RAW BINARY, not an LFS pointer." >&2
      echo "       HF rejects binary files; is git-lfs installed (\`git lfs install\`)?" >&2
      git reset --hard >/dev/null
      exit 1
    fi
  fi
done

if git diff --cached --quiet; then
  echo "[deploy] nothing to forward — the Space is already at ${start_branch}."
else
  # Fail loud if a Space-owned file somehow got staged: overwriting `Dockerfile` from main is
  # exactly the F6 bug that served a no-bake image and a `model_loaded=false` /health.
  if git diff --cached --name-only | grep -qE '^(Dockerfile|README\.md|\.gitattributes)$'; then
    echo "error: a Space-owned file was staged (Dockerfile / README.md / .gitattributes)." >&2
    echo "       Those belong to ${BRANCH}. Aborting rather than breaking the Space build." >&2
    git reset --hard >/dev/null
    exit 1
  fi
  git status --short
  git commit -m "deploy(space): sync app + docs from ${start_branch}"
fi

echo "[deploy] pushing ${BRANCH} → ${REMOTE}/main…"
git push "${REMOTE}" "${BRANCH}:main"

echo "[deploy] done. Watch the build at https://huggingface.co/spaces/JorgeEd/forge-pdm-mlops"
echo "[deploy] then verify: curl -s https://jorgeed-forge-pdm-mlops.hf.space/health"
