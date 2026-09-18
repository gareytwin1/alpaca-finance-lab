#!/usr/bin/env bash
# Invoked by cron once a minute during market hours, Mon-Fri.
#
# Cron runs with almost no environment (no PATH beyond a minimal default, no
# shell rc files sourced), so this script sets up everything bot.runner
# needs on its own: the working directory, the Alpaca credentials, and the
# python interpreter to run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

if [[ ! -f .env.alpaca ]]; then
    echo "$(date -Is) run_bot_once.sh: .env.alpaca is missing; refusing to run." >&2
    exit 1
fi

set -a
# shellcheck disable=SC1091
source .env.alpaca
set +a

# Cron's PATH is minimal and would otherwise resolve "python3" to the
# system interpreter, which does not have pandas/alpaca-py installed.
PYTHON3="/home/garey/miniconda3/bin/python3"
if [[ ! -x "$PYTHON3" ]]; then
    echo "$(date -Is) run_bot_once.sh: $PYTHON3 not found or not executable." >&2
    exit 1
fi

exec "$PYTHON3" -m bot.runner --once
