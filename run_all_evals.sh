#!/usr/bin/env bash
# Task G / Phase 7 regression tool.
#
# Runs the full test suite plus every eval harness in one shot, writing fresh
# timestamped reports (each eval script already timestamps its own output
# under evals/<name>/results/). Manual/on-demand only -- never wired into
# daily.yml or any CI trigger; run this by hand before/after a prompt change
# to get a before/after report pair for compare_reports.py.
#
# Usage:
#   ./run_all_evals.sh              # full runs -- real Sarvam API calls, costs money
#   ./run_all_evals.sh --limit 10   # cheap smoke run, forwarded to every eval script

set -uo pipefail  # no -e: run every step and report every failure, don't stop at the first

cd "$(dirname "${BASH_SOURCE[0]}")"

LIMIT_ARGS=()
if [[ "${1:-}" == "--limit" && -n "${2:-}" ]]; then
  LIMIT_ARGS=(--limit "$2")
fi

FAILED=()

run_step() {
  local name="$1"; shift
  echo
  echo "=== $name ==="
  if "$@"; then
    echo "--- $name: OK ---"
  else
    echo "--- $name: FAILED ---"
    FAILED+=("$name")
  fi
}

run_step "pytest (tests/ + evals/)" python -m pytest tests/ evals/ -q

run_step "triage eval" python evals/triage/run_triage_eval.py "${LIMIT_ARGS[@]}"

run_step "entity_quality eval" python evals/entity_quality/run_entity_quality_eval.py "${LIMIT_ARGS[@]}"

if [[ -f evals/faithfulness/run_faithfulness_eval.py ]]; then
  run_step "faithfulness eval" python evals/faithfulness/run_faithfulness_eval.py "${LIMIT_ARGS[@]}"
else
  echo
  echo "=== faithfulness eval ==="
  echo "skipped: evals/faithfulness/run_faithfulness_eval.py doesn't exist yet (Phase 4/5 not built)"
fi

echo
if [[ ${#FAILED[@]} -eq 0 ]]; then
  echo "All steps passed."
  exit 0
else
  echo "FAILED steps: ${FAILED[*]}"
  exit 1
fi
