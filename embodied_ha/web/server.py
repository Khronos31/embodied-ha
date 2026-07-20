#!/usr/bin/env python3
"""Embodied HA Web UI サーバー。静的ファイル配信 + JSONL 読み取り API + SSE ライブ更新。"""
import importlib.util
import datetime as _dt
import json, os, subprocess, time, queue, threading, tempfile, sys, re, platform, shutil, uuid
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
try:
    import codex_setup  # type: ignore
except Exception:
    codex_setup = None
# claude_setupはagy/codexと違い、本番実績のある既存経路(旧/api/setup/*とログイン
# 完了検知)が依存する。import失敗を握り潰すと既存機能が黙って未認証扱いに退行する
# ため、必須importとして失敗時は起動時に大きく落とす(sol指摘 2026-07-18)。
import claude_setup  # type: ignore  # noqa: E402 (sys.path調整後のimportが必要)
import harness_state  # type: ignore  # noqa: E402 (sys.path調整後のimportが必要)
LOG_DIR    = os.environ.get("EHA_LOG_DIR", os.path.join(SCRIPT_DIR, "log"))
PORT       = int(os.environ.get("INGRESS_PORT", 8099))

CHAT_LOG = os.path.join(LOG_DIR, "chat_log.jsonl")
OBS_LOG  = os.path.join(LOG_DIR, "observations.jsonl")
OBS_RECOVERED_LOG = os.path.join(LOG_DIR, "observations_recovered.jsonl")
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

GAME_CATALOG = [
    {
        "id": "wiki6",
        "name": "Wiki6（Wikipedia旅）",
        "description": "Wikipediaのリンクだけを辿り、スタート記事からゴール記事に最短クリック数で到達する。標準同梱。",
        "bundled": True,
        "requires": [],
    },
    {
        "id": "wordvec_race",
        "name": "WordVecレース",
        "description": "基準語を決めて交互に単語を出す。前の単語より遠くなければ負け。chiVeモデル（約490MB）が必要。",
        "bundled": False,
        "requires": ["chiVe mc90モデル（~490MB）"],
    },
]

DATA_DIR = os.environ.get("EHA_DATA_DIR", SCRIPT_DIR)
HOME_POLICY_FILE = os.environ.get("EHA_HOME_POLICY_FILE", os.path.join(DATA_DIR, "home_policy.md"))
EXTRA_CONTEXT_FILE = os.path.join(DATA_DIR, "extra_context.conf")

CHIVE_DIR  = "/data/word2vec/chive-1.3-mc90_gensim"
CHIVE_URL  = "https://sudachi.s3-ap-northeast-1.amazonaws.com/chive/chive-1.3-mc90_gensim.tar.gz"
VOICEVOX_SONG_DIR = os.environ.get("EHA_VOICEVOX_CORE_DIR", "/data/voicevox_core")
VOICEVOX_CORE_VERSION = "0.16.4"
VOICEVOX_REQUIRED_FREE_BYTES = int(2.5 * 1024 * 1024 * 1024)

_install_status: dict[str, dict] = {
    "chive": {"status": "idle", "message": ""},
    "voicevox_song": {"status": "idle", "message": ""},
}
_install_status_lock = threading.Lock()
_install_locks = {"chive": threading.Lock(), "voicevox_song": threading.Lock()}


def _set_install_status(key: str, status: str, message: str) -> None:
    with _install_status_lock:
        _install_status[key] = {"status": status, "message": message}


def _get_install_status(key: str) -> dict:
    with _install_status_lock:
        return dict(_install_status.get(key, {"status": "idle", "message": ""}))


def _record_selected_harness(harness: str) -> None:
    """Persist a successful harness installation without failing its request."""
    try:
        harness_state.set_selected_harness(harness)
    except Exception:
        print(f"[web] failed to record selected harness: {harness}", flush=True)


def _start_install_thread(key: str, target) -> bool:
    lock = _install_locks[key]
    with lock:
        if _get_install_status(key).get("status") == "running":
            return False
        _set_install_status(key, "running", "開始中...")
        threading.Thread(target=target, daemon=True).start()
        return True


def _chive_installed() -> bool:
    return os.path.exists(os.path.join(CHIVE_DIR, "chive-1.3-mc90.kv"))


def _voicevox_song_installed() -> bool:
    try:
        import voicevox_song
        return voicevox_song.is_installed(VOICEVOX_SONG_DIR)
    except Exception:
        return False


def _check_data_disk_space(required_bytes: int = VOICEVOX_REQUIRED_FREE_BYTES) -> None:
    usage = shutil.disk_usage("/data" if os.path.exists("/data") else os.path.dirname(VOICEVOX_SONG_DIR) or "/")
    if usage.free < required_bytes:
        free_gb = usage.free / (1024 ** 3)
        need_gb = required_bytes / (1024 ** 3)
        raise RuntimeError(f"ディスク空き容量が不足しています(空き:{free_gb:.1f} GB, 必要:約{need_gb:.1f}GB)")


def _voicevox_release_artifacts() -> tuple[str, str]:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        wheel_arch = "x86_64"
        downloader = "download-linux-x64"
    elif machine in {"aarch64", "arm64"}:
        wheel_arch = "aarch64"
        downloader = "download-linux-arm64"
    else:
        raise RuntimeError(f"未対応アーキテクチャです: {platform.machine()}")
    base = f"https://github.com/VOICEVOX/voicevox_core/releases/download/{VOICEVOX_CORE_VERSION}"
    wheel = f"{base}/voicevox_core-{VOICEVOX_CORE_VERSION}-cp310-abi3-manylinux_2_34_{wheel_arch}.whl"
    return wheel, f"{base}/{downloader}"


def _run_install():
    try:
        # gensim — /data/python-packages に永続インストール（コンテナ再起動後も残る）
        _set_install_status("chive", "running", "gensim をインストール中...")
        pkg_dir = "/data/python-packages"
        os.makedirs(pkg_dir, exist_ok=True)
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--target", pkg_dir,
             "--no-cache-dir", "-q", "gensim"],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            _set_install_status("chive", "error", f"gensim インストール失敗: {r.stderr[:200]}")
            return

        # chiVe
        _set_install_status("chive", "running", "chiVe モデルをダウンロード中（約490MB）...")
        os.makedirs("/data/word2vec", exist_ok=True)
        tar_path = "/data/word2vec/chive-1.3-mc90_gensim.tar.gz"
        urllib.request.urlretrieve(CHIVE_URL, tar_path)

        _set_install_status("chive", "running", "展開中...")
        r2 = subprocess.run(
            ["tar", "xzf", tar_path, "-C", "/data/word2vec"],
            capture_output=True, timeout=120,
        )
        os.remove(tar_path)
        if r2.returncode != 0:
            _set_install_status("chive", "error", "展開失敗")
            return

        _set_install_status("chive", "done", "インストール完了")
    except Exception as e:
        _set_install_status("chive", "error", str(e)[:300])


def _run_voicevox_song_install():
    tmp_dir = f"{VOICEVOX_SONG_DIR}.tmp-{uuid.uuid4().hex}"
    try:
        _check_data_disk_space()
        wheel_url, downloader_url = _voicevox_release_artifacts()
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)
        os.makedirs(tmp_dir, exist_ok=True)

        downloads_dir = os.path.join(tmp_dir, "downloads")
        os.makedirs(downloads_dir, exist_ok=True)
        wheel_path = os.path.join(downloads_dir, os.path.basename(wheel_url))
        downloader_path = os.path.join(downloads_dir, "download")

        _set_install_status("voicevox_song", "running", "voicevox_core wheel をダウンロード中...")
        urllib.request.urlretrieve(wheel_url, wheel_path)

        _set_install_status("voicevox_song", "running", "voicevox_core をインストール中...")
        pkg_dir = os.path.join(tmp_dir, "python-packages")
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--target", pkg_dir, "--no-cache-dir", "-q", wheel_path],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            raise RuntimeError(f"voicevox_core インストール失敗: {(r.stderr or r.stdout)[:300]}")

        _set_install_status("voicevox_song", "running", "VOICEVOXモデル・辞書・onnxruntimeをダウンロード中（約1.7GB）...")
        urllib.request.urlretrieve(downloader_url, downloader_path)
        os.chmod(downloader_path, 0o755)
        r2 = subprocess.run(
            [downloader_path, "--only", "onnxruntime", "dict", "models", "-o", tmp_dir],
            input="y\n", capture_output=True, text=True, timeout=1800,
        )
        if r2.returncode != 0:
            raise RuntimeError(f"VOICEVOXデータ取得失敗: {(r2.stderr or r2.stdout)[:300]}")

        import voicevox_song
        if not voicevox_song.is_installed(tmp_dir):
            raise RuntimeError("VOICEVOX Song のインストール検証に失敗しました")

        if os.path.exists(VOICEVOX_SONG_DIR):
            shutil.rmtree(VOICEVOX_SONG_DIR)
        os.replace(tmp_dir, VOICEVOX_SONG_DIR)
        _set_install_status("voicevox_song", "done", "インストール完了")
    except Exception as e:
        try:
            if os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir)
        except Exception:
            pass
        _set_install_status("voicevox_song", "error", str(e)[:300])
LOUNGE_QUEUE_LOG = os.path.join(LOG_DIR, "ai_lounge_queue.jsonl")
LOUNGE_RESOLVED_LOG = os.path.join(LOG_DIR, "ai_lounge_log.jsonl")


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
_SELF_RESTART_DELAY_SECONDS = 0.5
_SELF_RESTART_MAX_ATTEMPTS = 3
_SELF_RESTART_RETRY_DELAY_SECONDS = 1.0
_self_restart_lock = threading.Lock()
_self_restart_scheduled = False

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
_ANTIGRAVITY_LOGIN_PTY_FD: list = [None]   # [int | None]
_ANTIGRAVITY_LOGIN_PTY_LOCK = threading.Lock()
_ANTIGRAVITY_LOGIN_SESSION_LOCK = threading.Lock()
_ANTIGRAVITY_INSTALL_LOCK = threading.Lock()
_CLAUDE_MUTATION_LOCK = threading.Lock()
_CODEX_INSTALL_LOCK = threading.Lock()
_CODEX_ACTIVE_OPERATION: list = [None]  # [str | None]
_CODEX_ACTIVE_OPERATION_LOCK = threading.Lock()
_CODEX_LOGIN_TIMEOUT = 16 * 60
_CODEX_LOGIN_POLL_INTERVAL = 1
_CODEX_LOGIN_QUEUE_PUT_TIMEOUT = 0.1
_ANTIGRAVITY_LOGIN_URL_RE = re.compile(r"https://[^\s\x00-\x1f]+")

# Setup mutation routes are intentionally centralized so new aliases cannot
# accidentally bypass the ingress-only boundary. Status reads are not included.
_SETUP_MUTATION_PATHS = frozenset({
    "/api/setup/login", "/api/setup/login-code",
    "/api/setup/claude/login", "/api/setup/claude/login-code",
    "/api/setup/claude/install", "/api/setup/claude/uninstall",
    "/api/setup/claude/clear-auth", "/api/setup/claude/logout",
    "/api/setup/antigravity/install", "/api/setup/antigravity/login",
    "/api/setup/antigravity/input", "/api/setup/antigravity/login-code",
    "/api/setup/antigravity/uninstall", "/api/setup/antigravity/clear-auth",
    "/api/setup/antigravity/logout",
    "/api/setup/codex/install", "/api/setup/codex/login",
    "/api/setup/codex/uninstall", "/api/setup/codex/clear-auth",
    "/api/setup/codex/logout",
})
_SETUP_GUARD_ERROR = "setup endpoints are only available via the Web UI (ingress)"


def setup_guard(client_address) -> bool:
    """Return whether a setup mutation is allowed for this peer address.

    172.30.32.2 is the fixed ingress-proxy source documented by Home Assistant.
    Verify it on the deployed appliance; if that assumption changes, recover
    with EHA_INGRESS_SOURCE (one or more comma-separated sources) or the
    EHA_SETUP_GUARD=off emergency override.  The override is deliberately read
    for every request so an add-on restart can recover from a bad deployment
    assumption.
    """
    if os.environ.get("EHA_SETUP_GUARD", "").lower() == "off":
        return True
    sources = {
        source.strip()
        for source in os.environ.get("EHA_INGRESS_SOURCE", "172.30.32.2").split(",")
        if source.strip()
    }
    return client_address[0] in sources


def _codex_active_operation() -> str | None:
    with _CODEX_ACTIVE_OPERATION_LOCK:
        return _CODEX_ACTIVE_OPERATION[0]


def _acquire_codex_mutation(operation: str) -> bool:
    """Acquire Codex's shared mutation lock and record its actual operation.

    取得と操作名設定を同一クリティカルセクションで行う——分離すると
    「lock取得済み・操作名未設定」の瞬間にbusyメッセージが実操作名を
    含められない競合窓ができる(sol再レビュー指摘)。解放側も同様。
    """
    with _CODEX_ACTIVE_OPERATION_LOCK:
        if not _CODEX_INSTALL_LOCK.acquire(blocking=False):
            return False
        _CODEX_ACTIVE_OPERATION[0] = operation
        return True


def _release_codex_mutation() -> None:
    with _CODEX_ACTIVE_OPERATION_LOCK:
        _CODEX_ACTIVE_OPERATION[0] = None
        _CODEX_INSTALL_LOCK.release()


def _codex_busy_error() -> str:
    operation = _codex_active_operation()
    return f"Codex {operation or 'setup'} is running"


def _acquire_antigravity_destructive_locks() -> bool:
    """Exclude uninstall/clear-auth from both active Antigravity sessions."""
    if not _ANTIGRAVITY_INSTALL_LOCK.acquire(blocking=False):
        return False
    if _ANTIGRAVITY_LOGIN_SESSION_LOCK.acquire(blocking=False):
        return True
    _ANTIGRAVITY_INSTALL_LOCK.release()
    return False


def _release_antigravity_destructive_locks() -> None:
    _ANTIGRAVITY_LOGIN_SESSION_LOCK.release()
    _ANTIGRAVITY_INSTALL_LOCK.release()


def _request_addon_self_restart() -> bool:
    """Ask Supervisor to restart this add-on without exposing its token.

    成功で True、リトライ全滅で False を返すベストエフォート。Request 生成も
    retry の try 内に置くため、不正な EHA_SUPERVISOR_URL 等でも通常は False を返す
    (latch解放の最終的な保証は呼び出し元 request_later の finally が担う)。
    """
    supervisor_base = os.environ.get("EHA_SUPERVISOR_URL", "http://supervisor").rstrip("/")
    for attempt in range(_SELF_RESTART_MAX_ATTEMPTS):
        try:
            request = urllib.request.Request(
                f"{supervisor_base}/addons/self/restart",
                headers={"Authorization": f"Bearer {HA_TOKEN}"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10):
                return True
        except Exception:
            if attempt < _SELF_RESTART_MAX_ATTEMPTS - 1:
                time.sleep(_SELF_RESTART_RETRY_DELAY_SECONDS)
    print("[server] self-restart request failed", flush=True)
    return False


def _schedule_self_restart() -> None:
    """Request a restart after the logout response has had time to flush."""
    def request_later() -> None:
        # 成功時のみ latch を True のまま残す。それ以外の全経路(False返し・sleep/helper/
        # print の例外・BaseException)で latch を戻し、以後の再ログアウトで再試行できるように
        # する。latch解放は必ず finally で行い、内側のログ出力失敗より優先させる(airtight)。
        global _self_restart_scheduled
        ok = False
        try:
            try:
                time.sleep(_SELF_RESTART_DELAY_SECONDS)
                ok = _request_addon_self_restart()
            except Exception:
                print("[server] self-restart worker failed", flush=True)
        finally:
            if not ok:
                with _self_restart_lock:
                    _self_restart_scheduled = False

    global _self_restart_scheduled
    with _self_restart_lock:
        if _self_restart_scheduled:
            return
        _self_restart_scheduled = True
        try:
            threading.Thread(target=request_later, daemon=True).start()
        except Exception:
            _self_restart_scheduled = False
            raise


def _selected_harness_ready() -> bool:
    """Return whether the selected harness would boot after restart, without writing state.

    Read-only mirror of daemon.harness_ready().  The daemon remains the source
    of truth; unlike its legacy migration path, the web server never writes a
    grandfathered Claude selection flag.
    """
    selection_state, selected = harness_state.read_selection()
    if selection_state == "missing":
        selected = "claude" if claude_setup.is_authenticated() else None
    elif selection_state != "valid":
        return False
    if selected == "claude":
        return (
            claude_setup.is_authenticated()
            and claude_setup.resolve_claude_bin() is not None
        )
    if selected == "codex":
        return (
            codex_setup is not None
            and codex_setup.is_installed()
            and codex_setup.is_authenticated()
        )
    if selected == "agy":
        return (
            antigravity_setup is not None
            and antigravity_setup.is_installed()
            and antigravity_setup.is_authenticated()
        )
    return False


def is_authenticated() -> bool:
    """Compatibility wrapper for the existing Claude setup/login flow."""
    return claude_setup.is_authenticated()


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


def codex_status() -> dict:
    if codex_setup is None:
        return {
            "installed": False,
            "authenticated": False,
            "installing": False,
            "active_operation": None,
            "install_root": os.environ.get("EHA_CODEX_INSTALL_ROOT", "/data/codex-cli"),
            "home_dir": os.environ.get("EHA_CODEX_HOME", "/data/codex-home"),
            "binary_path": "",
            "auth_path": "",
        }
    state = codex_setup.state()
    operation = _codex_active_operation()
    state["installing"] = operation == "install"
    state["active_operation"] = operation
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



def _respond_terminal_queries(raw: bytes, master_fd: int):
    """ターミナルケーパビリティクエリに応答してアプリが待機状態にならないようにする。"""
    import re as _re
    # DECRQM query: \x1b[?<mode>$p → not recognized response
    for m in _re.finditer(rb'\x1b\[\?(\d+)\$p', raw):
        try:
            os.write(master_fd, b'\x1b[?' + m.group(1) + b';0$y')
        except OSError:
            pass
    # Kitty keyboard protocol query: \x1b[?u → no enhancements
    if rb'\x1b[?u' in raw:
        try:
            os.write(master_fd, b'\x1b[?0u')
        except OSError:
            pass
    # Primary device attributes: \x1b[c or \x1b[0c → VT100
    if rb'\x1b[c' in raw or rb'\x1b[0c' in raw:
        try:
            os.write(master_fd, b'\x1b[?1;0c')
        except OSError:
            pass


def _antigravity_login_handle_line(line: str, state: dict, master_fd, q: queue.Queue):
    """Antigravity login TUI の 1 行を解釈して自動応答する。"""
    line_lower = line.lower()

    if not state.get("sent_method"):
        if "google" in line_lower or ("1." in line and "oauth" in line_lower):
            print("[agy-login] method prompt, sending \\r", flush=True)
            os.write(master_fd, b"1\n")
            state["sent_method"] = True
            return

    if not state.get("url_found"):
        m = _ANTIGRAVITY_LOGIN_URL_RE.search(line)
        if m:
            print(f"[agy-login] URL found: {m.group(0)[:80]}...", flush=True)
            q.put(("url", {"url": m.group(0)}))
            state["url_found"] = True
            state["url_found_at"] = time.time()
        return

    if not state.get("sent_code_wait"):
        # URL lines contain "code" in query params (code_challenge=, response_type=code) — skip them
        if "https://" not in line and ("code" in line_lower or "authorization" in line_lower):
            print(f"[agy-login] code prompt detected: {repr(line[:80])}", flush=True)
            q.put(("waiting_code", {}))
            state["sent_code_wait"] = True
        return

    if "color scheme" in line_lower:
        print("[agy-login] color scheme prompt, sending \\r", flush=True)
        os.write(master_fd, b"\n")
        return

    if "terms of service" in line_lower or "terms" in line_lower:
        print("[agy-login] terms prompt, sending accept", flush=True)
        os.write(master_fd, b"\x1b[B\x1b[C\n")
        return

    if "trust" in line_lower and not state.get("auth_done"):
        print("[agy-login] trust prompt, sending \\r + marking done", flush=True)
        os.write(master_fd, b"\n")
        state["auth_done"] = True
        if antigravity_setup is not None:
            try:
                marker = antigravity_setup.auth_marker_path()
                os.makedirs(os.path.dirname(marker), exist_ok=True)
                open(marker, "w").close()
            except Exception:
                pass
        return

# --- SSE クライアント管理 ---
_sse_clients: list = []
_sse_lock = threading.Lock()

# --- ログインセッション（PTY fd をグローバルに保持してコード書き戻しに使う）---
_login_pty_fd: list = [None]   # [int | None]
_login_pty_lock = threading.Lock()

# --- エージェント稼働状態 ---
# status: "idle" | "thinking"
# source: "loop" | "explore" | "chat" | None
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





def _clean_text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _room_graph_path() -> str:
    return _clean_text(os.environ.get("EHA_ROOM_GRAPH_FILE")) or os.path.join(DATA_DIR, "floorplan_room_graph_draft.json")


def _load_room_graph() -> dict:
    try:
        with open(_room_graph_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _room_entries_from_graph(graph: dict) -> dict:
    value = graph.get("rooms")
    if not isinstance(value, dict):
        return {}
    rooms = {}
    for room_id, room_info in value.items():
        room_key = _clean_text(room_id)
        if not room_key or not isinstance(room_info, dict):
            continue
        item = dict(room_info)
        item.setdefault("display_name", room_key)
        rooms[room_key] = item
    return rooms


def _room_entries_from_preferences(prefs: dict) -> dict:
    rooms = {}
    explicit = prefs.get("rooms")
    if isinstance(explicit, dict):
        for room_id, room_info in explicit.items():
            room_key = _clean_text(room_id)
            if not room_key:
                continue
            item = dict(room_info) if isinstance(room_info, dict) else {}
            item.setdefault("display_name", room_key)
            rooms.setdefault(room_key, item)
    elif isinstance(explicit, list):
        for item in explicit:
            if not isinstance(item, dict):
                continue
            room_key = _clean_text(item.get("room") or item.get("id") or item.get("name"))
            if not room_key:
                continue
            room_item = dict(item)
            room_item.setdefault("display_name", room_key)
            rooms.setdefault(room_key, room_item)

    for key in ("cameras", "mics", "video_media", "audio_media", "speakers", "entities", "projection_targets"):
        value = prefs.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            room_key = _clean_text(item.get("room"))
            if not room_key or room_key in rooms:
                continue
            rooms[room_key] = {"display_name": room_key}
    return rooms


def get_body_rooms() -> dict:
    graph = _load_room_graph()
    rooms = _room_entries_from_graph(graph)
    prefs = _load_json_object(PREFS_FILE)
    for room_id, room_info in _room_entries_from_preferences(prefs).items():
        rooms.setdefault(room_id, room_info)
    ordered_rooms = {room_id: rooms[room_id] for room_id in sorted(rooms)}
    return {
        "rooms": ordered_rooms,
        "edges": graph.get("edges") if isinstance(graph.get("edges"), list) else [],
        "aliases_pending": graph.get("aliases_pending") if isinstance(graph.get("aliases_pending"), dict) else {},
        "assumptions": graph.get("assumptions") if isinstance(graph.get("assumptions"), list) else [],
        "questions_for_user": graph.get("questions_for_user") if isinstance(graph.get("questions_for_user"), list) else [],
        "room_graph_file": _room_graph_path(),
    }


def _load_json_object(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_prefs_for_update(path: str) -> tuple[dict, bool]:
    """RMW用のprefsロード。戻り値は (prefs, ok)。

    ファイル不在 -> ({}, True) で新規作成を許容する。
    存在するがパース不能、またはdictでない -> ({}, False) で書き込みを止める。
    """
    if not os.path.exists(path):
        return {}, True
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data, True
        return {}, False
    except Exception:
        return {}, False


def _load_game_enabled_map() -> dict[str, bool]:
    prefs = _load_json_object(PREFS_FILE)
    games = prefs.get("games", {})
    if not isinstance(games, dict):
        return {}
    plugins = games.get("plugins", {})
    if not isinstance(plugins, dict):
        return {}
    enabled_map: dict[str, bool] = {}
    for game_id, enabled in plugins.items():
        if isinstance(enabled, bool):
            enabled_map[str(game_id)] = enabled
    return enabled_map


def _build_game_catalog() -> list[dict]:
    enabled_map = _load_game_enabled_map()
    games: list[dict] = []
    for game in GAME_CATALOG:
        item = dict(game)
        item["enabled"] = enabled_map.get(game["id"], bool(game.get("bundled")))
        games.append(item)
    return games


def _load_lounge_module():
    path = os.path.join(SCRIPT_DIR, "lounge-mcp.py")
    spec = importlib.util.spec_from_file_location("embodied_ha_lounge_mcp", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("lounge-mcp.py を読み込めません")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _set_lounge_env_from_prefs() -> None:
    try:
        with open(PREFS_FILE, encoding="utf-8") as f:
            prefs = json.load(f)
    except Exception:
        prefs = {}
    ai_lounge = prefs.get("ai_lounge", {}) if isinstance(prefs, dict) else {}
    if not isinstance(ai_lounge, dict):
        ai_lounge = {}
    app_id = str(ai_lounge.get("app_id") or "").strip()
    installation_id = str(ai_lounge.get("installation_id") or "").strip()
    if app_id:
        os.environ["LOUNGE_APP_ID"] = app_id
    if installation_id:
        os.environ["LOUNGE_INSTALLATION_ID"] = installation_id


def get_lounge_queue() -> list:
    return [item for item in read_jsonl(LOUNGE_QUEUE_LOG, 1000) if item.get("status") == "pending"]


def get_lounge_log(limit: int = 20) -> list:
    return list(reversed(read_jsonl(LOUNGE_RESOLVED_LOG, limit)))


def get_chat_messages(limit: int = 300) -> list:
    """chat_log.jsonl を返す（{timestamp, source, claude, user}）。"""
    return read_jsonl(CHAT_LOG, limit)


def get_soliloquy_messages(limit: int = 300) -> list:
    """observations.jsonl + recovered + explore.jsonl + chat_log.jsonl の private をマージして返す。"""

    def _entry(row: dict, source: str, *, include_mode: bool = False):
        timestamp = row.get("timestamp", "")
        if not isinstance(timestamp, str) or not timestamp.strip():
            return None
        try:
            _dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except Exception:
            return None
        item = {
            "timestamp": timestamp,
            "source": source,
            "private": row.get("private", ""),
            "emotion": row.get("emotion", ""),
            "topic": row.get("topic"),
        }
        if include_mode:
            item["mode"] = row.get("mode", "")
        if isinstance(row.get("facts"), dict):
            item["facts"] = row.get("facts")
        if row.get("ungrounded_speech_claim"):
            item["ungrounded_speech_claim"] = True
        if row.get("recovered"):
            item["recovered"] = True
            item["recovered_from"] = row.get("recovered_from")
        return item

    messages = []
    seen_loop_timestamps = set()
    for row in read_jsonl(OBS_LOG):
        if row.get("private"):
            item = _entry(row, "loop")
            if item is not None:
                messages.append(item)
                seen_loop_timestamps.add(item["timestamp"])
    for row in read_jsonl(OBS_RECOVERED_LOG):
        if row.get("private"):
            item = _entry(row, "loop_recovered")
            if item is not None and item["timestamp"] not in seen_loop_timestamps:
                messages.append(item)
                seen_loop_timestamps.add(item["timestamp"])
    for row in read_jsonl(EXP_LOG):
        if row.get("private"):
            item = _entry(row, "explore", include_mode=True)
            if item is not None:
                messages.append(item)
    for row in read_jsonl(CHAT_LOG):
        if row.get("private"):
            item = _entry(row, "chat")
            if item is not None:
                messages.append(item)
    messages.sort(key=lambda d: d["timestamp"])
    return messages[-limit:]


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
               (OBS_LOG, ["soliloquy"]), (OBS_RECOVERED_LOG, ["soliloquy"]),
               (EXP_LOG, ["soliloquy"]),
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

    def _block_loopback_setup_mutation(self, path: str) -> bool:
        """Send the common ingress-only rejection for guarded setup routes."""
        if path not in _SETUP_MUTATION_PATHS or setup_guard(self.client_address):
            return False
        self.send_json({"error": _SETUP_GUARD_ERROR}, 403)
        return True

    def serve_index(self):
        """index.html に window.INGRESS_PATH を注入して返す。"""
        # 本番は HA ingress が X-Ingress-Path を付与する。ローカルプレビュー
        # (code-server の /proxy/<port>/ 経由) ではそのヘッダが来ないため、
        # EHA_BASE_PATH でベースパスを渡せるようにする（未設定なら従来どおり）。
        ingress_path = self.headers.get("X-Ingress-Path") or os.environ.get(
            "EHA_BASE_PATH", ""
        )
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

        if not _CLAUDE_MUTATION_LOCK.acquire(blocking=False):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(b'event: error\ndata: {"error": "Claude login is busy"}\n\n')
                self.wfile.flush()
            except Exception:
                pass
            return

        q: queue.Queue = queue.Queue(maxsize=200)
        worker_owns_lock = False

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
                _CLAUDE_MUTATION_LOCK.release()

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            threading.Thread(target=run_login, daemon=True).start()
            # From here the worker owns the lock until its finally block.  This
            # prevents a second login from overwriting the shared PTY FD.
            worker_owns_lock = True
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
        finally:
            if not worker_owns_lock:
                _CLAUDE_MUTATION_LOCK.release()

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
                env = antigravity_setup.subprocess_env()
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
                    if rc == 0:
                        _record_selected_harness("agy")
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

    def _serve_setup_codex_install(self):
        """Codex CLI を GitHub Releases から導入し、進捗を SSE 配信する。"""
        if not _acquire_codex_mutation("install"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            msg = f"event: error\ndata: {json.dumps({'error': _codex_busy_error()}, ensure_ascii=False)}\n\n"
            try:
                self.wfile.write(msg.encode())
                self.wfile.flush()
            except Exception:
                pass
            return

        worker_owns_lock = False
        try:
            q: queue.Queue = queue.Queue(maxsize=200)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            def run_install():
                try:
                    if codex_setup is None:
                        raise RuntimeError("Codex helpers unavailable")
                    result = codex_setup.install(progress=lambda text: q.put(("line", text)))
                    _record_selected_harness("codex")
                    q.put(("done", result))
                except Exception as e:
                    q.put(("error", str(e)))
                finally:
                    _release_codex_mutation()

            threading.Thread(target=run_install, daemon=True).start()
            worker_owns_lock = True
            while True:
                try:
                    etype, data = q.get(timeout=2)
                    if etype == "line":
                        msg = f"event: line\ndata: {json.dumps({'text': data}, ensure_ascii=False)}\n\n"
                    elif etype == "done":
                        msg = f"event: done\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
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
        finally:
            if not worker_owns_lock:
                _release_codex_mutation()

    def _serve_setup_claude_install(self):
        """Install Claude CLI from its verified release manifest via SSE."""
        if not _CLAUDE_MUTATION_LOCK.acquire(blocking=False):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            msg = f"event: error\ndata: {json.dumps({'error': 'Claude setup is busy'}, ensure_ascii=False)}\n\n"
            try:
                self.wfile.write(msg.encode())
                self.wfile.flush()
            except Exception:
                pass
            return

        worker_owns_lock = False
        try:
            q: queue.Queue = queue.Queue(maxsize=200)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            def run_install():
                try:
                    result = claude_setup.install(progress=lambda text: q.put(("line", text)))
                    _record_selected_harness("claude")
                    q.put(("done", result))
                except Exception as e:
                    q.put(("error", str(e)))
                finally:
                    _CLAUDE_MUTATION_LOCK.release()

            threading.Thread(target=run_install, daemon=True).start()
            worker_owns_lock = True
            while True:
                try:
                    etype, data = q.get(timeout=2)
                    if etype == "line":
                        msg = f"event: line\ndata: {json.dumps({'text': data}, ensure_ascii=False)}\n\n"
                    elif etype == "done":
                        msg = f"event: done\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
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
        finally:
            if not worker_owns_lock:
                _CLAUDE_MUTATION_LOCK.release()

    def _serve_setup_codex_login(self):
        """Start Codex device auth and stream its URL/code without using a PTY."""
        import subprocess as _sp

        if not _acquire_codex_mutation("login"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                msg = f"event: error\ndata: {json.dumps({'error': _codex_busy_error()})}\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
            except Exception:
                pass
            return

        proc = None
        reader = None
        error = None
        lines: queue.Queue = queue.Queue(maxsize=200)
        reader_done = threading.Event()
        reader_stop = threading.Event()
        reader_error = threading.Event()
        reader_error_text = []
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            if codex_setup is None:
                raise RuntimeError("Codex helpers unavailable")
            if not codex_setup.is_installed():
                raise RuntimeError("Codex CLI is not installed")
            os.makedirs(codex_setup.home_dir(), exist_ok=True)
            proc = _sp.Popen(
                [codex_setup.binary_path(), "login", "--device-auth"],
                stdin=_sp.DEVNULL, stdout=_sp.PIPE, stderr=_sp.STDOUT,
                text=True, bufsize=1, env=codex_setup.subprocess_env(),
            )

            def read_stdout():
                try:
                    assert proc is not None and proc.stdout is not None
                    for raw_line in proc.stdout:
                        for value in codex_setup.device_auth_values(raw_line):
                            while not reader_stop.is_set():
                                try:
                                    lines.put(("line", value), timeout=_CODEX_LOGIN_QUEUE_PUT_TIMEOUT)
                                    break
                                except queue.Full:
                                    pass
                except Exception as exc:
                    # The main loop owns process cleanup and SSE output.  Do not
                    # let reader failures turn into a full login-timeout wait.
                    reader_error_text.append(f"Codex login output reader failed: {exc}")
                    reader_error.set()
                    while not reader_stop.is_set():
                        try:
                            lines.put(("error", reader_error_text[0]),
                                      timeout=_CODEX_LOGIN_QUEUE_PUT_TIMEOUT)
                            break
                        except queue.Full:
                            pass
                finally:
                    reader_done.set()

            reader = threading.Thread(target=read_stdout, name="codex-login-reader", daemon=True)
            reader.start()
            deadline = time.monotonic() + _CODEX_LOGIN_TIMEOUT
            while True:
                if reader_error.is_set():
                    raise RuntimeError(reader_error_text[0])
                if codex_setup.is_authenticated():
                    self.wfile.write(b'event: done\ndata: {"authenticated": true}\n\n')
                    self.wfile.flush()
                    return
                try:
                    event, value = lines.get(timeout=_CODEX_LOGIN_POLL_INTERVAL)
                    if reader_error.is_set():
                        raise RuntimeError(reader_error_text[0])
                    if event == "error":
                        raise RuntimeError(value)
                    msg = f"event: line\ndata: {json.dumps({'text': value}, ensure_ascii=False)}\n\n"
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
                except queue.Empty:
                    # keepalive: 書き込みがないとクライアント切断を検知できず、
                    # 子プロセスとmutation lockがタイムアウトまで残る(実E2Eで確認)
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                if proc.poll() is not None and reader_done.is_set() and lines.empty():
                    # 承認成功時、codexはauth.jsonを書いて自ら即終了する(status 0、
                    # 実ブラウザ承認E2Eで確認)。死亡判定の前に認証完了を再確認しないと
                    # 「auth.jsonチェック→queue待ち→死亡チェック」の隙間で誤errorになる。
                    if codex_setup.is_authenticated():
                        self.wfile.write(b'event: done\ndata: {"authenticated": true}\n\n')
                        self.wfile.flush()
                        return
                    raise RuntimeError(f"Codex login exited before authentication completed (status {proc.returncode})")
                if time.monotonic() >= deadline:
                    raise RuntimeError("Codex device authentication timed out")
        except Exception as e:
            error = str(e)
        finally:
            # Wake a reader blocked on a full queue before waiting for it.
            reader_stop.set()
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except _sp.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            if reader is not None:
                reader.join()
            if proc is not None and proc.stdout is not None:
                proc.stdout.close()
            _release_codex_mutation()
        if error is not None:
            try:
                msg = f"event: error\ndata: {json.dumps({'error': error}, ensure_ascii=False)}\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
            except Exception:
                pass

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
                import fcntl as _fcntl, termios as _termios, struct as _struct
                _fcntl.ioctl(master_fd, _termios.TIOCSWINSZ, _struct.pack('HHHH', 24, 80, 0, 0))
                proc_box["master_fd"] = master_fd
                with _ANTIGRAVITY_LOGIN_PTY_LOCK:
                    _ANTIGRAVITY_LOGIN_PTY_FD[0] = master_fd

                env = os.environ.copy()
                env["HOME"] = antigravity_setup.home_dir()
                env["TERM"] = "xterm-256color"
                proc = _sp.Popen(
                    [antigravity_setup.binary_path()],
                    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                    close_fds=True, env=env
                )
                proc_box["proc"] = proc
                os.close(slave_fd)

                ansi = _re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|[0-9])")
                buf = ""
                state = {"sent_method": False, "url_found": False, "sent_code_wait": False}
                deadline = time.time() + 600
                # Keys to try in order to dismiss the URL pager (one per 0.5s timeout)
                _PAGER_DISMISS_KEYS = [b"q", b"\x1b", b" ", b"\r", b"G"]
                pager_dismiss_idx = [0]

                while time.time() < deadline and not stop_event.is_set():
                    try:
                        r, _, _ = select.select([master_fd], [], [], 0.5)
                        if not r:
                            if proc.poll() is not None:
                                break
                            if state.get("auth_done") or antigravity_setup.is_authenticated():
                                _stop_antigravity_process(proc, master_fd=master_fd, use_ctrl_d=True)
                                break
                            # After URL found, try to dismiss URL pager so code entry prompt appears
                            if state.get("url_found") and not state.get("sent_code_wait"):
                                url_age = time.time() - state.get("url_found_at", deadline)
                                if url_age > 1.5:
                                    idx = pager_dismiss_idx[0]
                                    if idx < len(_PAGER_DISMISS_KEYS):
                                        key = _PAGER_DISMISS_KEYS[idx]
                                        print(f"[agy-login] pager dismiss attempt {idx}: {repr(key)}", flush=True)
                                        try:
                                            os.write(master_fd, key)
                                        except OSError:
                                            pass
                                        pager_dismiss_idx[0] += 1
                            continue

                        raw_bytes = os.read(master_fd, 4096)
                        _respond_terminal_queries(raw_bytes, master_fd)
                        raw = raw_bytes.decode("utf-8", errors="replace")
                        clean = ansi.sub("", raw).replace("\r\n", "\n").replace("\r", "\n")
                        if clean.strip():
                            print(f"[agy-login] PTY: {repr(clean[:200])}", flush=True)
                        buf += clean

                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            _antigravity_login_handle_line(line, state, master_fd, q)

                        # Scan incomplete buf for code entry prompt (TUI prompt may lack trailing newline)
                        if state.get("url_found") and not state.get("sent_code_wait"):
                            buf_lower = buf.lower()
                            if ("code" in buf_lower or "authorization" in buf_lower) and "https://" not in buf:
                                print(f"[agy-login] code prompt in buf: {repr(buf[:80])}", flush=True)
                                q.put(("waiting_code", {}))
                                state["sent_code_wait"] = True
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

        if self._block_loopback_setup_mutation(path):
            return

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
        elif path == "/api/body/rooms":
            try:
                self.send_json(get_body_rooms())
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
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
        elif path == "/api/lounge-pem-status":
            pem_path = "/config/embodied-ha/github_app.pem"
            self.send_json({"exists": os.path.exists(pem_path)})
        elif path == "/api/lounge-queue":
            self.send_json(get_lounge_queue())
        elif path == "/api/lounge-log":
            qs = parse_qs(parsed.query)
            limit = int(qs.get("limit", ["20"])[0])
            self.send_json(get_lounge_log(limit))
        elif path == "/api/games":
            games = _build_game_catalog()
            for g in games:
                if g["id"] == "wordvec_race":
                    g["model_installed"] = _chive_installed()
            self.send_json({"games": games})
        elif path == "/api/games/install-status":
            self.send_json(_get_install_status("chive"))
        elif path == "/api/voicevox_song/status":
            status = _get_install_status("voicevox_song")
            installed = _voicevox_song_installed()
            if installed and status.get("status") == "idle":
                status["message"] = "インストール済み"
            self.send_json({"installed": installed, **status})
        elif path == "/api/voicevox_song/singers":
            if not _voicevox_song_installed():
                self.send_json([])
                return
            try:
                import voicevox_song
                self.send_json(voicevox_song.list_singers(VOICEVOX_SONG_DIR))
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
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
        elif path == "/api/setup/codex/status":
            self.send_json(codex_status())
        elif path == "/api/setup/claude/status":
            self.send_json(claude_setup.state())
        elif path == "/api/setup/antigravity/install":
            self._serve_setup_antigravity_install()
        elif path == "/api/setup/antigravity/login":
            self._serve_setup_antigravity_login()
        elif path in ("/api/setup/login", "/api/setup/claude/login"):
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
        elif path == "/api/home-policy":
            filepath = HOME_POLICY_FILE
            if not os.path.exists(filepath):
                filepath = os.path.join(SCRIPT_DIR, "home_policy.md")
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
        elif path == "/api/home-policy":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body_raw = self.rfile.read(length)
                content = body_raw.decode("utf-8")
                atomic_write(HOME_POLICY_FILE, content)
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        else:
            self.send_error(404)

    def _serve_setup_logout(self, harness: str) -> None:
        """Clear one harness's credentials, then restart into setup-waiting state."""
        if harness == "claude":
            helpers = claude_setup
            def acquire_lock():
                return _CLAUDE_MUTATION_LOCK.acquire(blocking=False)
            release_lock = _CLAUDE_MUTATION_LOCK.release
            busy_error = "Claude setup is busy"
            unavailable_error = "Claude helpers unavailable"
        elif harness == "codex":
            helpers = codex_setup
            def acquire_lock():
                return _acquire_codex_mutation("logout")
            release_lock = _release_codex_mutation
            busy_error = _codex_busy_error
            unavailable_error = "Codex helpers unavailable"
        elif harness == "agy":
            helpers = antigravity_setup
            acquire_lock = _acquire_antigravity_destructive_locks
            release_lock = _release_antigravity_destructive_locks
            busy_error = "Antigravity setup is busy"
            unavailable_error = "antigravity helpers unavailable"
        else:
            self.send_json({"error": "unknown harness"}, 500)
            return

        if helpers is None:
            self.send_json({"error": unavailable_error}, 500)
            return
        if not acquire_lock():
            self.send_json({"error": busy_error() if callable(busy_error) else busy_error}, 409)
            return
        try:
            try:
                result = helpers.clear_auth()
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                return
            if result.get("errors"):
                self.send_json({"ok": False, **result}, 500)
                return
            if _selected_harness_ready():
                # claudeで認証が残る=APIキー(アドオン設定 claude_api_key)保持のときだけ
                # 構成タブ案内を出す。APIキーが無いのに非選択claudeをlogoutした等では
                # 誤誘導になるため、汎用の「稼働中ハーネス不変」文言を使う。
                message = (
                    "サブスクからログアウトしました。APIキーは無効化されません。"
                    "構成タブから削除してください。"
                    if harness == "claude" and claude_setup.is_authenticated()
                    else "ログアウトしました。稼働中のハーネスに変更がないため再起動しません。"
                )
                self.send_json({
                    "ok": True,
                    "restarting": False,
                    "message": message,
                    **result,
                }, 200)
                return
            _schedule_self_restart()
            message = (
                "サブスクからログアウトしました。アドオンを再起動します。"
                "再起動後はセットアップ待ちになります。"
                if harness == "claude"
                else "ログアウトしました。アドオンを再起動します。"
                "再起動後はセットアップ待ちになります。"
            )
            self.send_json({
                "ok": True,
                "restarting": True,
                "message": message,
                **result,
            }, 200)
        finally:
            release_lock()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = self._strip_ingress(parsed.path)

        if self._block_loopback_setup_mutation(path):
            return

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
        elif path in ("/api/setup/login-code", "/api/setup/claude/login-code"):
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
        elif path == "/api/setup/claude/install":
            self._serve_setup_claude_install()
        elif path == "/api/setup/claude/uninstall":
            if not _CLAUDE_MUTATION_LOCK.acquire(blocking=False):
                self.send_json({"error": "Claude setup is busy"}, 409)
                return
            try:
                result = claude_setup.uninstall()
                self.send_json({"ok": True, **result})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            finally:
                _CLAUDE_MUTATION_LOCK.release()
        elif path == "/api/setup/claude/clear-auth":
            if not _CLAUDE_MUTATION_LOCK.acquire(blocking=False):
                self.send_json({"error": "Claude login is busy"}, 409)
                return
            try:
                result = claude_setup.clear_auth()
                if result.get("errors"):
                    # 部分成功を握り潰さない: 消えたファイルと失敗理由を両方返す
                    self.send_json({"ok": False, **result}, 500)
                else:
                    self.send_json({"ok": True, **result})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            finally:
                _CLAUDE_MUTATION_LOCK.release()
        elif path == "/api/setup/claude/logout":
            self._serve_setup_logout("claude")
        elif path == "/api/setup/antigravity/input" or path == "/api/setup/antigravity/login-code":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
                if path.endswith("login-code") and isinstance(body, dict) and "text" not in body and "code" in body:
                    body = {"text": body.get("code", "")}
                self._serve_setup_antigravity_input(body if isinstance(body, dict) else {})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == "/api/setup/antigravity/uninstall":
            if not _acquire_antigravity_destructive_locks():
                self.send_json({"error": "Antigravity setup is busy"}, 409)
                return
            try:
                if antigravity_setup is None:
                    self.send_json({"error": "antigravity helpers unavailable"}, 500)
                    return
                result = antigravity_setup.uninstall()
                self.send_json({"ok": True, **result})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            finally:
                _release_antigravity_destructive_locks()
        elif path == "/api/setup/antigravity/clear-auth":
            if not _acquire_antigravity_destructive_locks():
                self.send_json({"error": "Antigravity setup is busy"}, 409)
                return
            try:
                if antigravity_setup is None:
                    self.send_json({"error": "antigravity helpers unavailable"}, 500)
                    return
                result = antigravity_setup.clear_auth()
                self.send_json({"ok": True, **result})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            finally:
                _release_antigravity_destructive_locks()
        elif path == "/api/setup/antigravity/logout":
            self._serve_setup_logout("agy")
        elif path == "/api/setup/codex/install":
            self._serve_setup_codex_install()
        elif path == "/api/setup/codex/login":
            self._serve_setup_codex_login()
        elif path == "/api/setup/codex/uninstall":
            if not _acquire_codex_mutation("uninstall"):
                self.send_json({"error": _codex_busy_error()}, 409)
                return
            try:
                if codex_setup is None:
                    self.send_json({"error": "Codex helpers unavailable"}, 500)
                    return
                result = codex_setup.uninstall()
                self.send_json({"ok": True, **result})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            finally:
                _release_codex_mutation()
        elif path == "/api/setup/codex/clear-auth":
            if not _acquire_codex_mutation("clear-auth"):
                self.send_json({"error": _codex_busy_error()}, 409)
                return
            try:
                if codex_setup is None:
                    self.send_json({"error": "Codex helpers unavailable"}, 500)
                    return
                result = codex_setup.clear_auth()
                self.send_json({"ok": True, **result})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            finally:
                _release_codex_mutation()
        elif path == "/api/setup/codex/logout":
            self._serve_setup_logout("codex")
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
        elif path.startswith("/api/lounge-queue/") and path.endswith("/approve"):
            item_id = path[len("/api/lounge-queue/"):-len("/approve")].strip("/")
            if not item_id:
                self.send_json({"error": "id is required"}, 400)
                return
            try:
                _set_lounge_env_from_prefs()
                result = _load_lounge_module().approve_queue_item(item_id)
                notify_sse("update", {"room": "lounge"})
                self.send_json({"ok": True, "item": result})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path.startswith("/api/lounge-queue/") and path.endswith("/reject"):
            item_id = path[len("/api/lounge-queue/"):-len("/reject")].strip("/")
            if not item_id:
                self.send_json({"error": "id is required"}, 400)
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                reason = body.get("reason") if isinstance(body, dict) else None
                result = _load_lounge_module().reject_queue_item(item_id, reason)
                notify_sse("update", {"room": "lounge"})
                self.send_json({"ok": True, "item": result})
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
        elif path == "/api/lounge-pem":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
                pem_content = (body.get("pem") or "").strip()
                if not pem_content:
                    self.send_json({"error": "pem フィールドが空です"}, 400)
                    return
                if "PRIVATE KEY" not in pem_content:
                    self.send_json({"error": "有効な秘密鍵ファイルではありません"}, 400)
                    return
                pem_dir = "/config/embodied-ha"
                os.makedirs(pem_dir, exist_ok=True)
                pem_path = os.path.join(pem_dir, "github_app.pem")
                atomic_write(pem_path, pem_content + "\n")
                os.chmod(pem_path, 0o600)
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == "/api/games/toggle":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    self.send_json({"error": "invalid body"}, 400)
                    return
                game_id = str(body.get("id") or "").strip()
                if not game_id:
                    self.send_json({"error": "id is required"}, 400)
                    return
                if game_id not in {game["id"] for game in GAME_CATALOG}:
                    self.send_json({"error": "invalid id"}, 400)
                    return
                enabled = body.get("enabled")
                if not isinstance(enabled, bool):
                    self.send_json({"error": "enabled must be boolean"}, 400)
                    return
                prefs, ok = _load_prefs_for_update(PREFS_FILE)
                if not ok:
                    self.send_json({"error": "preferences.json が読み込めないため設定変更を中止しました（ファイル破損の可能性）"}, 500)
                    return
                games = prefs.get("games")
                if not isinstance(games, dict):
                    games = {}
                    prefs["games"] = games
                plugins = games.get("plugins")
                if not isinstance(plugins, dict):
                    plugins = {}
                    games["plugins"] = plugins
                plugins[game_id] = enabled
                atomic_write(PREFS_FILE, json.dumps(prefs, ensure_ascii=False, indent=2))
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == "/api/games/install":
            if not _start_install_thread("chive", _run_install):
                self.send_json({"ok": True, "message": "already running"})
                return
            self.send_json({"ok": True})
        elif path == "/api/games/uninstall":
            try:
                if os.path.exists(CHIVE_DIR):
                    shutil.rmtree(CHIVE_DIR)
                _set_install_status("chive", "idle", "")
                prefs, ok = _load_prefs_for_update(PREFS_FILE)
                if not ok:
                    self.send_json({"error": "preferences.json が読み込めないため設定変更を中止しました（ファイル破損の可能性）"}, 500)
                    return
                prefs.setdefault("games", {}).setdefault("plugins", {})["wordvec_race"] = False
                atomic_write(PREFS_FILE, json.dumps(prefs, ensure_ascii=False, indent=2))
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == "/api/voicevox_song/install":
            if _voicevox_song_installed():
                _set_install_status("voicevox_song", "done", "インストール済み")
                self.send_json({"ok": True, "message": "already installed"})
                return
            try:
                _check_data_disk_space()
            except Exception as e:
                msg = str(e)[:300]
                _set_install_status("voicevox_song", "error", msg)
                self.send_json({"error": msg}, 507)
                return
            if not _start_install_thread("voicevox_song", _run_voicevox_song_install):
                self.send_json({"ok": True, "message": "already running"})
                return
            self.send_json({"ok": True})
        elif path == "/api/voicevox_song/uninstall":
            try:
                if os.path.exists(VOICEVOX_SONG_DIR):
                    shutil.rmtree(VOICEVOX_SONG_DIR)
                _set_install_status("voicevox_song", "idle", "")
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
