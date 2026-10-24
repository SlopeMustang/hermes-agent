"""D-1 remedy(b) (t_eacbecca): log-level promotion only, no secret-scope binding added.

``GatewayShutdownMixin._notify_interrupted_cron_jobs`` (gateway/run_shutdown.py:905-969) swallows
any exception from ``cron.scheduler._resolve_delivery_targets()`` at **DEBUG** (the per-job
``except Exception`` at line 939/940), and the outer call-site wrapper
``_log_suppressed(logging.DEBUG, ...)`` (run_shutdown.py:1938) swallows the same failure class a
second time at DEBUG. Per t_7980043e RCA (same defect class as t_dfef8626's
``UnscopedSecretError`` under multiplex with no secret scope installed), this means the failure is
invisible to any WARNING+-level audit.

remedy(a) (installing the firing profile's secret scope before resolving delivery targets, the
same pattern as ``reconcile_pending``'s fix) is explicitly OUT OF SCOPE for this card -- deferred
per D-1a (ambient ``HERMES_HOME`` wrong-profile-binding risk, see t_7980043e comment thread). This
test does NOT assert that delivery resolution succeeds or that the correct profile's secret is
read; it only asserts that the swallowed exception is now VISIBLE at WARNING level instead of
being silently dropped at DEBUG.

RED (pre-fix): both run_shutdown.py:940 and :1938 log at DEBUG, so no WARNING-level record is
captured even with caplog set to WARNING.
GREEN (post-fix): both sites log at WARNING, so the swallowed ``UnscopedSecretError`` surfaces as
a WARNING-level record naming the affected job_id.
"""

from __future__ import annotations

import asyncio
import logging

from agent import secret_scope
from cron import jobs


class _FakeRunner:
    """Minimal stand-in for GatewayRunner: only ``_restart_requested`` is read before the
    per-job loop can raise (gateway/run_shutdown.py:927), so nothing else needs to exist."""

    _restart_requested = False


def test_notify_interrupted_cron_jobs_unscoped_secret_error_logs_at_warning(tmp_path, monkeypatch, caplog):
    """D-1c: the per-job swallow (line 940) must promote to WARNING so a WARNING+-level audit
    sees this failure class. Does not touch secret-scope binding (remedy(a) out of scope)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    secret_scope.set_multiplex_active(True)

    def _raise_unscoped_secret_error(job, *, for_failure=False):
        # The exact failure _resolve_delivery_targets -> _get_home_target_chat_id ->
        # _env_home_target_chat_id -> _home_env_lookup -> agent.secret_scope.get_secret raises
        # in production when multiplex is active and no scope has been installed for the firing
        # profile (D-1 RCA). Raised directly here rather than through the full chain -- what this
        # test verifies is the log level the swallow uses, not the chain itself.
        raise secret_scope.UnscopedSecretError("MATRIX_HOME_ROOM")

    from cron import scheduler

    monkeypatch.setattr(scheduler, "_resolve_delivery_targets", _raise_unscoped_secret_error)

    try:
        with jobs.use_cron_store(tmp_path / "cron"):
            job = jobs.create_job(prompt="report", schedule="every 1h", deliver="matrix")

            from gateway.run_shutdown import GatewayShutdownMixin

            with caplog.at_level(logging.WARNING, logger="gateway.run"):
                count = asyncio.run(
                    GatewayShutdownMixin._notify_interrupted_cron_jobs(_FakeRunner(), [job["id"]])
                )
    finally:
        secret_scope.set_multiplex_active(False)

    warning_plus = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "Cron interrupt targets unresolved" in r.getMessage() and job["id"] in r.getMessage()
        for r in warning_plus
    ), (
        "D-1 remedy(b): the UnscopedSecretError swallowed by the per-job `except Exception` at "
        "run_shutdown.py:939 must be logged at WARNING (line 940), not DEBUG, so it is visible to "
        "a WARNING+-level audit. Currently (pre-fix) this is logger.debug(...), invisible with "
        "caplog pinned to WARNING. No fix to secret-scope binding is expected or asserted here "
        "(remedy(a) is out of scope for this card -- see t_eacbecca)."
    )
    assert count == 0, "no delivery target was ever resolved, so no notice should have been sent"


def test_notify_interrupted_cron_jobs_call_site_suppression_is_warning():
    """D-1c: the call-site wrapper (run_shutdown.py:1938, ``_log_suppressed``) is the SECOND swallow
    layer -- promoting only the per-job one (line 940) is not enough if _notify_interrupted_cron_jobs
    itself raises before entering the per-job loop (e.g. the `cron.jobs`/`cron.scheduler` import at
    line 923-927 fails).

    The call site lives inside ``_stop_interrupt_remaining_work``, which depends on a live
    ``GatewayRunner`` + ``_StopContext`` + drain state to reach line 1938 -- too heavy to
    instantiate for a pure log-level check. Instead this asserts directly on the source text that
    the literal level argument passed to ``_log_suppressed`` immediately before
    ``await self._notify_interrupted_cron_jobs(`` is ``logging.WARNING``, not ``logging.DEBUG``.
    RED (pre-fix): the literal is ``logging.DEBUG``. GREEN (post-fix): ``logging.WARNING``.
    """
    import inspect
    import re

    import gateway.run_shutdown as run_shutdown_module

    source = inspect.getsource(run_shutdown_module)
    call_site = re.search(
        r'with _log_suppressed\((logging\.\w+), "Cron interrupt notification failed: %s"\):\s*\n'
        r'(?:[^\n]*\n)*?\s*await self\._notify_interrupted_cron_jobs\(',
        source,
    )
    assert call_site, (
        "Could not locate the `_log_suppressed(...)` call site immediately wrapping "
        "`await self._notify_interrupted_cron_jobs(...)` in gateway/run_shutdown.py -- expected "
        "structure near line 1938 not found (file may have been restructured)."
    )
    assert call_site.group(1) == "logging.WARNING", (
        f"D-1 remedy(b): run_shutdown.py:1938 must wrap "
        f"`await self._notify_interrupted_cron_jobs(...)` with "
        f"`_log_suppressed(logging.WARNING, ...)`, not `{call_site.group(1)}` -- otherwise a "
        f"failure raised before the per-job loop (e.g. the cron.jobs/cron.scheduler import) stays "
        f"invisible to WARNING+-level audit even after the line-940 promotion."
    )
