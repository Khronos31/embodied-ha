"""Read-only runtime status snapshot for harness selection (Step4 増分1a).

`harness_state.py` stays persistence-only (dependency-free). This module layers
the per-harness setup checks on top and is the *single* definition of
"is a harness ready" shared by both `daemon.harness_ready()` and the web
server's `_selected_harness_ready()` / `/api/setup/overview` (sol R5: avoid the
two mirrors drifting).

Everything here is read-only. The legacy Claude migration (writing the flag for
pre-flag authenticated instances) is a daemon-only side effect and is
deliberately NOT performed here (sol H7: migration is daemon preprocessing).
"""
from __future__ import annotations

import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import harness_state  # noqa: E402  persistence (mandatory)
import claude_setup  # noqa: E402  production-critical (mandatory, mirrors server.py)

# codex/agy setup modules are optional in the web server (defensive import →
# None). Mirror that here so importing harness_status never hard-fails when an
# optional harness module is missing; readiness() then fails closed for it.
try:
    import codex_setup  # noqa: E402
except Exception:
    codex_setup = None
try:
    import antigravity_setup  # noqa: E402
except Exception:
    antigravity_setup = None

HARNESSES = ("claude", "codex", "agy")


def readiness(harness: str | None) -> bool:
    """Whether ``harness`` can start the autonomous runtime (read-only).

    Byte-for-byte the same criteria the daemon used inline before increment 1a:
    Claude needs auth + a resolvable binary; codex/agy need install + auth.
    """
    if harness == "claude":
        return (
            claude_setup.is_authenticated()
            and claude_setup.resolve_claude_bin() is not None
        )
    if harness == "codex":
        return (
            codex_setup is not None
            and codex_setup.is_installed()
            and codex_setup.is_authenticated()
        )
    if harness == "agy":
        return (
            antigravity_setup is not None
            and antigravity_setup.is_installed()
            and antigravity_setup.is_authenticated()
        )
    return False


def _capture() -> dict:
    """Evaluate every harness predicate exactly once (single-read snapshot).

    sol 1a-review Med2: reading the flag twice or re-running install/auth
    predicates lets a concurrent state change produce a self-contradictory
    reply (``ready: true`` next to ``installed: false``). Capturing each
    predicate once here makes the snapshot internally consistent.
    """
    def _pair(mod) -> dict:
        if mod is None:
            return {"installed": False, "authenticated": False}
        return {
            "installed": bool(mod.is_installed()),
            "authenticated": bool(mod.is_authenticated()),
        }

    claude = _pair(claude_setup)
    # Claude readiness additionally needs a resolvable binary (not just auth).
    claude_bin = claude_setup.resolve_claude_bin() is not None
    return {
        "claude": claude,
        "claude_bin": claude_bin,
        "codex": _pair(codex_setup),
        "agy": _pair(antigravity_setup),
    }


def _ready_from_capture(harness: str | None, cap: dict) -> bool:
    """Readiness derived from an already-captured snapshot (no fresh reads).

    Same criteria as readiness(): Claude=auth+binary, codex/agy=install+auth.
    """
    if harness == "claude":
        return cap["claude"]["authenticated"] and cap["claude_bin"]
    if harness == "codex":
        return cap["codex"]["installed"] and cap["codex"]["authenticated"]
    if harness == "agy":
        return cap["agy"]["installed"] and cap["agy"]["authenticated"]
    return False


def snapshot() -> dict:
    """Read-only status snapshot shared by daemon readiness and the web overview.

    Public schema is fixed (sol L11): only ``selection_state`` / ``selected`` /
    ``effective`` / ``ready`` / per-harness ``installed``+``authenticated`` — no
    paths, tokens, or versions.

    ``selected`` is the persisted valid selection (or None). ``effective`` is the
    harness that will actually execute — invoke-agent's ultimate default is
    Claude, so a missing/invalid flag resolves to ``"claude"`` — while ``ready``
    gates whether the runtime should start. The flag is read once and every
    harness predicate is captured once (single-read consistency, sol Med2).
    """
    selection_state, selected = harness_state.read_selection()
    cap = _capture()

    if selection_state == "valid":
        effective = selected
        ready = _ready_from_capture(selected, cap)
    elif selection_state == "missing":
        # Legacy compatibility (read-only mirror of the daemon's Claude
        # migration): a pre-flag instance with authenticated Claude runs as
        # Claude, so the overview reports the running instance instead of
        # forcing the picker (sol H8/Med8).
        effective = "claude"
        ready = _ready_from_capture("claude", cap)
    else:  # invalid / unknown → fail closed, ask the user to pick a harness
        effective = "claude"
        ready = False

    return {
        "selection_state": selection_state,
        "selected": selected if selection_state == "valid" else None,
        "effective": effective,
        "ready": ready,
        "harnesses": {
            "claude": cap["claude"],
            "codex": cap["codex"],
            "agy": cap["agy"],
        },
    }
