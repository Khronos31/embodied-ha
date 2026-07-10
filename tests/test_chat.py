"""chat.py本体の小さなヘルパーの単体テスト（増分10、Codexレビュー対応）。

`_read_character`は、eha_config.pyがEHA_CHARACTER_FILEのパスを解決するだけで
内容を読んでいなかった回帰（全会話でキャラクター定義が空文字列になっていた
不具合）の修正対象。chat.sh:12の`cat "$EHA_CHARACTER_FILE" 2>/dev/null`と
同じく、読み取り失敗（ファイル無し等）はクラッシュさせず空文字列に丸める。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import chat  # type: ignore  # noqa: E402


class ReadCharacterTests(unittest.TestCase):
    def test_reads_existing_file_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            character_file = Path(tmp) / "character.md"
            character_file.write_text("私はあかね。特徴的な一文。", encoding="utf-8")
            result = chat._read_character(str(character_file))
        self.assertEqual(result, "私はあかね。特徴的な一文。")

    def test_missing_file_returns_empty_string(self):
        result = chat._read_character("/no/such/character.md")
        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
