#!/usr/bin/env python3
"""Embodied HA Web UI サーバー。静的ファイル配信 + JSONL 読み取り API + SSE ライブ更新。"""
import json, os, subprocess, time, queue, threading, tempfile, sys, re, re
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

WEB_DIR    = os.path.dirname(os.path.abspath(__file__))
SCRIPT_DIR = os.path.dirname(WEB_DIR)  # repo root
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
try:
    import antigravity_setup  # type: ignore
except Exception:
    antigravity_setup = None
LOG_DIR    = os.environ.get("EHA_LOG_DIR", os.path.join(SCRIPT_DIR, "log"))
PORT       = int(os.environ.get("INGRESS_PORT", 8099))

CHAT_LOG = os.path.join(LOG_DIR, "chat_log.jsonl")
OBS_LOG  = os.path.join(LOG_DIR, "observations.jsonl")
EXP_LOG  = os.path.join(LOG_DIR, "explore.jsonl")
NON_SPEECH_AUDIO_EVENTS_LOG = os.environ.get(
    "EHA_NON_SPEECH_AUDIO_EVENTS_FILE",
    os.path.join(LOG_DIR, "non_speech_audio_events.jsonl"),
)
AUDIO_EVENT_TAGS_LOG = os.environ.get(
    "EHA_AUDIO_EVENT_TAGS_FILE",
    os.path.join(LOG_DIR, "audio_event_tags.jsonl"),
)
WAV_DIR = os.environ.get("EHA_AUDIO_WAV_DIR")
if not WAV_DIR:
    eha_data_dir = os.environ.get("EHA_DATA_DIR")
    if eha_data_dir:
        WAV_DIR = os.path.join(eha_data_dir, "wav")
    else:
        WAV_DIR = "/config/embodied-ha/wav"

PREFS_FILE = os.environ.get("EHA_PREFS_FILE", os.path.join(SCRIPT_DIR, "preferences.json"))
PREFS_EXAMPLE_FILE = os.path.join(SCRIPT_DIR, "preferences.json.example")
CHARACTER_FILE = os.environ.get("EHA_CHARACTER_FILE", os.path.join(SCRIPT_DIR, "character.md"))

DATA_DIR = os.environ.get("EHA_DATA_DIR", SCRIPT_DIR)
EXTRA_CONTEXT_FILE = os.path.join(DATA_DIR, "extra_context.conf")


def atomic_write(filepath: str, content: str | bytes) -> bool:
    dir_name = os.path.dirname(filepath)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=dir_name or None, prefix="eha_tmp_", suffix=".tmp")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') if isinstance(content, str) else os.fdopen(fd, 'wb') as f:
            f.write(content)
        os.replace(temp_path, filepath)
        return True
    except Exception as e:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise e


def ha_api_raw_request(path: str, method: str = "GET", body: dict = None) -> tuple[int, str]:
    url = f"{HA_URL.rstrip('/')}/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json"
    }
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        return e.code, err_body
    except Exception as e:
        return 500, str(e)


def get_ha_entities(domains: list[str]) -> list[dict]:
    entities = []
    target_domains = set(domains)
    
    # 1. /api/states
    status, raw_states = ha_api_raw_request("/states")
    states = []
    if status == 200:
        try:
            states = json.loads(raw_states)
        except Exception:
            pass
            
    filtered_states = []
    for state in states:
        eid = state.get("entity_id", "")
        parts = eid.split(".", 1)
        if len(parts) == 2:
            dom = parts[0]
            if dom in target_domains:
                filtered_states.append(state)

    # 2. エリアマップ
    area_map = {}
    template_parts = []
    for dom in target_domains:
        if dom == "notify":
            continue
        template_parts.append(
            f"{{% for s in states.{dom} %}}"
            f"{{{{ s.entity_id }}}}:{{{{ area_name(s.entity_id) or '' }}}}\n"
            f"{{% endfor %}}"
        )
    
    if template_parts:
        template_str = "".join(template_parts)
        t_status, t_res = ha_api_raw_request("/template", method="POST", body={"template": template_str})
        if t_status == 200:
            for line in t_res.splitlines():
                if ":" in line:
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        eid, area = parts
                        area = area.strip()
                        area_map[eid.strip()] = area if area not in ("", "None") else None

    # 3. リスト作成
    for state in filtered_states:
        eid = state.get("entity_id", "")
        friendly_name = state.get("attributes", {}).get("friendly_name") or eid
        area = area_map.get(eid)
        entities.append({
            "entity_id": eid,
            "friendly_name": friendly_name,
            "area": area
        })

    # 4. notify
    if "notify" in target_domains:
        s_status, raw_services = ha_api_raw_request("/services")
        notify_services = {}
        if s_status == 200:
            try:
                raw = json.loads(raw_services)
                # 新形式 (HA 2023+): list of {"domain": ..., "services": {...}}
                # 旧形式: {"notify": {...}, ...}
                if isinstance(raw, list):
                    for item in raw:
                        if item.get("domain") == "notify":
                            notify_services = item.get("services", {})
                            break
                else:
                    notify_services = raw.get("notify", {})
            except Exception:
                pass
        # HA内部サービスは発話先として不要なので除外
        _INTERNAL = {"send_message", "persistent_notification"}
        for svc_name, svc_info in notify_services.items():
            if svc_name in _INTERNAL:
                continue
            eid = f"notify.{svc_name}"
            title = svc_info.get("name") or svc_name
            # "Send a notification via XXX" のような冗長な説明文を削る
            for prefix in ("Send a notification via ", "Send a notification with "):
                if title.startswith(prefix):
                    title = title[len(prefix):]
                    break
            entities.append({
                "entity_id": eid,
                "friendly_name": f"{title} ({eid})",
                "area": None
            })
            
    return entities

MQTT_HOST = os.environ.get("MQTT_HOST", "")
MQTT_PORT = os.environ.get("MQTT_PORT", "1883")
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
HA_URL    = os.environ["HA_URL"]
HA_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_CONFIG_DIR_PATH = os.environ.get("CLAUDE_CONFIG_DIR", "/data/.claude")

_ANTIGRAVITY_LOGIN_PTY_FD: list = [None]   # [int | None]
_ANTIGRAVITY_LOGIN_PTY_LOCK = threading.Lock()
_ANTIGRAVITY_LOGIN_SESSION_LOCK = threading.Lock()
_ANTIGRAVITY_INSTALL_LOCK = threading.Lock()
_ANTIGRAVITY_LOGIN_URL_RE = re.compile(r"https://\S+")


def is_authenticated() -> bool:
    # APIキー認証
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    # サブスク認証は OAuthトークン本体 .credentials.json の有無で判定する。
    # （.claude.json の userID は「ログイン記録」であって認証実体ではない。
    #   userID があってもトークンが無ければ claude は "Not logged in" になる）
    for fname in (".credentials.json", "credentials.json"):
        if os.path.exists(os.path.join(CLAUDE_CONFIG_DIR_PATH, fname)):
            return True
    return False


def antigravity_status() -> dict:
    if antigravity_setup is None:
        return {
            "installed": False,
            "authenticated": False,
            "installing": False,
            "login_active": False,
            "home_dir": os.environ.get("EHA_ANTIGRAVITY_HOME", "/data/"),
            "bin_dir": os.environ.get("EHA_ANTIGRAVITY_BIN_DIR", ""),
            "binary_path": os.environ.get("EHA_ANTIGRAVITY_BIN", ""),
            "oauth_token_path": "",
            "install_url": "https://antigravity.google/cli/install.sh",
        }
    state = antigravity_setup.state()
    state["installing"] = _ANTIGRAVITY_INSTALL_LOCK.locked()
    state["login_active"] = _ANTIGRAVITY_LOGIN_SESSION_LOCK.locked()
    return state


def _stop_antigravity_process(proc, master_fd=None, use_ctrl_d: bool = False):
    """Antigravity の後始末。

    login の PTY には Ctrl-D を 2 回送り、それでも残るなら kill する。
    install など PTY のない処理は terminate -> kill の順で止める。
    """
    if use_ctrl_d and master_fd is not None:
        for _ in range(2):
            try:
                os.write(master_fd, b"\x04")
                time.sleep(0.1)
            except OSError:
                break

    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
        time.sleep(0.2)
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass



def _antigravity_login_handle_line(line: str, state: dict, master_fd, q: queue.Queue):
    """Antigravity login TUI の 1 行を解釈して自動応答する。"""
    line_lower = line.lower()

    if not state.get("sent_method"):
        if "google" in line_lower or ("1." in line and "oauth" in line_lower):
            os.write(master_fd, b"1\n")
            state["sent_method"] = True
            return

    if not state.get("url_found"):
        m = _ANTIGRAVITY_LOGIN_URL_RE.search(line)
        if m:
            q.put(("url", {"url": m.group(0)}))
            state["url_found"] = True
        return

    if not state.get("sent_code_wait"):
        if "code" in line_lower or "authorization" in line_lower:
            q.put(("waiting_code", {}))
            state["sent_code_wait"] = True
        return

    if "color scheme" in line_lower:
        os.write(master_fd, b"\n")
        return

    if "terms of service" in line_lower or "terms" in line_lower:
        os.write(master_fd, b"\x1b[B\x1b[C\n")
        return

    if "trust" in line_lower:
        os.write(master_fd, b"\n")
        return

# --- SSE クライアント管理 ---
_sse_clients: list = []
_sse_lock = threading.Lock()

# --- ログインセッション（PTY fd をグローバルに保持してコード書き戻しに使う）---
_login_pty_fd: list = [None]   # [int | None]
_login_pty_lock = threading.Lock()

# --- エージェント稼働状態 ---
# status: "idle" | "thinking"
# source: "watch" | "explore" | "chat" | None
_agent_status: dict = {"status": "idle", "source": None}
_agent_status_lock = threading.Lock()


def set_agent_status(status: str, source: str | None = None):
    with _agent_status_lock:
        _agent_status["status"] = status
        _agent_status["source"] = source
    notify_sse("typing", {"typing": status == "thinking", "type": source})


def notify_sse(event_type: str, data: dict):
    msg = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)


# --- データ読み取り ---
def read_jsonl(path: str, limit: int = 300) -> list:
    lines = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        lines.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    return lines[-limit:]


def append_jsonl(path: str, row: dict) -> None:
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def get_chat_messages(limit: int = 300) -> list:
    """chat_log.jsonl を返す（{timestamp, source, claude, user}）。"""
    return read_jsonl(CHAT_LOG, limit)


def get_soliloquy_messages(limit: int = 300) -> list:
    """observations.jsonl + explore.jsonl + chat_log.jsonl の private をマージして返す。"""
    obs = [
        {"timestamp": d["timestamp"], "source": "watch",
         "private": d.get("private", ""), "emotion": d.get("emotion", ""),
         "topic": d.get("topic")}
        for d in read_jsonl(OBS_LOG)
        if d.get("private")
    ]
    cht = [
        {"timestamp": d["timestamp"], "source": "chat",
         "private": d.get("private", ""), "emotion": d.get("emotion", ""),
         "topic": d.get("topic")}
        for d in read_jsonl(CHAT_LOG)
        if d.get("private")
    ]
    exp = [
        {"timestamp": d["timestamp"], "source": "explore",
         "private": d.get("private", ""), "emotion": d.get("emotion", ""),
         "mode": d.get("mode", ""), "topic": d.get("topic")}
        for d in read_jsonl(EXP_LOG)
        if d.get("private")
    ]
    merged = sorted(obs + exp + cht, key=lambda d: d["timestamp"])
    return merged[-limit:]


# --- メッセージ送信 ---
def send_chat(message: str, source: str = "chat"):
    """MQTT 優先、なければ input_text REST 経由で chat.sh を起動する。"""
    import subprocess
    if MQTT_HOST:
        subprocess.run(
            ["mosquitto_pub", "-h", MQTT_HOST, "-p", MQTT_PORT,
             "-u", MQTT_USER, "-P", MQTT_PASS,
             "-t", "embodied_ha/chat/set", "-m", json.dumps({"message": message, "source": source}, ensure_ascii=False)],
            capture_output=True, timeout=5
        )
    else:
        payload = json.dumps(
            {"entity_id": "input_text.embodied_ha_chat_input", "value": message[:100]},
            ensure_ascii=False
        )
        subprocess.run([
            "curl", "-sf", "-X", "POST",
            "-H", f"Authorization: Bearer {HA_TOKEN}",
            "-H", "Content-Type: application/json",
            "-d", payload,
            f"{HA_URL}/services/input_text/set_value"
        ], capture_output=True, timeout=5)


# --- ファイル監視スレッド（SSE 通知用）---
def file_watcher():
    # chat_log は会話ルームと独り言ルーム（chat の private）の両方に使われるので両方へ通知する。
    watched = [(CHAT_LOG, ["chat", "soliloquy"]),
               (OBS_LOG, ["soliloquy"]), (EXP_LOG, ["soliloquy"]),
               (NON_SPEECH_AUDIO_EVENTS_LOG, ["audio"]), (AUDIO_EVENT_TAGS_LOG, ["audio"])]
    mtimes: dict = {}
    while True:
        for path, rooms in watched:
            try:
                mtime = os.path.getmtime(path)
                if path in mtimes and mtimes[path] != mtime:
                    for room in rooms:
                        notify_sse("update", {"room": room})
                mtimes[path] = mtime
            except FileNotFoundError:
                pass
        time.sleep(1)


# --- HTTP ハンドラー ---
class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # アクセスログ抑制

    def _strip_ingress(self, path: str) -> str:
        base = self.headers.get("X-Ingress-Path", "")
        if base and path.startswith(base):
            path = path[len(base):]
        return path or "/"

    def serve_index(self):
        """index.html に window.INGRESS_PATH を注入して返す。"""
        ingress_path = self.headers.get("X-Ingress-Path", "")
        try:
            with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as f:
                html = f.read()
            inject = (
                f'<script>window.INGRESS_PATH={json.dumps(ingress_path)};</script>'
            )
            html = html.replace("</head>", inject + "\n</head>", 1)
            data = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self.send_error(500)

    def serve_file(self, filename: str, content_type: str):
        try:
            with open(os.path.join(WEB_DIR, filename), "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404)

    def _serve_setup_login(self):
        """PTY で claude を起動し、ウィザード or /login 経由で OAuth URL を SSE 配信する。
        URL 表示後はユーザーが取得したコードを POST /api/setup/login-code で送り返す。"""
        import subprocess as _sp, pty, select, re as _re

        q: queue.Queue = queue.Queue(maxsize=200)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def run_login():
            master_fd = None
            try:
                master_fd, slave_fd = pty.openpty()
                with _login_pty_lock:
                    _login_pty_fd[0] = master_fd

                env = os.environ.copy()
                env["TERM"] = "dumb"
                # 専用ログインコマンドを使う。メインTUIウィザードを駆動する方式は
                # OAuthコード交換後に .credentials.json を永続化しないため不可
                # （userIDは書くがトークン本体が書かれない）。auth login はウィザード
                # 不要・URLが1行・コード送信1秒後に .credentials.json を書く。
                proc = _sp.Popen(
                    [CLAUDE_BIN, "auth", "login", "--claudeai"],
                    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                    close_fds=True, env=env
                )
                os.close(slave_fd)

                ansi = _re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|[0-9])")
                url_re = _re.compile(r"https://\S+")

                buf = ""
                url_found = False
                deadline = time.time() + 300

                while time.time() < deadline:
                    try:
                        r, _, _ = select.select([master_fd], [], [], 0.5)
                        if not r:
                            if proc.poll() is not None:
                                break
                            # コード送信後、.credentials.json が書かれたら完了
                            if url_found and is_authenticated():
                                try: proc.terminate()
                                except Exception: pass
                                break
                            continue

                        raw = os.read(master_fd, 4096).decode("utf-8", errors="replace")
                        clean = ansi.sub("", raw).replace("\r\n", "\n").replace("\r", "\n")
                        buf += clean

                        # URL は完全な行になってから取り出す（ストリーム途中の部分一致を避ける）
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            if not url_found:
                                m = url_re.search(line)
                                if m:
                                    q.put(("line", m.group(0)))
                                    url_found = True

                    except (OSError, IOError):
                        break
                    if proc.poll() is not None:
                        break

                rc = proc.poll()
                q.put(("done", rc if rc is not None else 0))
            except Exception as e:
                q.put(("error", str(e)))
            finally:
                with _login_pty_lock:
                    _login_pty_fd[0] = None
                if master_fd is not None:
                    try: os.close(master_fd)
                    except OSError: pass

        threading.Thread(target=run_login, daemon=True).start()

        try:
            while True:
                try:
                    etype, data = q.get(timeout=2)
                    if etype == "line":
                        msg = f"event: line\ndata: {json.dumps({'text': data}, ensure_ascii=False)}\n\n"
                    elif etype == "done":
                        msg = f"event: done\ndata: {json.dumps({'code': data})}\n\n"
                    else:
                        msg = f"event: error\ndata: {json.dumps({'error': data}, ensure_ascii=False)}\n\n"
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
                    if etype in ("done", "error"):
                        break
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except Exception:
            pass

    def _serve_setup_antigravity_install(self):
        """Antigravity CLI を公式 install.sh からオンデマンド導入し、進捗を SSE 配信する。"""
        import subprocess as _sp

        if not _ANTIGRAVITY_INSTALL_LOCK.acquire(blocking=False):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            msg = f"event: error\ndata: {json.dumps({'error': 'Antigravity install is already running'}, ensure_ascii=False)}\n\n"
            try:
                self.wfile.write(msg.encode())
                self.wfile.flush()
            except Exception:
                pass
            return

        q: queue.Queue = queue.Queue(maxsize=200)
        proc_box = {"proc": None}
        stop_event = threading.Event()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def run_install():
            proc = None
            try:
                if antigravity_setup is None:
                    raise RuntimeError("antigravity helpers unavailable")
                home_dir = antigravity_setup.home_dir()
                bin_dir = antigravity_setup.bin_dir()
                os.makedirs(home_dir, exist_ok=True)
                os.makedirs(bin_dir, exist_ok=True)
                script = antigravity_setup.fetch_install_script(timeout=60)
                env = os.environ.copy()
                env["HOME"] = home_dir
                env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
                proc = _sp.Popen(
                    ["bash", "-s", "--", "--dir", bin_dir],
                    stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.STDOUT,
                    text=True, bufsize=1, env=env
                )
                proc_box["proc"] = proc
                assert proc.stdin is not None and proc.stdout is not None
                proc.stdin.write(script)
                proc.stdin.close()
                for line in proc.stdout:
                    if stop_event.is_set():
                        break
                    line = line.rstrip("\n")
                    if line:
                        q.put(("line", line))
                if not stop_event.is_set():
                    rc = proc.wait()
                    q.put(("done", rc))
            except Exception as e:
                if not stop_event.is_set():
                    q.put(("error", str(e)))
            finally:
                _stop_antigravity_process(proc)
                try:
                    _ANTIGRAVITY_INSTALL_LOCK.release()
                except Exception:
                    pass

        threading.Thread(target=run_install, daemon=True).start()

        try:
            while True:
                try:
                    etype, data = q.get(timeout=2)
                    if etype == "line":
                        msg = f"event: line\ndata: {json.dumps({'text': data}, ensure_ascii=False)}\n\n"
                    elif etype == "done":
                        msg = f"event: done\ndata: {json.dumps({'code': data})}\n\n"
                    else:
                        msg = f"event: error\ndata: {json.dumps({'error': data}, ensure_ascii=False)}\n\n"
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
                    if etype in ("done", "error"):
                        break
                except queue.Empty:
                    if stop_event.is_set():
                        break
                    self.wfile.write(b":" + b" ping" + b"\n\n")
                    self.wfile.flush()
        except Exception:
            stop_event.set()
            _stop_antigravity_process(proc_box["proc"])
        finally:
            stop_event.set()
            _stop_antigravity_process(proc_box["proc"])

    def _serve_setup_antigravity_login(self):
        """Antigravity auth login を PTY で起動し、URL と完了状態だけを SSE 配信する。"""
        import subprocess as _sp, pty, select, re as _re

        if not _ANTIGRAVITY_LOGIN_SESSION_LOCK.acquire(blocking=False):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            msg = f"event: error\ndata: {json.dumps({'error': 'Antigravity login session is already active'}, ensure_ascii=False)}\n\n"
            try:
                self.wfile.write(msg.encode())
                self.wfile.flush()
            except Exception:
                pass
            return

        q: queue.Queue = queue.Queue(maxsize=200)
        proc_box = {"proc": None, "master_fd": None}
        stop_event = threading.Event()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def run_login():
            master_fd = None
            proc = None
            try:
                if antigravity_setup is None:
                    raise RuntimeError("antigravity helpers unavailable")
                if not antigravity_setup.is_installed():
                    raise RuntimeError("Antigravity CLI is not installed")
                master_fd, slave_fd = pty.openpty()
                proc_box["master_fd"] = master_fd
                with _ANTIGRAVITY_LOGIN_PTY_LOCK:
                    _ANTIGRAVITY_LOGIN_PTY_FD[0] = master_fd

                env = os.environ.copy()
                env["HOME"] = antigravity_setup.home_dir()
                env["TERM"] = "dumb"
                proc = _sp.Popen(
                    [antigravity_setup.binary_path(), "auth", "login"],
                    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                    close_fds=True, env=env
                )
                proc_box["proc"] = proc
                os.close(slave_fd)

                ansi = _re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|[0-9])")
                buf = ""
                state = {"sent_method": False, "url_found": False, "sent_code_wait": False}
                deadline = time.time() + 600

                while time.time() < deadline and not stop_event.is_set():
                    try:
                        r, _, _ = select.select([master_fd], [], [], 0.5)
                        if not r:
                            if proc.poll() is not None:
                                break
                            if antigravity_setup.is_authenticated():
                                _stop_antigravity_process(proc, master_fd=master_fd, use_ctrl_d=True)
                                break
                            continue

                        raw = os.read(master_fd, 4096).decode("utf-8", errors="replace")
                        clean = ansi.sub("", raw).replace("\\r\\n", "\\n").replace("\\r", "\\n")
                        buf += clean

                        while "\\n" in buf:
                            line, buf = buf.split("\\n", 1)
                            _antigravity_login_handle_line(line, state, master_fd, q)
                    except (OSError, IOError):
                        break
                    if proc.poll() is not None:
                        break

                if not stop_event.is_set():
                    rc = proc.poll()
                    q.put(("done", rc if rc is not None else 0))
            except Exception as e:
                if not stop_event.is_set():
                    q.put(("error", str(e)))
            finally:
                with _ANTIGRAVITY_LOGIN_PTY_LOCK:
                    _ANTIGRAVITY_LOGIN_PTY_FD[0] = None
                _stop_antigravity_process(proc, master_fd=master_fd, use_ctrl_d=True)
                try:
                    _ANTIGRAVITY_LOGIN_SESSION_LOCK.release()
                except Exception:
                    pass
                if master_fd is not None:
                    try:
                        os.close(master_fd)
                    except OSError:
                        pass

        threading.Thread(target=run_login, daemon=True).start()

        try:
            while True:
                try:
                    etype, data = q.get(timeout=2)
                    if etype == "url":
                        msg = f"event: url\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    elif etype == "waiting_code":
                        msg = f"event: waiting_code\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    elif etype == "done":
                        msg = f"event: done\ndata: {json.dumps({'code': data})}\n\n"
                    else:
                        msg = f"event: error\ndata: {json.dumps({'error': data}, ensure_ascii=False)}\n\n"
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
                    if etype in ("done", "error"):
                        break
                except queue.Empty:
                    if stop_event.is_set():
                        break
                    self.wfile.write(b":" + b" ping" + b"\n\n")
                    self.wfile.flush()
        except Exception:
            stop_event.set()
            _stop_antigravity_process(proc_box["proc"], master_fd=proc_box["master_fd"], use_ctrl_d=True)
        finally:
            stop_event.set()
            _stop_antigravity_process(proc_box["proc"], master_fd=proc_box["master_fd"], use_ctrl_d=True)


    def _serve_setup_antigravity_input(self, payload: dict):
        with _ANTIGRAVITY_LOGIN_PTY_LOCK:
            fd = _ANTIGRAVITY_LOGIN_PTY_FD[0]
        if fd is None:
            self.send_json({"error": "no active Antigravity login session"}, 400)
            return
        input_text = payload.get("input")
        text = (payload.get("text") or "")
        key = (payload.get("key") or "").strip().lower()
        if input_text is not None:
            os.write(fd, str(input_text).encode("utf-8"))
            self.send_json({"ok": True})
            return
        if text:
            os.write(fd, text.encode("utf-8") + b"\r")
            self.send_json({"ok": True})
            return
        keymap = {
            "enter": b"\r",
            "return": b"\r",
            "tab": b"\t",
            "esc": b"\x1b",
            "escape": b"\x1b",
            "up": b"\x1b[A",
            "down": b"\x1b[B",
            "right": b"\x1b[C",
            "left": b"\x1b[D",
            "space": b" ",
            "ctrl_c": b"\x03",
        }
        if key not in keymap:
            self.send_json({"error": "text or supported key is required"}, 400)
            return
        os.write(fd, keymap[key])
        self.send_json({"ok": True})

    def send_json(self, obj, status: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = self._strip_ingress(parsed.path)

        if path in ("/", ""):
            self.serve_index()
        elif path == "/style.css":
            self.serve_file("style.css", "text/css")
        elif path == "/app.js":
            self.serve_file("app.js", "application/javascript")
        elif path.startswith("/vendor/"):
            # 同梱アセット（CodeMirror・Lora フォント等）。パストラバーサル防止。
            rel = os.path.normpath(path[len("/vendor/"):])
            if rel.startswith("..") or os.path.isabs(rel):
                self.send_error(403)
            else:
                _ctypes = {".css": "text/css", ".js": "application/javascript",
                           ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf"}
                ctype = _ctypes.get(os.path.splitext(rel)[1], "application/octet-stream")
                self.serve_file(os.path.join("vendor", rel), ctype)
        elif path == "/api/messages":
            qs = parse_qs(parsed.query)
            room = qs.get("room", ["chat"])[0]
            limit = int(qs.get("limit", ["300"])[0])
            msgs = get_chat_messages(limit) if room == "chat" else get_soliloquy_messages(limit)
            self.send_json(msgs)
        elif path == "/api/status":
            with _agent_status_lock:
                self.send_json(dict(_agent_status))
        elif path == "/api/events":
            self._serve_sse()
        elif path == "/api/audio-events":
            qs = parse_qs(parsed.query)
            limit = int(qs.get("limit", ["300"])[0])
            self.send_json(read_jsonl(NON_SPEECH_AUDIO_EVENTS_LOG, limit))
        elif path == "/api/audio-event-tags":
            qs = parse_qs(parsed.query)
            limit = int(qs.get("limit", ["300"])[0])
            self.send_json(read_jsonl(AUDIO_EVENT_TAGS_LOG, limit))
        elif path.startswith("/api/audio-events/") and path.endswith("/wav"):
            event_id = path[len("/api/audio-events/"):-len("/wav")].strip("/")
            if not event_id or not all(c.isalnum() or c in "-_" for c in event_id):
                self.send_error(400, "Invalid event ID")
                return
            events = read_jsonl(NON_SPEECH_AUDIO_EVENTS_LOG, 1000)
            wav_path = None
            for ev in reversed(events):
                if ev.get("event_id") == event_id:
                    wav_path = ev.get("wav_ref")
                    break
            if not wav_path:
                wav_path = os.path.join(WAV_DIR, f"{event_id}.wav")
            wav_path = os.path.normpath(wav_path)
            expected_prefix = os.path.normpath(WAV_DIR)
            if not wav_path.startswith(expected_prefix + os.sep) and wav_path != expected_prefix:
                self.send_error(403, "Forbidden")
                return
            if not os.path.exists(wav_path):
                self.send_error(404, "Not Found")
                return
            try:
                with open(wav_path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", len(data))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.send_error(500, str(e))
        elif path == "/api/setup/status":
            self.send_json({"authenticated": is_authenticated(), "antigravity": antigravity_status()})
        elif path == "/api/setup/antigravity/status":
            self.send_json(antigravity_status())
        elif path == "/api/setup/antigravity/install":
            self._serve_setup_antigravity_install()
        elif path == "/api/setup/antigravity/login":
            self._serve_setup_antigravity_login()
        elif path == "/api/setup/login":
            self._serve_setup_login()
        elif path == "/api/preferences":
            filepath = PREFS_FILE
            if not os.path.exists(filepath) and os.path.exists(PREFS_EXAMPLE_FILE):
                filepath = PREFS_EXAMPLE_FILE
            try:
                with open(filepath, encoding="utf-8") as f:
                    data = json.load(f)
                self.send_json(data)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == "/api/character":
            filepath = CHARACTER_FILE
            if not os.path.exists(filepath):
                filepath = os.path.join(SCRIPT_DIR, "character.md")
            try:
                with open(filepath, encoding="utf-8") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", len(data.encode("utf-8")))
                self.end_headers()
                self.wfile.write(data.encode("utf-8"))
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == "/api/ha-entities":
            qs = parse_qs(parsed.query)
            domain_str = qs.get("domain", [""])[0]
            if not domain_str:
                self.send_json({"error": "domain is required"}, 400)
                return
            domains = [d.strip() for d in domain_str.replace("|", ",").split(",") if d.strip()]
            try:
                entities = get_ha_entities(domains)
                self.send_json(entities)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == "/api/stt-info":
            qs = parse_qs(parsed.query)
            provider = qs.get("provider", [""])[0].strip()
            if not provider:
                self.send_json({"languages": []})
            else:
                status, raw = ha_api_raw_request(f"/stt/{provider}")
                if status == 200:
                    try:
                        self.send_json(json.loads(raw))
                    except Exception as e:
                        print(f"[web] /api/stt-info invalid JSON for provider={provider}: {e}; body={raw[:500]}", flush=True)
                        self.send_json({"languages": [], "error": "invalid JSON from HA STT API"}, 502)
                else:
                    print(f"[web] /api/stt-info failed for provider={provider}: status={status} body={raw[:500]}", flush=True)
                    self.send_json({"languages": [], "error": f"HTTP {status}", "detail": raw[:500]}, 502)
        elif path == "/api/extra-context":
            filepath = EXTRA_CONTEXT_FILE
            if not os.path.exists(filepath):
                filepath = os.path.join(SCRIPT_DIR, "extra_context.conf")
            content = ""
            if os.path.exists(filepath):
                try:
                    with open(filepath, encoding="utf-8") as f:
                        content = f.read()
                except Exception as e:
                    self.send_json({"error": str(e)}, 500)
                    return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", len(content.encode("utf-8")))
            self.end_headers()
            self.wfile.write(content.encode("utf-8"))
        else:
            self.send_error(404)

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = self._strip_ingress(parsed.path)

        if path == "/api/preferences":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body_raw = self.rfile.read(length)
                body = json.loads(body_raw.decode("utf-8"))
                if not isinstance(body, dict) or len(body) == 0:
                    self.send_json({"error": "設定データが空か無効です"}, 400)
                    return
                atomic_write(PREFS_FILE, json.dumps(body, ensure_ascii=False, indent=2))
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == "/api/character":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body_raw = self.rfile.read(length)
                content = body_raw.decode("utf-8").strip()
                if len(content) < 10:
                    self.send_json({"error": "キャラクター定義が短すぎるか空です"}, 400)
                    return
                atomic_write(CHARACTER_FILE, content)
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = self._strip_ingress(parsed.path)

        if path == "/api/status":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
                status = body.get("status", "idle")
                source = body.get("source") or None
                if status not in ("thinking", "idle"):
                    self.send_json({"error": "invalid status"}, 400)
                    return
                set_agent_status(status, source)
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == "/api/read":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
                room = body.get("room", "chat")
                record = {"room": room, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
                read_path = os.path.join(LOG_DIR, "last_read.json")
                try:
                    existing = json.loads(open(read_path).read()) if os.path.exists(read_path) else {}
                except Exception:
                    existing = {}
                existing[room] = record["timestamp"]
                with open(read_path, "w", encoding="utf-8") as f:
                    json.dump(existing, f, ensure_ascii=False)
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == "/api/setup/login-code":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
                code = body.get("code", "").strip()
                with _login_pty_lock:
                    fd = _login_pty_fd[0]
                if fd is None:
                    self.send_json({"error": "no active login session"}, 400)
                    return
                if not code:
                    self.send_json({"error": "code is empty"}, 400)
                    return
                os.write(fd, (code + "\r").encode())
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == "/api/setup/antigravity/input" or path == "/api/setup/antigravity/login-code":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
                if path.endswith("login-code") and isinstance(body, dict) and "text" not in body and "code" in body:
                    body = {"text": body.get("code", "")}
                self._serve_setup_antigravity_input(body if isinstance(body, dict) else {})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == "/api/send":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
                message = body.get("message", "").strip()
                if not message:
                    self.send_json({"error": "empty"}, 400)
                    return
                source = body.get("source", "chat")
                threading.Thread(target=send_chat, args=(message, source), daemon=True).start()
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == "/api/audio-event-tags" or (path.startswith("/api/audio-events/") and path.endswith("/tags")):
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    self.send_json({"error": "invalid body"}, 400)
                    return
                route_event_id = ""
                if path.startswith("/api/audio-events/"):
                    route_event_id = path[len("/api/audio-events/"):-len("/tags")].strip("/")
                event_id = (body.get("event_id") or route_event_id or "").strip()
                tag_type = (body.get("type") or "manual").strip().lower()
                if not event_id:
                    self.send_json({"error": "event_id is required"}, 400)
                    return
                if tag_type not in {"manual", "gemini", "claude_audio", "rule", "other"}:
                    self.send_json({"error": "invalid type"}, 400)
                    return
                label = (body.get("label") or body.get("sound") or "").strip()
                disposition = (body.get("disposition") or "").strip().lower() or None
                if disposition not in {None, "ignore", "important", "notify", "silent_record"}:
                    self.send_json({"error": "invalid disposition"}, 400)
                    return
                candidates = body.get("candidates")
                if not label and not isinstance(candidates, list) and disposition is None:
                    self.send_json({"error": "label or candidates or disposition is required"}, 400)
                    return
                confidence = body.get("confidence")
                if confidence is None and tag_type == "manual":
                    confidence = 0.95
                row = {
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "event_id": event_id,
                    "type": tag_type,
                    "label": label or None,
                    "disposition": disposition,
                    "confidence": confidence,
                    "candidates": candidates if isinstance(candidates, list) else None,
                    "note": body.get("note"),
                    "actor": body.get("actor") or ("user" if tag_type == "manual" else tag_type),
                }
                append_jsonl(AUDIO_EVENT_TAGS_LOG, {k: v for k, v in row.items() if v is not None})
                notify_sse("update", {"room": "audio"})
                self.send_json({"ok": True, "tag": row})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == "/api/character/reset":
            default_path = os.path.join(SCRIPT_DIR, "character.md")
            target_path = CHARACTER_FILE
            
            if os.path.abspath(default_path) == os.path.abspath(target_path):
                self.send_json({"ok": True, "message": "Same path, reset bypassed"})
                return
                
            try:
                if not os.path.exists(default_path):
                    self.send_json({"error": "デフォルトの character.md が見つかりません"}, 404)
                    return
                with open(default_path, "r", encoding="utf-8") as f:
                    content = f.read()
                atomic_write(target_path, content)
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == "/api/extra-context":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body_raw = self.rfile.read(length)
                content = body_raw.decode("utf-8")
                atomic_write(EXTRA_CONTEXT_FILE, content)
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == "/api/speak-test":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
                room = (body.get("room") or "").strip()
                if not room:
                    self.send_json({"error": "room is required"}, 400)
                    return
                message = "スピーカーの接続テストです。"
                speak_py = os.path.join(SCRIPT_DIR, "speak.py")
                env = os.environ.copy()
                r = subprocess.run(
                    ["python3", speak_py, room, message],
                    capture_output=True, text=True, timeout=15, env=env
                )
                detail = (r.stdout or "").strip() or (r.stderr or "").strip()
                if r.returncode != 0:
                    self.send_json({"error": f"発話できませんでした: {detail}"}, 500)
                else:
                    self.send_json({"ok": True, "detail": detail})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        else:
            self.send_error(404)

    def _serve_sse(self):
        q: queue.Queue = queue.Queue(maxsize=50)
        with _sse_lock:
            _sse_clients.append(q)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            while True:
                try:
                    self.wfile.write(q.get(timeout=25))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # keepalive
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            with _sse_lock:
                if q in _sse_clients:
                    _sse_clients.remove(q)


if __name__ == "__main__":
    os.makedirs(LOG_DIR, exist_ok=True)
    threading.Thread(target=file_watcher, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"[web] Listening on :{PORT}", flush=True)
    server.serve_forever()
