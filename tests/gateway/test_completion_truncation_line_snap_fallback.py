"""Regression for a bug report (kanban t_1b0504c9): ``_build_process_completion_event``'s
line-boundary snap in its output-truncation logic can empty the tail entirely.

When the raw process output is one long line (no embedded newlines) and ends with a single
trailing newline (e.g. a plugin/CLI ``print()``), the last ~2000 chars can contain exactly one
``\\n`` sitting right near the end of that window. Snapping to "the text after the first
newline in the tail" then throws away almost everything, producing a truncation notice with
zero bytes of actual content even though the real output is large. The fix must fall back to
the unsnapped tail whenever snapping would leave too little content to be useful.
"""

from types import SimpleNamespace

import agent.redact as redact_mod
import gateway.run as gateway_run_mod
import tools.ansi_strip as ansi_strip_mod
import tools.process_registry as process_registry_mod
from gateway.run_notifications import GatewayNotificationsMixin


def _identity_patches(monkeypatch):
    """Neutralize the ANSI-strip/redaction/plugin-transform seams so the test exercises only
    the truncation math, not unrelated output-processing behavior."""
    monkeypatch.setattr(ansi_strip_mod, "strip_ansi", lambda s: s)
    monkeypatch.setattr(
        process_registry_mod, "transform_process_output",
        lambda output, *, command, returncode, task_id="": output,
    )
    monkeypatch.setattr(redact_mod, "redact_terminal_output", lambda text, command: text)
    # ``_build_process_completion_event`` does ``from gateway.run import
    # _redact_gateway_user_facing_secrets`` locally, so the facade module is the seam.
    monkeypatch.setattr(gateway_run_mod, "_redact_gateway_user_facing_secrets", lambda text: text)


def _session(output_buffer: str):
    return SimpleNamespace(
        output_buffer=output_buffer,
        exit_code=0,
        command="echo",
        task_id="",
        started_at=None,
        completion_reason="exited",
        termination_source="",
        owner_task_id="",
        parent_session_id="",
    )


def test_single_line_output_with_trailing_newline_is_not_emptied(monkeypatch):
    """Reported repro: a >2000-char single-line JSON blob terminated by one trailing '\\n'
    (an ordinary print()). The only newline the 2000-char tail window contains sits at its very
    end, so the old "snap to the char after the first newline in the tail" logic collapsed the
    shown content to 0 chars. The fix must keep real content instead."""
    _identity_patches(monkeypatch)
    raw = "x" * 5000 + "\n"
    watcher: dict = {}
    event = GatewayNotificationsMixin._build_process_completion_event(watcher, _session(raw), "sess-1")
    out = event["output"]
    assert out.startswith("[… output truncated — showing last ")
    shown = out.split("\n", 1)[1] if "\n" in out else ""
    assert len(shown) > 0, f"truncated output was emptied: {out!r}"
    # Real content must actually be present, not just a nonzero but token length -- the
    # fallback-to-unsnapped tail should keep (close to) the full ~2000-char window.
    assert len(shown) >= 1000, f"expected substantial fallback content, got {len(shown)} chars: {shown[:80]!r}"
    assert shown.count("x") >= 1000


def test_multiline_output_still_snaps_to_a_line_boundary(monkeypatch):
    """Normal multi-line output (the #23284 case this logic exists for) must still snap to a
    line boundary when doing so leaves substantial content, so notifications never start
    mid-line."""
    _identity_patches(monkeypatch)
    lines = [f"line-{i:04d}-" + "y" * 50 for i in range(200)]
    raw = "\n".join(lines)
    watcher: dict = {}
    event = GatewayNotificationsMixin._build_process_completion_event(watcher, _session(raw), "sess-2")
    out = event["output"]
    shown = out.split("\n", 1)[1] if "\n" in out else ""
    assert shown, "expected non-empty truncated tail for multi-line input"
    # Snapped output must start exactly at a line boundary (no partial "line-00NN-yyy..." prefix cut).
    assert shown.startswith("line-"), f"expected a clean line boundary, got: {shown[:40]!r}"


def test_short_output_is_returned_verbatim(monkeypatch):
    """Sanity: output at/under the limit is untouched by the truncation path."""
    _identity_patches(monkeypatch)
    raw = "short output\nsecond line\n"
    watcher: dict = {}
    event = GatewayNotificationsMixin._build_process_completion_event(watcher, _session(raw), "sess-3")
    assert event["output"] == raw
