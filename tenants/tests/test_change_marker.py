"""bump_schema (DB-free): a tenant status transition must bump BOTH the tenant's own
beat change-marker AND the public marker (public is always in beat's read set, so the
reload is observed immediately regardless of beat's tenant-list cache)."""
from unittest import mock

from django.test import SimpleTestCase

from tenants.celery import change_marker as cm


class BumpSchemaTests(SimpleTestCase):
    def test_bumps_both_the_schema_and_public_markers(self):
        fake = mock.MagicMock()
        with mock.patch.object(cm, "_cache", return_value=fake):
            cm.bump_schema("beta")
        fake.set_many.assert_called_once()
        keys = set(fake.set_many.call_args.args[0].keys())
        self.assertIn(cm.marker_key("beta"), keys)
        self.assertIn(cm.marker_key(cm.get_public_schema_name()), keys)
