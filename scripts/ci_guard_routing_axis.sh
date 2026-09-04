#!/usr/bin/env bash
# Guard — routing AXIS 1 (`current_db`) may be touched only inside tenants/context.py.
#
# Multi-DB multi-tenancy has TWO axes that must move together:
#   axis 1  current_db  -> which Aurora the router picks   (tenants/context.py)
#   axis 2  connection.set_tenant/set_schema -> which schema on THAT connection
# Setting axis 1 alone routes the ORM to a shard whose connection sits on another schema —
# silently wrong data, not an error. The sanctioned doors are the context managers in
# tenants/context.py: tenant_context / schema_context wire BOTH axes; use_alias wires axis 1
# alone, deliberately and documented ("code that manages the schema itself").
#
# READS go through the two named accessors, so nothing outside this module needs the var:
#   active_alias()  -> the alias, "default" when unset      (diagnostics, celery compat)
#   bound_alias()   -> the alias, None when unset           (TenantDatabaseRouter only —
#                      the None is what its strict guard keys on; do NOT coalesce it)
#
# The marker matches ATTRIBUTE ACCESS only (`current_db.set(` / `.get(` / `.reset(`), never
# the bare name — so the dozen prose mentions in docstrings ("the current_db ContextVar",
# "(current_db unset)") stay legal and need no rewording. That is deliberate: renaming the
# variable to `_current_db` would buy a symbol Python does not enforce anyway, at the cost of
# rewriting that prose — and stale prose is a failure mode this repo has already paid for.
#
# When to run: a pre-merge CI step / local pre-commit, alongside the sibling guards. Static,
# DB-free, source only.
set -euo pipefail
cd "$(dirname "$0")/.."

MARKER='current_db\.(set|get|reset)\('
ALLOW='^tenants/context\.py$'

mapfile -t HITS < <(
  grep -rnE --include='*.py' "$MARKER" . \
    | grep -vE '^\./(p_env/)' \
    | grep -vE '(/__pycache__/|/migrations/)' \
    | while IFS= read -r line; do
        file=${line%%:*}
        rest=${line#*:}; code=${rest#*:}    # drop "file:lineno:"
        code=${code%%#*}                    # strip trailing # comment
        if grep -qE "$MARKER" <<<"$code"; then printf '%s\n' "${file#./}"; fi
      done \
    | sort -u
)

bad=()
if ((${#HITS[@]})); then
  for f in "${HITS[@]}"; do
    grep -qE "$ALLOW" <<<"$f" || bad+=("$f")
  done
fi

if ((${#bad[@]})); then
  echo "FAIL: routing axis 1 (current_db) touched outside tenants/context.py:" >&2
  printf '  - %s\n' "${bad[@]}" >&2
  echo "To READ it use bound_alias() (keeps the None sentinel) or active_alias() (coalesces)." >&2
  echo "To SET it use tenant_context(...) / schema_context(...) — both axes — or use_alias()" >&2
  echo "if this code really does manage the schema itself. See tenants/context.py." >&2
  exit 1
fi

echo "OK: routing axis 1 confined to tenants/context.py (${#HITS[@]} file(s) checked)."
