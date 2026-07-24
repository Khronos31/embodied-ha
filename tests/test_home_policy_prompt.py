from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class HomePolicyPromptTest(unittest.TestCase):
    def test_home_policy_default_is_present_and_generic(self):
        text = (ROOT / "embodied_ha" / "home_policy.md").read_text(encoding="utf-8")
        self.assertIn("# 家のいい感じの状態（ホームポリシー）", text)
        self.assertIn("細かく指定しすぎず、意図を書くのがコツ", text)
        self.assertIn("人がいる部屋の照明を勝手に消さない", text)
        self.assertIn("深夜に音の出る操作はしない", text)
        for forbidden in ("Khronos31", "Claude", "Antigravity", "Alice", "Bob"):
            self.assertNotIn(forbidden, text)

    def test_run_sh_seeds_home_policy_file(self):
        text = (ROOT / "embodied_ha" / "run.sh").read_text(encoding="utf-8")
        self.assertIn('export EHA_HOME_POLICY_FILE="${EHA_HOME_POLICY_FILE:-$EHA_DATA_DIR/home_policy.md}"', text)
        self.assertIn('cp "$SCRIPT_DIR/home_policy.md" "$EHA_HOME_POLICY_FILE"', text)
        self.assertIn("home_policy.md を同梱デフォルトから初期化", text)

    def test_loop_injects_policy_only_for_observe_and_explore(self):
        text = (ROOT / "embodied_ha" / "loop.py").read_text(encoding="utf-8")
        self.assertIn('if selected_mode in ("observe", "explore") and home_policy:', text)
        self.assertIn('home_policy = _read_text(cfg.get("EHA_HOME_POLICY_FILE")', text)
        self.assertIn("policy_note", text)
        self.assertIn('ホームポリシーとの明らかなズレは', text)
        self.assertIn('ただし、人がいる部屋を勝手に変えない。深夜の音出し操作はしない。', text)


if __name__ == "__main__":
    unittest.main()
