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
import time

import camera_history
from embodied_action import action_fields_for_sensory, apply_action_to_body_state
from mcp_lib import image, serve, text
from media_capture import fetch_frame
from media_registry import resolve_media_item
from spatial_context import classify_sensory_origin
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

TOOL_WATCH_MEDIA = {
    "name": "watch_media",
    "description": (
        "テレビ・PC画面等のメディアを観る。カメラ(部屋を見る目)とは別で、侵入不要。\n"
        "video_media に登録された映像ソースを id / source / label で解決して現在フレームを返す。\n"
        "current_entity を問わず使える。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "video_media の id / source / label。省略時は video_media が1件ならそれを使う。",
            },
        },
        "required": [],
    },
}

TOOL_REVIEW_CAMERA_HISTORY = {
    "name": "review_camera_history",
    "description": (
        "現在侵入中のカメラに保存された直近の履歴画像を振り返る。ライブ映像ではなく、"
        "過去に定期取得されたJPEG画像を最大3枚返す。カメラ履歴が有効で、電脳体として"
        "そのカメラに侵入中のときだけ使用可能。カメラの指定やファイルパスの指定はできない。\n"
        "start_seconds_ago は範囲の古い側、end_seconds_ago は新しい側を表し、"
        "start_seconds_ago >= end_seconds_ago とする。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "start_seconds_ago": {
                "type": "integer",
                "minimum": 0,
                "description": "何秒前から見るか（古い側）。デフォルトは現在付近。",
            },
            "end_seconds_ago": {
                "type": "integer",
                "minimum": 0,
                "description": "何秒前まで見るか（新しい側）。省略時はstart_seconds_agoと同じ。",
            },
            "max_frames": {
                "type": "integer",
                "minimum": 1,
                "maximum": camera_history.MAX_RETURN_FRAMES,
                "description": "返す画像の最大枚数。デフォルト1、最大3。",
            },
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


def _env_enabled(name: str) -> bool:
    return _clean(os.environ.get(name)).lower() in {"1", "true", "yes", "on"}


def _history_tool_enabled() -> bool:
    enabled, _ = camera_history.history_settings(_load_prefs())
    return _env_enabled("EHA_CAMERA_HISTORY_ENABLED") and enabled


def _available_tools() -> list[dict]:
    tools = [TOOL_USE_DEVICE_CAMERA, TOOL_WATCH_MEDIA]
    if _history_tool_enabled():
        tools.append(TOOL_REVIEW_CAMERA_HISTORY)
    return tools


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
         "-H", "@-",
         "-H", "Content-Type: application/json",
         "-d", json.dumps({"entity_id": entity_id}), url],
        input=f"Authorization: Bearer {token}\n".encode(),
        capture_output=True,
    )
    return r.returncode == 0


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


def _handle_capture(camera: dict, current_entity: str, ha_url: str, go2rtc_url: str):
    source = _camera_source_for_capture(camera, current_entity)
    if not source:
        return [text("カメラソースが見つかりません")], True
    frame = fetch_frame(source, ha_url=ha_url, go2rtc_url=go2rtc_url, token=get_ha_token())
    if frame:
        b64 = base64.b64encode(frame).decode()
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
        return [
            text(json.dumps({"camera_context": context}, ensure_ascii=False)),
            image(b64),
        ]
    url = (
        f"{ha_url.rstrip('/')}/camera_proxy/{source}"
        if "." in source
        else go2rtc_url.rstrip("/") + f"/api/frame.jpeg?src={source}"
    )
    return [text(f"取得失敗: {source}（タイムアウトまたは未起動）\nURL: {url}")], True


def _handle_watch_media(source: str | None, ha_url: str, go2rtc_url: str):
    prefs = _load_prefs()
    item, resolved_source, _ = resolve_media_item(prefs, source, buckets=("video_media",), allow_single=True)
    if not item:
        if source:
            err = f"その映像ソースは未登録です（video_media に追加してください）: {clean(source)}"
        else:
            err = "watch_media に使える video_media が見つかりません"
        return [text(err)], True

    frame = fetch_frame(resolved_source, ha_url=ha_url, go2rtc_url=go2rtc_url, token=get_ha_token())
    if not frame:
        url = (
            f"{ha_url.rstrip('/')}/camera_proxy/{resolved_source}"
            if "." in resolved_source
            else go2rtc_url.rstrip("/") + f"/api/frame.jpeg?src={resolved_source}"
        )
        return [text(f"取得失敗: {resolved_source}（タイムアウトまたは未起動）\nURL: {url}")], True

    context = {
        "id": clean(item.get("id")),
        "source": resolved_source,
        "label": clean(item.get("label")),
        "room": clean(item.get("room")),
        "timestamp": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    return [
        text(json.dumps({"media_context": context}, ensure_ascii=False)),
        image(base64.b64encode(frame).decode()),
        text('観た内容を残すなら record_episode(kind="media_watch", ...) を使ってよい。'),
    ]


def _history_error(message: str):
    return [text(message)], True


def _bounded_history_arguments(arguments: dict, retention_minutes: int) -> tuple[int, int, int]:
    try:
        start = int(arguments.get("start_seconds_ago", 0))
        end_raw = arguments.get("end_seconds_ago")
        end = start if end_raw is None else int(end_raw)
        max_frames = int(arguments.get("max_frames", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("履歴の時間と枚数は整数で指定してください") from exc
    if start < 0 or end < 0:
        raise ValueError("履歴の時間は0秒以上で指定してください")
    if start < end:
        raise ValueError("start_seconds_ago は end_seconds_ago 以上にしてください")
    if start > retention_minutes * 60:
        raise ValueError(f"保存期間（{retention_minutes}分）より前の履歴は取得できません")
    if not 1 <= max_frames <= camera_history.MAX_RETURN_FRAMES:
        raise ValueError(f"max_frames は1〜{camera_history.MAX_RETURN_FRAMES}で指定してください")
    return start, end, max_frames


def _handle_review_camera_history(arguments: dict):
    if not _history_tool_enabled():
        return _history_error("カメラ履歴は無効です。高度な設定で有効にしてください。")

    _, current_entity, camera = _load_current_camera()
    if not current_entity:
        return _history_error("物理体モードではカメラ履歴を使用できません。カメラデバイスに侵入してください。")
    if not camera:
        return _history_error(f"現在侵入中のデバイス（{current_entity}）はカメラデバイスではありません。")

    source = _camera_source_for_capture(camera, current_entity)
    if not source:
        return _history_error("現在侵入中のカメラソースを解決できません。")
    _, retention_minutes = camera_history.history_settings(_load_prefs())
    try:
        start, end, max_frames = _bounded_history_arguments(arguments, retention_minutes)
        requested_at = time.time()
        records = camera_history.select_frames(
            os.environ.get("EHA_CAMERA_HISTORY_DIR", camera_history.DEFAULT_HISTORY_ROOT),
            source,
            start_seconds_ago=start,
            end_seconds_ago=end,
            max_frames=max_frames,
            retention_minutes=retention_minutes,
            now=requested_at,
        )
    except (TypeError, ValueError) as exc:
        return _history_error(str(exc))

    frames: list[tuple[camera_history.FrameRecord, bytes]] = []
    for record in records:
        try:
            frames.append((record, camera_history.read_frame(record)))
        except OSError:
            continue
    if not frames:
        return _history_error("指定した時刻付近に利用できるカメラ履歴がありません。")

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

    frame_contexts = [
        {
            "captured_at": datetime.datetime.fromtimestamp(record.captured_at).astimezone().isoformat(timespec="milliseconds"),
            "seconds_ago": max(0, round(requested_at - record.captured_at, 3)),
        }
        for record, _ in frames
    ]
    history_context = {
        "source": source,
        "historical_not_live": True,
        "requested_at": datetime.datetime.fromtimestamp(requested_at).astimezone().isoformat(timespec="seconds"),
        "requested_range_seconds_ago": {"start": start, "end": end},
        "retention_minutes": retention_minutes,
        "frames": frame_contexts,
    }
    content = [text(json.dumps({"camera_history_context": history_context}, ensure_ascii=False))]
    for frame_context, (_, frame) in zip(frame_contexts, frames):
        content.extend([
            text(
                "これはライブ映像ではなく、過去のカメラ履歴です。"
                f" 撮影時刻: {frame_context['captured_at']}"
            ),
            image(base64.b64encode(frame).decode()),
        ])
    return content


def _handle_ptz(camera: dict, current_entity: str, ha_url: str, direction: str):
    if not _camera_supports_ptz(camera, current_entity):
        return [text(f"現在侵入中のカメラデバイス（{current_entity}）は PTZ 非対応です。")], True
    entity_id = (camera.get("ptz") or {}).get(direction)
    if not entity_id:
        return [text(f"このカメラはPTZ非対応です。 direction={direction}")], True
    ok = press_button(entity_id, ha_url)
    msg = f"カメラを{direction}に向けました" if ok else f"PTZ操作失敗 ({entity_id})"
    return [text(msg)], not ok


def _handle_use_device_camera(arguments: dict, ha_url: str, go2rtc_url: str):
    action = _clean(arguments.get("action")) or "capture"
    _, current_entity, camera = _load_current_camera()
    if not current_entity:
        return [text("物理体モードではカメラを使用できません。カメラデバイスに侵入してください。")], True
    if not camera:
        return [text(f"現在侵入中のデバイス（{current_entity}）はカメラデバイスではありません。")], True
    if action == "capture":
        return _handle_capture(camera, current_entity, ha_url, go2rtc_url)
    if action in {"ptz_left", "ptz_right", "ptz_up", "ptz_down"}:
        return _handle_ptz(camera, current_entity, ha_url, action.removeprefix("ptz_"))
    return [text(f"不明な action: {action}")], True


def _tool_registry(ha_url: str, go2rtc_url: str) -> dict:
    tools = {
        "use_device_camera": {
            "spec": TOOL_USE_DEVICE_CAMERA,
            "handler": lambda arguments: _handle_use_device_camera(arguments, ha_url, go2rtc_url),
        },
        "watch_media": {
            "spec": TOOL_WATCH_MEDIA,
            "handler": lambda arguments: _handle_watch_media(arguments.get("source"), ha_url, go2rtc_url),
        },
    }
    if _history_tool_enabled():
        tools["review_camera_history"] = {
            "spec": TOOL_REVIEW_CAMERA_HISTORY,
            "handler": _handle_review_camera_history,
        }
    return tools


def main():
    args = parse_args()
    serve("camera-mcp", "3.1", _tool_registry(args.ha_url, args.go2rtc_url))


if __name__ == "__main__":
    main()
