#!/usr/bin/env bash
# Thin wrapper kept so existing invocations and doc references keep working.
# The rule itself lives in scripts/ci_guard_ast.py, which parses SYNTAX rather than text —
# see that file's module docstring for the three ways the previous grep version failed.
#
# When to run: BY HAND, or from a pre-merge step if one is ever wired. Nothing invokes it
# automatically today: scripts/ci_mode.sh runs `check`, `makemigrations --check` and the test
# suite, and does not touch the guards.
set -euo pipefail
cd "$(dirname "$0")/.."
exec "${PYTHON:-python}" scripts/ci_guard_ast.py redis
