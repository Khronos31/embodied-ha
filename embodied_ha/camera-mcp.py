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
import sys, json, base64, subprocess, os, argparse, datetime

from sensory_origin import classify_sensory_origin


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--ha-url",
                   default=os.environ.get("HA_URL"))
    p.add_argument("--go2rtc-url",
                   default=os.environ.get("GO2RTC_BASE", "http://homeassistant.local:1984"))
    return p.parse_args()


TOOL_GET = {
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

# pan_left/right の命名注意:
#   pan_left ボタン = 上から見て時計回り回転 → 部屋の右側が映る
#   pan_right ボタン = 上から見て反時計回り回転 → 部屋の左側が映る
# ツールの direction は「どちら側を映したいか」で指定する
_PTZ_BUTTON = {
    "left":  "button.rihinkunokamera_pan_right",
    "right": "button.rihinkunokamera_pan_left",
    "up":    "button.rihinkunokamera_tilt_up",
    "down":  "button.rihinkunokamera_tilt_down",
}

TOOL_PTZ = {
    "name": "camera_ptz",
    "description": (
        "リビングカメラをパン/チルト操作する。\n"
        "direction は「カメラが映す方向」を指定:\n"
        "  left  → 部屋の左側（カメラ視点）を向く\n"
        "  right → 部屋の右側（カメラ視点）を向く\n"
        "  up    → 上を向く\n"
        "  down  → 下を向く\n"
        "1回の呼び出しで少し動く。位置確認には camera_get を併用する。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["left", "right", "up", "down"],
                "description": "パン/チルト方向"
            }
        },
        "required": ["direction"]
    }
}


def get_ha_token():
    return os.environ.get("SUPERVISOR_TOKEN", "")


def _clean(value):
    return " ".join(str(value or "").split()).strip()


def _load_camera_prefs():
    path = os.environ.get("EHA_PREFS_FILE", "")
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    cameras = data.get("cameras") if isinstance(data, dict) else []
    return cameras if isinstance(cameras, list) else []


def camera_context(source):
    source = _clean(source)
    context = {
        "source": source,
        "room": "",
        "preset": "",
        "direction": "",
        "timestamp": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    matched = {}
    for item in _load_camera_prefs():
        if not isinstance(item, dict) or _clean(item.get("source")) != source:
            continue
        matched = item
        context["room"] = _clean(item.get("room") or item.get("label"))
        context["preset"] = _clean(item.get("preset"))
        context["direction"] = _clean(item.get("direction"))
        break

    sensory = classify_sensory_origin(
        source=source,
        label=matched.get("label") if isinstance(matched, dict) else "",
        room=matched.get("room") if isinstance(matched, dict) else "",
        note=matched.get("note") if isinstance(matched, dict) else "",
        modality="visual",
    )
    context.update(sensory)
    return context


def press_button(entity_id, ha_url):
    base = ha_url.rstrip("/")
    if base.endswith("/api"):
        base = base[:-4]
    url = f"{base}/api/services/button/press"
    token = get_ha_token()
    r = subprocess.run(
        ["curl", "-sf", "--max-time", "5", "-X", "POST",
         "-H", f"Authorization: Bearer {token}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps({"entity_id": entity_id}), url],
        capture_output=True
    )
    return r.returncode == 0


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
            send({"jsonrpc": "2.0", "id": id_, "result": {"tools": [TOOL_GET, TOOL_PTZ]}})

        elif method == "tools/call":
            tool_name = req["params"]["name"]
            call_args = req["params"].get("arguments", {})
            if tool_name == "camera_ptz":
                direction = (call_args.get("direction") or "").strip()
                entity_id = _PTZ_BUTTON.get(direction)
                if not entity_id:
                    send({"jsonrpc": "2.0", "id": id_, "result": {
                        "content": [{"type": "text", "text": f"不明な方向: {direction}"}],
                        "isError": True
                    }})
                    continue
                ok = press_button(entity_id, args.ha_url)
                msg = f"カメラを{direction}に向けました" if ok else f"PTZ操作失敗 ({entity_id})"
                send({"jsonrpc": "2.0", "id": id_, "result": {
                    "content": [{"type": "text", "text": msg}],
                    "isError": not ok
                }})
            elif tool_name == "camera_get":
                source = (call_args.get("source") or "").strip()
                if not source:
                    send({"jsonrpc": "2.0", "id": id_, "result": {
                        "content": [{"type": "text", "text": "source が空です"}],
                        "isError": True
                    }})
                    continue
                b64, url = fetch_image(source, args.ha_url, args.go2rtc_url)
                if b64:
                    context = camera_context(source)
                    send({"jsonrpc": "2.0", "id": id_, "result": {
                        "content": [
                            {"type": "text", "text": json.dumps({"camera_context": context}, ensure_ascii=False)},
                            {"type": "image", "data": b64, "mimeType": "image/jpeg"},
                        ]
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
