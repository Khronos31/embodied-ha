import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS_PATH = ROOT / "embodied_ha" / "web" / "app.js"

class WebUITests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(APP_JS_PATH.exists(), f"{APP_JS_PATH} does not exist")
        self.app_js = APP_JS_PATH.read_text(encoding="utf-8")

    def test_badge_helpers_for_chat_and_voice(self):
        """getBadgeText と getBadgeClass が chat と voice に対して正しいラベル/クラスを返し、自律モードに対しては空を返すこと"""
        
        def parse_helper(func_name):
            pattern = r"function\s+" + func_name + r"\s*\(\s*type\s*\)\s*\{([\s\S]*?)\}"
            match = re.search(pattern, self.app_js)
            self.assertIsNotNone(match, f"Function {func_name} not found in app.js")
            body = match.group(1)
            
            cases = {}
            current_cases = []
            default_val = None
            
            for line in body.splitlines():
                line = line.strip()
                case_match = re.match(r"case\s+'([^']+)'\s*:", line)
                if case_match:
                    current_cases.append(case_match.group(1))
                    continue
                return_match = re.match(r"return\s+'([^']*)'\s*;", line)
                if return_match:
                    val = return_match.group(1)
                    for c in current_cases:
                        cases[c] = val
                    current_cases = []
                    continue
            
            default_match = re.search(r"default\s*:[\s\S]*?return\s+'([^']*)'\s*;", body)
            if default_match:
                default_val = default_match.group(1)
            return cases, default_val

        # getBadgeText のテスト
        text_cases, text_default = parse_helper("getBadgeText")
        self.assertEqual(text_cases.get("chat"), "会話")
        self.assertEqual(text_cases.get("voice"), "会話")
        self.assertEqual(text_default, "")
        for autolabel in ["loop", "explore", "observe", "reflect", "web", "social"]:
            self.assertNotIn(autolabel, text_cases)

        # getBadgeClass のテスト
        class_cases, class_default = parse_helper("getBadgeClass")
        self.assertEqual(class_cases.get("chat"), "badge-chat")
        self.assertEqual(class_cases.get("voice"), "badge-chat")
        self.assertEqual(class_default, "")
        for autolabel in ["loop", "explore", "observe", "reflect", "web", "social"]:
            self.assertNotIn(autolabel, class_cases)

    def test_conversation_room_badges_exclude_autonomous_modes(self):
        """会話欄でエージェントの直接応答 (chat/voice) のみ '会話' バッジを表示し、自律モードとユーザー投稿は非表示であることを確認"""
        # chatMessages.filter(m => m.text) の map 処理部分を抽出
        map_pattern = r"displayList\s*=\s*chatMessages\.filter\(m\s*=>\s*m\.text\)\.map\(m\s*=>\s*\{([\s\S]*?)\}\);"
        match = re.search(map_pattern, self.app_js)
        self.assertIsNotNone(match, "chatMessages.filter(m => m.text).map not found")
        map_body = match.group(1)

        # isAgentDirectResponse の条件式をチェック
        cond_match = re.search(r"const\s+isAgentDirectResponse\s*=\s*(.*?);", map_body)
        self.assertIsNotNone(cond_match, "isAgentDirectResponse declaration not found")
        cond_expr = cond_match.group(1).replace(" ", "")
        
        # !isUser かつ (type == 'chat' || source == 'chat' || source == 'voice') であることをアサーション
        self.assertIn("!isUser", cond_expr)
        self.assertIn("m.type==='chat'", cond_expr)
        self.assertIn("m.source==='chat'", cond_expr)
        self.assertIn("m.source==='voice'", cond_expr)

        # badgeText と badgeClass のアサイン先のチェック
        self.assertIn("badgeText: isAgentDirectResponse ? '会話' : ''", map_body)
        self.assertIn("badgeClass: isAgentDirectResponse ? 'badge-chat' : ''", map_body)

    def test_soliloquy_room_badges_are_maintained(self):
        """独り言画面の '心の内' ラベル（badge-private）が維持されていることを確認"""
        map_pattern = r"displayList\s*=\s*chatMessages\.filter\(m\s*=>\s*m\.private\)\.map\(m\s*=>\s*\(([\s\S]*?)\)\);"
        match = re.search(map_pattern, self.app_js)
        self.assertIsNotNone(match, "chatMessages.filter(m => m.private).map not found")
        map_body = match.group(1)

        self.assertIn("badgeText: '心の内'", map_body)
        self.assertIn("badgeClass: 'badge-private'", map_body)

    def test_typing_indicator_logic(self):
        """タイピングインジケータで自律モード時に空文字+「中」などの不正バッジが表示されないことを確認"""
        self.assertIn("const badgeText = isPrivateTyping ? '考え中' : (getBadgeText(typingType) ? getBadgeText(typingType) + '中' : '');", self.app_js)
        self.assertIn("if (badgeText) {", self.app_js)


if __name__ == "__main__":
    unittest.main()
