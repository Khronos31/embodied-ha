#!/usr/bin/env python3
"""speak.py <room> <message> — preferences.json の speakers に従ってTTS/通知を送る。
環境変数: EHA_PREFS_FILE, HA_URL, SUPERVISOR_TOKEN
"""
import sys, json, os, subprocess


def get_ha_token():
    return os.environ.get("SUPERVISOR_TOKEN", "")


def curl_post(url, payload, ha_token):
    r = subprocess.run(
        ["curl", "-sf", "--max-time", "5", "-X", "POST",
         "-H", f"Authorization: Bearer {ha_token}",
         "-H", "Content-Type: application/json",
         "-d", payload, url],
        capture_output=True
    )
    return r.returncode == 0


def speak(room, message):
    prefs_file = os.environ.get("EHA_PREFS_FILE", "")
    ha_url = os.environ["HA_URL"]
    ha_token = get_ha_token()

    prefs = {}
    try:
        prefs = json.load(open(prefs_file, encoding="utf-8"))
    except Exception:
        pass

    config = prefs.get("speakers", {}).get(room, {})

    if not config:
        print(f"[speak] '{room}' は preferences.json に未登録。TTS をスキップ。", file=sys.stderr)
        return False

    if config.get("type") == "tts":
        payload = json.dumps({
            "entity_id": config["tts_entity"],
            "message": message,
            "media_player_entity_id": config["media_player"]
        }, ensure_ascii=False)
        ok = curl_post(f"{ha_url}/services/tts/speak", payload, ha_token)
        print(f"[speak] TTS:{room} {'OK' if ok else 'NG'}")
        return ok

    elif config.get("type") == "notify":
        # 新形式 notify.send_message + entity_id で送る。
        # Alexa/mobile_app などは notify エンティティ（notify.xxx）になっており、
        # 旧形式 services/notify/<name> は 400 になる（2026-06-23 実機で確認）。
        entity = config.get("entity", "")
        # 旧設定（notify. を外したサービス名）でも動くよう notify. を補完する
        if entity and not entity.startswith("notify."):
            entity = "notify." + entity
        data = {"entity_id": entity, "message": message}
        if config.get("title"):
            data["title"] = config["title"]
        payload = json.dumps(data, ensure_ascii=False)
        ok = curl_post(f"{ha_url}/services/notify/send_message", payload, ha_token)
        print(f"[speak] notify:{room} {'OK' if ok else 'NG'}")
        return ok

    else:
        print(f"[speak] 不明な speaker type: {config.get('type')}", file=sys.stderr)
        return False


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("使い方: speak.py <room> <message>", file=sys.stderr)
        sys.exit(1)
    ok = speak(sys.argv[1], sys.argv[2])
    sys.exit(0 if ok else 1)
