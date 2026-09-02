#!/usr/bin/env bash
# Phase 3 — run the DB-free CI checks for ONE mode.
#   scripts/ci_mode.sh mt          # USE_MULTITENANT=1: full suite (tenants + users)
#   scripts/ci_mode.sh standalone  # USE_MULTITENANT=0: business/users only (no tenants)
#
# The suite is entirely SimpleTestCase (no live DB). `makemigrations --check` and
# `check` are DB-free too, so CI needs no Postgres service.
#
# NOTE: CI must NOT have tenants_back/settings_local.py (it is gitignored and pins
# the django_tenants DB engine, which would mask the standalone backend). A clean
# checkout satisfies this automatically.
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="${1:?usage: ci_mode.sh mt|standalone}"
PY="${PYTHON:-python}"

case "$MODE" in
  mt)
    export USE_MULTITENANT=1
    labels=()                                     # discover everything
    ;;
  standalone)
    export USE_MULTITENANT=0
    # `tenants` is not installed here; its tests import tenants.models and would
    # fail at collection. Run explicit labels that exist in standalone instead.
    labels=(users customers drivers cars products orders)
    ;;
  *)
    echo "unknown mode: $MODE (expected mt|standalone)" >&2
    exit 2
    ;;
esac

echo "== [$MODE] USE_MULTITENANT=$USE_MULTITENANT :: django check =="
$PY manage.py check

echo "== [$MODE] makemigrations --check (fail if a model change lacks a migration) =="
$PY manage.py makemigrations --check --dry-run

echo "== [$MODE] tests =="
if ((${#labels[@]})); then
  $PY manage.py test "${labels[@]}" --verbosity=1
else
  $PY manage.py test --verbosity=1
fi

echo "== [$MODE] PASS =="
