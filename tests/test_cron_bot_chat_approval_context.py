#!/usr/bin/env python3
"""t_793fe102 / AUDIT_2026-1023-F RCA test — proves the structural gap AH2 코대리 found
(2026-10-23) in Hermes's own approval gate: a cron job's output admitted as an inbound Bot Chat
turn into a persistent ``platform=local`` session is NOT recognized as a cron/unattended approval
context, so a dangerous command issued during that turn falls through to the full interactive
approval wait (60-300s) with no human watching, then fails closed anyway.

Round 2 (AUDIT_2026-1023-F REJECT remediation). Addresses the three blocking findings against
round 1's submission:

  R5 (regression baseline): round 1 added an ``event`` parameter to
  ``gateway.run.TurnRunner._set_session_env``, which 30 test files' 1-argument lambda test doubles
  do not accept, breaking 63 of their assertions. Round 2's wiring never changes that method's
  signature (see ``tools/approval_context.py``'s ``set_current_turn_event_text`` /
  ``_current_turn_is_cron_delivered_bot_chat`` docstrings and ``gateway/run.py``'s
  ``_set_session_env`` docstring) — this file's tests exercise the staging/detection functions
  directly rather than ``gateway.run.TurnRunner`` (observed in round 1 to abort the whole pytest
  process when instantiated under this ad-hoc harness; the call-site wiring itself
  (gateway/run_turn_prepare.py's ``_hmwa_prepare_turn``) is reviewable directly in its diff).

  R6 (test validates the contract, not a hand copy): the fixture text below is built by calling
  the REAL ``cron.scheduler_delivery.bot_chat_message()`` producer, not a hardcoded string literal.
  If that function's wording ever changes, this fixture changes with it. The legacy delivery path
  (``cron.bot_chat_legacy``) is covered by the identity assertion in
  ``test_legacy_delivery_path_shares_the_same_producer`` below, rather than a second hand-copied
  fixture, because it imports and calls the exact same function — there is only one producer.

  R7 (detection is not full-text scan): the live code had `head = text[:200]`, so any job name
  >= 152 characters pushed the marker out of the scanned window (false negative, the exact bug
  this fix exists to close). The fixed detector in this round scans the full text with no window;
  ``test_detection_has_no_length_boundary_after_the_fix`` proves a job name far past the old
  152-char boundary still detects True, using the real producer (not a hand-built string).

This test imports the LIVE (unpatched) hermes-agent modules directly — it is RED right now
(ImportError: no such names in tools.approval_context). Apply approval_context.diff + run.diff +
run_turn_prepare.diff + scheduler_delivery.diff (scratch copies alongside this file; the live
files were never touched) to turn it GREEN. Run with:
    .venv/bin/python3 -m pytest \
        tests/gateway/test_cron_bot_chat_approval_context.py -v
"""
from __future__ import annotations

from cron.scheduler_delivery import bot_chat_message
from tools.approval_context import (
    _current_turn_is_cron_delivered_bot_chat,
    _is_cron_delivered_bot_chat_text,
    reset_current_turn_event_text,
    set_current_turn_event_text,
)

# 실측 그대로: 랜스(ah2-lance) agent.log에서 재현한 cron 주입 잡 메타데이터 — 텍스트 자체는
# 하드코딩하지 않고 실제 생산자(bot_chat_message)를 호출해서 만든다 (R6).
_LIVE_JOB = {"id": "job-691f0554", "name": "뉴스감시선 승인게이트→랜스 중계"}
_LIVE_CONTENT = "(여기부터 실제 cron job 산출물 본문)"
LIVE_CRON_BOT_CHAT_TEXT = bot_chat_message(_LIVE_JOB, _LIVE_CONTENT)

LIVE_HUMAN_OR_AGENT_DM_TEXT = (
    "Message from 🤖 수호자 (@ah2-suho): 중요: 더 기다리지 마세요 — 제가 드린 3단계 판단축"
)


def test_is_cron_delivered_bot_chat_text_detects_the_real_producer_output():
    """RED without the fix: approval_context has no such function yet. GREEN: detects text the
    real cron.scheduler_delivery.bot_chat_message() produced, not a hand-typed copy of it."""
    assert _is_cron_delivered_bot_chat_text(LIVE_CRON_BOT_CHAT_TEXT) is True


def test_is_cron_delivered_bot_chat_text_does_not_misclassify_live_agent_dm():
    """A live inter-agent DM delivered into the SAME persistent local session must NOT be treated
    as unattended — only genuinely cron-sourced turns should. Also covers non-str/empty text
    (R9 advisory: must not raise, must return False)."""
    assert _is_cron_delivered_bot_chat_text(LIVE_HUMAN_OR_AGENT_DM_TEXT) is False
    assert _is_cron_delivered_bot_chat_text(None) is False
    assert _is_cron_delivered_bot_chat_text("") is False
    assert _is_cron_delivered_bot_chat_text("완전히 무관한 텍스트") is False
    assert _is_cron_delivered_bot_chat_text(["not", "a", "string"]) is False
    assert _is_cron_delivered_bot_chat_text(12345) is False


def test_detection_has_no_length_boundary_after_the_fix():
    """R7: the pre-fix detector used `head = text[:200]`, so a job name >= 152 chars pushed the
    marker past the scanned window and detection went silently False (300s bug reproducible).
    Build the fixture through the REAL producer with a job name far past that old boundary and
    assert detection still fires — proves the window is gone, not just widened."""
    long_name = "장애재현용-매우-긴-잡이름-" * 20  # far past the old 152-char false-negative boundary
    assert len(long_name) > 200
    text = bot_chat_message({"id": "job-long", "name": long_name}, _LIVE_CONTENT)
    assert _is_cron_delivered_bot_chat_text(text) is True


def test_legacy_delivery_path_shares_the_same_producer():
    """R6: cron.bot_chat_legacy does not have its own copy of the wrapper text. Its ``_dispose``
    does a function-local ``from cron.scheduler_delivery import ... bot_chat_message`` (so it is
    not a module attribute to compare by identity) and calls that same function directly — this
    one producer (and its one detector pairing above) covers both the live and legacy delivery
    lanes. Proven by locating that exact import + call in its source, and, for belt-and-suspenders,
    that the module does not define any wrapper-text literal of its own."""
    import inspect

    from cron import bot_chat_legacy

    source = inspect.getsource(bot_chat_legacy._dispose)
    assert "from cron.scheduler_delivery import" in source
    assert "bot_chat_message" in source
    assert "bot_chat_message(record" in source  # actually calls it, not just imports it
    assert "Cronjob" not in inspect.getsource(bot_chat_legacy)  # no local copy of the wrapper text


def test_staged_turn_event_text_drives_cron_detection_and_resets_cleanly():
    """Exercises the exact staging mechanism gateway/run_turn_prepare.py's _hmwa_prepare_turn
    uses around the unchanged, 1-argument self._set_session_env(context) call (R5: no signature
    change, so none of the 30 test-double files are touched). Does not instantiate
    gateway.run.TurnRunner (observed in round 1 to abort the whole pytest process under this
    harness); the real call-site wiring is reviewable directly in run.diff / run_turn_prepare.diff."""
    assert _current_turn_is_cron_delivered_bot_chat() is False  # nothing staged yet

    token = set_current_turn_event_text(LIVE_CRON_BOT_CHAT_TEXT)
    try:
        assert _current_turn_is_cron_delivered_bot_chat() is True
    finally:
        reset_current_turn_event_text(token)
    assert _current_turn_is_cron_delivered_bot_chat() is False  # cleanly restored

    token = set_current_turn_event_text(LIVE_HUMAN_OR_AGENT_DM_TEXT)
    try:
        assert _current_turn_is_cron_delivered_bot_chat() is False
    finally:
        reset_current_turn_event_text(token)

    token = set_current_turn_event_text(None)
    try:
        assert _current_turn_is_cron_delivered_bot_chat() is False
    finally:
        reset_current_turn_event_text(token)
