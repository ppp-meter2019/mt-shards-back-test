"""Settings invariants for multi-tenant mode. DB-free (SimpleTestCase).

These tests only run in USE_MULTITENANT=True (the `tenants` app must be installed
for its test suite to be discovered), which is exactly the mode the invariants
apply to.
"""
import os
import subprocess
import sys
from collections.abc import Sequence

from django.conf import settings
from django.test import SimpleTestCase

from ._settings_ast import assert_literal_assignment


class InstalledAppsInvariantTests(SimpleTestCase):
    def test_tenants_before_django_tenants(self) -> None:
        """'tenants' MUST precede 'django_tenants' so our management commands
        (notably migrate_schemas) override the upstream ones — the app listed
        FIRST wins on a command-name collision. See settings_multitenant.py SHARED_APPS."""
        apps = list(settings.INSTALLED_APPS)
        self.assertIn("tenants", apps)
        self.assertIn("django_tenants", apps)
        self.assertLess(
            apps.index("tenants"),
            apps.index("django_tenants"),
            "'tenants' must come before 'django_tenants' in INSTALLED_APPS "
            "(command-override precedence).",
        )

    def test_installed_apps_are_deduplicated(self) -> None:
        """django-tenants requires INSTALLED_APPS to be the de-duplicated union
        of SHARED_APPS + TENANT_APPS."""
        apps = list(settings.INSTALLED_APPS)
        self.assertEqual(len(apps), len(set(apps)), "duplicate entries in INSTALLED_APPS")


class MiddlewareInvariantTests(SimpleTestCase):
    """Guards the delta-built MT MIDDLEWARE (settings_multitenant.py) — presence + relative
    order of the two tenant chains. Mirrors the tenants.E004 system check."""

    RESOLVE_CHAIN = (
        "corsheaders.middleware.CorsMiddleware",
        "tenants.middleware.ShardAwareTenantMiddleware",
        "tenants.middleware.TenantShardRoutingMiddleware",
    )
    AUTH_CHAIN = (
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "users.middleware.SchemaBoundSessionMiddleware",
    )

    def _assert_ordered(self, mw: list[str], chain: Sequence[str]) -> None:
        idx = []
        for name in chain:
            self.assertIn(name, mw, f"{name} missing from MIDDLEWARE")
            idx.append(mw.index(name))
        self.assertEqual(idx, sorted(idx), f"{chain} out of order in MIDDLEWARE")

    def test_resolved_middleware_chains(self) -> None:
        """The base list + MT inserts land in the required relative order."""
        mw = list(settings.MIDDLEWARE)
        self._assert_ordered(mw, self.RESOLVE_CHAIN)
        self._assert_ordered(mw, self.AUTH_CHAIN)

    # The standalone base list (settings_base.py). Pinned here because `from .settings_multitenant
    # import *` shadows the name in the settings module, so the pristine base can't be read
    # back at runtime in MT mode. If the base list changes, update this snapshot too.
    BASE_MIDDLEWARE = (
        "corsheaders.middleware.CorsMiddleware",
        "django.middleware.security.SecurityMiddleware",
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.middleware.common.CommonMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
        "django.middleware.clickjacking.XFrameOptionsMiddleware",
    )

    def test_base_middleware_flows_into_mt(self) -> None:
        """Delta build (not rewrite): every standalone-base middleware is still present
        in the MT list, so a base addition can't be silently dropped from MT."""
        mw = set(settings.MIDDLEWARE)
        for name in self.BASE_MIDDLEWARE:
            self.assertIn(name, mw, f"base middleware {name} missing from MT MIDDLEWARE")

    def test_e004_flags_missing_and_misordered(self) -> None:
        """The tenants.E004 check fires on a missing entry and on a swapped order."""
        from tenants.checks import mt_middleware_order
        good = list(settings.MIDDLEWARE)

        with self.settings(MIDDLEWARE=good):
            self.assertEqual(mt_middleware_order(None), [])

        missing = [m for m in good if m != "tenants.middleware.ShardAwareTenantMiddleware"]
        with self.settings(MIDDLEWARE=missing):
            self.assertTrue(any(e.id == "tenants.E004" for e in mt_middleware_order(None)))

        # swap Auth and SchemaBound so the session middleware precedes Auth
        swapped = list(good)
        a = swapped.index("django.contrib.auth.middleware.AuthenticationMiddleware")
        b = swapped.index("users.middleware.SchemaBoundSessionMiddleware")
        swapped[a], swapped[b] = swapped[b], swapped[a]
        with self.settings(MIDDLEWARE=swapped):
            self.assertTrue(any(e.id == "tenants.E004" for e in mt_middleware_order(None)))


class BootstrapFloatTests(SimpleTestCase):
    """commons.platform.mode.bootstrap_float — the load-time knob resolver used for
    FANOUT_PERIOD_DEFAULT: env → settings_mode.py → default, LOUD on a malformed value."""

    KNOB = "FANOUT_TEST_KNOB_XYZ"   # a name nothing sets in env or settings_mode.py

    def test_default_when_absent(self) -> None:
        from commons.platform.mode import bootstrap_float
        self.assertEqual(bootstrap_float(self.KNOB, 60.0), 60.0)

    def test_env_override_wins(self) -> None:
        import os
        from unittest import mock
        from commons.platform.mode import bootstrap_float
        with mock.patch.dict(os.environ, {self.KNOB: "30"}):
            self.assertEqual(bootstrap_float(self.KNOB, 60.0), 30.0)

    def test_malformed_env_fails_loud(self) -> None:
        import os
        from unittest import mock
        from commons.platform.mode import bootstrap_float
        with mock.patch.dict(os.environ, {self.KNOB: "abc"}):
            with self.assertRaises(ValueError):
                bootstrap_float(self.KNOB, 60.0)


class SettingsLocalContractTests(SimpleTestCase):
    """What a DEPLOYED settings_local_multitenant.py may import from `tenants_back.settings`.

    That file is gitignored and lives on the servers, so it cannot be migrated together with
    the code — whatever it imports today must keep resolving. The trap is that settings.py is
    a DISPATCHER built out of `import *`, and `import *` silently skips underscore-prefixed
    names: the _aurora_db_options / _proxy_db_options helpers (defined in
    settings_multitenant.py) only reach it because the dispatcher re-exports them EXPLICITLY.

    The failure used to be silent and total: a missing name raised ImportError inside
    `try: from .settings_local import *`, a bare `except ImportError` swallowed it, and the
    process booted on dev defaults — DEBUG=True, ALLOWED_HOSTS=["*"], the insecure SECRET_KEY,
    localhost DB. It happened for real when settings.py was split into base + dispatcher. The
    dispatcher's except is now narrowed to "this exact module does not exist", so the same
    mistake is loud — but the re-export still has to be there for the file to load at all.

    The expected names are parsed from the TRACKED .example, so the two cannot drift: add an
    import there and this test starts requiring it.
    """

    MT_EXAMPLE = "settings_local_multitenant.py.example"
    STANDALONE_EXAMPLE = "settings_local.py.example"

    def _example_imports(self, filename: str) -> list[str]:
        import ast
        example = settings.BASE_DIR / "tenants_back" / filename
        self.assertTrue(example.exists(), f"{filename} is missing")
        return [
            alias.name
            for node in ast.walk(ast.parse(example.read_text(encoding="utf-8")))
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "settings"
            for alias in node.names
        ]

    def test_dispatcher_exports_everything_the_example_imports(self) -> None:
        import tenants_back.settings as dispatcher
        names = self._example_imports(self.MT_EXAMPLE)
        self.assertTrue(names, "the example no longer imports from .settings — update this test")
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(dispatcher, name),
                    f"{self.MT_EXAMPLE} does `from .settings import {name}`, but the dispatcher "
                    f"does not expose it. `import *` skips underscore names — re-export it "
                    f"explicitly in the multi-tenant branch of tenants_back/settings.py, or a "
                    f"deployed settings_local_multitenant.py will fail to load.",
                )

    def test_underscore_helpers_are_re_exported(self) -> None:
        """Belt and braces: these two are what actually broke, and the example may not cover
        both (the deployed file imports _proxy_db_options; the example currently does not)."""
        import tenants_back.settings as dispatcher
        for name in ("_aurora_db_options", "_proxy_db_options"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(dispatcher, name, None)))

    def test_standalone_example_does_not_import_the_aurora_helpers(self) -> None:
        """The standalone branch of the dispatcher deliberately does NOT re-export them:
        Aurora / RDS Proxy is multi-tenant deployment topology, and the helpers now live in
        settings_multitenant.py. A standalone local file that imported one would raise
        ImportError at boot — catch the drift here instead, in the .example."""
        names = self._example_imports(self.STANDALONE_EXAMPLE)
        for forbidden in ("_aurora_db_options", "_proxy_db_options", "AWS_RDS_CA"):
            self.assertNotIn(
                forbidden, names,
                f"{self.STANDALONE_EXAMPLE} imports {forbidden}, which the standalone branch "
                f"of tenants_back/settings.py does not expose. Spell the OPTIONS out inline "
                f"instead, or move the helper — do not re-export the MT layer into standalone.",
            )


class MultitenantDatabasesContractTests(SimpleTestCase):
    """settings_multitenant.py must BUILD DATABASES, never derive it from the base.

    The base `default` is the STANDALONE database — in the host project, its real
    single-tenant production DB. The overlay used to spread it (`{**DATABASES, "default":
    {**DATABASES["default"], ...}}`), which meant flipping USE_MULTITENANT on would aim
    django-tenants at whatever database the base happened to name. Source-level, because the
    resolved setting says nothing: a local settings file overwrites DATABASES either way.
    """

    def test_databases_is_a_plain_literal(self) -> None:
        assert_literal_assignment(self, "DATABASES")

    def test_engine_is_the_django_tenants_backend(self) -> None:
        """The backend is a property of the MODE, so it is declared in this layer and not
        left to every deployed local settings file."""
        self.assertEqual(
            settings.DATABASES["default"]["ENGINE"], "django_tenants.postgresql_backend",
        )
        self.assertEqual(
            settings.ORIGINAL_BACKEND, "django.contrib.gis.db.backends.postgis",
        )


class MergeSeamContractTests(SimpleTestCase):
    """_PUBLIC_MODEL_ALLOWLIST (settings_base) and its runtime promotion.

    Every per-tenant app has its tables in the public schema, so the only per-model decision
    left is which of them may be touched there. Each test pins a mistake that is otherwise
    only discoverable by running against a real cluster.
    """

    def test_business_apps_are_in_both_app_lists(self) -> None:
        """SHARED_APPS gets them so that every FK target exists when the identity table is
        created in public; TENANT_APPS gets them because that is where their data lives.
        SHARED-only would mean no tenant data at all; TENANT-only would fail
        `migrate_schemas --shared` at CREATE TABLE."""
        from tenants_back import settings_base
        business = set(settings_base._BUSINESS_APPS)
        self.assertLessEqual(business, set(settings.SHARED_APPS))
        self.assertLessEqual(business, set(settings.TENANT_APPS))

    def test_identity_app_is_a_business_app(self) -> None:
        """The app owning AUTH_USER_MODEL is per-tenant like the rest — each schema has its
        own users — and being in both lists is what makes the identity table SHADOW:
        `search_path = [tenant, public]` resolves it to the tenant's own copy, so platform
        operators are unreachable from inside a tenant. Structural, not a permission check.

        Derived from AUTH_USER_MODEL rather than from a separate identity constant: naming
        the identity app twice is itself the drift this pins."""
        from tenants_back import settings_base
        self.assertIn(settings.AUTH_USER_MODEL.split(".", 1)[0], settings_base._BUSINESS_APPS)

    def test_identity_model_is_allowed_on_public(self) -> None:
        """The platform operator IS a row in the identity table on public — that is what the
        public admin authenticates. Drop the model from the allowlist and the guard refuses
        the one query the whole arrangement exists to permit."""
        self.assertIn(settings.AUTH_USER_MODEL.lower(), settings.PUBLIC_MODEL_ALLOWLIST)

    def test_allowlisted_models_have_a_table_in_public(self) -> None:
        """Allowing a model whose table is not in public is not a looser permission — every
        query it permits is a ProgrammingError.

        SHARED_APPS is the right bar, and it is wider than the per-tenant apps on purpose: a
        genuinely shared app (django.contrib.*, third-party) has its tables there anyway. At
        merge django_password_history is exactly that — UserPasswordHistory needs rows, and
        the app rides in on _THIRD_PARTY_APPS.
        """
        shared = set(settings.SHARED_APPS)
        for label in settings.PUBLIC_MODEL_ALLOWLIST:
            self.assertIn(
                label.split(".", 1)[0], shared,
                f"{label!r} is allowed on public, but its app has no tables there.",
            )

    def test_runtime_promotion_matches_its_source(self) -> None:
        """The router reads the public setting; the merge edits the private list. Nothing
        stops the two from drifting except this."""
        from tenants_back import settings_base
        self.assertEqual(frozenset(settings_base._PUBLIC_MODEL_ALLOWLIST),
                         settings.PUBLIC_MODEL_ALLOWLIST)

    def test_model_labels_are_lower_cased_and_well_formed(self) -> None:
        """Model._meta.label_lower is the form the guard compares against, so an entry in any
        other form silently never matches — the guard would then refuse a model the allowlist
        was written to permit."""
        for label in settings.PUBLIC_MODEL_ALLOWLIST:
            self.assertEqual(label, label.lower(), f"{label!r} must be lower-cased")
            self.assertEqual(label.count("."), 1,
                             f"{label!r} must be exactly 'app_label.modelname'")


class SettingsLoadReentrancyTests(SimpleTestCase):
    """Importing the project PACKAGE must not configure Django settings.

    `tenants_back` is both the settings package and the Celery app package, so Django imports
    it on the way to `tenants_back.settings`: `import_module("tenants_back.settings")` runs
    `tenants_back/__init__.py` FIRST. Anything that reads django.conf.settings from there
    re-enters the settings load, and Django answers the re-entrant `_setup()` by building a
    SECOND Settings object from whatever the settings module has executed so far.

    With DJANGO_SETTINGS_MODULE pointing at this package that is merely wasteful (the
    submodule import completes on its own, both objects come out identical). Point it at a
    wrapper OUTSIDE the package -- `prod_settings.py` doing `from tenants_back.settings
    import *`, the shape half of Django projects deploy -- and the inner object is built from
    a module that is still on the stack: global defaults only, DATABASES == {}, no
    USE_MULTITENANT. It is transient, replaced once the outer load finishes, but every
    settings read that happens inside that window silently gets the global default.

    This cannot be asserted in-process (settings are configured before the suite runs), hence
    the subprocess. It is also why `tenants/context.py` and `tenants/celery/compat.py` defer
    their `django_tenants.utils` import: that module evaluates get_tenant_database_alias() in
    two default args at import time, which was the one read inside the window.
    """

    def test_importing_the_project_package_does_not_configure_settings(self) -> None:
        script = (
            "import os, sys\n"
            "os.environ['DJANGO_SETTINGS_MODULE'] = 'tenants_back.settings'\n"
            "from django.conf import settings\n"
            "import tenants_back\n"
            "print('CONFIGURED' if settings.configured else 'CLEAN')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(settings.BASE_DIR),
            env={**os.environ, "USE_MULTITENANT": "1"},
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, f"import failed:\n{proc.stderr}")
        self.assertEqual(
            proc.stdout.strip().splitlines()[-1], "CLEAN",
            "importing tenants_back configured Django settings -- something on the Celery app "
            "import chain now reads django.conf.settings at module level. Find it and defer "
            "the read (or the import that causes it) to call time; see this class's docstring "
            "and the comment at the top of tenants_back/celery.py.",
        )
