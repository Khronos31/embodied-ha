#!/usr/bin/env python3
"""公開リポジトリに個人データ・秘密が混ざっていないかを検査する。

`AGENTS.md` は `personal_data/` を「excluded from public repo」と宣言している。
その宣言を、宣言のままにせず機械で守るための検査。追跡対象のファイル
（`git ls-files`）だけを見る——公開されるのはそこだから。

検査するもの:

1. **置いてはいけないパス** — 個人設定の実物、鍵、ログ。`.example` は対象外。
2. **秘密らしき文字列** — 各社APIキー、HAの長期アクセストークン、実体のある秘密鍵。
   テスト用の明らかなダミー（本体が短いPEM等）は落とさない。
3. **実名** — 一覧ファイルがあるときだけ。置き場は `tests/persona_names.local`、
   または環境変数 `EHA_PERSONA_NAMES_FILE`（worktreeごとに置き直さずに済む）。
   一覧の中で `!` から始まる行は除外パスのglob（LICENSEやREADMEのように、
   リポジトリの持ち主を名乗るのが当然のファイル向け）。**名前の一覧は公開
   リポジトリに置けない**ので、この段はローカル実行でのみ有効になる（CIでは
   スキップされる）。これは既存の設計を踏襲したもので、`.gitignore` が
   `tests/persona_names.local` と `tests/test_no_hardcoded_persona_names.py` を
   除外しているのと同じ考え方。

使い方:
    check_repo_hygiene.py [--repo PATH]
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

FORBIDDEN_DIRS = ("personal_data/", "song_library/", ".storage/", "embodied_ha/log/")
FORBIDDEN_NAMES = (
    "preferences.json",
    "extra_context.conf",
    "secrets.yaml",
    "secrets.json",
    ".env",
)
FORBIDDEN_SUFFIXES = (".pem", ".key", ".p12", ".pfx")

# 秘密の検出。プレフィクスは分割して書く——このファイル自身が検査対象なので、
# 自分の正規表現に自分で引っかからないようにするため。
SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("Anthropic APIキー", "sk-" + r"ant-[A-Za-z0-9_\-]{20,}"),
    ("OpenAI APIキー", r"\bsk-" + r"[A-Za-z0-9]{32,}"),
    ("GitHub token", r"\bgh" + r"p_[A-Za-z0-9]{20,}"),
    ("GitHub fine-grained token", r"\bgithub_" + r"pat_[A-Za-z0-9_]{20,}"),
    ("Google APIキー", "AIz" + r"a[0-9A-Za-z_\-]{30,}"),
    ("JWT（HAの長期アクセストークン等）", r"\bey" + r"J[A-Za-z0-9_\-]{10,}\.ey" + r"J[A-Za-z0-9_\-]{10,}"),
    ("ハードコードされたトークン値", r"SUPERVISOR_TOKEN\s*[:=]\s*[\"']?[A-Za-z0-9._\-]{20,}"),
)

# 実体のある秘密鍵だけを落とす。テストが使う "-----BEGIN PRIVATE KEY-----\ntest\n..." のような
# ダミーは、本体が短いので対象外になる。
PEM_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\\rn]*([A-Za-z0-9+/=\s\\rn]{100,})"
)

PERSONA_NAMES_FILE = "tests/persona_names.local"
PERSONA_NAMES_ENV = "EHA_PERSONA_NAMES_FILE"


def tracked_files(repo: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", repo, "ls-files"], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise SystemExit(f"git ls-files に失敗した: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line]


def path_violations(paths: list[str]) -> list[str]:
    problems = []
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        if any(path == d.rstrip("/") or path.startswith(d) for d in FORBIDDEN_DIRS):
            problems.append(f"{path}: 公開リポジトリに置かないディレクトリ配下")
        elif name in FORBIDDEN_NAMES:
            problems.append(f"{path}: 個人設定の実物（`.example` だけを追跡する）")
        elif any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            problems.append(f"{path}: 鍵ファイル")
    return problems


def _read_text(repo: str, path: str) -> str | None:
    try:
        return (Path(repo) / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def content_violations(repo: str, paths: list[str]) -> list[str]:
    compiled = [(label, re.compile(pattern)) for label, pattern in SECRET_PATTERNS]
    problems = []
    for path in paths:
        text = _read_text(repo, path)
        if text is None:
            continue
        for label, pattern in compiled:
            match = pattern.search(text)
            if match:
                line = text[: match.start()].count("\n") + 1
                problems.append(f"{path}:{line}: {label} らしき文字列")
        pem = PEM_PATTERN.search(text)
        if pem:
            line = text[: pem.start()].count("\n") + 1
            problems.append(f"{path}:{line}: 実体のありそうな秘密鍵")
    return problems


def persona_violations(repo: str, paths: list[str]) -> tuple[list[str], bool]:
    """(検出結果, 検査したか) を返す。名前の一覧が無ければ検査しない。"""
    # 一覧の置き場は環境変数で外に出せる。worktreeごとに置き直さずに済むため、
    # 「新しい作業コピーだけ検査が無効」という穴を塞げる。
    override = os.environ.get(PERSONA_NAMES_ENV, "").strip()
    names_path = Path(override) if override else Path(repo) / PERSONA_NAMES_FILE
    try:
        raw = names_path.read_text(encoding="utf-8")
    except OSError:
        return [], False

    # `!glob` の行は除外パス。公開リポジトリの持ち主を名乗るのが当然のファイル
    # （LICENSE・README）や、その語が出ないことを検査しているテストのため。
    names, exempt = [], []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        (exempt if line.startswith("!") else names).append(line.lstrip("!"))
    if not names:
        return [], False

    problems = []
    for path in paths:
        if path == PERSONA_NAMES_FILE:
            continue
        if any(fnmatch(path, pattern) for pattern in exempt):
            continue
        text = _read_text(repo, path)
        if text is None:
            continue
        for name in names:
            if name in text:
                line = text[: text.index(name)].count("\n") + 1
                problems.append(f"{path}:{line}: 実名らしき語")
                break
    return problems, True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="リポジトリのパス")
    args = parser.parse_args()

    paths = tracked_files(args.repo)
    problems = path_violations(paths) + content_violations(args.repo, paths)
    persona_problems, persona_checked = persona_violations(args.repo, paths)
    problems += persona_problems

    print(f"[repo-hygiene] 追跡ファイル {len(paths)} 件を検査した。")
    if not persona_checked:
        print(
            f"[repo-hygiene] 実名の検査はスキップした（{PERSONA_NAMES_FILE} も "
            f"${PERSONA_NAMES_ENV} も無い）。名前の一覧は公開リポジトリに置けないため、"
            "この段はローカル実行専用。"
        )

    for problem in problems:
        print(f"[repo-hygiene] NG: {problem}", file=sys.stderr)

    if problems:
        print(
            f"[repo-hygiene] {len(problems)} 件。公開してよいものか確認すること。",
            file=sys.stderr,
        )
        return 1
    print("[repo-hygiene] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
