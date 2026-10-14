#!/usr/bin/env bash
#
# Corkage setup - installs project-level dependencies, installs the `corkage`
# console entry point, seeds the demo cellar, then EXITS.
# No sudo, no system packages, no network services: the app is standard library
# only, so the dependency step is a no-op apart from the test runner.
#
#   bash install.sh

set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

if ! command -v "$PY" >/dev/null 2>&1; then
    echo "error: $PY was not found on PATH (Corkage needs Python 3.9+)" >&2
    exit 1
fi
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "error: Corkage needs Python 3.9 or newer ($PY is $("$PY" -V 2>&1))" >&2
    exit 1
fi

echo "1/4  refreshing pip tooling"
"$PY" -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1 \
    || echo "     note: pip tooling was not refreshed (offline or read-only environment)"
"$PY" -m pip --version

echo "2/4  installing dependencies from requirements.txt"
"$PY" -m pip install --upgrade -r requirements.txt

echo "3/4  installing the corkage console entry point"
if "$PY" -m pip install --no-build-isolation -e . >/dev/null 2>&1; then
    echo "     installed: corkage (also runs as: $PY corkage.py)"
else
    echo "     note: console entry point not installed; run: $PY corkage.py ..." >&2
fi

echo "4/4  seeding the demo cellar (idempotent)"
"$PY" corkage.py demo

"$PY" -c "import corkage" || { echo "error: corkage.py failed to import" >&2; exit 1; }
echo
echo "Setup complete. Nothing is running in the background."
echo "Try: corkage cork list        (demo passphrase: cenius)"
echo "Or : $PY corkage.py cork list"
