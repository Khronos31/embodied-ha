"""出荷コードに個体名・居住者名・maintainer handle をベタ書きさせない hard gate。

Why: 複数エージェント(Claude/Codex/Antigravity)が編集するたびに user-facing 文字列へ
個体名(例: あなたのエージェント名)をハードコードする混入が何度掃除しても再発する
(2026-07-23 に game-mcp の勝敗メッセージ等で発覚)。多ハーネス(別ハーネスの個体・無名
default 個体)で誤り、かつ公開 repo に persona/居住者名が漏れる。動的値
(EHA_CHARACTER_NAME / resident_name)を使うこと。

★禁止名そのものは**このテストにも公開リポジトリにも書かない**(それ自体が persona 漏れに
なる=本末転倒)。禁止名は gitignore 済みの非公開ファイル `tests/persona_names.local`
(1行1件)から読み込む。雛形は `tests/persona_names.example`。fork は自分の名前で
`.local` を作れば同じガードが使える。`.local` が無ければ skip(maintainer のローカル実行が
実ゲート。日次 grep=[[feedback-daily-static-analysis]] section 8 が backstop)。
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "embodied_ha"
NAMES_FILE = Path(__file__).resolve().parent / "persona_names.local"
SCAN_SUFFIXES = {".py", ".sh", ".js", ".md", ".json", ".yaml"}


def _shipped_files():
    """公開=git 追跡下の embodied_ha/ ファイルだけを対象にする(gitignore された
    ローカル資産まで拾って false-positive を出さない)。git が無ければ rglob へ fallback。"""
    try:
        out = subprocess.run(
            ["git", "ls-files", "embodied_ha"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        return [ROOT / p for p in out.stdout.splitlines() if p]
    except Exception:
        return list(SHIPPED.rglob("*"))


def _load_forbidden_names():
    """非公開リストを読む。無ければ None(=skip)。"""
    if not NAMES_FILE.exists():
        return None
    names = []
    for line in NAMES_FILE.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            names.append(s)
    return names or None


class NoHardcodedPersonaNamesTests(unittest.TestCase):
    def test_shipped_code_has_no_hardcoded_persona_names(self):
        names = _load_forbidden_names()
        if names is None:
            self.skipTest(
                "tests/persona_names.local が無い(gitignore 済・非公開)。個体名/居住者名を"
                "1行1件で作成すると、出荷コードへのハードコードを検出する。雛形: "
                "tests/persona_names.example"
            )
        pattern = re.compile("|".join(re.escape(n) for n in names), re.IGNORECASE)
        hits = []
        for path in sorted(_shipped_files()):
            if not path.is_file() or path.suffix.lower() not in SCAN_SUFFIXES:
                continue
            rel = path.relative_to(ROOT).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:100]}")
        self.assertEqual(
            hits,
            [],
            "出荷コードに個体名/居住者名のハードコードがあります(動的値へ):\n" + "\n".join(hits),
        )


if __name__ == "__main__":
    unittest.main()
