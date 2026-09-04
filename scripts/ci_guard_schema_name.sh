#!/usr/bin/env bash
# Phase 3 guard — `connection.schema_name` is the REAL multi-tenant coupling
# marker (it is injected by the django_tenants DB backend; on the plain PostGIS
# backend used in standalone it does NOT exist → AttributeError). A "no `import
# tenants`" audit is necessary but NOT sufficient.
#
# The marker is read in TWO forms, and the guard catches BOTH:
#   * bare attribute:  connection.schema_name
#   * getattr form:    getattr(connection, "schema_name", <default>)
# The getattr form is one of the standalone-SAFE patterns (returns the default in
# standalone), so it MUST be tracked too — otherwise a new getattr-form reader would
# slip past a bare-attribute-only regex and silently break standalone.
#
# Outside the `tenants` app, the marker may appear ONLY in the audited, standalone-safe
# files below (see deploy/standalone_multitenant_design.md §5). A new, un-audited reader
# would silently break standalone, so CI fails on one.
#
# When to run: a pre-merge CI step (and/or a local pre-commit) — static, DB-free, source
# only. NOT a runtime/boot check (that is the tenants.E00x system checks).
set -euo pipefail
cd "$(dirname "$0")/.."

# The marker in BOTH forms (ERE). ['"] matches a single OR double quote.
MARKER='connection\.schema_name|getattr\([[:space:]]*connection[[:space:]]*,[[:space:]]*['\''"]schema_name['\''"]'

# Audited, standalone-safe readers (each MT-gated or getattr-guarded / not wired).
# These files also compare against the LITERAL "public" rather than calling
# get_public_schema_name(): importing it would pull in django_tenants, which is not
# installed in standalone. That is deliberate — do not 'fix' it. Anything added here
# must carry the same note at its comparison site.
ALLOW='^(users/authentication\.py|users/middleware\.py|users/serializers\.py|users/signals\.py|users/permissions\.py|products/management/commands/seed_products\.py)$'

# Collect files with a REAL (non-#-comment) marker read, outside tenants/.
mapfile -t HITS < <(
  grep -rnE --include='*.py' "$MARKER" . \
    | grep -vE '^\./(tenants/|p_env/)' \
    | grep -vE '(/__pycache__/|/migrations/)' \
    | while IFS= read -r line; do
        file=${line%%:*}                    # ./path/file.py
        rest=${line#*:}; code=${rest#*:}    # drop "file:lineno:"
        code=${code%%#*}                    # strip trailing # comment (as before)
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
  echo "FAIL: un-audited connection.schema_name reader(s) outside tenants/:" >&2
  printf '  - %s\n' "${bad[@]}" >&2
  echo "Make it standalone-safe (MT-gate on settings.USE_MULTITENANT or getattr" >&2
  echo "with a safe default), then add it to ALLOW in $0." >&2
  exit 1
fi

echo "OK: connection.schema_name marker (bare + getattr) confined to audited files (${#HITS[@]} checked)."
