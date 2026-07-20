"""chat.py用の応答後処理関数群。

chat.shの後処理小ブロック（feature-flags記録/pending_proposal消化/
chat_log追記/MQTT publish）を、importできる関数として切り出したもの
（[[embodied-ha-pythonize-chat-loop-design-2026-07-09]] 増分5）。

各関数は「観測可能な挙動」をchat.shと一致させることを優先している:
- record_presented_features/consume_pending_proposal:
  chat.sh側はpython try/except + bash `2>/dev/null || true` の二重ガード。
  ここではpython側のtry/exceptだけで同じ「絶対にクラッシュしない」契約を再現する。
- append_chat_log: **意図的にガード無し**。chat.shの元コードにも
  try/exceptも`|| true`も無く、失敗時はスクリプト全体が`set -e`で
  中断される。ここでも例外はそのまま伝播させる
  （フォルトインジェクションテストで確認、呼び出し側=chat.pyの
  オーケストレーターは、この関数の例外をここだけ握りつぶさないこと）。
- publish_private_to_mqtt: chat.sh側はpythonコード自体にtry/exceptは
  無いが、外側のbash呼び出し全体が`2>/dev/null || true`で包まれており、
  観測可能な挙動としては「絶対にクラッシュしない」。chat.pyには
  それを包む外側のbash層が無いため、同じ観測可能な挙動を再現するには
  関数内部にtry/exceptを持たせる必要がある（元のpythonソースには
  無かったガードを移植時に追加した、唯一の意図的な差分）。
"""
import json
import os
import subprocess

from instance_identity import MQTT_PREFIX


def record_presented_features(parsed, script_dir, run=subprocess.run):
    """feature_presented を feature-flags.py add へ記録する（chat.sh:660-671と同一契約）。

    どんな例外が起きても呼び出し側へは伝播しない。
    """
    try:
        fp = parsed.get("feature_presented")
        ids = fp if isinstance(fp, list) else ([fp] if fp else [])
        ids = [str(x).strip() for x in ids if x and str(x).strip().lower() != "null"]
        if ids:
            run(["python3", os.path.join(script_dir, "feature-flags.py"), "add"] + ids, timeout=5)
    except Exception:
        pass


def consume_pending_proposal(parsed, pending_file, print_fn=print):
    """proposal_resolvedがtrueならpending_proposal.jsonを削除する（chat.sh:680-689と同一契約）。

    どんな例外が起きても呼び出し側へは伝播しない。
    """
    try:
        if parsed.get("proposal_resolved") and os.path.exists(pending_file):
            os.remove(pending_file)
            print_fn("[chat] 保留中の提案を消化しました")
    except Exception:
        pass


def append_chat_log(parsed, reply, user_msg, chat_source, timestamp, chat_log_file):
    """chat_log.jsonlへ会話ターンを追記する（chat.sh:857-878と同一契約）。

    voiceモードは呼び出し側で事前にガードすること（chat.shの
    `if [ "${CHAT_SOURCE:-chat}" != "voice" ]` に相当、この関数自体は
    無条件で追記する）。

    **意図的にガード無し**。chat.sh の元コードにも例外処理が無く、
    失敗時はスクリプト全体が中断される。ここでも例外はそのまま
    伝播させる（フォルトインジェクションテスト対象）。
    """
    reply = parsed.get("reply", "") or reply
    private = parsed.get("private", "") or ""
    rec = {"timestamp": timestamp, "source": chat_source, "user": user_msg, "claude": reply}
    if private:
        rec["private"] = private
    with open(chat_log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def publish_private_to_mqtt(parsed, mqtt_host, mqtt_port="1883", mqtt_user="", mqtt_pass="", run=subprocess.run):
    """private内省をMQTT(embodied_ha/observation/state)へpublishする（chat.sh:886-897と同一契約）。

    観測可能な挙動としてはchat.shの元コードと同じく「絶対にクラッシュ
    しない」。ただしchat.sh側の無防備はbash外側の`2>/dev/null || true`に
    依存しており、その外側の層が無いchat.py側では、ここで明示的に
    try/exceptを持つ必要がある（移植時に追加した唯一の意図的差分）。
    """
    try:
        p = parsed.get("private")
        if p and mqtt_host:
            run(
                ["mosquitto_pub", "-h", mqtt_host, "-p", str(mqtt_port),
                 "-u", mqtt_user, "-P", mqtt_pass,
                 "-r", "-t", f"{MQTT_PREFIX}/observation/state", "-m", p[:255]],
                capture_output=True, timeout=5,
            )
    except Exception:
        pass
