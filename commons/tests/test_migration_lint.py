"""CI lint of the project's migrations (deploy/migrations_plan_rev7.md §13) — a test, not
a system check, so no `manage.py` invocation has to scan files:

  * a migration with ``atomic = False`` starts with LockTimeoutOff() and ends with
    LockTimeoutRestore() (the §8.2 recipe; otherwise the migrator's 5s lock_timeout kills
    every CREATE INDEX CONCURRENTLY, or a later migration runs with no timeout at all);
  * ``CONCURRENTLY IF NOT EXISTS`` and AddIndexConcurrently are forbidden: an INVALID index
    left by a failed run would count as "existing" and stay invalid for good;
  * every ``CREATE [UNIQUE] INDEX CONCURRENTLY "<name>"`` is preceded, in the same
    migration, by ``DROP INDEX CONCURRENTLY IF EXISTS "<name>"``: without it a CIC that
    failed half-way leaves an INVALID index of that name, and every retry fails with
    "already exists".

Static (ast), so it imports nothing and runs the same in both modes.
"""
import ast
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

_FORBIDDEN = [(re.compile(r"CONCURRENTLY\s+IF\s+NOT\s+EXISTS", re.I), "CONCURRENTLY IF NOT EXISTS"),
              (re.compile(r"\bAddIndexConcurrently\b"), "AddIndexConcurrently")]
_CIC = re.compile(r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY\s+(?!IF\b)"?([\w.$]+)"?', re.I)
_DROP = re.compile(r'DROP\s+INDEX\s+CONCURRENTLY\s+IF\s+EXISTS\s+"?([\w.$]+)"?', re.I)


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        f = node.func
        return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
    return None


def _assignments(cls: ast.ClassDef):
    """(name, value) of `x = …` and of the annotated `x: T = …` in a class body."""
    for st in cls.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            yield st.targets[0].id, st.value
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
            yield st.target.id, st.value


def _cic_without_drop(tree: ast.AST) -> list[str]:
    """CREATE INDEX CONCURRENTLY of a name not DROPped (IF EXISTS) earlier in the module.
    Works on the string constants in source order (implicit concatenation is already one
    constant in the AST)."""
    strings = sorted((n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)),
                     key=lambda n: (n.lineno, n.col_offset))
    dropped: set[str] = set()
    problems = []
    for node in strings:
        dropped.update(m.lower() for m in _DROP.findall(node.value))
        for name in _CIC.findall(node.value):
            if name.lower() not in dropped:
                problems.append(f'CREATE INDEX CONCURRENTLY "{name}" without a preceding '
                                f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"')
    return problems


def lint(source: str) -> list[str]:
    """Problems in one migration module's source (empty = clean)."""
    problems = [f"uses {name}" for rx, name in _FORBIDDEN if rx.search(source)]
    tree = ast.parse(source)
    problems += _cic_without_drop(tree)
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Migration"):
        atomic, ops = True, None
        for name, value in _assignments(cls):
            if name == "atomic" and isinstance(value, ast.Constant):
                atomic = bool(value.value)
            elif name == "operations":
                ops = value
        if atomic:
            continue
        if not isinstance(ops, (ast.List, ast.Tuple)) or not ops.elts:
            problems.append("atomic = False with operations that are not a literal list")
            continue
        if _call_name(ops.elts[0]) != "LockTimeoutOff":
            problems.append("atomic = False: the first operation must be LockTimeoutOff()")
        if _call_name(ops.elts[-1]) != "LockTimeoutRestore":
            problems.append("atomic = False: the last operation must be LockTimeoutRestore()")
    return problems


def project_migrations() -> list[Path]:
    """Every migration module of the project (incl. the test canary), never site-packages."""
    root = Path(settings.BASE_DIR)
    return sorted(p for p in root.rglob("migrations*/[0-9]*.py")
                  if not {"p_env", "node_modules", ".venv", "venv"} & set(p.parts))


class MigrationLint(SimpleTestCase):
    def test_project_migrations_are_clean(self) -> None:
        files = project_migrations()
        self.assertTrue(any("canary" in p.parts for p in files), "the canary must be linted too")
        bad = {str(p): lint(p.read_text(encoding="utf-8")) for p in files}
        self.assertEqual({k: v for k, v in bad.items() if v}, {})

    def test_lint_catches(self) -> None:
        recipe = ("class Migration:\n    atomic = False\n"
                  "    operations = [LockTimeoutOff(), RunSQL('x'), LockTimeoutRestore()]\n")
        self.assertEqual(lint(recipe), [])
        self.assertEqual(lint(recipe.replace("LockTimeoutOff(), ", "")),
                         ["atomic = False: the first operation must be LockTimeoutOff()"])
        self.assertEqual(lint(recipe.replace(", LockTimeoutRestore()", "")),
                         ["atomic = False: the last operation must be LockTimeoutRestore()"])
        self.assertEqual(lint("class Migration:\n    operations = [RunSQL('x')]\n"), [])   # atomic
        self.assertEqual(lint("SQL = 'create index concurrently  if not exists i on t (c)'\n"),
                         ["uses CONCURRENTLY IF NOT EXISTS"])
        self.assertEqual(lint("from django.contrib.postgres.operations import AddIndexConcurrently\n"),
                         ["uses AddIndexConcurrently"])

    def test_annotated_atomic_is_seen(self) -> None:
        self.assertEqual(lint("class Migration:\n    atomic: bool = False\n"
                              "    operations: list = [RunSQL('x')]\n"),
                         ["atomic = False: the first operation must be LockTimeoutOff()",
                          "atomic = False: the last operation must be LockTimeoutRestore()"])

    def test_cic_needs_a_preceding_drop_of_the_same_name(self) -> None:
        create = 'RunSQL(\'CREATE INDEX CONCURRENTLY "i1" ON "t" ("c")\', \'DROP INDEX CONCURRENTLY IF EXISTS "i1"\')'
        drop = 'RunSQL(\'DROP INDEX CONCURRENTLY IF EXISTS "i1"\', noop)'
        wrap = ("class Migration:\n    atomic = False\n"
                "    operations = [LockTimeoutOff(), {}, LockTimeoutRestore()]\n")
        self.assertEqual(lint(wrap.format(f"{drop}, {create}")), [])
        only_create = lint(wrap.format(create))     # its reverse_sql DROP comes AFTER: no help
        self.assertEqual(only_create, ['CREATE INDEX CONCURRENTLY "i1" without a preceding '
                                       'DROP INDEX CONCURRENTLY IF EXISTS "i1"'])
        other = lint(wrap.format(drop.replace('"i1"', '"i2"') + ", " + create))
        self.assertEqual(len(other), 1)
        unique = lint(wrap.format(create.replace("CREATE INDEX", "CREATE UNIQUE INDEX", 1)))
        self.assertEqual(len(unique), 1)
        split = ("SQL = ('CREATE INDEX CONCURRENTLY \"i3\" '\n"
                 "       'ON \"t\" (\"c\")')\n")                 # implicit concatenation
        self.assertEqual(len(lint(split)), 1)
