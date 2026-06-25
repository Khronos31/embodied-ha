import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import counterfactual_state as cs  # type: ignore  # noqa: E402


class CounterfactualStateTests(unittest.TestCase):
    def test_record_and_select_best_recent_counterfactual(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            low = cs.record_counterfactual(
                "watch",
                "speak",
                "リビングに声をかけようとした",
                "low_confidence",
                ["motion=unknown"],
                0.2,
                log_dir=tmpdir,
            )
            high = cs.record_counterfactual(
                "explore",
                "act",
                "照明を消そうとした",
                "quiet_window",
                ["hour=2"],
                0.9,
                boundary_reason="深夜帯（1-6時）のため自律操作抑制",
                log_dir=tmpdir,
            )

            self.assertEqual(low["confidence"], 0.2)
            self.assertEqual(cs.best_recent_counterfactual(tmpdir)["summary"], high["summary"])

    def test_counterfactual_sentence_uses_boundary_reason(self):
        sentence = cs.counterfactual_sentence(
            {
                "summary": "声をかけようとした",
                "rejected_because": "quiet_window",
                "boundary_reason": "深夜帯（1-6時）のため発話抑制",
            }
        )
        self.assertEqual(sentence, "声をかけようとしたけど、深夜帯（1-6時）のため発話抑制からやめた")


if __name__ == "__main__":
    unittest.main()
