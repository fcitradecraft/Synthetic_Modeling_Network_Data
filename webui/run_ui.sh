#!/bin/bash
set -e
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

"$REPO_ROOT/aml-env/bin/python3" -c "import flask" 2>/dev/null || {
  echo "Installing missing dependency (flask)..."
  "$REPO_ROOT/aml-env/bin/pip" install -q flask
}

( sleep 1 && open "http://127.0.0.1:5000" ) &
"$REPO_ROOT/aml-env/bin/python3" "$REPO_ROOT/webui/app.py"
