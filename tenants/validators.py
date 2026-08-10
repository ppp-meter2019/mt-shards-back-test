"""Hostname + reserved-host validation for tenant Domains.

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
"""
import re

from django.core.exceptions import ValidationError
from django_tenants.utils import get_public_schema_name

# A single DNS label: ASCII letters/digits/hyphen, 1-63 chars, no leading/trailing
# hyphen. Bounded quantifier, no nesting => LINEAR match, zero backtracking surface.
# Used to validate a whole hostname label-by-label (validate_hostname) AND a
# ReservedHostRule LABEL value (validate_label). ASCII-only on purpose (no IDN);
# callers lower-case first.
_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

HOSTNAME_MAX = 253


def normalize_host(value: str) -> str:
    """Lower-case, strip surrounding whitespace and a trailing dot (FQDN root)."""
    return (value or "").strip().lower().rstrip(".")


def validate_hostname(value: str) -> str:
    """Return the normalized host or raise ValidationError if malformed.

    Validated label-by-label (no nested-quantifier regex), so there is no
    catastrophic-backtracking surface regardless of input length. Also rejects an
    all-numeric top label (an IPv4-like host such as 1.2.3.4 / 999), which is never
    a valid tenant domain and would collide with by-IP request paths.
    """
    host = normalize_host(value)
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
    """Return the normalized single DNS label or raise (for LABEL rules)."""
    label = normalize_host(value)
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


def matching_rule(host: str):
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