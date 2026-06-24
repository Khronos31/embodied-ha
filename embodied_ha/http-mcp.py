#!/usr/bin/env python3
"""汎用HTTP MCPサーバー（embodied-ha 用）。

ツール:
  http_get  … ローカルネットワーク上の HTTP GET
  http_post … ローカルネットワーク上の HTTP POST

URL は localhost / 127.x.x.x / 10.x.x.x / 172.16-31.x.x / 192.168.x.x / homeassistant.local のみ許可する。外部インターネットへのアクセスは不可。
env: なし
"""

from __future__ import annotations

import ipaddress
import json
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from mcp_lib import serve, text

TIMEOUT_SECONDS = 30
ALLOWED_HOSTNAMES = {"localhost", "homeassistant.local"}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise HTTPError(req.full_url, code, f"redirect blocked to {newurl}", headers, fp)


_OPENER = build_opener(_NoRedirect())


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _json_error(message: str) -> tuple[list[dict[str, str]], bool]:
    payload = json.dumps({"error": _clean(message) or "unknown error"}, ensure_ascii=False)
    return [text(payload)], True


def _normalize_headers(headers: Any) -> dict[str, str]:
    if not isinstance(headers, Mapping):
        return {}
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        header_key = _clean(key)
        if not header_key:
            continue
        normalized[header_key] = str(value)
    return normalized


def _is_allowed_ip(hostname: str) -> bool:
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    if ip.version != 4:
        return False
    octets = [int(part) for part in hostname.split(".")]
    if octets[0] == 127:
        return True
    if octets[0] == 10:
        return True
    if octets[0] == 192 and octets[1] == 168:
        return True
    if octets[0] == 172 and 16 <= octets[1] <= 31:
        return True
    return False


def _validate_url(url: Any) -> str:
    url_text = _clean(url)
    if not url_text:
        raise ValueError("url が空です")
    parsed = urlparse(url_text)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("http/https のみ許可されています")
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValueError("host が空です")
    if hostname in ALLOWED_HOSTNAMES or _is_allowed_ip(hostname):
        return url_text
    raise ValueError(f"local network only: {hostname}")


def _decode_body(data: bytes, headers: Any) -> str:
    charset = "utf-8"
    try:
        detected = headers.get_content_charset()  # type: ignore[attr-defined]
        if detected:
            charset = detected
    except Exception:
        pass
    return data.decode(charset, errors="replace")


def _request(url: Any, *, method: str, body: str = "", headers: Any = None) -> tuple[list[dict[str, str]], bool]:
    try:
        validated_url = _validate_url(url)
        normalized_headers = _normalize_headers(headers)
        if method == "POST" and not any(key.lower() == "content-type" for key in normalized_headers):
            normalized_headers["Content-Type"] = "application/json"
        data = body.encode("utf-8") if method == "POST" else None
        request = Request(validated_url, data=data, headers=normalized_headers, method=method)
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            payload = _decode_body(response.read(), response.headers)
        return [text(payload)], False
    except HTTPError as exc:
        detail = _decode_body(exc.read() or b"", exc.headers) if getattr(exc, "fp", None) else ""
        message = f"HTTP {exc.code}: {exc.reason}"
        if detail:
            message = f"{message} | {detail[:400]}"
        return _json_error(message)
    except URLError as exc:
        return _json_error(f"network error: {getattr(exc, 'reason', exc)}")
    except Exception as exc:
        return _json_error(str(exc))


def http_get(args: dict[str, Any]):
    return _request(args.get("url"), method="GET", headers=args.get("headers"))


def http_post(args: dict[str, Any]):
    return _request(
        args.get("url"),
        method="POST",
        body=str(args.get("body") or ""),
        headers=args.get("headers"),
    )


def main() -> None:
    serve("http-mcp", "1.0", {
        "http_get": {
            "spec": {
                "name": "http_get",
                "description": "ローカルネットワーク上の HTTP GET を実行する。localhost / 127.x.x.x / 10.x.x.x / 172.16-31.x.x / 192.168.x.x / homeassistant.local のみ許可。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "取得先 URL"},
                        "headers": {
                            "type": "object",
                            "description": "任意の HTTP ヘッダー",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    "required": ["url"],
                },
            },
            "handler": http_get,
        },
        "http_post": {
            "spec": {
                "name": "http_post",
                "description": "ローカルネットワーク上の HTTP POST を実行する。body は JSON 文字列として送り、Content-Type は未指定なら application/json を付与する。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "送信先 URL"},
                        "body": {"type": "string", "description": "送る JSON 文字列"},
                        "headers": {
                            "type": "object",
                            "description": "任意の HTTP ヘッダー",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    "required": ["url", "body"],
                },
            },
            "handler": http_post,
        },
    })


if __name__ == "__main__":
    main()
