#!/usr/bin/env bash
# Installs ci/requirements.txt into a virtual environment on the runner and puts it first on the PATH of the later
# steps, so their python3 -m addons lines find PyYAML. A venv, because pip may not replace the runner's own packages.
# Usage: bash ci/setup-python.sh
set -euo pipefail

venv="${RUNNER_TEMP:-$(mktemp -d)}/venv"
python3 -m venv "$venv"
"$venv/bin/pip" install --quiet --disable-pip-version-check --requirement ci/requirements.txt
echo "$venv/bin" >> "${GITHUB_PATH:-/dev/null}"
"$venv/bin/python" --version
