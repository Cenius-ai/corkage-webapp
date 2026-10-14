#!/usr/bin/env bash
#
# Corkage end-to-end demo - non-interactive, offline, exit 0 on success.
#
#   sh demo.sh          (or: bash demo.sh)
#
# Step 1 seeds the standard preview cellar at the normal location
# ($CORKAGE_HOME/store.json, default ~/.corkage/store.json) with the demo
# passphrase, so `corkage cork list` works straight after a fresh clone.
# Step 2 replays a full keeper session in a throwaway cellar so the transcript
# is repeatable and never touches your own data.

set -eu
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
run() {
    printf '\n$ %s\n' "$*"
    "$@"
}

echo "Corkage demo - cork log, cellar readings and soak/decant reminders"
echo "================================================================"

# 1. standard demo cellar (idempotent; never overwrites a foreign cellar)
run "$PY" corkage.py --version
run "$PY" corkage.py demo

# 2. full session in a scratch cellar
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
CORKAGE_HOME="$scratch/cellar"
CORKAGE_PASSPHRASE=cenius
export CORKAGE_HOME CORKAGE_PASSPHRASE

run "$PY" corkage.py init
run "$PY" corkage.py cork list
run "$PY" corkage.py reading list
run "$PY" corkage.py reminder list

run "$PY" corkage.py cork add --bottle "Nebbiolo 2015" --condition crumbly \
    --note "dry and brittle, crumbled under the worm"
run "$PY" corkage.py reading add --temp 13.7 --humidity 69 --note "after topping up"
due="$("$PY" -c 'import datetime;print((datetime.date.today()+datetime.timedelta(days=21)).isoformat())')"
run "$PY" corkage.py reminder add --kind soak --due "$due" --bottle "Nebbiolo 2015" \
    --note "soak the replacement cork overnight"

run "$PY" corkage.py cork list
run "$PY" corkage.py reading list
run "$PY" corkage.py reminder list

printf '\n$ %s\n' "$PY corkage.py reminder done --id 1"
"$PY" corkage.py reminder done --id 1

printf '\n--- guard rails (each line is one message plus the exit code) ---\n'
show_failure() {
    printf '\n$ %s\n' "$*"
    set +e
    "$@"
    code=$?
    set -e
    printf '[exit %s]\n' "$code"
}
show_failure "$PY" corkage.py cork add --bottle "Rioja 2012" --condition mushy
show_failure "$PY" corkage.py reading add --temp 90 --humidity 50
show_failure "$PY" corkage.py reminder add --kind soak --due 31/12/2026 --bottle "Rioja 2012"
show_failure env CORKAGE_PASSPHRASE=wrong "$PY" corkage.py cork list
show_failure env CORKAGE_HOME="$scratch/other" "$PY" corkage.py cork list

printf '\nDemo complete: cork log, cellar readings and reminders all exercised.\n'
