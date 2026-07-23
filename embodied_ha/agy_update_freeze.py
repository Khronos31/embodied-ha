"""Antigravity CLI(agy) の自動更新を凍結する（Phase 1: /etc/hosts リダイレクトのみ）。

agy は起動時に更新チェック用の別プロセス(bg-updater)を spawn し、更新ホストへ
manifest を取りに行く。勝手にバージョンが上がると本番稼働中のエージェントの挙動が
変わるため、更新ホストを 127.0.0.1 へ向けて到達不能(即 ECONNREFUSED)にし、凍結する。

Phase 1 の設計根拠(2026-07-20、Fable 実機レビュー): 更新チェックは bg-updater 別プロセスに
隔離されており、失敗してもフォアグラウンドのターンには影響しない(SCS 上の agy は
update_status.json が長期間 "Update failed" のまま正常稼働している実証がある)。したがって
ダミー HTTPS サーバ・自己署名 cert・SSL_CERT_FILE バンドルは不要で、hosts 1 行で足りる可能性が
高い。実 agy での「ターンが壊れないか」の確認はデプロイゲート繰り越し。壊れると観測された
場合のみダミーサーバ方式(Phase 2)へ格上げする(設計は [[embodied-ha-agent-setup-step3-phase0-spec]])。

宛先は 127.0.0.1 固定(ブラックホール IP はタイムアウトまでハングするため不可)。
/etc/hosts は Docker がランタイムで bind-mount するため、`sed -i` や tempfile+os.replace 等の
rename 方式は "Device or resource busy" で失敗する。よって同一 inode への truncate 書き戻し
("w") しか使えない。これは厳密には atomic ではない(truncate 後・書き込み前にプロセスが死ぬと
hosts が壊れうる)が、rename が使えない以上ここでの最善。窓は数十バイト・数マイクロ秒で、
書き込む内容は既存行のサブセット。プロセス間の add/remove は flock で直列化し、重複行や
読み書きレースを防ぐ(同一プロセス内の複数書き手・手動 CLI・Docker 側追記との競合を想定)。
"""
from __future__ import annotations

import fcntl
import os
import sys

UPDATE_HOST = "antigravity-cli-auto-updater-974169037036.us-central1.run.app"
REDIRECT_IP = "127.0.0.1"
MARKER = "# eha-agy-freeze"
HOSTS_PATH = "/etc/hosts"
_LOCK_PATH = "/tmp/eha-agy-freeze.lock"


def _redirect_line() -> str:
    return f"{REDIRECT_IP}\t{UPDATE_HOST}\t{MARKER}\n"


class _FileLock:
    """プロセス間で add/remove を直列化する flock ラッパー(fail-open)。"""

    def __init__(self, path: str = _LOCK_PATH):
        self._path = path
        self._fd = None

    def __enter__(self):
        try:
            self._fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except OSError:
            # ロックが取れない環境(権限・fs)でも凍結処理自体は続行する(fail-open)。
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
        return False


def _read_lines(hosts_path: str) -> list[str]:
    with open(hosts_path, encoding="utf-8") as f:
        return f.readlines()


def _is_redirect_line(line: str) -> bool:
    """自分が書いたリダイレクト行か(マーカー単独ではなく更新ホスト同伴で判定)。

    マーカー文字列だけの誤検知(コメント行・別用途行)を避けるため、更新ホスト名も要求する。
    """
    return MARKER in line and UPDATE_HOST in line


def is_redirect_active(hosts_path: str = HOSTS_PATH) -> bool:
    """マーカー付きのリダイレクト行が /etc/hosts に存在するか。"""
    try:
        return any(_is_redirect_line(line) for line in _read_lines(hosts_path))
    except FileNotFoundError:
        return False


def add_hosts_redirect(hosts_path: str = HOSTS_PATH) -> bool:
    """更新ホスト→127.0.0.1 のリダイレクトを冪等に追加する。追加したら True。

    既にリダイレクト行があれば何もしない。既存ファイルが末尾改行を欠く場合は先に改行を補い、
    直前行への連結を防ぐ。bind-mount された /etc/hosts へは "a"(追記)で in-place 書き込みできる。
    """
    with _FileLock():
        if is_redirect_active(hosts_path):
            return False
        try:
            with open(hosts_path, encoding="utf-8") as f:
                existing = f.read()
        except FileNotFoundError:
            existing = ""
        prefix = "\n" if existing and not existing.endswith("\n") else ""
        with open(hosts_path, "a", encoding="utf-8") as f:
            f.write(prefix + _redirect_line())
        return True


def remove_hosts_redirect(hosts_path: str = HOSTS_PATH) -> bool:
    """自分が書いたリダイレクト行を取り除く。取り除いたら True。

    install 時の一時解除や uninstall 後の後始末に使う。全文読み→リダイレクト行除外→"w" で
    同一ファイルへ truncate 書き戻し(rename しないので bind-mount でも成功。atomic ではないが
    rename 不可のため最善。flock で直列化)。
    """
    with _FileLock():
        try:
            lines = _read_lines(hosts_path)
        except FileNotFoundError:
            return False
        kept = [line for line in lines if not _is_redirect_line(line)]
        if len(kept) == len(lines):
            return False
        with open(hosts_path, "w", encoding="utf-8") as f:
            f.writelines(kept)
        return True


def reconcile(installed: bool, hosts_path: str = HOSTS_PATH) -> bool:
    """agy インストール状態に hosts を一致させる。add/remove のどちらを行ったか返す。

    run.sh(起動時)と web サーバ起動時の両方から呼び、Web だけ再起動した場合にも
    凍結状態を再確立する(watchdog が Web を再起動しても凍結が解けたままにならないように)。
    """
    return add_hosts_redirect(hosts_path) if installed else remove_hosts_redirect(hosts_path)


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else ""
    if action == "add":
        changed = add_hosts_redirect()
        print(f"[agy-freeze] hosts redirect {'added' if changed else 'already active'}: "
              f"{UPDATE_HOST} -> {REDIRECT_IP}")
        return 0
    if action == "remove":
        changed = remove_hosts_redirect()
        print(f"[agy-freeze] hosts redirect {'removed' if changed else 'not present'}")
        return 0
    if action == "status":
        print("active" if is_redirect_active() else "inactive")
        return 0
    print("usage: agy_update_freeze.py {add|remove|status}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
