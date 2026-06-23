#!/usr/bin/env python3
"""カメラスナップショット MCP サーバー（embodied-ha 用）。

go2rtc ストリームと HA カメラプロキシの両方に対応。
source の形式で自動判別:
  camera.xxx 形式  → HA カメラプロキシ（/api/camera_proxy/<entity_id>）
  それ以外         → go2rtc ストリーム（/api/frame.jpeg?src=<name>）

起動引数（省略時は環境変数のデフォルト値を使用）:
  --ha-url      HA API ベース URL   (env: HA_URL)
  --go2rtc-url  go2rtc ベース URL   (env: GO2RTC_BASE)
"""
import sys, json, base64, subprocess, os, argparse


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--ha-url",
                   default=os.environ.get("HA_URL"))
    p.add_argument("--go2rtc-url",
                   default=os.environ.get("GO2RTC_BASE", "http://homeassistant.local:1984"))
    return p.parse_args()


TOOL = {
    "name": "camera_get",
    "description": (
        "カメラのスナップショットを取得して画像で返す。\n"
        "source に HA カメラの entity_id（camera.xxx 形式）または go2rtc ストリーム名を指定。\n"
        "  camera.xxx 形式  → HA カメラプロキシ経由\n"
        "  それ以外         → go2rtc ストリーム経由（例: capture_tv, capture_pc）\n"
        "利用可能なカメラは preferences.json の cameras セクション、または長期記憶から確認できる。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "HA entity_id（camera.xxx）または go2rtc ストリーム名"
            }
        },
        "required": ["source"]
    }
}


def get_ha_token():
    return os.environ.get("SUPERVISOR_TOKEN", "")


def fetch_image(source, ha_url, go2rtc_url):
    if "." in source:
        # HA カメラプロキシ（camera.entity_id 形式）
        base = ha_url.rstrip("/")
        if base.endswith("/api"):
            base = base[:-4]
        url = f"{base}/api/camera_proxy/{source}"
        token = get_ha_token()
        r = subprocess.run(
            ["curl", "-sf", "--max-time", "8", "-H", f"Authorization: Bearer {token}", url],
            capture_output=True
        )
    else:
        # go2rtc ストリーム
        url = go2rtc_url.rstrip("/") + f"/api/frame.jpeg?src={source}"
        r = subprocess.run(["curl", "-sf", "--max-time", "8", url], capture_output=True)

    if r.returncode != 0 or len(r.stdout) < 100:
        return None, url
    return base64.b64encode(r.stdout).decode(), url


def send(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def main():
    args = parse_args()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue

        method = req.get("method", "")
        id_ = req.get("id")

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": id_, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "camera-mcp", "version": "2.0"}
            }})

        elif method == "notifications/initialized":
            pass

        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": id_, "result": {"tools": [TOOL]}})

        elif method == "tools/call":
            tool_name = req["params"]["name"]
            call_args = req["params"].get("arguments", {})
            if tool_name == "camera_get":
                source = (call_args.get("source") or "").strip()
                if not source:
                    send({"jsonrpc": "2.0", "id": id_, "result": {
                        "content": [{"type": "text", "text": "source が空です"}],
                        "isError": True
                    }})
                    continue
                b64, url = fetch_image(source, args.ha_url, args.go2rtc_url)
                if b64:
                    send({"jsonrpc": "2.0", "id": id_, "result": {
                        "content": [{"type": "image", "data": b64, "mimeType": "image/jpeg"}]
                    }})
                else:
                    send({"jsonrpc": "2.0", "id": id_, "result": {
                        "content": [{"type": "text", "text": f"取得失敗: {source}（タイムアウトまたは未起動）\nURL: {url}"}],
                        "isError": True
                    }})
            else:
                send({"jsonrpc": "2.0", "id": id_, "result": {
                    "content": [{"type": "text", "text": f"未知のツール: {tool_name}"}],
                    "isError": True
                }})


if __name__ == "__main__":
    main()
