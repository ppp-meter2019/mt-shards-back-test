#!/usr/bin/env bash
# Console-boundary guard — the dependency between the operator console and the runtime
# layer runs ONE WAY:
#
#     tenants.console  ->  tenants          allowed (models, validators, permissions, context)
#     tenants          ->  tenants.console  FORBIDDEN
#
# Why it is a guard and not an app boundary: tenants/console owns no models, so a separate
# Django app would buy an INSTALLED_APPS entry and nothing else (see
# tenants/console/__init__.py). A subpackage gives the same cohesion — but only a grep keeps
# it honest, because Python will happily let the middleware import a DRF viewset.
#
# What breaks without it: the runtime (middleware / resolver / router / celery / checks) must
# stay importable and deployable with the console absent — that is what lets the host project
# take the tenancy layer without the operator UI, and what keeps a request-path import from
# dragging in DRF viewsets, serializers and the admin. A reverse import is silent until then.
#
# When to run: a pre-merge CI step (and/or a local pre-commit) — static, DB-free, source only.
# NOT a runtime/boot check (that is the tenants.E00x system checks).
set -euo pipefail
cd "$(dirname "$0")/.."

# Any reference to the console package from outside it.
MARKER='tenants[._]console|from[[:space:]]+\.console|from[[:space:]]+\.[[:space:]]*import[[:space:]]+console'

# Sanctioned reverse references. Each is a CONVENTION Django imposes on a fixed path, not a
# design choice — anything added here needs the same justification at its import site.
#   tenants/admin.py  django.contrib.admin autodiscovers `<app_label>.admin` by name; it
#                     cannot be told to look in a subpackage, so this one-line shim is what
#                     makes the console's registrations run at all.
ALLOW='^(tenants/admin\.py)$'

mapfile -t HITS < <(
  grep -rlnE --include='*.py' "$MARKER" tenants/ \
    | grep -vE '^tenants/console/' \
    | grep -vE '(/__pycache__/|/migrations/|/tests/)' \
    | sort -u
)

bad=()
if ((${#HITS[@]})); then
  for f in "${HITS[@]}"; do
    grep -qE "$ALLOW" <<<"$f" || bad+=("$f")
  done
fi

if ((${#bad[@]})); then
  echo "FAIL: runtime module(s) importing the operator console:" >&2
  printf '  - %s\n' "${bad[@]}" >&2
  echo "The console may import the runtime, never the reverse. Move the shared piece down" >&2
  echo "into the runtime layer (tenants/models.py, validators.py, permissions.py, ...)," >&2
  echo "or, if Django's conventions force the import, add it to ALLOW in $0 with a note." >&2
  exit 1
fi

echo "OK: runtime does not import tenants.console (${#HITS[@]} sanctioned reference(s))."
