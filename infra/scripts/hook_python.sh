#!/bin/sh
# Run an azd hook script with the deployment's Python, even from a fresh shell.
# Order: $PYTHON, then `azd env get-value PYTHON`, then python3.
# Set it once per azd environment: azd env set PYTHON <venv>/bin/python
set -u

interpreter="${PYTHON:-}"
if [ -z "$interpreter" ] && command -v azd >/dev/null 2>&1; then
  # Read stdout only; a missing key is not an error here.
  interpreter="$(azd env get-value PYTHON 2>/dev/null)" || interpreter=""
fi
if [ -z "$interpreter" ]; then
  interpreter=python3
fi
if ! command -v "$interpreter" >/dev/null 2>&1; then
  echo "hook_python.sh: Python not found: $interpreter" >&2
  echo "Set the deployment interpreter once: azd env set PYTHON <venv>/bin/python" >&2
  exit 127
fi
exec "$interpreter" "$@"
