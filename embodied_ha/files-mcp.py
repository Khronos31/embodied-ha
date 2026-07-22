#!/usr/bin/env python3
"""ファイル読み取り MCP サーバー（embodied-ha 用）。

ツール:
  read_file … 絶対 or cwd 相対のパスを受け取り、テキスト内容を返す。

背景: codex/agy ハーネスは本環境(HAOS 非特権コンテナ)で bubblewrap サンドボックスを
初期化できず、シェル経由のファイル読み取り(cat 等)が bwrap エラーで全滅する
(2026-07-22 実測)。Claude Code の組み込み Read に相当する能力を、シェルを介さず
EHA 管理プロセスで安全に提供するのがこの MCP。--dangerously-bypass(=HA 全体到達)を
与えずに Read だけを最小権限で許すための薄いラッパー(ゆの案・2026-07-22)。

方針(ゆの決定 2026-07-22):
  - read-anything: パス制限はしない(Claude の native Read と同じ到達範囲=コンテナ内どこでも)。
  - secure-read: O_NOFOLLOW で開き(末端 symlink 拒否)、fstat で regular file 確認
    (fifo/device/dir を拒否=ブロッキング/副作用回避)、size cap で OOM 回避。
env: なし(パスは呼び出し引数)。
"""
import errno
import os
import stat

from mcp_lib import serve, text

# テキスト読み取りの上限。超過分は切り詰めて注記する(巨大ファイルでの OOM を避ける)。
MAX_READ_BYTES = 1024 * 1024  # 1 MiB

# 仮想ファイルシステム。/proc/<pid>/environ 等はプロセスの環境変数(SUPERVISOR_TOKEN 等の秘密)を
# NUL 区切りで返すため、read_file からは読ませない(Claude Code の Read も /proc/environ を拒否する=パリティ)。
# 判定は fstat 後に /proc/self/fd/<fd> の解決済み実パスで行い、中間 symlink 経由の到達も塞ぐ。
_DENY_REALPATH_PREFIXES = ("/proc/", "/sys/")


def read_file(args):
    raw_path = (args.get("path") or "").strip()
    if not raw_path:
        return [text("read_file: path が空です。読みたいファイルのパスを指定してください。")], True

    # O_NOFOLLOW: 末端が symlink なら拒否(想定外の場所への誘導を防ぐ)。
    # O_NONBLOCK: fifo/デバイスを O_RDONLY で開くとライタ待ちでブロックし得るため非ブロックで開き、
    #   下の fstat で regular file でないと分かった時点で弾く(通常ファイルには無影響)。
    try:
        fd = os.open(
            raw_path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError:
        return [text(f"read_file: ファイルが見つかりません: {raw_path}")], True
    except IsADirectoryError:
        return [text(f"read_file: ディレクトリは読めません(ファイルを指定してください): {raw_path}")], True
    except OSError as e:
        # ELOOP=symlink, EACCES=権限 等
        if e.errno == errno.ELOOP:
            return [text(f"read_file: symlink は読めません(実体パスを指定してください): {raw_path}")], True
        return [text(f"read_file: 開けませんでした: {raw_path} ({e.strerror or e})")], True

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return [text(f"read_file: 通常ファイルではありません(fifo/デバイス/ソケット等は不可): {raw_path}")], True

        # 開いた fd の解決済み実パスで /proc・/sys を拒否(procfs の environ は S_ISREG を通るため必須)。
        try:
            real = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            real = raw_path
        if real.startswith(_DENY_REALPATH_PREFIXES):
            return [text(f"read_file: 仮想ファイルシステム(/proc・/sys)は読めません: {raw_path}")], True

        # short read 対応: cap まで(または EOF まで)ループで読む。
        chunks = []
        remaining = MAX_READ_BYTES
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        # truncation は st_size でなく「cap を埋めた上でまだ読める」で判定(procfs 等 size 不定に強い)。
        probe = os.read(fd, 1) if remaining == 0 else b""
        truncated = bool(probe)
        # 境界直後の 1 byte が NUL ならバイナリ(NUL 判定に含める・cap 直後の取りこぼしを塞ぐ)。
        if probe == b"\x00":
            data += probe
    except OSError as e:
        return [text(f"read_file: 読み取り失敗: {raw_path} ({e.strerror or e})")], True
    finally:
        os.close(fd)

    # NUL を含むならバイナリ扱い(environ 等 NUL 区切りもここで二重に弾く。UTF-8 decode 素通り穴を塞ぐ)。
    if b"\x00" in data:
        return [text(f"read_file: バイナリファイル(NUL を含む)のため内容を表示できません: {raw_path}")], True

    # テキストとしてデコード。truncation で末尾のマルチバイト文字が分断された場合は、
    # 不完全な末尾数バイトを落として再デコードする(正当な UTF-8 をバイナリ誤判定しない)。
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        content = None
        if truncated:
            for cut in (1, 2, 3):
                try:
                    content = data[:-cut].decode("utf-8")
                    break
                except UnicodeDecodeError:
                    content = None
        if content is None:
            return [text(
                f"read_file: バイナリまたは非 UTF-8 ファイルのため内容を表示できません: {raw_path}"
            )], True

    if truncated:
        content += f"\n\n…(先頭 約 {MAX_READ_BYTES} バイトで切り詰めました)"
    return [text(content)]


if __name__ == "__main__":
    serve("files-mcp", "1.0", {
        "read_file": {
            "spec": {
                "name": "read_file",
                "description": (
                    "ファイルのパスを受け取り、その中身(テキスト)を返す。\n"
                    "絶対パス、または現在の作業ディレクトリからの相対パスを指定する。\n"
                    "通常ファイルのみ(ディレクトリ・デバイス・symlink は不可)。\n"
                    "巨大ファイルは先頭のみ返す。バイナリは内容を表示しない。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "読みたいファイルのパス(絶対 or cwd 相対)",
                        },
                    },
                    "required": ["path"],
                },
            },
            "handler": read_file,
        },
    })
