"""Persist the first-run agent CLI terms acknowledgement.

Embodied HA invokes each provider's official CLI and may drive its install and
authentication prompts on the user's behalf.  The Web UI records an explicit
acknowledgement before those setup endpoints become available.  Existing
instances with a valid harness selection are grandfathered so an upgrade does
not interrupt a running agent.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path

import harness_state

CONSENT_VERSION = "2026-08-12"
CONSENT_STATEMENT = (
    "Embodied HAがツールのインストールと認証操作を代行することを理解し、"
    "事前に利用する各ツールの利用規約を確認して同意しました。"
)
TERMS_LINKS = (
    {
        "provider": "Anthropic",
        "label": "Consumer Terms",
        "url": "https://www.anthropic.com/legal/consumer-terms",
    },
    {
        "provider": "Anthropic",
        "label": "Commercial Terms",
        "url": "https://www.anthropic.com/legal/commercial-terms",
    },
    {
        "provider": "OpenAI",
        "label": "Terms of Use",
        "url": "https://openai.com/policies/terms-of-use/",
    },
    {
        "provider": "Google Antigravity",
        "label": "Additional Terms of Service",
        "url": "https://antigravity.google/terms",
    },
)


def consent_path() -> Path:
    override = os.environ.get("EHA_SETUP_TERMS_FILE")
    if override:
        return Path(override)
    data_dir = os.environ.get("EHA_DATA_DIR", os.path.dirname(__file__))
    return Path(data_dir) / "setup_terms_consent.json"


def _read_record() -> dict | None:
    try:
        value = json.loads(consent_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("accepted") is not True:
        return None
    if value.get("version") != CONSENT_VERSION:
        return None
    if not isinstance(value.get("accepted_at"), str):
        return None
    return value


def is_accepted() -> bool:
    return _read_record() is not None


def is_grandfathered() -> bool:
    """Existing selected installations must keep running after an upgrade."""
    try:
        state, _ = harness_state.read_selection()
    except OSError:
        return False
    return state == "valid"


def is_required() -> bool:
    return not is_accepted() and not is_grandfathered()


def public_status() -> dict:
    record = _read_record()
    grandfathered = record is None and is_grandfathered()
    return {
        "required": record is None and not grandfathered,
        "accepted": record is not None,
        "grandfathered": grandfathered,
        "version": CONSENT_VERSION,
        "accepted_at": record.get("accepted_at") if record else None,
        "statement": CONSENT_STATEMENT,
        "terms": [dict(item) for item in TERMS_LINKS],
    }


def accept(version: str) -> dict:
    if version != CONSENT_VERSION:
        raise ValueError("表示中の利用規約確認画面を再読み込みしてください")

    path = consent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "accepted": True,
        "version": CONSENT_VERSION,
        "accepted_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "statement": CONSENT_STATEMENT,
        "terms": [dict(item) for item in TERMS_LINKS],
    }
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return public_status()
