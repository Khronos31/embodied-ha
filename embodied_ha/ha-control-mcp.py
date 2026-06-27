#!/usr/bin/env python3
"""HA 家電操作 MCP サーバー（embodied-ha 用・書き込み専用）。

ha_get（読み取り）とは別サーバーに分離することで、「このサーバーを繋ぐ＝操作能力を持つ」
という物理的なゲートを作る。自律操作 OFF のループ（watch/explore）にはこのサーバーを
繋がないことで、ツール自体を渡さない（プロンプト頼みにしない多層防御）。

全ての操作は log/actions.jsonl に記録する（事後報告・監査証跡）。

env: HA_URL, SUPERVISOR_TOKEN, EHA_LOG_DIR, EHA_ACTOR(任意: watch/explore/chat 等)
"""
import os
import json
import subprocess
import datetime

from embodied_action import action_fields_for_control, apply_action_to_body_state
from mcp_lib import serve, text, log

HA_URL = os.environ["HA_URL"].rstrip("/")
LOG_DIR = os.environ.get("EHA_LOG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "log"))
ACTIONS_LOG = os.path.join(LOG_DIR, "actions.jsonl")
ACTOR = os.environ.get("EHA_ACTOR", "agent")

# 家電操作で許可するドメイン。script は service にスクリプト名を直書きできる
# （視聴予約 viewing_reservation_set 等のための設計）。家人本人が起点で第三者
# 注入経路が無いため by-design で許可（2026-06-22レビュー）。
ALLOWED_DOMAINS = {"light", "climate", "switch", "media_player", "cover", "fan", "script"}


def _token():
    return os.environ.get("SUPERVISOR_TOKEN", "")


def _record(domain, service, entity_id, data, ok, extra=None):
    """全操作を actions.jsonl に追記（事後報告の監査証跡）。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        rec = {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "actor": ACTOR,
            "domain": domain, "service": service,
            "entity_id": entity_id, "data": data, "ok": ok,
        }
        if isinstance(extra, dict):
            rec.update(extra)
        with open(ACTIONS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def ha_call_service(args):
    domain = (args.get("domain") or "").strip()
    service = (args.get("service") or "").strip()
    entity_id = (args.get("entity_id") or "").strip()
    data = args.get("data") or {}

    if domain not in ALLOWED_DOMAINS:
        return [text(f"拒否: ドメイン '{domain}' は許可されていません"
                     f"（許可: {', '.join(sorted(ALLOWED_DOMAINS))}）")], True
    # script ドメインで service がスクリプト名直呼び（turn_on/off/toggle以外）は entity_id 不要
    entity_optional = (domain == "script" and service not in ("turn_on", "turn_off", "toggle"))
    if not service or (not entity_id and not entity_optional):
        return [text(f"不完全: service と entity_id が必要です（domain={domain} "
                     f"service={service} entity_id={entity_id}）")], True

    action_fields = action_fields_for_control(entity_id, domain, service)
    payload = dict(data) if entity_optional else {"entity_id": entity_id, **data}
    r = subprocess.run(
        ["curl", "-sf", "--max-time", "10", "-X", "POST",
         "-H", f"Authorization: Bearer {_token()}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload, ensure_ascii=False),
         f"{HA_URL}/services/{domain}/{service}"],
        capture_output=True, text=True
    )
    ok = r.returncode == 0
    _record(domain, service, entity_id, data, ok, action_fields)
    if not ok:
        return [text(f"操作失敗: {domain}.{service} {entity_id}（returncode={r.returncode}）")], True
    try:
        apply_action_to_body_state(
            action_mode=action_fields.get("action_mode"),
            action_cost=action_fields.get("action_cost"),
            target_room=action_fields.get("target_room"),
            target_host=action_fields.get("target_host"),
            move_cost=action_fields.get("move_cost"),
        )
    except Exception:
        pass
    log(f"[ha-control] {ACTOR}: {domain}.{service} {entity_id} {data} OK")
    return [text(f"実行しました: {domain}.{service} {entity_id} {data}")]


serve("ha-control-mcp", "1.0", {
    "ha_call_service": {
        "spec": {
            "name": "ha_call_service",
            "description": (
                "家電などを操作する（HAサービス呼び出し）。\n"
                f"許可ドメイン: {', '.join(sorted(ALLOWED_DOMAINS))}。\n"
                "例: エアコンON → domain=climate, service=turn_on, entity_id=climate.living\n"
                "例: 温度設定 → domain=climate, service=set_temperature, "
                "entity_id=climate.living, data={\"temperature\": 26}\n"
                "例: 視聴予約 → domain=script, service=viewing_reservation_set, "
                "data={\"reservation_time\": \"...\", \"reservation_channel\": \"フジテレビ\"}\n"
                "entity_id は ha_get で実在を確認してから指定すること。\n"
                "操作したら必ずユーザーに事後報告すること（speak / reply）。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "light/climate/switch/media_player/cover/fan/script"},
                    "service": {"type": "string", "description": "turn_on / set_temperature など"},
                    "entity_id": {"type": "string", "description": "対象エンティティ（script直呼び時は省略可）"},
                    "data": {"type": "object", "description": "追加パラメータ（温度・モード等）。任意"},
                },
                "required": ["domain", "service"],
            },
        },
        "handler": ha_call_service,
    },
})
