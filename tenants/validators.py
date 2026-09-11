"""Hostname + reserved-host validation for tenant Domains, and schema-name validation.

Two tiers (see the reserved-host design discussion):

  * Hard invariants, enforced in CODE and never defeatable by the rules table:
      - a Domain must be a syntactically valid hostname;
      - the platform/public-tenant's own hosts are always reserved.
  * Soft, operator-managed rules, stored in tenants.ReservedHostRule and editable
    from the public admin site / management API. These ADD reservations on top of
    the code invariants.

`validate_tenant_domain` is the single entry point used by the DRF serializer and
by Domain.clean() (admin). Management commands (bootstrap_*) are operator-trusted
and intentionally bypass this, matching how Tenant/Shard creation bypass clean().

SCHEMA NAMES — three names, three distinct contracts. They are deliberately separate
functions rather than one shared regex: a single pattern serving both "may we create this?"
and "is this safe in SQL?" has to answer two different questions, and the call sites then
diverge on how they apply it (`match` vs `fullmatch`, normalize vs not):

  validate_schema_name()      "is this a name we want to CREATE?"  normalizes, then raises
  is_safe_schema_identifier() "can this string break out of quotes?"  boolean floor
  quote_schema()              "give me this as an SQL identifier"  validates AND quotes

The middle one is deliberately far more permissive than the first: the first is the
CONVENTION for new names, the second is the SAFETY floor for values that may predate it
(a data migration out of the legacy single-tenant estate, a manual INSERT).
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django_tenants.utils import get_public_schema_name

if TYPE_CHECKING:                       # annotation-only: models are imported lazily below
    from .models import ReservedHostRule

# A single DNS label: ASCII letters/digits/hyphen, 1-63 chars, no leading/trailing
# hyphen. Bounded quantifier, no nesting => LINEAR match, zero backtracking surface.
# Used to validate a whole hostname label-by-label (validate_hostname) AND a
# ReservedHostRule LABEL value (validate_label). ASCII-only on purpose (no IDN);
# callers lower-case first.
_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

HOSTNAME_MAX = 253


def normalize_host(value: str) -> str:
    """Lower-case, strip surrounding whitespace and EVERY trailing dot.

    Aggressive on purpose: this is the CANONICALIZER Domain.save() applies on every write
    path, including the ones that bypass validation (.create() from operator-trusted
    commands, data migrations). Its job is to produce a value that can still match an
    incoming Host header — leaving a trailing dot there would make the tenant silently
    unreachable, since request.get_host() returns the raw header and the column compares
    exactly. Refusing malformed input is validate_hostname()'s job, not this one's, and it
    inspects the RAW value so `a.b..` is REJECTED rather than repaired.
    """
    return (value or "").strip().lower().rstrip(".")


def validate_hostname(value: str) -> str:
    """Return the normalized host or raise ValidationError if malformed.

    Validated label-by-label (no nested-quantifier regex), so there is no
    catastrophic-backtracking surface regardless of input length. Also rejects an
    all-numeric top label (an IPv4-like host such as 1.2.3.4 / 999), which is never
    a valid tenant domain and would collide with by-IP request paths.
    """
    # NOT normalize_host(): that strips EVERY trailing dot, which would repair `a.b..` into
    # a valid host. Exactly ONE trailing dot is legal (it denotes the DNS root, so
    # `example.com.` and `example.com` are the same host); anything beyond it leaves an
    # empty label, which the per-label loop below then rejects — the same verdict `a..b`
    # already gets. Without this the same malformation passed or failed depending only on
    # WHERE in the string it appeared.
    host = (value or "").strip().lower()
    if host.endswith("."):
        host = host[:-1]
    if not host:
        raise ValidationError("Hostname is required.")
    if len(host) > HOSTNAME_MAX:
        raise ValidationError(f"Hostname too long (max {HOSTNAME_MAX} characters).")
    labels = host.split(".")
    if not all(_LABEL_RE.fullmatch(lbl) for lbl in labels):
        raise ValidationError(
            "Not a valid hostname (ASCII letters, digits and hyphens; dot-separated "
            "labels of 1-63 chars, no leading/trailing hyphen)."
        )
    if labels[-1].isdigit():
        raise ValidationError("Top-level label cannot be all-numeric (looks like an IP address).")
    return host


def validate_label(value: str) -> str:
    """Return the normalized single DNS label or raise (for LABEL rules).

    Lower-cases and trims but does NOT run normalize_host: that strips trailing dots, so
    `www.` would silently become the accepted label `www` — while the error below promises
    "no dots". A LABEL rule value with a dot in it is a mistake, not a formatting variant.
    """
    label = (value or "").strip().lower()
    if not _LABEL_RE.fullmatch(label):
        raise ValidationError(
            "Must be a single DNS label (no dots): ASCII letters, digits and "
            "hyphens, 1-63 chars, no leading/trailing hyphen."
        )
    return label


def _public_hosts() -> set:
    """Hosts owned by the public/management tenant — always reserved (code invariant)."""
    from .models import Domain
    return set(
        Domain.objects.filter(tenant__schema_name=get_public_schema_name())
        .values_list("domain", flat=True)
    )


def matching_rule(host: str) -> ReservedHostRule | None:
    """Return the first active ReservedHostRule that reserves `host`, or None.

    Called only on the (rare) tenant/domain create path, so a per-call scan of the
    active rules is fine — no caching needed.
    """
    from .models import ReservedHostRule

    host = normalize_host(host)
    for rule in ReservedHostRule.objects.filter(is_active=True):
        if rule.matches(host):
            return rule
    return None


def reserved_schema_labels() -> set:
    """Values of active GLOBAL label rules (base_domain="").

    Only global label rules reserve schema names: a global label means "this word
    is reserved everywhere, including as an identifier", whereas a base-scoped label
    is explicitly about hosts under a specific base and does not constrain schema
    names. EXACT/SUFFIX rules are hostnames (with dots) and can never equal a schema
    name, so they are irrelevant here.
    """
    from .models import ReservedHostRule
    return {
        normalize_host(r.value)
        for r in ReservedHostRule.objects.filter(
            is_active=True, match_type=ReservedHostRule.MatchType.LABEL, base_domain="")
    }


# ---------------------------------------------------------------------------
# Schema names
# ---------------------------------------------------------------------------
# The shape a NEW tenant schema may take. Every constraint is load-bearing:
#   (?!pg_)        reserved by PostgreSQL for system schemas. Case-insensitive for free,
#                  because validate_schema_name() lower-cases before matching.
#   [a-z0-9] first a leading digit IS allowed ("1st_choice" is a legitimate company name).
#                  Postgres would need such a name quoted — which is exactly why every SQL
#                  site goes through quote_schema() rather than hand-written quotes.
#   [a-z] only     upstream schema_exists() compares LOWER(nspname)=LOWER(%s) while
#                  CREATE SCHEMA "X" is case-SENSITIVE, so a mixed-case name makes those two
#                  disagree about whether a schema exists — migrate_schemas would skip
#                  CREATE and then migrate into a schema that is not there. Hence names are
#                  FOLDED to lower case, never merely rejected.
#   _ not -        `-` is the minus operator, so a hyphenated name is unusable unquoted in
#                  psql / pg_dump -n / an ad-hoc incident query. Also FOLDED, not rejected.
#   no . / / \     schema_name also lands in a filesystem path upstream
#                  (django_tenants.utils.get_tenant_path -> os.path.join), so path
#                  separators and dot segments must never reach it.
#   <= 63          PostgreSQL NAMEDATALEN - 1; longer is silently truncated.
SCHEMA_NAME_RE = re.compile(r"^(?!pg_)[a-z0-9][a-z0-9_]{0,62}$")

# What can actually break OUT of the two shapes a schema name is interpolated into:
#   ours      CREATE / DROP SCHEMA "{s}"   -> only a bare `"` terminates a quoted identifier
#   upstream  SET search_path = '{s}'      -> only a bare `'` terminates a string literal
# A quoted PostgreSQL identifier does NOT process backslash escapes and `;` does not
# terminate it, so neither is a break-out character. Control bytes are excluded because
# they corrupt logs and psycopg rejects NUL anyway.
_UNSAFE_IDENT_RE = re.compile(r"[\x00-\x1f\"']")

SCHEMA_NAME_MAX = 63


def normalize_schema_name(value: str) -> str:
    """Canonical form: trimmed, lower-cased, hyphens folded to underscores.

    Folded rather than rejected because both are what a HUMAN types when mirroring a
    hostname: `Freedom-First` from `freedom-first.example.com`. A host label legitimately
    allows `-` and mixed case (see _LABEL_RE) while a schema name does not, so the mapping
    host-label -> schema_name is NOT the identity — it goes through here:

        cadc.example.com          -> cadc
        freedom-first.example.com -> freedom_first
        24-7-transit.example.com  -> 24_7_transit
    """
    return (value or "").strip().lower().replace("-", "_")


def validate_schema_name(value: str) -> str:
    """Normalize, then raise django ValidationError if the result is still unusable.

    The single entry point for the API serializer, Tenant.clean() and bootstrap_tenant, so
    the admin and the API enforce one rule. Returns the NORMALIZED name — callers must use
    the return value, not the input.
    """
    name = normalize_schema_name(value)
    if not name:
        raise ValidationError("Schema name is required.")
    if len(name) > SCHEMA_NAME_MAX:
        raise ValidationError(
            f"Schema name is too long ({len(name)} > {SCHEMA_NAME_MAX} characters). "
            f"PostgreSQL truncates past that, which would silently alias two tenants."
        )
    if not SCHEMA_NAME_RE.fullmatch(name):
        raise ValidationError(
            f"'{name}' is not a usable schema name. Use ASCII: letters, digits and "
            f"underscores, starting with a letter or a digit, and not starting with 'pg_'. "
            f"Upper case and hyphens are folded automatically (Foo-Bar -> foo_bar); "
            f"quotes, spaces, dots and non-ASCII are not accepted."
        )
    return name


def is_safe_schema_identifier(value: str) -> bool:
    """Whether `value`, EXACTLY as it stands, is safe to interpolate as an SQL identifier.

    DELIBERATELY more permissive than SCHEMA_NAME_RE, and that gap is the point: this is the
    safety floor for names that may predate validate_schema_name() (django-tenants' own
    validator on the model field is `^(?!pg_).{1,63}$`, i.e. nearly anything). Refusing to
    serve such a name over a NAMING preference would break a working tenant; refusing to
    serve one that can break out of quotes is the whole job. Does NOT normalize.
    """
    return (
        bool(value)
        and len(value) <= SCHEMA_NAME_MAX
        and not _UNSAFE_IDENT_RE.search(value)
    )


def quote_schema(name: str) -> str:
    """The ONLY sanctioned way to put a schema name into SQL: validate AND quote in one
    call, so a call site cannot do one without the other.

    Why not `is_safe_schema_identifier(x)` and then f'"{x}"' by hand: that leaves the quotes
    optional, while SCHEMA_NAME_RE deliberately admits names (a leading digit) that are ONLY
    usable quoted. Why not django's connection.ops.quote_name(): for PostgreSQL it is a bare
    '"%s"' % name with NO escaping of an internal `"`, so it cannot rescue an unvalidated
    name. Quoting and validation are only safe TOGETHER.

    Raises ValueError (a programming/data error, not user input — the ValidationError paths
    above are where user input is rejected).
    """
    if not is_safe_schema_identifier(name):
        raise ValueError(
            f"refusing to build SQL for schema {name!r}: not a safe SQL identifier "
            f"(quotes, control characters, empty or over {SCHEMA_NAME_MAX} chars). A name "
            f"that predates validation must be corrected on the Tenant row first."
        )
    return f'"{name}"'


def validate_tenant_schema_name(value: str) -> str:
    """Reject a schema_name that collides with a reserved global subdomain label
    (www/api/admin/...). Format/uniqueness/pg_ checks stay in the serializer; this
    adds only the reserved-word check so a tenant can't be named 'admin' or 'api'.
    """
    name = (value or "").strip().lower()
    if name in reserved_schema_labels():
        raise ValidationError(f"Schema name '{name}' is reserved.")
    return name


def validate_tenant_domain(value: str) -> str:
    """Validate a business-tenant Domain: format + code invariants + reserved rules.

    Returns the normalized host, or raises django ValidationError. The DRF
    serializer converts that to a 400; the admin form surfaces it inline.
    """
    host = validate_hostname(value)
    if host in _public_hosts():
        raise ValidationError(f"Host '{host}' is reserved for platform management.")
    rule = matching_rule(host)
    if rule is not None:
        raise ValidationError(rule.denial_message(host))
    return host