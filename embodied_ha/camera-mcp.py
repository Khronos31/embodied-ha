#!/usr/bin/env python3
"""カメラデバイス MCP サーバー（embodied-ha 用）。

use_device_camera から、現在侵入中のカメラデバイスだけを操作する。
"""
from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import subprocess
import sys

from embodied_action import action_fields_for_sensory, apply_action_to_body_state
from sensory_origin import classify_sensory_origin
from state_utils import clean, get_device_capabilities, load_prefs

TOOL_USE_DEVICE_CAMERA = {
    "name": "use_device_camera",
    "description": (
        "現在侵入中のカメラデバイスを操作する。電脳体でカメラデバイスに侵入中のみ使用可能。\n"
        "物理体モード、またはカメラ以外のデバイスに侵入中の場合はエラーを返す。\n"
        "action=capture: 現在のカメラ画像を取得する\n"
        "action=ptz_left/right/up/down: カメラをパン・チルト操作する"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["capture", "ptz_left", "ptz_right", "ptz_up", "ptz_down"],
                "description": "実行するアクション。デフォルトは capture",
            }
        },
        "required": [],
    },
}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--ha-url", default=os.environ.get("HA_URL"))
    p.add_argument("--go2rtc-url", default=os.environ.get("GO2RTC_BASE", "http://homeassistant.local:1984"))
    return p.parse_args()


def get_ha_token():
    return os.environ.get("SUPERVISOR_TOKEN", "")


def _clean(value):
    return " ".join(str(value or "").split()).strip()


def _prefs_path() -> str:
    return clean(os.environ.get("EHA_PREFS_FILE"))


def _load_prefs() -> dict:
    return load_prefs(_prefs_path())


def _load_body_location() -> dict:
    path = clean(os.environ.get("EHA_BODY_LOCATION_FILE")) or "/config/embodied-ha/body_location.json"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_camera_devices() -> list[dict]:
    prefs = _load_prefs()
    devices = prefs.get("cameras")
    return devices if isinstance(devices, list) else []


def _load_legacy_cameras() -> list[dict]:
    prefs = _load_prefs()
    cameras = prefs.get("cameras")
    return cameras if isinstance(cameras, list) else []


def _match_camera_device(source: str) -> dict:
    source = _clean(source)
    if not source:
        return {}
    for item in _load_legacy_cameras():
        if isinstance(item, dict) and _clean(item.get("source")) == source:
            return item
    for item in _load_camera_devices():
        if not isinstance(item, dict):
            continue
        if _clean(item.get("entity")) == source or _clean(item.get("ha_entity")) == source:
            return item
    return {}


def camera_context(source):
    source = _clean(source)
    context = {
        "source": source,
        "room": "",
        "preset": "",
        "direction": "",
        "timestamp": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    matched = _match_camera_device(source)
    if matched:
        context["room"] = _clean(matched.get("room") or matched.get("label"))
        context["preset"] = _clean(matched.get("preset"))
        context["direction"] = _clean(matched.get("direction"))

    sensory = classify_sensory_origin(
        source=source,
        label=matched.get("label") if isinstance(matched, dict) else "",
        room=matched.get("room") if isinstance(matched, dict) else "",
        area=matched.get("area") if isinstance(matched, dict) else "",
        entity_id=matched.get("entity") or matched.get("ha_entity") if isinstance(matched, dict) else "",
        note=matched.get("note") if isinstance(matched, dict) else "",
        modality="visual",
    )
    context.update(sensory)
    context.update(action_fields_for_sensory(sensory, host=source))
    return context


# pan_left/right の命名注意:
#   pan_left ボタン = 上から見て時計回り回転 → 部屋の右側が映る
#   pan_right ボタン = 上から見て反時計回り回転 → 部屋の左側が映る
# ツールの direction は「どちら側を映したいか」で指定する


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
        capture_output=True,
    )
    return r.returncode == 0


def fetch_image(source, ha_url, go2rtc_url):
    if "." in source:
        base = ha_url.rstrip("/")
        if base.endswith("/api"):
            base = base[:-4]
        url = f"{base}/api/camera_proxy/{source}"
        token = get_ha_token()
        r = subprocess.run(
            ["curl", "-sf", "--max-time", "8", "-H", f"Authorization: Bearer {token}", url],
            capture_output=True,
        )
    else:
        url = go2rtc_url.rstrip("/") + f"/api/frame.jpeg?src={source}"
        r = subprocess.run(["curl", "-sf", "--max-time", "8", url], capture_output=True)

    if r.returncode != 0 or len(r.stdout) < 100:
        return None, url
    return base64.b64encode(r.stdout).decode(), url


def send(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _load_current_camera():
    loc = _load_body_location()
    current_entity = clean(loc.get("current_entity"))
    if not current_entity:
        return loc, current_entity, None
    prefs = _load_prefs()
    caps = get_device_capabilities(current_entity, prefs)
    return loc, current_entity, caps.get("camera")


def _camera_source_for_capture(camera: dict, current_entity: str) -> str:
    return _clean(camera.get("ha_entity")) or _clean(camera.get("source")) or _clean(camera.get("entity")) or current_entity


def _camera_supports_ptz(camera: dict, current_entity: str) -> bool:
    return bool(camera.get("ptz"))


def _handle_capture(camera: dict, current_entity: str, ha_url: str, go2rtc_url: str, req_id):
    source = _camera_source_for_capture(camera, current_entity)
    if not source:
        send({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": "カメラソースが見つかりません"}], "isError": True}})
        return
    b64, url = fetch_image(source, ha_url, go2rtc_url)
    if b64:
        context = camera_context(source)
        try:
            apply_action_to_body_state(
                action_mode=context.get("action_mode"),
                action_cost=context.get("action_cost"),
                target_room=context.get("source_room"),
                target_host=context.get("target_host"),
                move_cost=context.get("move_cost"),
            )
        except Exception:
            pass
        send({"jsonrpc": "2.0", "id": req_id, "result": {
            "content": [
                {"type": "text", "text": json.dumps({"camera_context": context}, ensure_ascii=False)},
                {"type": "image", "data": b64, "mimeType": "image/jpeg"},
            ]
        }})
    else:
        send({"jsonrpc": "2.0", "id": req_id, "result": {
            "content": [{"type": "text", "text": f"取得失敗: {source}（タイムアウトまたは未起動）\nURL: {url}"}],
            "isError": True
        }})


def _handle_ptz(camera: dict, current_entity: str, ha_url: str, direction: str, req_id):
    if not _camera_supports_ptz(camera, current_entity):
        send({"jsonrpc": "2.0", "id": req_id, "result": {
            "content": [{"type": "text", "text": f"現在侵入中のカメラデバイス（{current_entity}）は PTZ 非対応です。"}],
            "isError": True
        }})
        return
    entity_id = (camera.get("ptz") or {}).get(direction)
    if not entity_id:
        send({"jsonrpc": "2.0", "id": req_id, "result": {
            "content": [{"type": "text", "text": f"このカメラはPTZ非対応です。 direction={direction}"}],
            "isError": True
        }})
        return
    ok = press_button(entity_id, ha_url)
    msg = f"カメラを{direction}に向けました" if ok else f"PTZ操作失敗 ({entity_id})"
    send({"jsonrpc": "2.0", "id": req_id, "result": {
        "content": [{"type": "text", "text": msg}],
        "isError": not ok
    }})


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
                "serverInfo": {"name": "camera-mcp", "version": "3.0"}
            }})

        elif method == "notifications/initialized":
            pass

        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": id_, "result": {"tools": [TOOL_USE_DEVICE_CAMERA]}})

        elif method == "tools/call":
            tool_name = req["params"]["name"]
            call_args = req["params"].get("arguments", {})
            if tool_name != "use_device_camera":
                send({"jsonrpc": "2.0", "id": id_, "result": {
                    "content": [{"type": "text", "text": f"未知のツール: {tool_name}"}],
                    "isError": True
                }})
                continue

            action = _clean(call_args.get("action")) or "capture"
            loc, current_entity, camera = _load_current_camera()
            if not current_entity:
                send({"jsonrpc": "2.0", "id": id_, "result": {
                    "content": [{"type": "text", "text": "物理体モードではカメラを使用できません。カメラデバイスに侵入してください。"}],
                    "isError": True
                }})
                continue
            if not camera:
                send({"jsonrpc": "2.0", "id": id_, "result": {
                    "content": [{"type": "text", "text": f"現在侵入中のデバイス（{current_entity}）はカメラデバイスではありません。"}],
                    "isError": True
                }})
                continue

            if action == "capture":
                _handle_capture(camera, current_entity, args.ha_url, args.go2rtc_url, id_)
            elif action in {"ptz_left", "ptz_right", "ptz_up", "ptz_down"}:
                _handle_ptz(camera, current_entity, args.ha_url, action.removeprefix("ptz_"), id_)
            else:
                send({"jsonrpc": "2.0", "id": id_, "result": {
                    "content": [{"type": "text", "text": f"不明な action: {action}"}],
                    "isError": True
                }})


if __name__ == "__main__":
    main()
