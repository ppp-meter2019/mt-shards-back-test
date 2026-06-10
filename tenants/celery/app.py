"""CeleryApp + prerun/postrun schema switch — multi-DB adaptation of
tenant_schemas_celery.app.

Upstream sets the same schema on a fixed list of databases. We resolve the
tenant's SHARD from the schema and enter our shard-aware tenant_context (which
sets current_db -> the shard AND the schema on that shard's connection), then
exit it after the task. The context manager is stored on the task instance,
which is a singleton per prefork worker process — safe because one task runs
at a time there.
"""
from celery import Celery
from celery.signals import task_prerun, task_postrun

from .compat import get_public_schema_name, tenant_context
from .task import headers_with_schema


def _schema_from_request(task):
    """Read _schema_name from the task message (headers, or merged request)."""
    req = task.request
    if req.headers and "_schema_name" in req.headers:    # Redis broker merges headers
        return req.headers.get("_schema_name")
    return req.get("_schema_name")


def switch_schema(task, **kw):
    """task_prerun: enter the tenant's shard + schema for the task's duration."""
    schema = _schema_from_request(task) or get_public_schema_name()
    if schema == get_public_schema_name():
        task._tenant_cm = None                           # public/management → default.public
        return
    tenant = task.get_tenant_for_schema(schema)
    cm = tenant_context(tenant)                          # shard-aware: current_db + shard schema
    cm.__enter__()
    task._tenant_cm = cm


def restore_schema(task, **kw):
    """task_postrun: leave the tenant context (restores current_db + shard conn)."""
    cm = getattr(task, "_tenant_cm", None)
    if cm is not None:
        cm.__exit__(None, None, None)
        task._tenant_cm = None


task_prerun.connect(switch_schema, sender=None, dispatch_uid="tenants_switch_schema")
task_postrun.connect(restore_schema, sender=None, dispatch_uid="tenants_restore_schema")


class CeleryApp(Celery):
    registry_cls = "tenants.celery.registry:TenantTaskRegistry"
    task_cls = "tenants.celery.task:TenantTask"

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("task_cls", self.task_cls)
        super().__init__(*args, **kwargs)

    def create_task_cls(self):
        return self.subclass_with_self(
            self.task_cls, abstract=True, name="TenantTask", attribute="_app",
        )

    def send_task(self, name, args=None, kwargs=None, **options):
        options["headers"] = headers_with_schema(options.get("headers") or {})
        return super().send_task(name, args=args, kwargs=kwargs, **options)
