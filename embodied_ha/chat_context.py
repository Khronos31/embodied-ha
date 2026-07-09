"""chat.py用の文脈構築関数群。

chat.shに埋め込まれていたコンテキスト構築ブロック（RECENT_ACTIVITY/
CURRENT_MOOD/PENDING_PROPOSAL/ENTITY_TABLE/CHAT_HISTORY/TURN_TAKING_STATE/
投射カメラ検出）を、importできる関数として切り出したもの
（[[embodied-ha-pythonize-chat-loop-design-2026-07-09]] 増分2）。

各関数のエラー処理特性は chat.sh の元コードと意図的に同一にしてある
（例: build_turn_taking_state はガード無しで例外を伝播させる——これは
見落としではなく元コードの挙動をそのまま保持したもの）。
"""
import json
import os
import datetime

import sociality_state as ss


def build_recent_activity(log_file, explore_log):
    """observations.jsonl + explore.jsonlの末尾をタイムスタンプ順にマージする。"""
    entries = []

    def load(path, label, getter):
        if not path or not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        for line in content.splitlines()[-8:]:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                entries.append((d.get("timestamp", ""), label, d.get("emotion", ""), getter(d)))
            except Exception:
                pass

    load(log_file, "観察", lambda d: d.get("private", ""))
    load(explore_log, "探索", lambda d: d.get("topic", ""))
    entries.sort(key=lambda e: e[0])
    out = [f"{ts[:16]} [{label}/{emo}] {text}" for ts, label, emo, text in entries[-8:] if text]
    return "\n".join(out) if out else "なし"


def build_current_mood(log_file):
    """observations.jsonlの最後に記録されたemotionフィールドを取り出す。"""
    mood = ""
    if log_file and os.path.exists(log_file):
        with open(log_file, encoding="utf-8") as fh:
            content = fh.read()
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                mood = json.loads(line).get("emotion", "") or mood
            except Exception:
                pass
    return mood or "おだやか"


def build_pending_proposal(pending_file):
    """pending_proposal.jsonを読み、2時間以内なら提案文をJSON化して返す。"""
    if not (pending_file and os.path.exists(pending_file) and os.path.getsize(pending_file) > 0):
        return "なし"
    try:
        with open(pending_file, encoding="utf-8") as fh:
            d = json.load(fh)
        ts = datetime.datetime.fromisoformat(d["timestamp"])
        now = datetime.datetime.now(datetime.timezone.utc).astimezone()
        if (now - ts).total_seconds() <= 7200:
            a = d["action"]
            return json.dumps({"提案文": d["proposal"], "action": a}, ensure_ascii=False)
        return "なし"
    except Exception:
        return "なし"


def build_entity_table(prefs_file):
    """preferences.json の entities を Markdown テーブルとして描画する。空なら空文字列。"""
    try:
        with open(prefs_file, encoding="utf-8") as fh:
            prefs = json.load(fh)
    except Exception:
        prefs = {}
    rows = [r for r in prefs.get("entities", []) if r.get("entity_id")]
    if not rows:
        return ""
    out = ["| 名前 | entity_id | 備考 |", "|------|-----------|------|"]
    for r in rows:
        note = r.get("note", "") or ""
        out.append(f"| {r.get('name', '')} | {r['entity_id']} | {note} |")
    return "\n".join(out)


def build_chat_history(chat_log_file, resident):
    """chat_log.jsonlの末尾10行を対話形式に整形する。"""
    if not (chat_log_file and os.path.exists(chat_log_file) and os.path.getsize(chat_log_file) > 0):
        return "なし"
    with open(chat_log_file, encoding="utf-8") as fh:
        content = fh.read()
    tail_lines = content.splitlines()[-10:]
    lines = []
    for line in tail_lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            lines.append(f"{resident}さん: {d.get('user', '')}")
            lines.append(f"Claude: {d.get('claude', '')}")
        except Exception:
            pass
    return "\n".join(lines) if lines else "なし"


def build_turn_taking_state(log_dir, resident):
    """sociality_state.get_turn_taking_stateの結果をJSON文字列として返す。

    元のchat.shコードにエラーガードが無いのと同様、ここでも例外を
    そのまま伝播させる(意図的維持。フォルトインジェクションテスト対象)。
    """
    state = ss.get_turn_taking_state(log_dir, resident)
    return json.dumps(state, ensure_ascii=False, indent=2)


def resolve_projected_camera_entity(body_location_file=None):
    """body_location.jsonのcurrent_entityがcamera.*なら、そのentity_idを返す。

    body_location_fileが指定されない場合の既定値は本番の絶対パス
    (/config/embodied-ha/body_location.json)——chat.shの既存フォールバック
    と意図的に同一。テスト時は必ず明示的なパスを渡すこと。
    """
    f = body_location_file or "/config/embodied-ha/body_location.json"
    try:
        with open(f, encoding="utf-8") as fh:
            d = json.load(fh)
        h = (d.get("current_entity") or "").strip()
        if h.startswith("camera."):
            return h
    except Exception:
        pass
    return ""
