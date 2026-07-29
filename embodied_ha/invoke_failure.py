"""invoke-agent の失敗を永続化し、連続失敗を数える。

**なぜ要るか**: 2026-07-27〜28 に claude の refresh token が失効し、
`invoke-agent` が毎回 `returncode=1 / stdout_empty` で即死して認知ループが
**21時間止まった**。気づいたのは翌日の人間の日次チェックで、仕組み側の検知はゼロだった。

失敗そのものは stderr に出ていたが、
- アドオンログは Supervisor のリングバッファなので**遡れなくなる**
- `loop.py` は stderr の**末尾400字だけ**を出しており、原因の先頭が落ちる

ため、後から「なぜ止まったか」を辿れなかった。ここで JSONL に残し、連続回数を数える。

状態は2ファイルに分ける:
- `invoke_failures.jsonl` … 失敗1件ごとの記録（追記のみ・上限あり）
- `invoke_failure_state.json` … 連続失敗カウンタ（成功で消える）
"""
import json
import os
from datetime import datetime

from state_utils import file_lock, read_json

FAILURES_FILE = "invoke_failures.jsonl"
STATE_FILE = "invoke_failure_state.json"

# 追記のみのファイルが無制限に伸びないよう、書き込み時に古い行を落とす。
# 失敗は本来まれなので、直近500件あれば原因追跡には足りる。
MAX_FAILURE_LINES = 500

# stderr は先頭と末尾の両方を残す。末尾だけだと、
# ハーネスが長い transcript を stderr へ流す経路（run_claude）で原因の先頭が落ちる。
STDERR_HEAD_CHARS = 2000
STDERR_TAIL_CHARS = 2000

# 何回続けて落ちたら人間へ上げるか。30分間隔のループなら 3 回 ≒ 1.5 時間。
DEFAULT_ALERT_THRESHOLD = 3


def _path(log_dir: str, name: str) -> str:
    # log_dir を推測して既定値を持たせない。サーバーごとに別々の既定を持って
    # 静かに別の場所へ書いた前例（findings F-35）があるため、空なら書かない。
    if not (log_dir or "").strip():
        raise ValueError("log_dir が空です")
    return os.path.join(log_dir, name)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clip_stderr(stderr: str) -> str:
    """原因が読める形へ切り詰める。落とすなら「落とした」と分かる形にする。"""
    text = (stderr or "").strip()
    if len(text) <= STDERR_HEAD_CHARS + STDERR_TAIL_CHARS:
        return text
    dropped = len(text) - STDERR_HEAD_CHARS - STDERR_TAIL_CHARS
    return (
        text[:STDERR_HEAD_CHARS]
        + f"\n…（中略 {dropped} 文字）…\n"
        + text[-STDERR_TAIL_CHARS:]
    )


def record_failure(log_dir: str, *, source: str, mode: str = "", returncode: int | None = None,
                   stdout_empty: bool | None = None, stderr: str = "", harness: str = "") -> None:
    """失敗1件を `invoke_failures.jsonl` へ残す。失敗しても呼び出し元は止めない。"""
    row = {
        "timestamp": _now_iso(),
        "source": source,
        "mode": mode,
        "harness": harness,
        "returncode": returncode,
        "stdout_empty": stdout_empty,
        "stderr": clip_stderr(stderr),
    }
    try:
        path = _path(log_dir, FAILURES_FILE)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with file_lock(path):
            lines = []
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        lines = [line for line in f if line.strip()]
                except OSError:
                    # 読めないときは既存を消さない。追記に切り替える。
                    with open(path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    return
            lines = lines[-(MAX_FAILURE_LINES - 1):]
            lines.append(json.dumps(row, ensure_ascii=False) + "\n")
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.replace(tmp, path)
    except Exception as e:
        # 記録の失敗でループを落とさない。ただし黙らない。
        print(f"[invoke_failure] 失敗記録を書けませんでした（log_dir={log_dir!r}）: {e}", flush=True)


def read_state(log_dir: str) -> dict:
    try:
        path = _path(log_dir, STATE_FILE)
    except ValueError:
        return {}
    state = read_json(path, {})
    if not isinstance(state, dict):
        return {}
    return state


def mark_failure(log_dir: str, *, source: str, detail: str = "") -> dict:
    """連続失敗を1つ進めて、更新後の状態を返す。"""
    path = _path(log_dir, STATE_FILE)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with file_lock(path):
        state = read_json(path, {})
        if not isinstance(state, dict):
            state = {}
        consecutive = int(state.get("consecutive") or 0) + 1
        state = {
            "consecutive": consecutive,
            "first_failed_at": state.get("first_failed_at") or _now_iso(),
            "last_failed_at": _now_iso(),
            "last_source": source,
            "last_detail": detail,
            "alerted_at": state.get("alerted_at") or "",
        }
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, path)
    return state


def mark_success(log_dir: str) -> None:
    """1回でも通ったら連続失敗を解除する。"""
    path = _path(log_dir, STATE_FILE)
    if not os.path.exists(path):
        return
    with file_lock(path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def mark_alerted(log_dir: str) -> None:
    """通知済みの印を付ける。同じ連続失敗で何度も通知しないため。"""
    path = _path(log_dir, STATE_FILE)
    with file_lock(path):
        state = read_json(path, {})
        if not isinstance(state, dict) or not state:
            return
        state["alerted_at"] = _now_iso()
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, path)


def alert_threshold(environ: dict | None = None) -> int:
    env = environ if environ is not None else os.environ
    try:
        value = int((env.get("EHA_INVOKE_FAILURE_ALERT_THRESHOLD") or "").strip())
    except (TypeError, ValueError):
        return DEFAULT_ALERT_THRESHOLD
    return max(1, value)


def should_alert(state: dict, *, threshold: int) -> bool:
    """しきい値に達し、まだこの連続失敗で通知していないか。"""
    if not isinstance(state, dict):
        return False
    if int(state.get("consecutive") or 0) < threshold:
        return False
    return not (state.get("alerted_at") or "")


def alert_message(state: dict) -> str:
    consecutive = int(state.get("consecutive") or 0)
    since = state.get("first_failed_at") or "不明"
    detail = (state.get("last_detail") or "").strip()
    text = (
        f"自律ループの起動に{consecutive}回続けて失敗しています（最初の失敗: {since}）。"
        "ハーネスの認証が切れている可能性があります。Web UI で再ログインしてください。"
    )
    if detail:
        text += f"\n直近のエラー: {detail}"
    return text
