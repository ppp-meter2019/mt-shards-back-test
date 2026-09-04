"""commons.platform — the mode-aware seam that lets ONE codebase run standalone or
multi-tenant (USE_MULTITENANT), for THIS project. NOT a generic reusable library.

Application/business code imports the primitives here (tenancy / admin / commands / beat /
mode) instead of importing `tenants` or `django_tenants` directly, so the same code runs in
both modes:
  * multitenant -> the real shard-aware helpers from the `tenants` app, reached through a
                   USE_MULTITENANT-guarded seam (`tenants` is absent in standalone);
  * standalone  -> no-ops / plain fallbacks (single default DB, no schemas, no fanout).


DEPENDENCY DIRECTION
--------------------
`tenants` imports `commons.platform`; commons reaches back only through mode-GUARDED seams.
What is absolutely forbidden is an UNGUARDED top-level `tenants` import — it would break
standalone at import time. Two guarded shapes are sanctioned, both unreachable in standalone:
  * lazy, in-function          — admin.management_site(), tenancy.active_target_schemas()
  * module-level INSIDE the
    `if settings.USE_MULTITENANT:` branch — tenancy's re-export block, so schema_context /
    tenant_context / use_alias stay zero-cost ALIASES rather than wrapper generators.


TWO HALVES — the distinction that actually drives the design
------------------------------------------------------------
  * LOAD-TIME  (all of mode.py; the load-time HALF of beat.py) — reached while settings.py is
    still EXECUTING (settings_base.py:41, settings_multitenant.py:17). On those paths: no import
    from `tenants` / `django_tenants`, and no read of django.conf.settings — touching it
    mid-load caches an incomplete settings object. The app registry does not exist yet either,
    so nothing there can look anything up. Three constructs exist solely because of this rule
    and are its consequences, not three separate quirks:
        beat.FANOUT_TASK_NAME   a bare STRING naming a `tenants` task, because the task
                                object cannot be imported or resolved at settings-load;
                                the wiring is pinned instead by the tenants.E005 check.
        mode.use_multitenant()  reads env / settings_mode.py directly, NOT settings.*
        beat.bootstrap_float()  same, for FANOUT_PERIOD_DEFAULT (the load-time beat tick).
    NB beat.py is deliberately MIXED, and this is the trap to watch: scoped_schedule() and
    FANOUT_PERIOD_DEFAULT are load-time and obey the above, while beat_conf() and task_queue()
    run at task runtime and read settings.* freely (beat.py:58, :150). The discipline there is
    PER-FUNCTION, not per-module — adding a settings read to scoped_schedule would break
    settings loading in the host project, adding one to beat_conf is routine.
  * RUNTIME    (tenancy.py, admin.py, commands.py) — imported after django.setup(); free to
    reach into `tenants` through the guarded seams above. NB tenancy.py reads
    settings.USE_MULTITENANT at import, so it must never be pulled in at settings-load time.
"""
