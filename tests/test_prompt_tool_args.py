import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATHS = (
    ROOT / "embodied_ha" / "chat.sh",
    ROOT / "embodied_ha" / "loop.sh",
)


class PromptToolArgumentTests(unittest.TestCase):
    def test_remember_prompt_uses_text_argument(self):
        chat_text = (ROOT / "embodied_ha" / "chat.sh").read_text(encoding="utf-8")
        self.assertIn("remember ツールに text を渡して記録する", chat_text)
        self.assertNotIn("remember ツールに note", chat_text)

    def test_prompts_do_not_pair_old_argument_names_with_renamed_tools(self):
        old_arg = r"(?:note|entry)"
        tools = ("remember", "append_narrative")
        for path in PROMPT_PATHS:
            prompt_text = path.read_text(encoding="utf-8")
            for tool_name in tools:
                with self.subTest(path=path.name, tool=tool_name):
                    self.assertIsNone(
                        re.search(rf"{tool_name}[^\n]{{0,100}}{old_arg}", prompt_text)
                    )
                    self.assertIsNone(
                        re.search(rf"{old_arg}[^\n]{{0,100}}{tool_name}", prompt_text)
                    )


if __name__ == "__main__":
    unittest.main()
