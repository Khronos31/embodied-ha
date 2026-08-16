#!/usr/bin/env python3
"""ファイル読み取り MCP サーバー（embodied-ha 用）。

ツール:
  read_file  … 絶対パスを受け取り、許可されたテキスト内容を返す。
  view_image … 絶対パスを受け取り、許可された画像を画像として返す。

背景: codex/agy ハーネスは本環境(HAOS 非特権コンテナ)で bubblewrap サンドボックスを
初期化できず、シェル経由のファイル読み取り(cat 等)が bwrap エラーで全滅する
(2026-07-22 実測)。Claude Code の組み込み Read に相当する能力を、シェルを介さず
EHA 管理プロセスで安全に提供するのがこの MCP。--dangerously-bypass(=HA 全体到達)を
与えずに Read だけを最小権限で許すための薄いラッパー。

方針:
  - policy-read: `read_policy.py` と解決済み実パスの検査で認証・機密パスを拒否。
    Claude の native Read に設定する deny rule と対象を揃える。
  - secure-read: O_NOFOLLOW で開き(末端 symlink 拒否)、fstat で regular file 確認
    (fifo/device/dir を拒否=ブロッキング/副作用回避)、size cap で OOM 回避。
env: なし(パスは呼び出し引数)。
"""
import base64
import errno
import os
import stat

from mcp_lib import image, serve, text
from read_policy import read_deny_reason

# テキスト読み取りの上限。超過分は切り詰めて注記する(巨大ファイルでの OOM を避ける)。
MAX_READ_BYTES = 1024 * 1024  # 1 MiB

# 画像の上限。テキストより大きく取るが、切り詰めはしない(壊れた画像になるため)。
# base64 で約 4/3 に膨らんだうえでモデルのコンテキストへ載る点も踏まえた値。
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MiB

# 仮想ファイルシステム。/proc/<pid>/environ 等はプロセスの環境変数(SUPERVISOR_TOKEN 等の秘密)を
# NUL 区切りで返すため、read_file からは読ませない(Claude Code の Read も /proc/environ を拒否する=パリティ)。
# 判定は fstat 後に /proc/self/fd/<fd> の解決済み実パスで行い、中間 symlink 経由の到達も塞ぐ。
_DENY_REALPATH_PREFIXES = ("/proc/", "/sys/")


def _open_checked(raw_path, tool):
    """パス検査と安全な open をまとめる。``(fd, error)`` を返す。

    fd を返したときは呼び出し側が閉じる。エラー時は fd=None で、そのまま
    ハンドラの戻り値として使える content を返す。``tool`` は文言の接頭辞にだけ
    使い、判定は両ツールで完全に同一にする——「テキストなら読めないが画像なら
    読める」ような差を機密境界に持ち込まないため。
    """
    if not raw_path:
        return None, [text(f"{tool}: path が空です。読みたいファイルのパスを指定してください。")]
    if not os.path.isabs(raw_path):
        return None, [text(f"{tool}: 絶対パスを指定してください。")]
    reason = read_deny_reason(raw_path)
    if reason:
        return None, [text(f"{tool}: {reason}")]

    # O_NOFOLLOW: 末端が symlink なら拒否(想定外の場所への誘導を防ぐ)。
    # O_NONBLOCK: fifo/デバイスを O_RDONLY で開くとライタ待ちでブロックし得るため非ブロックで開き、
    #   下の fstat で regular file でないと分かった時点で弾く(通常ファイルには無影響)。
    try:
        fd = os.open(
            raw_path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError:
        return None, [text(f"{tool}: ファイルが見つかりません: {raw_path}")]
    except IsADirectoryError:
        return None, [text(f"{tool}: ディレクトリは読めません(ファイルを指定してください): {raw_path}")]
    except OSError as e:
        # ELOOP=symlink, EACCES=権限 等
        if e.errno == errno.ELOOP:
            return None, [text(f"{tool}: symlink は読めません(実体パスを指定してください): {raw_path}")]
        return None, [text(f"{tool}: 開けませんでした: {raw_path} ({e.strerror or e})")]

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            return None, [text(
                f"{tool}: 通常ファイルではありません(fifo/デバイス/ソケット等は不可): {raw_path}"
            )]

        # 開いた fd の解決済み実パスで /proc・/sys を拒否(procfs の environ は S_ISREG を通るため必須)。
        try:
            real = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            real = raw_path
        if real.startswith(_DENY_REALPATH_PREFIXES):
            os.close(fd)
            return None, [text(f"{tool}: 仮想ファイルシステム(/proc・/sys)は読めません: {raw_path}")]
        reason = read_deny_reason(real)
        if reason:
            os.close(fd)
            return None, [text(f"{tool}: {reason}")]
    except OSError as e:
        os.close(fd)
        return None, [text(f"{tool}: 読み取り失敗: {raw_path} ({e.strerror or e})")]
    return fd, None


def read_file(args):
    raw_path = (args.get("path") or "").strip()
    fd, error = _open_checked(raw_path, "read_file")
    if error is not None:
        return error, True

    try:
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


def _image_mime(header):
    """先頭バイトから画像形式を判定する。拡張子は信用しない。"""
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def view_image(args):
    """画像ファイルを画像 content block として返す。

    `read_file` と分けている理由:

    - **返り値の型が違う**。MCP の content block はテキストと画像で別物なので、
      1つのツールに両方を持たせると呼ぶ側が結果の形を予測できない。
    - **上限の性質が違う**。テキストは切り詰めても残りが読めるが、**画像は途中で
      切ると壊れたバイト列にしかならない**。だから truncate せず、超過は拒否する。
    - これが無いと `read_file` の「NUL を含めばバイナリとして拒否」が画像を巻き添えに
      する。agy は native `view_file` を塞いだ時点で `/config`・`/data` 配下の画像を
      読む手段を失っており（F-27・2.0.14）、その穴をここで埋める。

    機密パスの判定は `read_file` と完全に同じ経路を通る（`_open_checked`）。
    """
    raw_path = (args.get("path") or "").strip()
    fd, error = _open_checked(raw_path, "view_image")
    if error is not None:
        return error, True

    try:
        chunks = []
        remaining = MAX_IMAGE_BYTES
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        # 上限を埋めきってまだ読めるなら大きすぎる。壊れた画像を返すより拒否する。
        if remaining == 0 and os.read(fd, 1):
            return [text(
                f"view_image: 画像が大きすぎます(上限 約 {MAX_IMAGE_BYTES // (1024 * 1024)} MiB): {raw_path}"
            )], True
    except OSError as e:
        return [text(f"view_image: 読み取り失敗: {raw_path} ({e.strerror or e})")], True
    finally:
        os.close(fd)

    mime = _image_mime(data[:16])
    if mime is None:
        return [text(
            "view_image: 画像として認識できません(PNG・JPEG・GIF・WebP のみ)。"
            f"テキストなら read_file を使ってください: {raw_path}"
        )], True
    return [image(base64.b64encode(data).decode("ascii"), mime)]


if __name__ == "__main__":
    serve("files-mcp", "1.1", {
        "read_file": {
            "spec": {
                "name": "read_file",
                "description": (
                    "ファイルのパスを受け取り、その中身(テキスト)を返す。\n"
                    "絶対パスを指定する。\n"
                    "通常ファイルのみ(ディレクトリ・デバイス・symlink は不可)。\n"
                    "巨大ファイルは先頭のみ返す。バイナリは内容を表示しない。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "読みたいファイルの絶対パス",
                        },
                    },
                    "required": ["path"],
                },
            },
            "handler": read_file,
        },
        "view_image": {
            "spec": {
                "name": "view_image",
                "description": (
                    "画像ファイルのパスを受け取り、その画像を返す。\n"
                    "絶対パスを指定する。PNG・JPEG・GIF・WebP に対応する。\n"
                    "テキストファイルを読むときは read_file を使う。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "見たい画像ファイルの絶対パス",
                        },
                    },
                    "required": ["path"],
                },
            },
            "handler": view_image,
        },
    })
