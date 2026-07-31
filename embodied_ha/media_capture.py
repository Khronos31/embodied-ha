"""Shared frame capture helper for visual media."""

from __future__ import annotations

import subprocess


def _camera_proxy_url(ha_url: str, source: str) -> str:
    base = (ha_url or "").rstrip("/")
    if base.endswith("/api"):
        base = base[:-4]
    return f"{base}/api/camera_proxy/{source}"


def fetch_frame(source: str, *, ha_url: str, go2rtc_url: str, token: str) -> bytes | None:
    """Fetch a JPEG frame for ``source``.

    ``source`` values containing ``.`` are treated as Home Assistant entity IDs
    and fetched via ``camera_proxy`` with a bearer token. Everything else is
    treated as a go2rtc source name.
    """

    source = (source or "").strip()
    if not source:
        return None

    if "." in source:
        url = _camera_proxy_url(ha_url, source)
        cmd = ["curl", "-sf", "--max-time", "8"]
        if token:
            cmd += ["-H", "@-"]
        cmd += [url]
    else:
        url = (go2rtc_url or "").rstrip("/") + f"/api/frame.jpeg?src={source}"
        cmd = ["curl", "-sf", "--max-time", "8", url]

    try:
        header_input = f"Authorization: Bearer {token}\n".encode() if "." in source and token else None
        result = subprocess.run(cmd, input=header_input, capture_output=True)
    except Exception:
        return None
    if result.returncode != 0 or len(result.stdout or b"") < 100:
        return None
    return result.stdout
