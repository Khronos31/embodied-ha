#!/usr/bin/env python3
"""chat.sh のPython移植（[[embodied-ha-pythonize-chat-loop-design-2026-07-09]]）。

daemon.pyから起動される、ユーザー発言への応答生成スクリプト。
環境変数 CHAT_MESSAGE にユーザーの発言、CHAT_SOURCE に発信源
（既定 "chat"、他に "voice"）が入る。

実行順序・エラー処理特性はchat.shと意図的に同一にしてある
（増分1〜7、chat_*.py / response_parse.py / eha_config.py を参照）。
"""
import datetime
import json
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import chat_context  # noqa: E402
import chat_invoke  # noqa: E402
import chat_postprocess  # noqa: E402
import chat_prefs_update  # noqa: E402
import eha_config  # noqa: E402
from media_capture import fetch_frame  # noqa: E402
from observe_context import build_projected_camera_blocks  # noqa: E402
from response_parse import chat_extract  # noqa: E402


def _web_ui_status(status, source, ingress_port, run=subprocess.run):
    """Web UIへステータスをPOSTする（chat.sh:32-33/_web_idleと同一、失敗は無視）。"""
    body = json.dumps({"status": status, "source": source}, ensure_ascii=False)
    try:
        run(
            ["curl", "-sf", "-X", "POST", f"http://localhost:{ingress_port}/api/status",
             "-H", "Content-Type: application/json", "-d", body],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def _run_subprocess_text(cmd, env=None, fallback="", timeout=None):
    """`2>/dev/null || echo fallback`相当のsubprocess呼び出しヘルパー。"""
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return fallback
        return result.stdout.rstrip("\n")
    except Exception:
        return fallback


def _build_long_memory(memory_file, script_dir, run=subprocess.run):
    """mem-context.py経由で長期記憶の要約を取得する（chat.sh:72-77と同一）。

    chat.sh側はガード無し（`set -e`下で失敗時は会話プロセス全体がクラッシュ
    する）。ここも意図的に例外を握りつぶさない（フォルトインジェクション
    テスト対象。Codexレビューで発見された、増分間で唯一この関数だけ
    誤って握りつぶしていた不一致の修正）。
    """
    if not (memory_file and os.path.isfile(memory_file) and os.path.getsize(memory_file) > 0):
        return "なし"
    result = run(
        ["python3", os.path.join(script_dir, "mem-context.py"), memory_file, "40"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.rstrip("\n")


def _read_character(character_file):
    """character.mdの内容を読む（chat.sh:12の`cat "$EHA_CHARACTER_FILE" 2>/dev/null`と同一）。

    Codexレビューで発見: eha_config.pyはEHA_CHARACTER_FILEのパスを解決する
    だけで内容を読んでおらず、chat.py側にchat.sh:12に相当する読み取りが
    欠落していた（全会話でキャラクター定義が空文字列になる回帰）。
    """
    try:
        with open(character_file, encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return ""


def _build_recent_chat_context(log_dir, resident, character_name, script_dir, chat_log_file):
    """recent_chat_context.py経由で今日の会話(直近10件より前)を取得する（chat.sh:137-141と同一）。"""
    if not (chat_log_file and os.path.isfile(chat_log_file) and os.path.getsize(chat_log_file) > 0):
        return ""
    env = {**os.environ, "LOG_DIR": log_dir, "RESIDENT": resident, "EHA_CHARACTER_NAME": character_name or ""}
    return _run_subprocess_text(
        ["python3", os.path.join(script_dir, "recent_chat_context.py")], env=env, fallback=""
    )


def _build_open_loops():
    """`loops list` CLIで開いたループ一覧を取得する（chat.sh:144と同一）。"""
    return _run_subprocess_text(["loops", "list"], fallback="なし")


def _build_sensors(script_dir):
    """render-sensors.py経由で在宅・センサー状況を取得する（chat.sh:155と同一）。"""
    return _run_subprocess_text(
        ["python3", os.path.join(script_dir, "render-sensors.py"), "--context", "chat"], fallback="取得失敗"
    )


def _build_body_location_context(script_dir):
    """body-context.py経由で身体位置の文脈を取得する（chat.sh:156と同一）。"""
    return _run_subprocess_text(
        ["python3", os.path.join(script_dir, "body-context.py")],
        fallback="# 身体位置\n取得失敗",
    )


def _build_features_presented(script_dir):
    """feature-flags.py get経由で提示済み機能idを取得する（chat.sh:183と同一）。"""
    return _run_subprocess_text(["python3", os.path.join(script_dir, "feature-flags.py"), "get"], fallback="")


def _build_projected_camera_blocks(cfg, prefs_file, projected_camera_source):
    """投射中カメラの画像content blockを構築する（loop.sh:499-509のobserveモードと同一パターン）。

    失敗しても呼び出し側へは伝播せず、stderrへログして空リストを返す
    （loop.sh側のtry/exceptと同一契約）。
    """
    try:
        with open(prefs_file, encoding="utf-8") as fh:
            prefs = json.load(fh)
    except Exception:
        prefs = {}
    try:
        return build_projected_camera_blocks(
            projected_camera_source or "",
            prefs,
            fetch_frame=fetch_frame,
            ha_url=cfg.get("HA_URL", ""),
            go2rtc_url=cfg.get("GO2RTC_BASE", "http://homeassistant.local:1984"),
            token=cfg.get("SUPERVISOR_TOKEN", ""),
        )
    except Exception as e:
        print(f"[chat] projected camera fetch failed: {e}", file=sys.stderr)
        return []


def run(environ=None):
    environ = dict(environ if environ is not None else os.environ)
    cfg = eha_config.load_config(script_dir=SCRIPT_DIR, environ=environ)

    log_dir = cfg.get("EHA_LOG_DIR") or os.path.join(SCRIPT_DIR, "log")
    log_file = os.path.join(log_dir, "observations.jsonl")
    explore_log = os.path.join(log_dir, "explore.jsonl")
    pending_file = os.path.join(log_dir, "pending_proposal.json")
    memory_file = os.path.join(log_dir, "memory.md")
    chat_log_file = os.path.join(log_dir, "chat_log.jsonl")
    tmp_dir = "/tmp/embodied-ha"

    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)

    timestamp = datetime.datetime.now().astimezone().isoformat()
    user_msg = cfg.get("CHAT_MESSAGE") or ""
    chat_source = cfg.get("CHAT_SOURCE") or "chat"
    ingress_port = cfg.get("INGRESS_PORT") or "8099"
    resident = cfg.get("RESIDENT", "ユーザー")

    if not user_msg:
        print("[chat] CHAT_MESSAGE が空。終了。")
        return

    _web_ui_status("thinking", chat_source, ingress_port)
    try:
        _run_chat_turn(cfg, chat_source, user_msg, resident, timestamp,
                        log_dir, log_file, explore_log, pending_file, memory_file, chat_log_file)
    finally:
        _web_ui_status("idle", None, ingress_port)


def _run_chat_turn(cfg, chat_source, user_msg, resident, timestamp,
                    log_dir, log_file, explore_log, pending_file, memory_file, chat_log_file):
    body_location_file = cfg.get("EHA_BODY_LOCATION_FILE") or "/config/embodied-ha/body_location.json"
    prefs_file = cfg.get("EHA_PREFS_FILE")
    data_dir = cfg.get("EHA_DATA_DIR", "/config/embodied-ha")
    character_file = cfg.get("EHA_CHARACTER_FILE") or os.path.join(SCRIPT_DIR, "character.md")
    character = _read_character(character_file)

    recent_activity = chat_context.build_recent_activity(log_file, explore_log)
    current_mood = chat_context.build_current_mood(log_file)
    long_memory = _build_long_memory(memory_file, SCRIPT_DIR)
    pending = chat_context.build_pending_proposal(pending_file)
    entity_table = chat_context.build_entity_table(prefs_file)
    chat_hist = chat_context.build_chat_history(chat_log_file, resident)
    recent_chat_context = _build_recent_chat_context(
        log_dir, resident, cfg.get("EHA_CHARACTER_NAME", ""), SCRIPT_DIR, chat_log_file
    ).strip()
    open_loops = _build_open_loops()
    # TURN_TAKING_STATE: chat.sh元コードと同様、ガード無し（意図的。フォルトインジェクションテスト対象）
    turn_taking_state = chat_context.build_turn_taking_state(log_dir, resident)
    sensors = _build_sensors(SCRIPT_DIR)
    body_location_context = _build_body_location_context(SCRIPT_DIR)

    # 投射カメラの画像注入。chat.sh旧実装(PROJECTED_CAMERA_B64)は取得のみで
    # 実際のcontent block注入コードが無く機能していなかった(2026-06-28導入時
    # から一度も配線されず、855cb28のshellcheck巻き添え削除でloop.sh側も
    # 一時失われた経緯あり)。loop.shのobserveモードがv1.26.4で正式復活させた
    # observe_context.build_projected_camera_blocksと同じ仕組みをchat.pyで
    # 正しく実装する(ゆの指摘・確認済み。chat.sh本体の修正は別スコープ)。
    projected_camera_source = chat_context.resolve_projected_camera_entity(body_location_file)
    projected_camera_blocks = _build_projected_camera_blocks(cfg, prefs_file, projected_camera_source)

    features_md_path = os.path.join(SCRIPT_DIR, "features.md")
    features_md = ""
    if os.path.isfile(features_md_path):
        with open(features_md_path, encoding="utf-8") as fh:
            features_md = fh.read()
    features_presented = _build_features_presented(SCRIPT_DIR)

    recent_auditory_input = chat_context.build_recent_auditory_input(
        chat_source, user_msg, prefs_file, body_location_file
    )
    queued_ctx = chat_context.resolve_queued_listen_context("chat")
    if queued_ctx.get("RECENT_AUDITORY_INPUT"):
        recent_auditory_input = queued_ctx["RECENT_AUDITORY_INPUT"]
    queued_listen_file = queued_ctx.get("EHA_QUEUED_LISTEN_FILE")

    active_desires_raw = cfg.get("ACTIVE_DESIRES", "")
    inner_voice = chat_invoke.build_inner_voice(active_desires_raw)
    body_narrative = chat_invoke.build_body_narrative(cfg.get("EHA_BODY_STATE", "") or "{}")
    user_room, user_room_speaker = chat_invoke.resolve_voice_user_room(chat_source, data_dir, prefs_file)

    prompt = chat_invoke.build_chat_prompt(
        character=character,
        resident=resident,
        projected_camera_source=projected_camera_source,
        recent_activity=recent_activity,
        current_mood=current_mood,
        inner_voice=inner_voice,
        body_narrative=body_narrative,
        body_location_context=body_location_context,
        turn_taking_state=turn_taking_state,
        sensors=sensors,
        long_memory=long_memory,
        open_loops=open_loops,
        recent_chat_context=recent_chat_context,
        chat_hist=chat_hist,
        entity_table=entity_table,
        pending=pending,
        features_md=features_md,
        features_presented=features_presented,
        extra_context=cfg.get("EXTRA_CONTEXT", ""),
        policies_raw=cfg.get("POLICIES", "").strip(),
        chat_source=chat_source,
        user_room=user_room,
        user_room_speaker=user_room_speaker,
        recent_auditory_input=recent_auditory_input,
        user_msg=user_msg,
    )
    # chat.shはprepare_queued_listen_session()の戻り値を`eval "$(export ...)"`で
    # シェル環境へ持ち込み、以降のClaude呼び出しの環境にも自然に継承されていた
    # (EHA_SESSION_BIN/EHA_SESSION_MODEL等、深聴きセッション限定のバックエンド
    # 切替に使われる)。Codexレビューで、chat.py側がこの伝播を欠いていることが
    # 発見された。cfgのコピーへ全キー（Noneを除く）をマージしてから
    # build_claude_envへ渡すことで、同じ継承挙動を再現する。
    env_with_queued_ctx = dict(cfg)
    for key, value in queued_ctx.items():
        if value is not None:
            env_with_queued_ctx[key] = str(value)
    claude_env = chat_invoke.build_claude_env(env_with_queued_ctx)
    cwd = (
        cfg.get("EHA_AGENT_CWD")
        or cfg.get("EHA_CLAUDE_CWD")
        or os.path.join(cfg.get("EHA_DATA_DIR", "/config/embodied-ha"), "workdir")
    )
    response_text = chat_invoke.invoke_chat_claude(
        chat_source=chat_source,
        prompt=prompt,
        prefix_blocks=projected_camera_blocks,
        script_dir=SCRIPT_DIR,
        claude_env=claude_env,
        cwd=cwd,
        claude_bin=cfg.get("CLAUDE_BIN", "claude"),
        is_queued_listen=bool(queued_listen_file),
        sound_file=queued_listen_file,
        prefs_file=prefs_file,
    )

    if queued_listen_file:
        try:
            os.remove(queued_listen_file)
        except OSError:
            pass

    parsed = chat_extract(response_text)

    reply = parsed.get("reply", "") or ""
    if not reply:
        reply = "（うまく返事を作れませんでした）"

    chat_postprocess.record_presented_features(parsed, SCRIPT_DIR)

    print(f"[chat] {resident}さん: {user_msg}")
    print(f"[chat] Claude: {reply}")

    chat_postprocess.consume_pending_proposal(parsed, pending_file)
    chat_prefs_update.update_preferences(parsed, prefs_file)

    if chat_source != "voice":
        append_chat_log_kwargs = dict(
            parsed=parsed, reply=reply, user_msg=user_msg, chat_source=chat_source,
            timestamp=timestamp, chat_log_file=chat_log_file,
        )
        # append_chat_log: chat.sh元コードと同様、意図的にガード無し
        chat_postprocess.append_chat_log(**append_chat_log_kwargs)

    chat_postprocess.publish_private_to_mqtt(
        parsed, cfg.get("MQTT_HOST", ""), cfg.get("MQTT_PORT", "1883"),
        cfg.get("MQTT_USER", ""), cfg.get("MQTT_PASS", ""),
    )


if __name__ == "__main__":
    run()
