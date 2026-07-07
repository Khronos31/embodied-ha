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

import memory_state  # type: ignore  # noqa: E402


def load_memory_mcp_module():
    path = ROOT / "embodied_ha" / "memory-mcp.py"
    spec = importlib.util.spec_from_file_location("memory_mcp_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MemoryGrowthTests(unittest.TestCase):
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

    def test_record_episode_round_trip(self):
        payload = {
            "timestamp": "2026-06-23T10:00:00+09:00",
            "day": "2026-06-23",
            "source": "watch",
            "kind": "observation",
            "summary": "玄関で荷物を受け取った",
            "detail": "宅配便が来た",
            "tags": ["watch", "delivery"],
            "importance": 0.8,
        }
        recorded = self._json(self.memory_mcp.record_episode(payload))
        loaded = self._json(self.memory_mcp.get_episode({"episode_id": recorded["id"]}))
        self.assertEqual(loaded["id"], recorded["id"])
        self.assertEqual(loaded["summary"], payload["summary"])
        self.assertEqual(loaded["tags"], payload["tags"])
        self.assertEqual(loaded["day"], payload["day"])
        self.assertTrue((self.log_dir / "memory" / "episodes" / f"{recorded['id']}.json").exists())


    def test_working_memory_tracks_episode_activation(self):
        recorded = self._json(
            self.memory_mcp.record_episode(
                {
                    "timestamp": "2026-06-23T10:00:00+09:00",
                    "day": "2026-06-23",
                    "source": "watch",
                    "kind": "observation",
                    "summary": "机の上に青いマグがある",
                }
            )
        )
        self._json(self.memory_mcp.get_episode({"episode_id": recorded["id"]}))
        working = self._json(self.memory_mcp.get_working_memory({}))
        self.assertEqual(working[0]["episode_id"], recorded["id"])
        self.assertEqual(working[0]["reason"], "get_episode")

    def test_episode_evidence_preserves_camera_context(self):
        recorded = self._json(
            self.memory_mcp.record_episode(
                {
                    "timestamp": "2026-06-23T10:00:00+09:00",
                    "day": "2026-06-23",
                    "source": "watch",
                    "kind": "observation",
                    "summary": "ソファに本が置かれている",
                    "evidence": [
                        {
                            "camera_context": {
                                "source": "camera.living",
                                "room": "リビング",
                                "preset": "sofa",
                                "direction": "left",
                            }
                        }
                    ],
                }
            )
        )
        self.assertEqual(recorded["evidence"][0]["camera_context"]["preset"], "sofa")

    def test_episode_evidence_preserves_audio_context(self):
        recorded = self._json(
            self.memory_mcp.record_episode(
                {
                    "timestamp": "2026-06-23T10:00:00+09:00",
                    "day": "2026-06-23",
                    "source": "watch",
                    "kind": "observation",
                    "summary": "テレビから音がしている",
                    "evidence": [
                        {
                            "audio_context": {
                                "source": "capture_tv",
                                "duration": 5,
                                "peak_db": -12.3,
                                "has_sound": True,
                            }
                        }
                    ],
                }
            )
        )
        self.assertEqual(recorded["evidence"][0]["audio_context"]["source"], "capture_tv")

    def test_get_episode_returns_default_for_missing_id(self):
        payload = self._json(self.memory_mcp.get_episode({}))
        self.assertEqual(payload["id"], "")
        self.assertEqual(payload["timestamp"], "")
        self.assertEqual(payload["summary"], "")
        self.assertEqual(payload["kind"], "observation")

    def test_list_episodes_filters_by_kind(self):
        observation = self._json(
            self.memory_mcp.record_episode(
                {
                    "timestamp": "2026-06-23T10:00:00+09:00",
                    "day": "2026-06-23",
                    "source": "watch",
                    "kind": "observation",
                    "summary": "部屋の温度を見た",
                }
            )
        )
        media = self._json(
            self.memory_mcp.record_episode(
                {
                    "timestamp": "2026-06-23T11:00:00+09:00",
                    "day": "2026-06-23",
                    "source": "watch",
                    "kind": "media_watch",
                    "summary": "映画を見た",
                }
            )
        )
        items = self._json(self.memory_mcp.list_episodes({"kind": "media_watch"}))
        self.assertEqual([item["id"] for item in items], [media["id"]])
        self.assertNotIn(observation["id"], [item["id"] for item in items])

    def test_build_daybook_is_idempotent(self):
        episode = self._json(
            self.memory_mcp.record_episode(
                {
                    "timestamp": "2026-06-23T10:00:00+09:00",
                    "day": "2026-06-23",
                    "source": "watch",
                    "kind": "observation",
                    "summary": "宅配便が来た",
                    "detail": "玄関で受け取った",
                    "tags": ["delivery"],
                }
            )
        )
        first = self._json(
            self.memory_mcp.build_daybook(
                {
                    "date": "2026-06-23",
                    "summary": "一日の圧縮メモ",
                    "themes": ["来客"],
                    "episode_ids": [episode["id"]],
                    "highlights": [{"summary": "宅配便の受け取り"}],
                    "open_questions": ["次の配達はいつか"],
                }
            )
        )
        second = self._json(
            self.memory_mcp.build_daybook(
                {
                    "date": "2026-06-23",
                    "summary": "別の要約",
                    "episode_ids": [episode["id"]],
                }
            )
        )
        daybook_files = list((self.log_dir / "memory" / "daybooks").glob("*.json"))
        self.assertEqual(len(daybook_files), 1)
        self.assertEqual(first["summary"], second["summary"])
        self.assertEqual(first["episode_ids"], [episode["id"]])

    def test_get_daybook_returns_default_for_missing_date(self):
        payload = self._json(self.memory_mcp.get_daybook({}))
        self.assertEqual(payload["date"], "")
        self.assertEqual(payload["summary"], "")
        self.assertEqual(payload["episode_ids"], [])

    def test_build_daybook_preserves_zero_raw_entry_count(self):
        daybook = self._json(
            self.memory_mcp.build_daybook(
                {
                    "date": "2026-06-23",
                    "summary": "空の要約",
                    "episode_ids": [],
                    "raw_entry_count": 0,
                }
            )
        )
        self.assertEqual(daybook["raw_entry_count"], 0)
        self.assertEqual(daybook["episode_count"], 0)

    def test_mem_context_prioritizes_daybooks(self):
        memory_md = self.log_dir / "memory.md"
        memory_md.write_text(
            "## コア記憶\n\n古い核心\n\n---\n\n## 最近の気づき\n\n- 2026-06-20 | 古い気づき\n",
            encoding="utf-8",
        )
        memory_state.save_daybook(
            self.tmpdir.name,
            {
                "date": "2026-06-23",
                "summary": "日次要約",
                "themes": ["会話"],
                "highlights": [{"summary": "重要な出来事"}],
                "open_questions": ["次に何を確認するか"],
            },
        )
        result = subprocess.run(
            [sys.executable, str(ROOT / "embodied_ha" / "mem-context.py"), str(memory_md), "40"],
            capture_output=True,
            text=True,
            check=True,
        )
        out = result.stdout
        self.assertIn("## 日次サマリー", out)
        self.assertLess(out.index("## 日次サマリー"), out.index("## コア記憶"))
        self.assertIn("日次要約", out)
        self.assertIn("重要な出来事", out)

    def test_episode_is_indexed_to_fts_and_recall_uses_it(self):
        episode = memory_state.save_episode(
            self.tmpdir.name,
            {
                "timestamp": "2026-06-23T10:00:00+09:00",
                "day": "2026-06-23",
                "source": "watch",
                "kind": "observation",
                "summary": "青いマグがテーブル右にある",
                "detail": "朝のカメラ確認",
            },
        )
        hits = memory_state.search_fts(self.tmpdir.name, ["青いマグ"], limit=5)
        self.assertEqual(hits[0]["episode_id"], episode["id"])
        env = {**os.environ, "EHA_LOG_DIR": self.tmpdir.name}
        result = subprocess.run(
            ["bash", str(ROOT / "embodied_ha" / "recall.sh"), "青いマグ"],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        self.assertIn("source=fts5", result.stdout)
        self.assertIn(episode["id"], result.stdout)

    def test_remember_and_recall_still_work(self):
        self.memory_mcp.remember({"text": "猫のフードを買う"})
        memory_md = self.log_dir / "memory.md"
        self.assertIn("猫のフードを買う", memory_md.read_text(encoding="utf-8"))
        recall = self._text(self.memory_mcp.recall({"keywords": ["フード"]}))
        self.assertIn("猫のフードを買う", recall)

    def test_loops_add_and_list_still_work(self):
        added = self._text(self.memory_mcp.loops_add({"text": "フィルター掃除", "source": "watch"}))
        self.assertIn("ループを追加しました", added)
        listing = self._text(self.memory_mcp.loops_list({}))
        self.assertIn("フィルター掃除", listing)


if __name__ == "__main__":
    unittest.main()
