"""Source-level assertions about the settings modules.

Separate from _support.py on purpose: that one builds FAKES of runtime objects for the
resolver tests, this one parses SOURCE and asserts on its shape. The two share nothing but
the word "test", and folding them together would turn a themed module into a bucket.

Why source-level at all: the resolved settings cannot answer these questions. A literal dict
and a `{**base, ...}` merge produce an identical `settings.DATABASES`, and a deployed local
settings file overwrites both either way — so the rule is only ever visible in the text.
"""
import ast
from pathlib import Path

from django.conf import settings

MT_SETTINGS = "settings_multitenant.py"
BASE_SETTINGS = "settings_base.py"


def settings_module_path(module: str) -> Path:
    return settings.BASE_DIR / "tenants_back" / module


def assert_literal_assignment(case, name: str, module: str = MT_SETTINGS) -> ast.Dict:
    """Assert `name` is assigned exactly ONCE in `module`, as a plain dict literal with no
    `**` unpacking. Returns the ast.Dict so callers can assert further on its contents.

    The multi-tenant layer must BUILD its DATABASES/CACHES rather than derive them from the
    base. The base is the STANDALONE host project's configuration: for DATABASES its `default`
    is that project's live single-tenant DB, and deriving would aim django-tenants straight at
    it the moment USE_MULTITENANT flips on; for CACHES it would inherit whatever instance and
    options that project happens to name.

    `{**other, "a": 1}` parses as an ast.Dict whose `keys` are `[None, Constant('a')]` — the
    unpacking is represented by a None key. That is what the last assertion detects, and it is
    the one line here that is not obvious from reading it.
    """
    path = settings_module_path(module)
    case.assertTrue(path.exists(), f"{module} is missing")

    assigns = [
        node for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    ]
    case.assertEqual(
        len(assigns), 1,
        f"expected exactly one module-level {name} assignment in {module}, found "
        f"{len(assigns)} — more than one makes 'is it a literal?' unanswerable here",
    )

    value = assigns[0].value
    case.assertIsInstance(
        value, ast.Dict,
        f"{name} in {module} must be a literal dict, not an expression derived from the base",
    )
    case.assertNotIn(
        None, value.keys,
        f"{name} in {module} unpacks another dict (`{{**{name}, ...}}`). The multi-tenant "
        f"layer must not inherit the standalone definition — see this helper's docstring for "
        f"what that costs per setting.",
    )
    return value


def _module_assignment(case, name: str, module: str) -> ast.Assign:
    """The single module-level `name = ...` node in `module`."""
    path = settings_module_path(module)
    case.assertTrue(path.exists(), f"{module} is missing")
    assigns = [
        node for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    ]
    case.assertEqual(
        len(assigns), 1,
        f"expected exactly one module-level {name} assignment in {module}, found {len(assigns)}",
    )
    return assigns[0]


def assert_no_dict_key(case, name: str, forbidden: str, module: str) -> None:
    """Assert no dict key named `forbidden` appears ANYWHERE inside the `name` assignment.

    Structural, at any nesting depth — CACHES entries hold their options one level down. The
    string-matching version this replaced stripped `#` comments and then searched the whole
    file, so it could be tripped by a docstring that merely NAMED the setting, and it could be
    satisfied by a value that happened to contain the word. Neither failure is theoretical in
    a codebase whose settings modules are more than half prose.
    """
    node = _module_assignment(case, name, module)
    # Dict KEYS only, at any depth: a bare ast.walk would also collect values, and a cache
    # LOCATION or a comment-free string that merely contained the word would then match.
    keys = set()
    for sub in ast.walk(node.value):
        if isinstance(sub, ast.Dict):
            keys |= {k.value for k in sub.keys
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    case.assertNotIn(
        forbidden, keys,
        f"{name} in {module} sets {forbidden!r}. Tenant-scoping the cache is a property of "
        f"the multi-tenant MODE and belongs in {MT_SETTINGS}.",
    )


def assert_module_constant(case, name: str, expected, module: str) -> None:
    """Assert `name` is assigned a literal constant equal to `expected` in `module`.

    Replaces an `assertIn('NAME = "value"', source)` check, which broke on any reformatting
    (quote style, spacing, a trailing comment) and was equally satisfied by the same text
    appearing inside a comment.
    """
    node = _module_assignment(case, name, module)
    case.assertIsInstance(
        node.value, ast.Constant,
        f"{name} in {module} is not a literal constant",
    )
    case.assertEqual(node.value.value, expected, f"{name} in {module}")
