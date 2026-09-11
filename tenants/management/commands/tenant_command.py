"""Shard-aware `tenant_command` override.

django_tenants' tenant_command switches only `connection.set_tenant(tenant)` on the DEFAULT
connection and never sets our `current_db` ContextVar / the tenant's SHARD — so a wrapped
sub-command's tenant-model ORM routes to the 'default' database (wrong shard). We run the
wrapped sub-command inside our shard-aware `tenant_context(tenant)` instead: it sets
current_db -> the tenant's shard AND the schema on that shard's connection (both axes), so the
sub-command's ORM lands on the right shard. This closes the whole category — every command run
via `tenant_command <cmd> --schema=X` now routes correctly (e.g. seed_products), with no
per-command tenant_context needed.

Wins over the upstream command because `tenants` precedes `django_tenants` in SHARED_APPS.
Exists only under multitenant (django_tenants installed); in standalone the wrapped command is
run directly against the single DB.

NB: run_from_argv mirrors upstream's short arg-surgery (pinned to the installed django_tenants
version — see requirements) but replaces the default-connection set_tenant with tenant_context.
"""

import argparse
from typing import Any

from django.core.management import call_command, get_commands, load_command_class
from django.core.management.base import BaseCommand, CommandError
from django_tenants.management.commands.tenant_command import Command as _UpstreamTenantCommand

from tenants.context import tenant_context


class Command(_UpstreamTenantCommand):
    help = "Shard-aware wrapper: run a Django command for one tenant, on that tenant's shard."

    def run_from_argv(self, argv: list[str]) -> None:
        # Mirrors django_tenants' arg surgery, but runs the wrapped command inside
        # tenant_context(tenant) instead of a default-connection set_tenant.
        if len(argv) <= 2:
            return
        try:
            app_name = get_commands()[argv[2]]
        except KeyError:
            raise CommandError("Unknown command: %r" % argv[2])
        klass = app_name if isinstance(app_name, BaseCommand) else load_command_class(app_name, argv[2])

        del argv[1]
        schema_parser = argparse.ArgumentParser()
        schema_parser.add_argument("-s", "--schema", dest="schema_name", help="specify tenant schema")
        schema_namespace, args = schema_parser.parse_known_args(argv)

        tenant = self.get_tenant_from_options_or_interactive(schema_name=schema_namespace.schema_name)
        with tenant_context(tenant):                    # current_db -> shard + schema on shard conn
            klass.run_from_argv(args)

    def handle(self, *args: Any, **options: Any) -> None:
        tenant = self.get_tenant_from_options_or_interactive(**options)
        options.pop("schema_name", None)
        subcommand_name, *subcommand_options = options.pop("command_name")
        subcommand_options += options.pop("command_options")
        with tenant_context(tenant):
            call_command(subcommand_name, *subcommand_options, **options)
