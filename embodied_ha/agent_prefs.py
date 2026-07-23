"""Persistent per-harness model/effort preferences for the default tier (Step4増分2).

The UI lets the user pick, for the selected harness, which concrete model + reasoning
effort the *default* tier resolves to. Those choices live in a small JSON at
``/data/agent_prefs.json`` (not options.json — keeps the setup surface from being
casually edited into an inconsistent state, mirroring the harness-selection flag).

run.sh reads `env_overrides(harness)` at boot and exports the corresponding
``EHA_<H>_MODEL_DEFAULT`` / effort env vars, which invoke-agent.sh already consumes.
When the file is absent or a key is unset, nothing is exported and invoke-agent.sh
falls back to its built-in defaults (sonnet/medium, terra/medium, Gemini flash) —
so an instance with no prefs behaves exactly as before (byte-safe for existing instances).

Changes take effect on the next run.sh boot; the save endpoint self-restarts to
apply them (save-then-restart contract, sol Med6).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading

VALID_HARNESSES = ("claude", "codex", "agy")
VALID_EFFORTS = ("low", "medium", "high")

# Serialises the endpoint's load→merge→save so concurrent saves don't lose an
# update (sol Med6/Low). os.replace() only prevents a torn file, not a lost write.
_save_lock = threading.Lock()


def _has_control_chars(value: str) -> bool:
    """True if the string contains control chars (incl. the TSV/record separators
    run.sh parses on). Blocks env-record injection via a crafted model name (sol High)."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)

_PREFS_FILE_ENV = "EHA_AGENT_PREFS_FILE"
_DEFAULT_PREFS_FILE = "/data/agent_prefs.json"

# Per-harness mapping of logical field -> the exact env var invoke-agent.sh reads
# for the default tier. Note codex's effort key is REASONING_EFFORT (sol Med6).
_ENV_KEY_MAP = {
    "claude": {"model": "EHA_CLAUDE_MODEL_DEFAULT", "effort": "EHA_CLAUDE_EFFORT_DEFAULT"},
    "codex": {"model": "EHA_CODEX_MODEL_DEFAULT", "effort": "EHA_CODEX_REASONING_EFFORT_DEFAULT"},
    # agy encodes effort in the model name, so it has no separate effort key.
    "agy": {"model": "EHA_AGY_MODEL_DEFAULT"},
}


def prefs_path() -> str:
    """Return the agent-prefs path, resolving the environment each call."""
    return os.environ.get(_PREFS_FILE_ENV, _DEFAULT_PREFS_FILE)


def load() -> dict:
    """Return the prefs dict, or ``{}`` for a missing/empty/corrupt file (fail soft)."""
    try:
        with open(prefs_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _harness_pref(harness: str, prefs: dict | None = None) -> dict:
    if prefs is None:
        prefs = load()
    tier = prefs.get("default_tier")
    if not isinstance(tier, dict):
        return {}
    entry = tier.get(harness)
    return entry if isinstance(entry, dict) else {}


def env_overrides(harness: str, prefs: dict | None = None) -> dict[str, str]:
    """Env vars to export for ``harness``'s default tier (only ones actually set).

    Unknown harness, absent prefs, or blank values yield an empty dict, so run.sh
    exports nothing and invoke-agent.sh keeps its built-in defaults.
    """
    keys = _ENV_KEY_MAP.get(harness)
    if not keys:
        return {}
    entry = _harness_pref(harness, prefs)
    overrides: dict[str, str] = {}
    for field, env_key in keys.items():
        value = entry.get(field)
        if isinstance(value, str) and value.strip() and not _has_control_chars(value):
            # Skip control-char values even for a hand-edited prefs file (defence
            # in depth alongside validate_entry, since run.sh reads this directly).
            overrides[env_key] = value.strip()
    return overrides


def validate_entry(harness: str, model: str | None, effort: str | None) -> None:
    """Raise ValueError if a save request is malformed (endpoint input guard)."""
    if harness not in VALID_HARNESSES:
        raise ValueError(f"unknown harness: {harness!r}")
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if _has_control_chars(model):
            raise ValueError("model must not contain control characters")
    if effort is not None:
        if harness == "agy":
            raise ValueError("agy encodes effort in the model name; effort is not accepted")
        if effort not in VALID_EFFORTS:
            raise ValueError(f"effort must be one of {VALID_EFFORTS}")


def set_default_tier(harness: str, model: str | None = None, effort: str | None = None,
                     prefs: dict | None = None) -> dict:
    """Return a new prefs dict with ``harness``'s default-tier model/effort updated.

    Validates first. Does not write; callers persist with save() so the write and the
    subsequent self-restart stay in the endpoint.
    """
    validate_entry(harness, model, effort)
    if prefs is None:
        prefs = load()
    prefs = dict(prefs)
    tier = dict(prefs.get("default_tier") if isinstance(prefs.get("default_tier"), dict) else {})
    entry = dict(tier.get(harness) if isinstance(tier.get(harness), dict) else {})
    if model is not None:
        entry["model"] = model.strip()
    if effort is not None:
        entry["effort"] = effort
    tier[harness] = entry
    prefs["default_tier"] = tier
    return prefs


def save(prefs: dict) -> None:
    """Atomically persist the prefs dict."""
    path = prefs_path()
    parent = os.path.dirname(path) or os.curdir
    os.makedirs(parent, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=".agent_prefs-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(prefs, f, ensure_ascii=False, indent=2)
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


def update_default_tier(harness: str, model: str | None = None, effort: str | None = None) -> dict:
    """Validate, merge, and persist a default-tier change under a lock, returning the
    new prefs. Serialising load→merge→save prevents a concurrent save from dropping
    the other field (sol Low)."""
    with _save_lock:
        updated = set_default_tier(harness, model=model, effort=effort)
        save(updated)
        return updated
