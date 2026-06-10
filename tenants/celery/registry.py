"""Auto-wrap legacy class-based tasks so they inherit TenantTask
(mirrors tenant_schemas_celery.registry; only the import path changes)."""
import inspect

from celery.app.registry import TaskRegistry

from .task import TenantTask


class TenantTaskRegistry(TaskRegistry):
    def register(self, task):
        if inspect.isclass(task) and not issubclass(task, TenantTask):
            class DynamicTenantTask(task, TenantTask):
                name = task.name
            task = DynamicTenantTask
        super().register(task)
