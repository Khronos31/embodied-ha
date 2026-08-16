#!/usr/bin/env python3
"""「mainの公開状態はversionを伴うリリースでしか動かない」を機械で検査する。

検査する不変条件（正本はメモリ `embodied_ha_three_instance_release_procedure.md`
の「機械チェックの候補」節）:

1. mainへ入る変更が `embodied_ha/` を変えるなら、`config.yaml` の `version` 変更を伴うこと
2. 公開済みの版番号を再利用しないこと

2は実際に一度起きた事故への対策——2026-07-26に2.0.8が7分間だけ別内容で二度公開された。

⚠️ **タグは使わない。** このリポジトリにはタグが1つも無いため、版番号の履歴は
`main` の first-parent を辿って `config.yaml` から復元する。first-parent に限るのは、
「実際にmainに載って公開された版」だけを既発行として数えるため（マージされずに
終わったリリース候補ブランチの番号は、公開されていないので再利用を禁じない）。

使い方:
    check_release_invariants.py --base <ref> [--head <ref>] [--repo <path>]
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

CONFIG_PATH = "embodied_ha/config.yaml"
ADDON_PREFIX = "embodied_ha/"
VERSION_RE = re.compile(r'^version:\s*"?([^"\s]+)"?\s*$', re.MULTILINE)
EMPTY_TREE_SHA = "0" * 40


class CheckError(Exception):
    """検査そのものが実行できない（履歴が足りない等）。"""


def git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CheckError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def version_at(repo: str, rev: str) -> str | None:
    """指定リビジョンの config.yaml の version。ファイルが無ければ None。"""
    try:
        content = git(repo, "show", f"{rev}:{CONFIG_PATH}")
    except CheckError:
        return None
    match = VERSION_RE.search(content)
    return match.group(1) if match else None


def changed_paths(repo: str, base: str, head: str) -> list[str]:
    output = git(repo, "diff", "--name-only", f"{base}..{head}")
    return [line for line in output.splitlines() if line]


def published_versions(repo: str, rev: str) -> list[str]:
    """rev までの first-parent 履歴に載った版番号を、新しい順で返す。"""
    revisions = git(
        repo, "log", "--first-parent", "--format=%H", rev, "--", CONFIG_PATH
    ).split()
    seen: list[str] = []
    for revision in revisions:
        version = version_at(repo, revision)
        if version and version not in seen:
            seen.append(version)
    return seen


def check(repo: str, base: str, head: str) -> tuple[list[str], list[str]]:
    """(errors, notes) を返す。"""
    errors: list[str] = []
    notes: list[str] = []

    base_version = version_at(repo, base)
    head_version = version_at(repo, head)
    if head_version is None:
        raise CheckError(f"{head} に {CONFIG_PATH} の version が見つからない")

    addon_paths = [p for p in changed_paths(repo, base, head) if p.startswith(ADDON_PREFIX)]

    if addon_paths and base_version == head_version:
        listed = "\n".join(f"    {path}" for path in addon_paths[:10])
        more = f"\n    ... 他 {len(addon_paths) - 10} 件" if len(addon_paths) > 10 else ""
        errors.append(
            f"{ADDON_PREFIX} が変わっているのに version が {head_version} のまま。\n"
            "  同じ版番号で中身が違う状態を作らないこと（リリース手順の不変条件1）。\n"
            f"{listed}{more}"
        )
    elif not addon_paths:
        notes.append(f"{ADDON_PREFIX} の変更なし。version は {head_version} のままでよい。")

    if base_version != head_version:
        already = published_versions(repo, base)
        if head_version in already:
            errors.append(
                f"version {head_version} は既にmainへ公開済み。\n"
                "  公開済みの版番号を再利用しないこと（リリース手順の不変条件2）。\n"
                "  2026-07-26に2.0.8が別内容で二度公開された事故と同じ形。"
            )
        else:
            notes.append(f"version {base_version} → {head_version}（未使用の番号）")
            if already and not _is_newer(head_version, already[0]):
                notes.append(
                    f"⚠️ 直前の公開版 {already[0]} より大きくない。意図的な番号なら無視してよい。"
                )

    return errors, notes


def _is_newer(candidate: str, previous: str) -> bool:
    def parts(value: str) -> tuple:
        return tuple(int(x) if x.isdigit() else x for x in re.split(r"[.\-+]", value))

    try:
        return parts(candidate) > parts(previous)
    except TypeError:
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="変更前のリビジョン")
    parser.add_argument("--head", default="HEAD", help="変更後のリビジョン")
    parser.add_argument("--repo", default=".", help="リポジトリのパス")
    args = parser.parse_args()

    if args.base.strip(EMPTY_TREE_SHA[0]) == "":
        print("[release-invariants] 比較対象のbaseが無いので検査をスキップする。")
        return 0

    try:
        errors, notes = check(args.repo, args.base, args.head)
    except CheckError as exc:
        print(f"[release-invariants] 検査できない: {exc}", file=sys.stderr)
        return 2

    for note in notes:
        print(f"[release-invariants] {note}")
    for error in errors:
        print(f"[release-invariants] NG: {error}", file=sys.stderr)

    if errors:
        return 1
    print("[release-invariants] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
