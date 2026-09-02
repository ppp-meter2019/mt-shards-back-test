#!/usr/bin/env bash
# Phase 3 guard — the tenant context managers `schema_context` / `tenant_context` MUST be
# imported from `tenants.context` (OUR shard-aware versions: they wire current_db + the schema
# on the SHARD connection). Importing the django_tenants.utils originals is FORBIDDEN in project
# code: they are single-DB, and binding them is timing-dependent (the apps.ready() monkeypatch
# only re-points LATE importers, so a module that imported them before ready() would silently
# keep the wrong, default-only behavior).
#
# apps.py patches via the ALIASED assignment form (`import django_tenants.utils as dt_utils;
# dt_utils.schema_context = ...`), which neither pattern below matches — so it needs no allowlist.
#
# When to run: a pre-merge CI step / local pre-commit (static, DB-free), alongside
# ci_guard_schema_name.sh. NOT a runtime check.
set -euo pipefail
cd "$(dirname "$0")/.."

# Forbidden: `from django_tenants.utils import … schema_context/tenant_context …`
#        OR: `django_tenants.utils.schema_context` / `.tenant_context` (attribute form).
MARKER='from django_tenants\.utils import .*(schema_context|tenant_context)|django_tenants\.utils\.(schema_context|tenant_context)'

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

if ((${#HITS[@]})); then
  echo "FAIL: schema_context/tenant_context imported from django_tenants.utils (use tenants.context):" >&2
  printf '  - %s\n' "${HITS[@]}" >&2
  echo "Import the shard-aware versions from tenants.context instead. Only tenants/apps.py may" >&2
  echo "reference the django_tenants.utils originals — and only to monkeypatch them." >&2
  exit 1
fi

echo "OK: schema_context/tenant_context sourced from tenants.context only (${#HITS[@]} offenders)."
