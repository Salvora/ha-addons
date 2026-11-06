#!/bin/sh
# The checks .gitea/workflows/check.yml runs, one part per argument, each inside its tool's image:
#   python     ruff and pytest, in the add-on's base image (ghcr.io/home-assistant/base-python)
#   workflows  actionlint on .gitea/workflows/, shellcheck on ci/*.sh and the add-on's run.sh (rhysd/actionlint, which
#              has shellcheck)
# POSIX sh, because the images are Alpine-based. Run from the repository root.
set -eu
case "${1:-}" in
  python)
    apk add --no-cache --quiet git openssl
    python3 -m pip install --quiet --root-user-action=ignore \
      --requirement ha-gitops-sync/requirements.txt --requirement ci/requirements.txt
    ruff check .
    ruff format --check .
    python3 -m pytest -q
    ;;
  workflows)
    actionlint -color .gitea/workflows/*.yml
    shellcheck ci/*.sh ha-gitops-sync/run.sh
    ;;
  *)
    echo "usage: ci/check.sh python|workflows" >&2
    exit 2
    ;;
esac
