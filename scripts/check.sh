#!/usr/bin/env bash
# Run the full quality gate: lint, format, types, tests.
#
#   ./scripts/check.sh          # check formatting, do not modify files
#   ./scripts/check.sh --fix    # apply lint fixes and formatting
#
# Exits non-zero on the first failure so it works as a pre-commit or CI step.
set -euo pipefail

cd "$(dirname "$0")/.."

FIX=0
[[ "${1:-}" == "--fix" ]] && FIX=1

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

if [[ $FIX -eq 1 ]]; then
    step "ruff check --fix"
    uv run ruff check --fix src/ tests/
    step "ruff format"
    uv run ruff format src/ tests/
else
    step "ruff check"
    uv run ruff check src/ tests/
    step "ruff format --check"
    uv run ruff format --check src/ tests/
fi

step "mypy"
uv run mypy

step "pytest"
uv run pytest -q

printf '\n\033[32mAll checks passed.\033[0m\n'
