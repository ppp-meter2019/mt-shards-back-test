"""Schema-bound JWT authentication.

A JWT is signed with one global SECRET_KEY and identifies the user by `user_id`
(a per-schema PK). Each tenant schema has its own auth_user with PKs starting at
1, so stock JWTAuthentication would let a token minted for user_id=N in tenant
`alpha` authenticate as `beta`'s user_id=N (same PK, different person).

The login serializers stamp a `schema` claim on every token. Here we enforce it:
the token is valid only on the schema it was issued for. Fail-closed — a missing
or mismatched claim is rejected, so a token used in the wrong tenant is useless
(it cannot grant cross-tenant access; worst case it's a dead token → 401).
"""

from django.db import connection
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken


class SchemaBoundJWTAuthentication(JWTAuthentication):
    def get_validated_token(self, raw_token):
        token = super().get_validated_token(raw_token)   # signature + expiry checks
        # connection.schema_name is the tenant this request is served on (set by
        # ShardAwareTenantMiddleware). Both issuance and this check read the same
        # field, so same-tenant always matches and cross-tenant never does.
        if token.get("schema") != connection.schema_name:
            raise InvalidToken("Token is not valid for this tenant.")
        return token
