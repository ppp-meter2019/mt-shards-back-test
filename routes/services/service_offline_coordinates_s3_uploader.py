"""Upload offline vehicle GPS coordinates (batched by the mobile app while
offline) to S3.

Context: part of moving coordinate storage off MongoDB into a multi-tenant
data lake (Jira IT-21249); the same per-tenant, date-partitioned key layout is
what a downstream Athena / Kinesis-Firehose pipeline (Jira IT-21374) expects,
so objects can be scanned by tenant + day without listing the whole bucket.

Runtime: this project runs SYNC Gunicorn prefork workers over WSGI (see the
"Worker model" note in settings.py); boto3 is blocking, which is exactly right
here. Call this straight from a DRF view or a Celery task. Do NOT add an async
wrapper - the async/ASGI path is intentionally unused.

Tenants: we store the NUMERIC tenant id (``Tenant.pk`` == ``request.tenant.id``),
not the schema name. It is cheaper to repeat in every object + partition path,
and it maps 1:1 to the ``tenant_id`` integer column the final coordinates DB
will use. The schema name stays a lookup key inside the app, out of the lake.
"""

import gzip
import json
import logging
from functools import lru_cache
from io import BytesIO

import boto3
from botocore.exceptions import ClientError
from django.conf import settings
from django.utils import timezone

_logger = logging.getLogger(__name__)

# Top-level key prefix for every offline-coordinates object (see the key layout
# built in build_offline_coordinates_object).
COORDINATES_PREFIX = "offline-coordinates/"


def _tenant_prefix(tenant_id: int) -> str:
    """S3 key prefix for a single tenant's offline coordinates."""
    return f"{COORDINATES_PREFIX}tenant={tenant_id}/"


@lru_cache(maxsize=None)
def _s3_client(region: str | None):
    """One boto3 S3 client per (process, region).

    boto3 clients are thread-safe and creating them is relatively expensive, so
    we cache per prefork worker. Credentials come from the instance / ECS-task
    IAM role - never hardcode keys. ``region`` is cached as a hashable arg.
    """
    return boto3.client("s3", region_name=region)


def build_offline_coordinates_object(
    data: dict, tenant_id: int, *, use_gzip: bool = False
) -> tuple[str, bytes, str, int]:
    """Serialize one payload and compute its S3 key WITHOUT touching S3.

    Split out from the upload so callers (e.g. the loadtest command) can time
    serialization and the network PUT separately.

    :param data: the mobile-app payload (see :func:`generate_offline_coordinates_data`).
    :param tenant_id: the active tenant's numeric id (``request.tenant.id``);
        added to the document and to the key.
    :param use_gzip: if True, gzip the JSON (``.json.gz``).
    :return: ``(s3_key, body, content_type, raw_size)`` where ``body`` is what
        gets uploaded (possibly compressed) and ``raw_size`` is the uncompressed
        JSON byte length.
    """
    payload = {**data, "tenant_id": tenant_id}
    raw_body = json.dumps(payload, default=str).encode("utf-8")
    raw_size = len(raw_body)

    if use_gzip:
        compressed_buffer = BytesIO()
        with gzip.GzipFile(fileobj=compressed_buffer, mode="wb") as gz_file:
            gz_file.write(raw_body)
        body = compressed_buffer.getvalue()
        content_type = "application/gzip"
        extension = "json.gz"
    else:
        body = raw_body
        content_type = "application/json"
        extension = "json"

    route_id = data.get("route_id", "unknown")
    now = timezone.now()  # tz-aware UTC (USE_TZ=True)
    # Hive-style partitions let Athena/Firehose prune by tenant + day. The file
    # stem carries route_id + microseconds so repeated uploads never collide.
    s3_key = (
        f"{_tenant_prefix(tenant_id)}"
        f"dt={now:%Y-%m-%d}/"
        f"{route_id}_{now:%H%M%S%f}.{extension}"
    )
    return s3_key, body, content_type, raw_size


def put_offline_coordinates_object(s3_key: str, body: bytes, content_type: str) -> str:
    """PUT a prebuilt object to the coordinates bucket. Returns the S3 key.

    :raises ValueError: if the bucket setting is unset.
    :raises botocore.exceptions.ClientError: on an S3 failure (already logged).
    """
    bucket_name = getattr(settings, "AWS_S3_COORDINATES_BUCKET", "")
    if not bucket_name:
        raise ValueError(
            "AWS_S3_COORDINATES_BUCKET is not configured "
            "(set it in settings_local.py)."
        )

    try:
        _s3_client(getattr(settings, "AWS_S3_REGION", None)).put_object(
            Bucket=bucket_name, Key=s3_key, Body=body, ContentType=content_type
        )
    except ClientError:
        _logger.exception(
            "failed to upload offline coordinates to s3://%s/%s",
            bucket_name, s3_key,
        )
        raise

    _logger.info("offline coordinates uploaded to s3://%s/%s", bucket_name, s3_key)
    return s3_key


def upload_offline_coordinates_to_s3(
    data: dict, tenant_id: int, *, use_gzip: bool = False
) -> str:
    """Upload one offline-coordinates payload to the shared coordinates bucket.

    The payload is stored as-is, enriched with ``tenant_id`` so files from
    different tenants can be told apart inside a shared bucket, and written
    under a Hive-style partition prefix (``tenant=.../dt=...``).

    :param data: the payload from the mobile app - ``route_id``, ``vehicle_id``,
        ``vehicle_name``, ``user_id``, ``coordinates_list``, ... (the shape
        produced by :func:`generate_offline_coordinates_data`).
    :param tenant_id: the active tenant's numeric id (``request.tenant.id``).
    :param use_gzip: if True, gzip the JSON before upload (``.json.gz``).
    :return: the S3 key of the uploaded object.
    :raises ValueError: if the bucket setting is unset.
    :raises botocore.exceptions.ClientError: on an S3 failure (already logged).
    """
    s3_key, body, content_type, _ = build_offline_coordinates_object(
        data, tenant_id, use_gzip=use_gzip
    )
    return put_offline_coordinates_object(s3_key, body, content_type)


def list_offline_coordinates_objects(
    *, tenant_id: int | None = None, prefix: str | None = None, limit: int | None = None
):
    """List offline-coordinates objects in the bucket with their sizes.

    :param tenant_id: if given, restrict to this tenant's prefix
        (``offline-coordinates/tenant=<id>/``).
    :param prefix: explicit key prefix; overrides ``tenant_id`` when both given.
    :param limit: stop after yielding this many objects (None = no limit).
    :yield: dicts ``{"key": str, "size": int, "last_modified": datetime}``,
        newest keys as S3 returns them (lexicographic by key).
    :raises ValueError: if the bucket setting is unset.
    :raises botocore.exceptions.ClientError: on an S3 failure.
    """
    bucket_name = getattr(settings, "AWS_S3_COORDINATES_BUCKET", "")
    if not bucket_name:
        raise ValueError(
            "AWS_S3_COORDINATES_BUCKET is not configured "
            "(set it in settings_local.py)."
        )

    if prefix is None:
        prefix = _tenant_prefix(tenant_id) if tenant_id is not None else COORDINATES_PREFIX

    client = _s3_client(getattr(settings, "AWS_S3_REGION", None))
    paginator = client.get_paginator("list_objects_v2")

    yielded = 0
    for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
        for obj in page.get("Contents", []):
            yield {
                "key": obj["Key"],
                "size": obj["Size"],
                "last_modified": obj["LastModified"],
            }
            yielded += 1
            if limit is not None and yielded >= limit:
                return


def generate_offline_coordinates_data(rows_count: int) -> dict:
    """Generate fake offline-coordinates data shaped like the mobile app body.

    Useful for tests, load generation and manually exercising the S3 upload.
    ``random`` is imported lazily so production request paths never pull it in.

    :param rows_count: how many coordinate rows to put into ``coordinates_list``.
    :return: a dict with ``vehicle_id``, ``vehicle_name``, ``route_id`` and a
        ``coordinates_list`` of ``rows_count`` points (each with ``timestamp``,
        ``lat``, ``lng``, ``gps_on``).
    """
    if rows_count < 0:
        raise ValueError("rows_count must be non-negative")

    import random

    route_id = random.randint(1, 1_000_000)
    base_timestamp = int(timezone.now().timestamp())

    coordinates_list = [
        {
            # Original coordinates arrive as unix timestamps, in ascending order.
            "timestamp": base_timestamp + offset,
            "lat": round(random.uniform(-90.0, 90.0), 6),
            "lng": round(random.uniform(-180.0, 180.0), 6),
            "gps_on": random.choice([True, False]),
        }
        for offset in range(rows_count)
    ]

    return {
        "vehicle_id": random.randint(1, 100_000),
        "vehicle_name": f"Vehicle {random.randint(1, 9999)}",
        "route_id": route_id,
        "coordinates_list": coordinates_list,
    }