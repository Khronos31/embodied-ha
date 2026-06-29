import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))


def load_memory_mcp_module():
    path = ROOT / "embodied_ha" / "memory-mcp.py"
    spec = importlib.util.spec_from_file_location("memory_mcp_causal_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MemoryCausalTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmpdir.name)
        self.memory_mcp = load_memory_mcp_module()
        self.memory_mcp.LOG_DIR = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def _text(self, result):
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        self.assertEqual(result[0]["type"], "text")
        return result[0]["text"]

    def _json(self, result):
        return json.loads(self._text(result))

    def _record_episode(self, *, timestamp, day, source, kind, summary, detail="", tags=None, importance=0.5):
        return self._json(
            self.memory_mcp.record_episode(
                {
                    "timestamp": timestamp,
                    "day": day,
                    "source": source,
                    "kind": kind,
                    "summary": summary,
                    "detail": detail,
                    "tags": tags or [],
                    "importance": importance,
                }
            )
        )

    def _record_chain(self, *, cause_episode_id="", effect_episode_id="", relation="correlated", summary="", mechanism="", confidence=0.5, tags=None, cause_episode=None, effect_episode=None):
        payload = {
            "cause_episode_id": cause_episode_id,
            "effect_episode_id": effect_episode_id,
            "relation": relation,
            "summary": summary,
            "mechanism": mechanism,
            "confidence": confidence,
            "tags": tags or [],
        }
        if cause_episode is not None:
            payload["cause_episode"] = cause_episode
        if effect_episode is not None:
            payload["effect_episode"] = effect_episode
        return self._json(self.memory_mcp.record_causal_chain(payload))

    def test_get_causal_chain_returns_default_for_missing_pair(self):
        payload = self._json(self.memory_mcp.get_causal_chain({}))
        self.assertEqual(payload["id"], "")
        self.assertEqual(payload["cause_episode_id"], "")
        self.assertEqual(payload["effect_episode_id"], "")
        self.assertEqual(payload["relation"], "correlated")
        self.assertEqual(payload["support_episode_ids"], [])

    def test_record_causal_chain_round_trip_and_dedup(self):
        cause = self._record_episode(
            timestamp="2026-06-23T08:00:00+09:00",
            day="2026-06-23",
            source="watch",
            kind="observation",
            summary="朝に窓を開けた",
            detail="換気をした",
            tags=["airflow"],
            importance=0.7,
        )
        effect = self._record_episode(
            timestamp="2026-06-23T08:10:00+09:00",
            day="2026-06-23",
            source="watch",
            kind="observation",
            summary="部屋が涼しくなった",
            detail="空気が入れ替わった",
            tags=["temperature"],
            importance=0.6,
        )
        first = self._record_chain(
            cause_episode_id=cause["id"],
            effect_episode_id=effect["id"],
            relation="triggered",
            summary="換気で部屋が涼しくなった",
            mechanism="窓を開けて空気が入れ替わった",
            confidence=0.91,
            tags=["airflow", "temperature"],
        )
        self.assertEqual(first["relation"], "caused")
        self.assertEqual(first["cause_episode_id"], cause["id"])
        self.assertEqual(first["effect_episode_id"], effect["id"])
        self.assertCountEqual(first["support_episode_ids"], [cause["id"], effect["id"]])

        loaded = self._json(
            self.memory_mcp.get_causal_chain(
                {"cause_episode_id": cause["id"], "effect_episode_id": effect["id"]}
            )
        )
        self.assertEqual(loaded["id"], first["id"])
        self.assertEqual(loaded["summary"], first["summary"])
        self.assertEqual(loaded["mechanism"], first["mechanism"])

        second = self._record_chain(
            cause_episode_id=cause["id"],
            effect_episode_id=effect["id"],
            relation="enabled",
            summary="上書きされないはず",
            mechanism="別の説明",
            confidence=0.5,
            tags=["duplicate"],
        )
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["summary"], first["summary"])
        self.assertEqual(second["mechanism"], first["mechanism"])
        chain_files = list((self.log_dir / "memory" / "causal_chains").glob("*.json"))
        self.assertEqual(len(chain_files), 1)

    def test_record_causal_chain_saves_linked_episodes_from_objects(self):
        chain = self._record_chain(
            relation="blocked",
            summary="停電で部屋が暗くなった",
            mechanism="ブレーカーが落ちて照明が止まった",
            confidence=0.77,
            tags=["power", "lighting"],
            cause_episode={
                "timestamp": "2026-06-23T12:00:00+09:00",
                "day": "2026-06-23",
                "source": "explore",
                "kind": "observation",
                "summary": "ブレーカーが落ちていた",
                "detail": "電気が消えた",
                "tags": ["power"],
            },
            effect_episode={
                "timestamp": "2026-06-23T12:01:00+09:00",
                "day": "2026-06-23",
                "source": "explore",
                "kind": "observation",
                "summary": "部屋が真っ暗になった",
                "detail": "照明が落ちた",
                "tags": ["lighting"],
            },
        )
        self.assertEqual(chain["relation"], "prevented")
        self.assertTrue(chain["cause_episode_id"])
        self.assertTrue(chain["effect_episode_id"])
        self.assertEqual(len(list((self.log_dir / "memory" / "episodes").glob("*.json"))), 2)
        self.assertEqual(len(list((self.log_dir / "memory" / "causal_chains").glob("*.json"))), 1)

    def test_relation_normalization_variants(self):
        cases = [
            ("triggered", "caused"),
            ("enabled", "enabled"),
            ("blocked", "prevented"),
            ("related", "correlated"),
        ]
        for index, (raw_relation, expected) in enumerate(cases):
            with self.subTest(raw_relation=raw_relation):
                cause = self._record_episode(
                    timestamp=f"2026-06-23T13:0{index}:00+09:00",
                    day="2026-06-23",
                    source="watch",
                    kind="observation",
                    summary=f"原因側 {index}",
                    detail="原因の詳細",
                )
                effect = self._record_episode(
                    timestamp=f"2026-06-23T13:0{index}:30+09:00",
                    day="2026-06-23",
                    source="watch",
                    kind="observation",
                    summary=f"結果側 {index}",
                    detail="結果の詳細",
                )
                chain = self._record_chain(
                    cause_episode_id=cause["id"],
                    effect_episode_id=effect["id"],
                    relation=raw_relation,
                    summary=f"{raw_relation} の例",
                    mechanism="テスト用",
                    confidence=0.55,
                )
                self.assertEqual(chain["relation"], expected)

    def test_recall_prioritizes_structured_memory_before_raw_logs(self):
        cause = self._record_episode(
            timestamp="2026-06-23T14:00:00+09:00",
            day="2026-06-23",
            source="watch",
            kind="observation",
            summary="ブレーカー確認",
            detail="停電とは無関係",
        )
        effect = self._record_episode(
            timestamp="2026-06-23T14:05:00+09:00",
            day="2026-06-23",
            source="watch",
            kind="observation",
            summary="ライトを確認した",
            detail="停電前の記録ではない",
        )
        self._record_chain(
            cause_episode_id=cause["id"],
            effect_episode_id=effect["id"],
            relation="caused",
            summary="停電で部屋が暗くなった",
            mechanism="照明が落ちた",
            confidence=0.88,
        )
        (self.log_dir / "observations.jsonl").write_text(
            json.dumps(
                {
                    "timestamp": "2026-06-23T14:10:00+09:00",
                    "private": "停電して部屋が真っ暗になった",
                    "emotion": "concerned",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        (self.log_dir / "memory.md").write_text(
            "- 2026-06-23 | 停電の復旧手順を覚えておく\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            ["bash", str(ROOT / "embodied_ha" / "recall.sh"), "停電"],
            cwd=str(ROOT),
            env={**os.environ, "EHA_LOG_DIR": self.tmpdir.name},
            capture_output=True,
            text=True,
            check=True,
        )
        out = result.stdout
        self.assertIn("【因果】", out)
        self.assertIn("[観察]", out)
        self.assertIn("[記憶]", out)
        self.assertLess(out.index("【因果】"), out.index("[観察]"))
        self.assertLess(out.index("[観察]"), out.index("[記憶]"))

    def test_chat_and_explore_expose_memory_tools(self):
        chat_text = (ROOT / "embodied_ha" / "chat.sh").read_text(encoding="utf-8")
        explore_text = (ROOT / "embodied_ha" / "loop.sh").read_text(encoding="utf-8")
        for text in (chat_text, explore_text):
            self.assertIn("mcp__memory__record_episode", text)
            self.assertIn("mcp__memory__record_causal_chain", text)
            self.assertIn("mcp__memory__get_episode", text)
            self.assertIn("mcp__memory__get_causal_chain", text)
        self.assertIn("record_causal_chain ツール", chat_text)
        self.assertIn("record_episode で episode として残す", chat_text)
        self.assertIn("record_causal_chain も使い", explore_text)


if __name__ == "__main__":
    unittest.main()
