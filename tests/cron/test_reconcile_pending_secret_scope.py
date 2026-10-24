"""Regression: receipt-recovery delivery read a profile secret unscoped.

``reconcile_pending()`` (``cron.scheduler_tick._pre_dispatch_housekeeping`` runs it every tick,
BEFORE due jobs are even read) replays any admission journal whose firer died and calls
``cron.scheduler._save_compose_deliver()`` directly for it.

Unlike the ordinary ``run_one_job`` tail -- which spans ``claim_dispatch`` through
save/compose/deliver/finish inside ``_install_fire_secret_scope()`` -- this recovery path never
installed the firing profile's secret scope. Delivery-target resolution
(``_resolve_delivery_targets`` -> ``_get_home_target_chat_id`` -> ``get_secret``) then raised
``UnscopedSecretError`` under multiplex hosting for ANY bare-platform deliver target backed by a
secret (e.g. Matrix's ``MATRIX_HOME_ROOM``). The per-journal ``except Exception`` in
``reconcile_pending`` swallows it (logs "Cron receipt recovery deferred") WITHOUT retiring the
journal, so the SAME stuck admission re-raised on every subsequent tick forever -- 1934 times in
production (agent.log.1) for one receipt.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from agent import secret_scope
from cron import executions, jobs, scheduler_authority
from hermes_cli import gateway_client


def peer(monkeypatch, call):
    class Peer:
        async def rpc(self, method, **params):
            return call(method, params)

    @asynccontextmanager
    async def connected():
        yield Peer()

    monkeypatch.setattr(gateway_client, "connect_gateway", connected)


def test_reconcile_pending_binds_secret_scope_before_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("MATRIX_HOME_ROOM=!room:example.org\n")

    secret_scope.set_multiplex_active(True)
    seen: dict = {}

    def _fake_save_compose_deliver(d, fence, final_response, output, **kwargs):
        # The exact read the real _save_compose_deliver -> _resolve_delivery_targets ->
        # _get_home_target_chat_id -> _env_home_target_chat_id -> _home_env_lookup chain performs
        # for a bare Matrix delivery target. Raises UnscopedSecretError with no scope bound.
        seen["chat"] = secret_scope.get_secret("MATRIX_HOME_ROOM")

    from cron import scheduler

    monkeypatch.setattr(scheduler, "_save_compose_deliver", _fake_save_compose_deliver)

    try:
        with jobs.use_cron_store(tmp_path / "cron"):
            job = jobs.create_job(prompt="report", schedule="every 1h", deliver="local")
            execution = executions.create_execution(job["id"], source="test")
            executions.mark_execution_running(execution["id"])
            with executions._transaction() as conn:
                conn.execute(
                    "UPDATE executions SET process_id=?,pid=? WHERE id=?",
                    ("old-owner", 999999, execution["id"]),
                )
            params = {"job_id": job["id"], "request_id": execution["id"], "extra_prompt": None}
            journal = scheduler_authority.journal_path(job["id"], execution["id"])
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text(json.dumps({"params": params, "receipt": None}))
            peer(
                monkeypatch,
                lambda method, params: {
                    "status": "terminal",
                    "job": job,
                    "result": [True, "document", "answer", None],
                },
            )
            scheduler_authority.reconcile_pending()
    finally:
        secret_scope.set_multiplex_active(False)

    assert "chat" in seen, (
        "reconcile_pending must call _save_compose_deliver() -- if this fails, the per-journal "
        "except-block swallowed an exception (UnscopedSecretError, pre-fix) before the fake ran"
    )
    assert seen["chat"] == "!room:example.org", (
        "reconcile_pending must bind the firing profile's secret scope BEFORE "
        "_save_compose_deliver() runs, or get_secret() fails closed under multiplex "
        "(agent.secret_scope.UnscopedSecretError: MATRIX_HOME_ROOM)"
    )
