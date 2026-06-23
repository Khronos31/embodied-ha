#!/usr/bin/env python3
"""Embodied HA デーモン
観察ループ(watch.sh)と会話(chat.sh)をHAエンティティ経由でトリガーする常駐プロセス。
起動はアドオン(addon/run.sh)から exec で呼ばれる。直接起動する場合:

トリガー方法（MQTT。config.yaml の services: mqtt:need で MQTT は必須）:
  - embodied_ha/chat/set        … 会話(chat.sh)を起動。ペイロードがユーザーの発言
  - embodied_ha/observe/trigger … 観察ループ(watch.sh)を手動起動
"""
import os
import subprocess
import threading
import time
import json
import random
import fcntl

import body_state

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG_DIR = os.environ.get("EHA_LOG_DIR", os.path.join(_SCRIPT_DIR, "log"))
WATCH_SH = os.path.join(_SCRIPT_DIR, "watch.sh")
CHAT_SH = os.path.join(_SCRIPT_DIR, "chat.sh")
EXPLORE_SH = os.path.join(_SCRIPT_DIR, "explore.sh")
HA_URL = os.environ["HA_URL"]
SCHEDULE_INTERVAL = 1200  # 観察ループ(watch.sh)の定期実行間隔（秒）= 20分
EXPLORE_INTERVAL = 1800   # 自律探索(explore.sh)の定期実行間隔（秒）= 30分
SENSOR_COOLDOWN = 300     # センサートリガーのクールダウン（秒）= 5分
DESIRES_FILE = os.environ.get("EHA_DESIRES_FILE", os.path.join(_SCRIPT_DIR, "desires.json"))
DESIRE_STATE_FILE = os.path.join(_LOG_DIR, "desire_state.json")
DESIRE_THRESHOLD = 0.6
SCHEDULE_FILE = os.path.join(_SCRIPT_DIR, "schedule.json")
LOCK_FILE = os.path.join(_LOG_DIR, "daemon.lock")
# --- MQTT I/O ---
MQTT_HOST = os.environ.get("MQTT_HOST", "")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
# 各スクリプトの最大実行時間（秒）。Claude呼び出しがレート競合でハングしても
# ロックを永久に握りっぱなしにしないための上限。watch/exploreはClaude複数回＋
# ロールアップ/daybookで長くなりうるので余裕を持たせる。
WATCH_TIMEOUT = 600
CHAT_TIMEOUT = 300
EXPLORE_TIMEOUT = 600

ENV_PATH = os.environ.get("EHA_TOOLS_PATH", "/config/.tools/bin:/config/.tools/npm-global/bin:/config/.tools/node/bin") + ":" + os.environ.get("PATH", "/usr/bin:/bin")
_watch_lock = threading.Lock()
_chat_lock = threading.Lock()
_explore_lock = threading.Lock()
_desires_lock = threading.Lock()
_body_lock = threading.Lock()
_last_sensor_watch = 0.0  # センサートリガーの最終実行時刻
_BODY_STATE_FILE = os.path.join(os.environ.get("EHA_DATA_DIR", _SCRIPT_DIR), "body_state.json")

def get_ha_token():
    return os.environ.get("SUPERVISOR_TOKEN", "")

def load_schedule():
    try:
        with open(SCHEDULE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_body_state():
    with _body_lock:
        return body_state.load_state(_BODY_STATE_FILE)


def _save_body_state(state):
    with _body_lock:
        body_state.save_state(_BODY_STATE_FILE, state)


def _body_state_json(state=None):
    if state is None:
        state = _load_body_state()
    return body_state.serialize_state(state)


def _log_body_state(label, state, **fields):
    print(body_state.format_log_line(label, state, **fields), flush=True)


def tick_body_state(loop_name, trigger_reason="", active_desires=None):
    current = _load_body_state()
    updated = body_state.advance_tick(
        current,
        loop_name=loop_name,
        trigger_reason=trigger_reason,
        active_desires=active_desires,
    )
    _save_body_state(updated)
    _log_body_state(
        f"tick/{loop_name}",
        updated,
        reason=trigger_reason,
        active_desires=len(active_desires or []),
    )
    return updated


def finish_body_state(loop_name, success, duration_seconds, *, spoke=False, action_taken=False):
    current = _load_body_state()
    updated = body_state.apply_feedback(
        current,
        loop_name=loop_name,
        success=success,
        duration_seconds=duration_seconds,
        spoke=spoke,
        action_taken=action_taken,
    )
    _save_body_state(updated)
    _log_body_state(
        f"done/{loop_name}",
        updated,
        success="yes" if success else "no",
        duration_s=f"{duration_seconds:.1f}",
        spoke="yes" if spoke else "no",
        action="yes" if action_taken else "no",
    )
    return updated

def tick_desires():
    """欲求値を1ループ分加算し、閾値を超えた欲求のプロンプト一覧を返す。"""
    try:
        with _desires_lock:
            with open(DESIRES_FILE, encoding="utf-8") as f:
                desires = json.load(f)
            try:
                with open(DESIRE_STATE_FILE, encoding="utf-8") as f:
                    state = json.load(f)
            except Exception:
                state = {}
            active = []
            for name, cfg in desires.items():
                # 1つの不正エントリ（growth_rate/prompt欠落）で全体を止めない
                if not isinstance(cfg, dict) or "prompt" not in cfg:
                    continue
                state[name] = state.get(name, 0.0) + cfg.get("growth_rate", 0)
                if state[name] >= DESIRE_THRESHOLD:
                    active.append(cfg["prompt"])
                    state[name] = 0.0
            os.makedirs(os.path.dirname(DESIRE_STATE_FILE), exist_ok=True)
            # アトミック書き込み（書き込み中クラッシュ/並行読みでの破損を防ぐ）
            tmp = DESIRE_STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, DESIRE_STATE_FILE)
            if active:
                print(f"[daemon] desires fired: {len(active)}", flush=True)
            return active
    except Exception as e:
        print(f"[daemon] tick_desires error: {e}", flush=True)
        return []

def run_watch(trigger_reason="定期実行", active_desires=None, body_state_snapshot=None, is_sensor=False):
    global _last_sensor_watch
    # is_sensor は呼び出し側が明示的に渡す（人感センサー起因かどうかを真偽値で受け取る）。
    if is_sensor:
        elapsed = time.time() - _last_sensor_watch
        if elapsed < SENSOR_COOLDOWN:
            print(f"[daemon] watch sensor cooldown ({int(elapsed)}s < {SENSOR_COOLDOWN}s), skip: {trigger_reason}", flush=True)
            return
    if not _watch_lock.acquire(blocking=False):
        print(f"[daemon] watch already running, skip: {trigger_reason}", flush=True)
        return
    start = time.perf_counter()
    success = False
    try:
        if is_sensor:
            _last_sensor_watch = time.time()
        if body_state_snapshot is None:
            try:
                body_state_snapshot = tick_body_state("watch", trigger_reason, active_desires)
            except Exception as e:
                print(f"[daemon] body state tick error (watch): {e}", flush=True)
                body_state_snapshot = _load_body_state()
        else:
            _log_body_state(
                "start/watch",
                body_state_snapshot,
                reason=trigger_reason,
                active_desires=len(active_desires or []),
            )
        print(f"[daemon] watch start: {trigger_reason}", flush=True)
        env = {**os.environ, "TRIGGER_REASON": trigger_reason, "PATH": ENV_PATH, "EHA_BODY_STATE": _body_state_json(body_state_snapshot)}
        if active_desires:
            env["ACTIVE_DESIRES"] = json.dumps(active_desires, ensure_ascii=False)
        try:
            proc = subprocess.run(["bash", WATCH_SH], env=env, timeout=WATCH_TIMEOUT)
            success = proc.returncode == 0
            print(f"[daemon] watch done: {trigger_reason}", flush=True)
        except subprocess.TimeoutExpired:
            print(f"[daemon] watch TIMEOUT (>{WATCH_TIMEOUT}s), killed: {trigger_reason}", flush=True)
    finally:
        try:
            finish_body_state("watch", success, time.perf_counter() - start)
        except Exception as e:
            print(f"[daemon] body state finish error (watch): {e}", flush=True)
        _watch_lock.release()

def on_observe_trigger(payload):
    """embodied_ha/observe/trigger の payload を観察の『経緯』として watch に渡す。
    ボタン(payload_press='OBSERVE')や空は汎用の手動実行扱い。HAオートメーションが
    カスタム文字列（例「玄関のドアが開いた、誰か来たかも」）を publish したら、それを
    trigger_reason にして watch のコンテキスト【今回のトリガー】に流す。
    明示トリガーなので is_sensor=False（cooldown対象外。多重実行は _watch_lock で防止）。"""
    reason = (payload or "").strip()
    if reason in ("", "OBSERVE", "PRESS"):
        reason = "手動実行"
    run_watch(reason, is_sensor=False)


def run_chat(message):
    # MQTT text エンティティの state_topic に echo して HA の表示を同期
    mqtt_pub("embodied_ha/chat/state", message)
    if not _chat_lock.acquire(blocking=False):
        print("[daemon] chat already running, skip", flush=True)
        return
    start = time.perf_counter()
    success = False
    try:
        print(f"[daemon] chat start: {message[:30]}", flush=True)
        try:
            body_state_snapshot = tick_body_state("chat", f"会話:{message[:40]}")
        except Exception as e:
            print(f"[daemon] body state tick error (chat): {e}", flush=True)
            body_state_snapshot = _load_body_state()
        env = {**os.environ, "CHAT_MESSAGE": message, "PATH": ENV_PATH, "EHA_BODY_STATE": _body_state_json(body_state_snapshot)}
        try:
            proc = subprocess.run(["bash", CHAT_SH], env=env, timeout=CHAT_TIMEOUT)
            success = proc.returncode == 0
            print("[daemon] chat done", flush=True)
        except subprocess.TimeoutExpired:
            print(f"[daemon] chat TIMEOUT (>{CHAT_TIMEOUT}s), killed", flush=True)
    finally:
        try:
            finish_body_state("chat", success, time.perf_counter() - start, spoke=success)
        except Exception as e:
            print(f"[daemon] body state finish error (chat): {e}", flush=True)
        _chat_lock.release()

def run_explore(body_state_snapshot=None):
    if not _explore_lock.acquire(blocking=False):
        print("[daemon] explore already running, skip", flush=True)
        return
    start = time.perf_counter()
    success = False
    try:
        if body_state_snapshot is None:
            try:
                body_state_snapshot = tick_body_state("explore", "定期実行")
            except Exception as e:
                print(f"[daemon] body state tick error (explore): {e}", flush=True)
                body_state_snapshot = _load_body_state()
        else:
            _log_body_state("start/explore", body_state_snapshot, reason="定期実行")
        print("[daemon] explore start", flush=True)
        env = {**os.environ, "PATH": ENV_PATH, "EHA_BODY_STATE": _body_state_json(body_state_snapshot)}
        try:
            proc = subprocess.run(["bash", EXPLORE_SH], env=env, timeout=EXPLORE_TIMEOUT)
            success = proc.returncode == 0
            print("[daemon] explore done", flush=True)
        except subprocess.TimeoutExpired:
            print(f"[daemon] explore TIMEOUT (>{EXPLORE_TIMEOUT}s), killed", flush=True)
    finally:
        try:
            finish_body_state("explore", success, time.perf_counter() - start)
        except Exception as e:
            print(f"[daemon] body state finish error (explore): {e}", flush=True)
        _explore_lock.release()

def mqtt_pub(topic, payload):
    """MQTT トピックに1メッセージ publish（MQTT_HOST 未設定時はno-op）。"""
    if not MQTT_HOST:
        return
    try:
        subprocess.run(
            ["mosquitto_pub", "-h", MQTT_HOST, "-p", str(MQTT_PORT),
             "-u", MQTT_USER, "-P", MQTT_PASS, "-t", topic, "-m", payload],
            capture_output=True, timeout=5
        )
    except Exception as e:
        print(f"[daemon] mqtt_pub error: {e}", flush=True)


def mqtt_listen(topic, handler, label):
    """mosquitto_sub でトピックを永続購読。切断時は5秒後に再接続。"""
    cmd = ["mosquitto_sub", "-h", MQTT_HOST, "-p", str(MQTT_PORT),
           "-u", MQTT_USER, "-P", MQTT_PASS, "-t", topic]
    while True:
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True)
            for line in proc.stdout:
                line = line.strip()
                if line:
                    threading.Thread(target=handler, args=(line,), daemon=True).start()
            proc.wait()
        except Exception as e:
            print(f"[daemon] {label} mqtt error: {e}", flush=True)
        finally:
            # 例外で抜けても mosquitto_sub を残さない（再接続のたびに増殖するのを防ぐ）
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        time.sleep(5)


def run_chance(schedule=None, body_state_snapshot=None, loop_name="watch") -> int:
    """時間帯と body state に応じた実行確率(%)を返す"""
    if schedule is None:
        schedule = load_schedule()
    h = time.localtime().tm_hour
    if 0 <= h < 7:
        base = schedule.get("night_probability", 10)
    elif 22 <= h:
        base = schedule.get("late_probability", 30)
    else:
        base = schedule.get("day_probability", 100)
    if body_state_snapshot is None:
        body_state_snapshot = _load_body_state()
    return body_state.compute_run_chance(base, body_state_snapshot, loop_name)

def scheduler():
    schedule = load_schedule()
    time.sleep(schedule.get("watch_interval", SCHEDULE_INTERVAL))
    while True:
        # ループ本体は必ずtry/exceptで囲む。未捕捉例外でスレッドが静かに死ぬと
        # 定期観察が永久停止するのに、プロセスは生きていて気づけないため。
        try:
            schedule = load_schedule()
            active_desires = tick_desires()
            interval_min = schedule.get("watch_interval", SCHEDULE_INTERVAL) // 60
            reason = f"定期実行（{interval_min}分間隔）"
            try:
                body_snapshot = tick_body_state("watch", reason, active_desires)
            except Exception as e:
                print(f"[daemon] body state tick error (watch scheduler): {e}", flush=True)
                body_snapshot = _load_body_state()
            chance = run_chance(schedule, body_snapshot, "watch")
            if chance >= 100 or random.randint(1, 100) <= chance:
                threading.Thread(target=run_watch, args=(reason, active_desires), kwargs={"body_state_snapshot": body_snapshot, "is_sensor": False}, daemon=True).start()
            else:
                print(f"[daemon] watch skipped by chance ({chance}%)", flush=True)
        except Exception as e:
            print(f"[daemon] scheduler error: {e}", flush=True)
        time.sleep(schedule.get("watch_interval", SCHEDULE_INTERVAL))

def explore_scheduler():
    schedule = load_schedule()
    time.sleep(schedule.get("explore_interval", EXPLORE_INTERVAL))
    while True:
        try:
            schedule = load_schedule()
            reason = "定期実行（探索）"
            try:
                body_snapshot = tick_body_state("explore", reason)
            except Exception as e:
                print(f"[daemon] body state tick error (explore scheduler): {e}", flush=True)
                body_snapshot = _load_body_state()
            chance = run_chance(schedule, body_snapshot, "explore")
            if chance >= 100 or random.randint(1, 100) <= chance:
                threading.Thread(target=run_explore, kwargs={"body_state_snapshot": body_snapshot}, daemon=True).start()
            else:
                print(f"[daemon] explore skipped by chance ({chance}%)", flush=True)
        except Exception as e:
            print(f"[daemon] explore_scheduler error: {e}", flush=True)
        time.sleep(schedule.get("explore_interval", EXPLORE_INTERVAL))

# --- 多重起動ガード（flock）---
# threading.Lock は全部プロセスローカルなので、daemon.py が複数走ると
# 同じエンティティを各々ポーリングして二重観察・二重トリガーになる（2026-06-22に4重起動を踏んだ）。
# OSレベルの排他ロックで「同時に1プロセスだけ」を保証する。flockはSIGKILLでも自動解放される。
os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
_lock_fp = open(LOCK_FILE, "w")  # プロセス終了まで開いたまま保持（GC回避のためグローバル）
try:
    fcntl.flock(_lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    print("[daemon] 既に別のdaemonが稼働中。起動を中止します。", flush=True)
    raise SystemExit(1)

# --- I/O スレッド: MQTT で chat/observe トリガーを購読 ---
# config.yaml の services: mqtt:need で MQTT は必須。未設定時は受信できない旨を明示。
if MQTT_HOST:
    threading.Thread(
        target=mqtt_listen,
        args=("embodied_ha/chat/set", run_chat, "mqtt-chat"),
        daemon=True,
    ).start()
    threading.Thread(
        target=mqtt_listen,
        args=("embodied_ha/observe/trigger", on_observe_trigger, "mqtt-observe"),
        daemon=True,
    ).start()
    print(f"[daemon] MQTT I/O started ({MQTT_HOST}:{MQTT_PORT})", flush=True)
else:
    print("[daemon] 警告: MQTT_HOST 未設定。チャット/観察トリガーを受信できません"
          "（MQTT統合・Mosquitto が必要）。定期ループのみ動作します。", flush=True)
threading.Thread(target=scheduler, daemon=True).start()
threading.Thread(target=explore_scheduler, daemon=True).start()
print("[daemon] started (I/O + watch-sched + explore-sched)", flush=True)

# メインスレッドを生かし続ける
while True:
    time.sleep(60)
