"""The Celery seam: what ends up in a task message's `_schema_name` header.

tenants.celery.compat re-exports current_schema_name from tenants.context (one owner for the
tenant-identity read; commons.platform.cache_keys used to carry a byte-identical copy). The
READ itself — bound shard vs stale default, empty schema, a broken connection — is covered by
tenants/tests/test_context.py::CurrentSchemaNameTests, which also pins that the re-export is
the same object.

What is left here, and what had no test at all before: headers_with_schema, the function that
actually puts the answer on the wire.
"""
from unittest import mock

from django.test import SimpleTestCase

from tenants.celery import task as task_mod
from tenants.celery.task import headers_with_schema


class HeadersWithSchemaTests(SimpleTestCase):
    def test_stamps_the_current_schema_when_absent(self) -> None:
        with mock.patch.object(task_mod, "current_schema_name", lambda: "acme"):
            self.assertEqual(headers_with_schema(None), {"_schema_name": "acme"})
            self.assertEqual(headers_with_schema({"x": 1}),
                             {"x": 1, "_schema_name": "acme"})

    def test_an_explicit_schema_wins_and_is_not_copied(self) -> None:
        """A caller that already decided (sub_dispatch does, per schema) must not be
        overridden by whatever context the dispatcher happens to run in."""
        given = {"_schema_name": "beta"}
        with mock.patch.object(task_mod, "current_schema_name", lambda: "acme"):
            out = headers_with_schema(given)
        self.assertIs(out, given)                    # same object: returned untouched
        self.assertEqual(out["_schema_name"], "beta")

    def test_the_caller_s_dict_is_not_mutated(self) -> None:
        """deepcopy, not update: the caller may reuse the dict for a second send."""
        given = {"nested": {"k": 1}}
        with mock.patch.object(task_mod, "current_schema_name", lambda: "acme"):
            out = headers_with_schema(given)
        self.assertNotIn("_schema_name", given)
        self.assertIsNot(out["nested"], given["nested"])

    def test_public_is_stamped_as_a_real_answer(self) -> None:
        """An unbound axis is legitimate (trigger_warm enqueues before the axis is bound), so
        public goes on the wire rather than being treated as "unknown"."""
        with mock.patch.object(task_mod, "current_schema_name", lambda: "public"):
            self.assertEqual(headers_with_schema(None), {"_schema_name": "public"})

    def test_a_read_error_is_not_swallowed_into_a_header(self) -> None:
        """No task is better than a task stamped with the wrong tenant."""
        def boom() -> str:
            raise RuntimeError("bad connection state")

        with mock.patch.object(task_mod, "current_schema_name", boom):
            with self.assertRaises(RuntimeError):
                headers_with_schema(None)
