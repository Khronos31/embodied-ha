#!/usr/bin/env python3
"""Embodied HA デーモン
自律ループ(loop.sh)と会話(chat.sh)をHAエンティティ経由でトリガーする常駐プロセス。
起動はアドオン(addon/run.sh)から exec で呼ばれる。直接起動する場合:

トリガー方法（MQTT。config.yaml の services: mqtt:need で MQTT は必須）:
  - embodied_ha/chat/set        … 会話(chat.sh)を起動。ペイロードがユーザーの発言
  - embodied_ha/loop/trigger … 自律ループ(loop.sh)を手動起動
"""
import os
import subprocess
import threading
import time
import json
import random
import fcntl

import body_state
import anomaly_state
import desire_state

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG_DIR = os.environ.get("EHA_LOG_DIR", os.path.join(_SCRIPT_DIR, "log"))
CHAT_SH = os.path.join(_SCRIPT_DIR, "chat.sh")  # 巻き戻し用に残置。増分9でCHAT_PYへ切替
CHAT_PY = os.path.join(_SCRIPT_DIR, "chat.py")
LOOP_SH = os.path.join(_SCRIPT_DIR, "loop.sh")
AUDIO_DAEMON = os.path.join(_SCRIPT_DIR, "audio_daemon.py")
WEB_SERVER = os.path.join(_SCRIPT_DIR, "web", "server.py")
HA_URL = os.environ["HA_URL"]
LOOP_INTERVAL = 1800   # 自律ループ(loop.sh)の定期実行間隔（秒）= 30分
SENSOR_COOLDOWN = 300     # センサートリガーのクールダウン（秒）= 5分
DESIRES_FILE = os.environ.get("EHA_DESIRES_FILE", os.path.join(_SCRIPT_DIR, "desires.json"))
DESIRE_STATE_FILE = os.path.join(_LOG_DIR, "desire_state.json")
SCHEDULE_FILE = os.path.join(_SCRIPT_DIR, "schedule.json")
LOCK_FILE = os.path.join(_LOG_DIR, "daemon.lock")
# --- MQTT I/O ---
MQTT_HOST = os.environ.get("MQTT_HOST", "")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
# 各スクリプトの最大実行時間（秒）。Claude呼び出しがレート競合でハングしても
# ロックを永久に握りっぱなしにしないための上限。loopはClaude複数回＋
# ロールアップ/daybookで長くなりうるので余裕を持たせる。
CHAT_TIMEOUT = 300
LOOP_TIMEOUT = 600
ANOMALY_NIGHT_URGENCY_THRESHOLD = 30
ANOMALY_NIGHT_URGENCY_FACTOR = 0.0
QUIET_ANOMALY_PERIODS = {"late", "night", "deep_night"}

ENV_PATH = os.environ.get("EHA_TOOLS_PATH", "/config/.tools/bin:/config/.tools/npm-global/bin:/config/.tools/node/bin") + ":" + os.environ.get("PATH", "/usr/bin:/bin")
_chat_lock = threading.Lock()
_loop_lock = threading.Lock()
_desires_lock = threading.Lock()
_body_lock = threading.Lock()
_runtime_lock = threading.Lock()
_runtime_started = threading.Event()
_BODY_STATE_FILE = os.path.join(os.environ.get("EHA_DATA_DIR", _SCRIPT_DIR), "body_state.json")
_ANOMALY_STATE_FILE = os.environ.get("EHA_ANOMALY_STATE_FILE", os.path.join(_LOG_DIR, "anomaly_state.json"))
_CLAUDE_CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR", "/data/.claude")


def load_enabled_mics() -> list[dict]:
    prefs_file = os.environ.get("EHA_PREFS_FILE", "")
    if not prefs_file:
        return []
    try:
        with open(prefs_file, encoding="utf-8") as f:
            prefs = json.load(f)
    except Exception as e:
        print(f"[daemon] failed to load preferences for audio daemon: {e}", flush=True)
        return []
    sources = prefs.get("mics")
    if not isinstance(sources, list):
        return []
    return [item for item in sources if isinstance(item, dict) and item.get("stt_enabled") is True]


def auth_ready() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    for fname in (".credentials.json", "credentials.json"):
        if os.path.exists(os.path.join(_CLAUDE_CONFIG_DIR, fname)):
            return True
    return False

def get_ha_token():
    return os.environ.get("SUPERVISOR_TOKEN", "")

def load_schedule():
    prefs_file = os.environ.get("EHA_PREFS_FILE", "")
    if prefs_file:
        try:
            with open(prefs_file, encoding="utf-8") as f:
                prefs = json.load(f)
            loop_schedule = prefs.get("loop_schedule")
            if isinstance(loop_schedule, dict) and loop_schedule:
                return loop_schedule
        except Exception:
            pass
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

def _load_anomaly_state():
    with _body_lock:
        return anomaly_state.load_state(_ANOMALY_STATE_FILE)


def _save_anomaly_state(state):
    with _body_lock:
        anomaly_state.save_state(_ANOMALY_STATE_FILE, state)


def _anomaly_state_json(state=None):
    if state is None:
        state = _load_anomaly_state()
    return anomaly_state.serialize_state(state)


def _log_anomaly_state(label, state, **fields):
    print(anomaly_state.format_log_line(label, state, **fields), flush=True)


def _anomaly_context(state=None):
    if state is None:
        state = _load_anomaly_state()
    return anomaly_state.format_context_block(state)


def _anomaly_urgency(state=None):
    if state is None:
        state = _load_anomaly_state()
    return anomaly_state.compute_explore_urgency(state)
def _load_desire_catalog():
    return desire_state.load_desires(DESIRES_FILE)


def _load_desire_state(catalog=None):
    return desire_state.load_state(DESIRE_STATE_FILE, catalog=catalog)


def _save_desire_state(state, catalog=None):
    desire_state.save_state(DESIRE_STATE_FILE, state, catalog=catalog)


def tick_body_state(loop_name, trigger_reason="", active_desires=None):
    with _body_lock:
        updated = body_state.update_state(
            _BODY_STATE_FILE,
            lambda current: body_state.advance_tick(
                current,
                loop_name=loop_name,
                trigger_reason=trigger_reason,
                active_desires=active_desires,
            ),
        )
    _log_body_state(
        f"tick/{loop_name}",
        updated,
        reason=trigger_reason,
        active_desires=len(active_desires or []),
    )
    return updated


def finish_body_state(loop_name, success, duration_seconds, *, spoke=False, action_taken=False):
    with _body_lock:
        updated = body_state.update_state(
            _BODY_STATE_FILE,
            lambda current: body_state.apply_feedback(
                current,
                loop_name=loop_name,
                success=success,
                duration_seconds=duration_seconds,
                spoke=spoke,
                action_taken=action_taken,
            ),
        )
    _log_body_state(
        f"done/{loop_name}",
        updated,
        success="yes" if success else "no",
        duration_s=f"{duration_seconds:.1f}",
        spoke="yes" if spoke else "no",
        action="yes" if action_taken else "no",
    )
    return updated

def tick_desires(body_state_snapshot=None, loop_name="loop", trigger_reason="", emit_active=True):
    """欲求状態を更新し、必要なら loop に流すプロンプト一覧を返す。"""
    try:
        catalog = _load_desire_catalog()
        with _desires_lock:
            state = _load_desire_state(catalog)
            updated = desire_state.decay_tick(
                state,
                catalog=catalog,
                body_state=body_state_snapshot,
                loop_name=loop_name,
                trigger_reason=trigger_reason,
            )
            pressure = desire_state.compute_pressure(updated, catalog=catalog, body_state=body_state_snapshot)
            active_names = desire_state.active_desire_names(updated, catalog)
            active_prompts = desire_state.active_desire_prompts(updated, catalog) if emit_active else []

            if emit_active and active_names:
                updated = desire_state.consume_active_desires(
                    updated,
                    active_names,
                    catalog=catalog,
                )

            _save_desire_state(updated, catalog)
        if active_prompts:
            print(f"[daemon] desires fired: {len(active_prompts)} pressure={pressure:.3f}", flush=True)
        print(
            desire_state.format_log_line(
                f"tick/{loop_name}",
                updated,
                catalog=catalog,
                pressure=f"{pressure:.3f}",
                reason=trigger_reason,
                fired=len(active_prompts),
            ),
            flush=True,
        )
        return active_prompts, pressure
    except Exception as e:
        print(f"[daemon] tick_desires error: {e}", flush=True)
        return [], 0.0

def run_loop(trigger_reason="定期実行", active_desires=None, body_state_snapshot=None, anomaly_state_snapshot=None, mode=None):
    if not _loop_lock.acquire(blocking=False):
        print(f"[daemon] loop already running, skip: {trigger_reason}", flush=True)
        return
    start = time.perf_counter()
    success = False
    try:
        if body_state_snapshot is None:
            try:
                body_state_snapshot = tick_body_state("loop", trigger_reason, active_desires)
            except Exception as e:
                print(f"[daemon] body state tick error (loop): {e}", flush=True)
                body_state_snapshot = _load_body_state()
        else:
            _log_body_state(
                "start/loop",
                body_state_snapshot,
                reason=trigger_reason,
                active_desires=len(active_desires or []),
            )
        if anomaly_state_snapshot is None:
            try:
                anomaly_state_snapshot = _load_anomaly_state()
            except Exception as e:
                print(f"[daemon] anomaly state load error (loop): {e}", flush=True)
                anomaly_state_snapshot = anomaly_state.normalize_state(None)
        else:
            _log_anomaly_state("start/loop", anomaly_state_snapshot, reason=trigger_reason)
        print(f"[daemon] loop start: {trigger_reason}", flush=True)
        env = {
            **os.environ,
            "TRIGGER_REASON": trigger_reason,
            "PATH": ENV_PATH,
            "EHA_BODY_STATE": _body_state_json(body_state_snapshot),
            "ANOMALY_CONTEXT": anomaly_state.format_context_block(anomaly_state_snapshot),
            "ANOMALY_URGENCY": str(anomaly_state.compute_explore_urgency(anomaly_state_snapshot)),
        }
        if mode:
            env["MODE"] = mode
        if active_desires:
            env["ACTIVE_DESIRES"] = json.dumps(active_desires, ensure_ascii=False)
        try:
            proc = subprocess.run(["bash", LOOP_SH], env=env, timeout=LOOP_TIMEOUT)
            success = proc.returncode == 0
            print(f"[daemon] loop done: {trigger_reason}", flush=True)
        except subprocess.TimeoutExpired:
            print(f"[daemon] loop TIMEOUT (>{LOOP_TIMEOUT}s), killed: {trigger_reason}", flush=True)
    finally:
        try:
            finish_body_state("loop", success, time.perf_counter() - start)
        except Exception as e:
            print(f"[daemon] body state finish error (loop): {e}", flush=True)
        _loop_lock.release()


def on_loop_trigger(payload):
    """embodied_ha/loop/trigger の payload を loop に渡す。
    ボタン(payload_press='LOOP')や空は汎用の手動実行扱い。HAオートメーションが
    カスタム文字列を publish したら、それを trigger_reason にして loop のコンテキストに流す。"""
    reason = (payload or "").strip()
    if not reason or reason.upper() == "LOOP":
        reason = "手動実行"
    run_loop(reason, mode="observe")


def run_chat(message, source="chat"):
    # MQTT text エンティティの state_topic に echo して HA の表示を同期
    mqtt_pub("embodied_ha/chat/state", message)
    if not _chat_lock.acquire(blocking=False):
        print("[daemon] chat already running, skip", flush=True)
        return
    start = time.perf_counter()
    success = False
    try:
        source = str(source or "chat").strip() or "chat"
        print(f"[daemon] chat start [{source}]: {message[:30]}", flush=True)
        try:
            body_before = _load_body_state()
            active_desires, _ = tick_desires(body_before, "chat", f"会話:{message[:40]}", emit_active=True)
            body_state_snapshot = tick_body_state("chat", f"会話:{message[:40]}", active_desires)
        except Exception as e:
            print(f"[daemon] body state tick error (chat): {e}", flush=True)
            body_state_snapshot = _load_body_state()
            active_desires = []
        env = {
            **os.environ,
            "CHAT_MESSAGE": message,
            "CHAT_SOURCE": source,
            "PATH": ENV_PATH,
            "EHA_BODY_STATE": _body_state_json(body_state_snapshot),
        }
        if active_desires:
            env["ACTIVE_DESIRES"] = json.dumps(active_desires, ensure_ascii=False)
        try:
            proc = subprocess.run(["python3", CHAT_PY], env=env, timeout=CHAT_TIMEOUT)
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


def on_chat_trigger(payload):
    message = (payload or "").strip()
    source = "chat"
    if not message:
        return
    try:
        parsed = json.loads(message)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        message = str(parsed.get("message", "")).strip()
        source = str(parsed.get("source", "chat")).strip() or "chat"
    if not message:
        print("[daemon] chat trigger missing message, skip", flush=True)
        return
    run_chat(message, source=source)


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


def _schedule_period(schedule: dict | None = None, *, hour: int | None = None) -> str:
    h = time.localtime().tm_hour if hour is None else int(hour) % 24
    if 0 <= h < 7:
        return "deep_night" if isinstance(schedule, dict) and "deep_night_probability" in schedule else "night"
    if 22 <= h:
        return "late" if isinstance(schedule, dict) and "late_probability" in schedule else "night"
    return "day"


def _base_probability_for_period(schedule: dict, period: str) -> int:
    defaults = {"day": 100, "late": 30, "night": 10, "deep_night": 10}
    key = f"{period}_probability"
    if key not in schedule and period == "deep_night":
        key = "night_probability"
    return schedule.get(key, defaults.get(period, 100))


def _effective_anomaly_urgency(schedule: dict, period: str, anomaly_urgency: int | float) -> int:
    try:
        urgency = int(anomaly_urgency)
    except (TypeError, ValueError):
        return 0
    if urgency <= 0:
        return 0
    high_threshold = schedule.get("anomaly_night_urgency_threshold", ANOMALY_NIGHT_URGENCY_THRESHOLD)
    try:
        high_threshold = int(high_threshold)
    except (TypeError, ValueError):
        high_threshold = ANOMALY_NIGHT_URGENCY_THRESHOLD
    if period in QUIET_ANOMALY_PERIODS and urgency < high_threshold:
        factor = schedule.get("anomaly_night_urgency_factor", ANOMALY_NIGHT_URGENCY_FACTOR)
        try:
            factor = max(0.0, min(1.0, float(factor)))
        except (TypeError, ValueError):
            factor = ANOMALY_NIGHT_URGENCY_FACTOR
        return int(round(urgency * factor))
    return urgency


def run_chance(
    schedule=None,
    body_state_snapshot=None,
    loop_name="loop",
    desire_pressure=0.0,
    anomaly_urgency=0,
    *,
    hour: int | None = None,
) -> int:
    """時間帯と body state に応じた実行確率(%)を返す"""
    if schedule is None:
        schedule = load_schedule()
    period = _schedule_period(schedule, hour=hour)
    base = _base_probability_for_period(schedule, period)
    if body_state_snapshot is None:
        body_state_snapshot = _load_body_state()
    chance = body_state.compute_run_chance(base, body_state_snapshot, loop_name)
    if desire_pressure:
        if loop_name == "loop":
            chance += round(desire_pressure * 16)
        elif loop_name == "chat":
            chance += round(desire_pressure * 18)
    if loop_name == "loop" and anomaly_urgency:
        chance += _effective_anomaly_urgency(schedule, period, anomaly_urgency)
    # 下限クランプ。既定0＝各時間帯の確率をそのまま尊重（0%なら発火しない）。
    # 体調が悪い時間帯に完全停止して復帰しなくなるのを防ぎたい場合のみ正の値を設定する。
    try:
        min_p = max(0, min(100, int(schedule.get("min_probability", 0))))
    except (TypeError, ValueError):
        min_p = 0
    return max(min_p, min(100, chance))

def loop_scheduler():
    schedule = load_schedule()
    time.sleep(schedule.get("loop_interval", LOOP_INTERVAL))
    while True:
        # ループ本体は必ずtry/exceptで囲む。未捕捉例外でスレッドが静かに死ぬと
        # 定期ループが永久停止するのに、プロセスは生きていて気づけないため。
        try:
            schedule = load_schedule()
            interval_min = schedule.get("loop_interval", LOOP_INTERVAL) // 60
            reason = f"定期実行（{interval_min}分間隔）"
            try:
                body_before = _load_body_state()
            except Exception as e:
                print(f"[daemon] body state load error (loop scheduler): {e}", flush=True)
                body_before = body_state.normalize_state(None)
            try:
                anomaly_before = _load_anomaly_state()
            except Exception as e:
                print(f"[daemon] anomaly state load error (loop scheduler): {e}", flush=True)
                anomaly_before = anomaly_state.normalize_state(None)
            active_desires, desire_pressure = tick_desires(body_before, "loop", reason, emit_active=True)
            try:
                body_snapshot = tick_body_state("loop", reason, active_desires)
            except Exception as e:
                print(f"[daemon] body state tick error (loop scheduler): {e}", flush=True)
                body_snapshot = body_before
            anomaly_urgency = anomaly_state.compute_explore_urgency(anomaly_before)
            chance = run_chance(schedule, body_snapshot, "loop", desire_pressure, anomaly_urgency=anomaly_urgency)
            if chance >= 100 or random.randint(1, 100) <= chance:
                threading.Thread(
                    target=run_loop,
                    kwargs={
                        "body_state_snapshot": body_snapshot,
                        "anomaly_state_snapshot": anomaly_before,
                        "active_desires": active_desires,
                    },
                    daemon=True,
                ).start()
            else:
                print(f"[daemon] loop skipped by chance ({chance}%, anomaly={anomaly_urgency})", flush=True)
        except Exception as e:
            print(f"[daemon] loop_scheduler error: {e}", flush=True)
        time.sleep(schedule.get("loop_interval", LOOP_INTERVAL))

def audio_daemon_watchdog():
    env = {**os.environ, "PATH": ENV_PATH}
    while True:
        proc = None
        try:
            proc = subprocess.Popen(["python3", AUDIO_DAEMON], env=env)
            print("[daemon] audio daemon started", flush=True)
            rc = proc.wait()
            print(f"[daemon] audio daemon exited with code {rc}; restarting in 60s", flush=True)
        except Exception as e:
            print(f"[daemon] audio daemon watchdog error: {e}", flush=True)
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        time.sleep(60)


def web_server_watchdog():
    env = {**os.environ, "PATH": ENV_PATH}
    while True:
        proc = None
        try:
            proc = subprocess.Popen(["python3", WEB_SERVER], env=env)
            print("[daemon] web server started", flush=True)
            rc = proc.wait()
            print(f"[daemon] web server exited with code {rc}; restarting in 60s", flush=True)
        except Exception as e:
            print(f"[daemon] web server watchdog error: {e}", flush=True)
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        time.sleep(60)


def start_runtime_threads():
    if _runtime_started.is_set():
        return
    with _runtime_lock:
        if _runtime_started.is_set():
            return
        if MQTT_HOST:
            threading.Thread(
                target=mqtt_listen,
                args=("embodied_ha/chat/set", on_chat_trigger, "mqtt-chat"),
                daemon=True,
            ).start()
            threading.Thread(
                target=mqtt_listen,
                args=("embodied_ha/loop/trigger", on_loop_trigger, "mqtt-loop"),
                daemon=True,
            ).start()
            print(f"[daemon] MQTT I/O started ({MQTT_HOST}:{MQTT_PORT})", flush=True)
        else:
            print("[daemon] 警告: MQTT_HOST 未設定。チャット/観察トリガーを受信できません"
                  "（MQTT統合・Mosquitto が必要）。定期ループのみ動作します。", flush=True)
        threading.Thread(target=loop_scheduler, daemon=True).start()
        if load_enabled_mics():
            threading.Thread(target=audio_daemon_watchdog, daemon=True).start()
            print("[daemon] audio daemon watchdog enabled", flush=True)
        print("[daemon] started (I/O + loop-sched)", flush=True)
        _runtime_started.set()


def boot_runtime_when_ready():
    while not auth_ready():
        time.sleep(5)
    print("[daemon] Claude認証を検出。runtime を開始します", flush=True)
    start_runtime_threads()


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

# --- Web UI / runtime 起動 ---
threading.Thread(target=web_server_watchdog, daemon=True).start()
print("[daemon] web server watchdog enabled", flush=True)
if auth_ready():
    start_runtime_threads()
else:
    print("[daemon] Claude 未認証。Web UI でセットアップ後に runtime を開始します", flush=True)
    threading.Thread(target=boot_runtime_when_ready, daemon=True).start()
# 保守パイプラインの生存確認（サイレント停止の早期検知）
try:
    marker = os.path.join(_LOG_DIR, ".last_daybook")
    if os.path.exists(marker):
        with open(marker, encoding="utf-8") as f:
            last = f.read().strip()
        import datetime as _dt

        if last:
            gap = (_dt.date.today() - _dt.date.fromisoformat(last)).days
            if gap >= 2:
                print(f"[daemon] 警告: daybook が {gap} 日更新されていません（保守パイプライン停止の疑い）", flush=True)
except Exception:
    pass
# メインスレッドを生かし続ける
while True:
    time.sleep(60)
