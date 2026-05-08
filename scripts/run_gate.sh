#!/usr/bin/env bash
# Wizard-1 validation gate runner.
#
# Loads ANTHROPIC_API_KEY from a local `.env` file (gitignored) so you
# don't have to mess with shell exports, then runs the gate.
#
# Usage:
#   bash scripts/run_gate.sh           # replay mode (offline, free)
#   bash scripts/run_gate.sh --live    # live mode (calls Anthropic, ~$0.07)
#
# To set up `.env`:
#   1. Create a file named exactly `.env` in this folder
#      (/Users/hurshpatel/Documents/praxis/praxis-deid-tool/.env)
#   2. One line:
#      ANTHROPIC_API_KEY=sk-ant-<your-new-key>
#   3. Save. The .gitignore prevents it from ever being committed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env ]]; then
  set -o allexport
  # shellcheck disable=SC1091
  source .env
  set +o allexport
fi

if [[ "${1:-}" == "--live" ]]; then
  if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "ERROR: ANTHROPIC_API_KEY not set."
    echo ""
    echo "Create a .env file in this folder with one line:"
    echo "  ANTHROPIC_API_KEY=sk-ant-<your-new-key>"
    echo ""
    echo "Or export the key in your shell before running this script."
    exit 2
  fi
  echo "Running LIVE gate (calls Anthropic API; ~\$0.07 expected)..."
  echo ""
  exec python3 scripts/validate_wizard.py --live
else
  echo "Running REPLAY gate (offline, free, uses recorded fixture)..."
  echo ""
  exec python3 scripts/validate_wizard.py --replay
fi
