"""Request-path identity binding: a credential is valid only on the tenant it was issued for.

Two halves of one invariant, each the analog of the other:
  jwt.py      SchemaBoundJWTAuthentication  — rejects a token whose `schema` claim != the
                                              tenant being served
  session.py  SchemaBoundSessionMiddleware  — rejects a session stamped on another tenant

Both live here rather than in a business app because neither IS business logic: they import
nothing from the app that defines the user model, they are wired ONLY by
settings_multitenant.py, and standalone loads neither. They were in `users/` by history, which
put a tenant-isolation control in the one package a host project replaces wholesale.

Deliberately NOT in the operator-console subpackage: that is UI behind a one-way boundary,
and nothing on the request path may import from it. (Spelling the dotted path here would
trip ci_guard_console_boundary.sh, which greps text and so cannot tell a prose mention
from an import — the exact failure mode ci_guard_ast.py was written to retire.)

CONTRACT, and it binds whoever issues credentials. Both halves are FAIL-CLOSED, so each is only
as good as its stamping side, which stays in the login path a host project owns:
  * token issuance MUST set token["schema"] = connection.schema_name
  * session login MUST go through django.contrib.auth.login(), so the user_logged_in receiver
    stamps session["schema"]
A host whose login path omits either will see every credential of that kind rejected at once —
loudly, on the first request. That is the intended direction: a missing stamp must not be
"healed" by writing the current schema and allowing the request, which would let a replayed
credential through on the attacker's host.
"""
