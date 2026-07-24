"""chat.py/loop.py共通の環境変数デフォルト解決。

旧config.sh（削除済みchat.sh/loop.shが起動時にsourceしていた）の
ロジックをPythonへ移植したもの（[[embodied-ha-pythonize-chat-loop-design-2026-07-09]]
増分7）。bashの`${VAR:-default}`は「未設定または空文字列」のとき既定値を
使うため、Python側もdict.setdefault（キーの有無のみ判定）ではなく、
同じ「空文字列も未設定扱い」の判定を行う_env_defaultを使っている。
現行ランタイムの正本はこのモジュール。config.sh は移植検証用の履歴資料。
"""
import json
import os
import subprocess


def _env_default(environ, key, default):
    """bashの`${VAR:-default}`と同一（未設定・空文字列のどちらでも既定値を使う）。"""
    if not environ.get(key):
        environ[key] = default


def _build_extra_context(data_dir, run=subprocess.run):
    """extra_context.confの各行をbashコマンドとして実行し、出力を連結する（config.sh:42-53と同一）。"""
    if not data_dir:
        return ""
    conf_path = os.path.join(data_dir, "extra_context.conf")
    if not os.path.isfile(conf_path):
        return ""
    with open(conf_path, encoding="utf-8") as fh:
        lines = fh.readlines()
    outputs = []
    for raw_line in lines:
        line = raw_line.rstrip("\n")
        if line.strip().startswith("#"):
            continue
        if not line.strip():
            continue
        result = run(["bash", "-c", line], capture_output=True, text=True)
        outputs.append(result.stdout)
        outputs.append("")
    return "\n".join(outputs).strip("\n") if outputs else ""


def _build_policies(prefs_file):
    """preferences.json の policies を箇条書きにする（config.sh:56-70と同一）。"""
    if not (prefs_file and os.path.isfile(prefs_file)):
        return ""
    try:
        with open(prefs_file, encoding="utf-8") as fh:
            prefs = json.load(fh)
        lines = [f"- {p.strip()}" for p in prefs.get("policies", []) if isinstance(p, str) and p.strip()]
        return "\n".join(lines)
    except Exception:
        return ""


def load_config(script_dir=None, environ=None, run_extra_context=subprocess.run):
    """config.shが行うenv var解決を再現し、解決済みの辞書を返す（元のosenvironは変更しない）。"""
    resolved = dict(environ if environ is not None else os.environ)
    script_dir = script_dir or os.path.dirname(os.path.abspath(__file__))

    _env_default(resolved, "RESIDENT", "ユーザー")
    _env_default(resolved, "HA_URL", "http://supervisor/core/api")
    _env_default(resolved, "GO2RTC_BASE", "http://homeassistant.local:1984")
    _env_default(resolved, "CLAUDE_BIN", "claude")
    _env_default(resolved, "CLAUDE_CONFIG_DIR", "/config/.tools/claude-home")
    _env_default(resolved, "EHA_TOOLS_PATH", "/config/.tools/bin:/config/.tools/npm-global/bin:/config/.tools/node/bin")
    _env_default(resolved, "EHA_ANTIGRAVITY_HOME", "/data/")
    _env_default(resolved, "EHA_ANTIGRAVITY_BIN_DIR", os.path.join(resolved["EHA_ANTIGRAVITY_HOME"], "bin"))
    _env_default(resolved, "EHA_ANTIGRAVITY_BIN", os.path.join(resolved["EHA_ANTIGRAVITY_BIN_DIR"], "agy"))

    data_dir = resolved.get("EHA_DATA_DIR") or "/config/embodied-ha"
    _env_default(resolved, "EHA_NEXT_LISTEN_REQUEST_FILE", os.path.join(data_dir, "runtime", "next_listen_request.json"))
    _env_default(resolved, "EHA_NEXT_LISTEN_LOG_FILE", os.path.join(data_dir, "log", "next_listen_log.jsonl"))
    _env_default(resolved, "EHA_AUDIO_SESSION_BIN", "agy")
    _env_default(resolved, "EHA_AUDIO_SESSION_MODEL", "Gemini 3.5 Flash (High)")
    _env_default(resolved, "EHA_MEMORY_DIR", "/config/.tools/claude-home/projects/-config/memory")
    _env_default(resolved, "EHA_PREFS_FILE", os.path.join(script_dir, "preferences.json"))
    _env_default(resolved, "EHA_CHARACTER_FILE", os.path.join(script_dir, "character.md"))

    resolved["EXTRA_CONTEXT"] = _build_extra_context(resolved.get("EHA_DATA_DIR"), run=run_extra_context)
    resolved["POLICIES"] = _build_policies(resolved.get("EHA_PREFS_FILE"))

    return resolved
