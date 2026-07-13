
"""Loadtest: generate fake offline GPS coordinates for a tenant and upload
them to S3, measuring how long each step takes.

Purpose: benchmark the S3 write path for offline-coordinate batches (Jira
IT-21249 - replacing MongoDB coordinate storage). The number that matters most
is the per-iteration S3 upload (PUT) time, which is timed in isolation from
serialization.

Interactive (prompts for tenant / rows / iterations):
    python manage.py loadtest_offline_coordinates_s3

Non-interactive:
    python manage.py loadtest_offline_coordinates_s3 \
        --tenant acme --rows 5000 --iterations 10 --gzip

Every run also appends a human-readable log file (path printed at the end).
"""

import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django_tenants.utils import get_public_schema_name

from routes.services.service_offline_coordinates_s3_uploader import (
    build_offline_coordinates_object,
    generate_offline_coordinates_data,
    put_offline_coordinates_object,
)
from tenants.models import Tenant

_MB = 1024 * 1024


class Command(BaseCommand):
    help = (
        "Generate fake offline coordinates for a tenant and upload them to S3, "
        "timing generation, data size and (most importantly) the S3 upload per iteration."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            help="Tenant schema_name or numeric id. Omit to choose interactively.",
        )
        parser.add_argument(
            "--rows", type=int,
            help="Coordinate rows to generate per iteration. Omit to be prompted.",
        )
        parser.add_argument(
            "--iterations", type=int,
            help="How many generate+upload cycles to run. Omit to be prompted.",
        )
        parser.add_argument(
            "--gzip", action="store_true",
            help="Gzip the JSON before upload (uploads .json.gz).",
        )
        parser.add_argument(
            "--log-file",
            help="Where to write the run log. "
                 "Default: <BASE_DIR>/logs/offline_coords_s3_loadtest_<ts>.log",
        )

    # -- input resolution ---------------------------------------------------

    def _resolve_tenant(self, raw) -> Tenant:
        qs = Tenant.objects.select_related("shard").exclude(
            schema_name=get_public_schema_name()
        )
        if raw:
            raw = raw.strip()
            lookup = {"pk": raw} if raw.isdigit() else {"schema_name": raw}
            try:
                return qs.get(**lookup)
            except Tenant.DoesNotExist:
                raise CommandError(f"Tenant {raw!r} not found (or is the public tenant).")

        tenants = list(qs.order_by("id"))
        if not tenants:
            raise CommandError("No non-public tenants exist. Provision one first.")

        self.stdout.write("Available tenants:")
        for i, t in enumerate(tenants, 1):
            self.stdout.write(
                f"  {i}. id={t.id}  {t.schema_name}  (name={t.name}, "
                f"shard={t.shard.alias}, status={t.status})"
            )
        choice = self._ask("Select tenant number", cast=int, default=1)
        if not 1 <= choice <= len(tenants):
            raise CommandError(f"Choice {choice} out of range 1..{len(tenants)}.")
        return tenants[choice - 1]

    def _ask(self, prompt, *, cast, default):
        try:
            raw = input(f"{prompt} [{default}]: ").strip()
        except EOFError:
            raise CommandError(
                f"No value for {prompt!r} and no TTY to prompt. "
                f"Pass it as a command-line option instead."
            )
        if not raw:
            return default
        try:
            return cast(raw)
        except ValueError:
            raise CommandError(f"Invalid value for {prompt!r}: {raw!r}")

    # -- main ---------------------------------------------------------------

    def handle(self, *args, **opts):
        tenant = self._resolve_tenant(opts["tenant"])
        rows = opts["rows"] if opts["rows"] is not None else self._ask(
            "Rows per iteration", cast=int, default=1000
        )
        iterations = opts["iterations"] if opts["iterations"] is not None else self._ask(
            "Number of iterations", cast=int, default=1
        )
        use_gzip = opts["gzip"]

        if rows < 0:
            raise CommandError("--rows must be non-negative.")
        if iterations < 1:
            raise CommandError("--iterations must be >= 1.")

        started = timezone.now()
        if opts["log_file"]:
            log_path = Path(opts["log_file"]).resolve()
        else:
            log_dir = Path(settings.BASE_DIR) / "logs"
            log_path = log_dir / f"offline_coords_s3_loadtest_{started:%Y%m%d_%H%M%S}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path = log_path.resolve()

        with log_path.open("w", encoding="utf-8") as log:

            def emit(line: str):
                """Write to stdout AND the log file, after every iteration."""
                self.stdout.write(line)
                log.write(line + "\n")
                log.flush()

            emit(
                f"offline-coordinates S3 loadtest @ {started:%Y-%m-%d %H:%M:%S %Z}\n"
                f"  tenant   : id={tenant.id} {tenant.schema_name} "
                f"(shard={tenant.shard.alias})\n"
                f"  rows/iter: {rows}\n"
                f"  iterations: {iterations}\n"
                f"  gzip     : {use_gzip}\n"
                + "-" * 72
            )

            gen_times, up_times, raw_mbs, up_mbs = [], [], [], []

            for n in range(1, iterations + 1):
                t0 = time.perf_counter()
                data = generate_offline_coordinates_data(rows)
                gen_time = time.perf_counter() - t0

                s3_key, body, content_type, raw_size = build_offline_coordinates_object(
                    data, tenant.id, use_gzip=use_gzip
                )

                t1 = time.perf_counter()
                put_offline_coordinates_object(s3_key, body, content_type)
                up_time = time.perf_counter() - t1

                raw_mb = raw_size / _MB
                up_mb = len(body) / _MB
                gen_times.append(gen_time)
                up_times.append(up_time)
                raw_mbs.append(raw_mb)
                up_mbs.append(up_mb)

                size_part = f"data={raw_mb:.3f} MB"
                if use_gzip:
                    size_part += f" (uploaded {up_mb:.3f} MB gz)"
                emit(
                    f"iter {n:>3}/{iterations}: "
                    f"generation={gen_time:.3f}s | {size_part} | "
                    f"S3 upload={up_time:.3f}s  ->  {s3_key}"
                )

            n = len(up_times)
            emit(
                "-" * 72 + "\n"
                f"SUMMARY ({n} iteration(s)):\n"
                f"  generation : total={sum(gen_times):.3f}s  avg={sum(gen_times)/n:.3f}s\n"
                f"  data size  : total={sum(raw_mbs):.3f} MB  avg={sum(raw_mbs)/n:.3f} MB/iter\n"
                f"  S3 upload  : total={sum(up_times):.3f}s  avg={sum(up_times)/n:.3f}s  "
                f"min={min(up_times):.3f}s  max={max(up_times):.3f}s"
            )

        self.stdout.write(self.style.SUCCESS(f"\nLog written to {log_path}"))
