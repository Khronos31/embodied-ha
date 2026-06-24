import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))

import memory_state as ms  # type: ignore  # noqa: E402


def load_memory_mcp():
    path = ROOT / "embodied_ha" / "memory-mcp.py"
    spec = importlib.util.spec_from_file_location("memory_mcp", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MemoryConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = self.tmpdir.name
        self.mcp = load_memory_mcp()
        self.mcp.LOG_DIR = self.log_dir

    def tearDown(self):
        self.tmpdir.cleanup()

    def _mcp_text(self, result):
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        self.assertEqual(result[0]["type"], "text")
        return result[0]["text"]

    def _save_episode(self, *, timestamp, summary, detail, evidence_source, tags=None):
        episode = ms.save_episode(
            self.log_dir,
            {
                "timestamp": timestamp,
                "day": timestamp[:10],
                "kind": "observation",
                "source": "watch",
                "summary": summary,
                "detail": detail,
                "tags": tags or [],
                "entities": ["living_room"],
                "actors": ["resident"],
                "importance": 0.6,
                "evidence": [{"source": evidence_source, "note": summary}],
            },
        )
        return episode

    def test_duplicate_fingerprints_merge_and_preserve_evidence(self):
        day = "2026-06-20"
        ep1 = self._save_episode(
            timestamp=f"{day}T08:00:00+09:00",
            summary="リビングの電気がついていた",
            detail="watch log",
            evidence_source="watch-1",
            tags=["light", "evening"],
        )
        ep2 = self._save_episode(
            timestamp=f"{day}T08:01:00+09:00",
            summary="リビングの電気がついていた",
            detail="watch log",
            evidence_source="watch-2",
            tags=["light"],
        )

        report = json.loads(self._mcp_text(self.mcp.consolidate_memory({"scope": day, "day": day})))
        canonical = ms.list_episodes(self.log_dir, day=day, status="canonical", reverse=True)
        superseded = ms.list_episodes(self.log_dir, day=day, status="superseded", reverse=True)

        self.assertEqual(report["scope"], day)
        self.assertEqual(canonical[0]["id"], ep1["id"])
        self.assertEqual(len(canonical), 1)
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0]["merged_into"], canonical[0]["id"])
        evidence_sources = {item.get("source") for item in canonical[0]["evidence"]}
        self.assertEqual(evidence_sources, {"watch-1", "watch-2"})
        self.assertTrue((Path(self.log_dir) / "memory" / "consolidations" / f"{day}.json").exists())

    def test_conflicting_topics_remain_as_conflict(self):
        day = "2026-06-21"
        self._save_episode(
            timestamp=f"{day}T08:00:00+09:00",
            summary="リビングの電気がついていた",
            detail="watch log",
            evidence_source="watch-a",
            tags=["light"],
        )
        self._save_episode(
            timestamp=f"{day}T08:10:00+09:00",
            summary="リビングの電気が消えていた",
            detail="watch log",
            evidence_source="watch-b",
            tags=["light"],
        )

        report = json.loads(self._mcp_text(self.mcp.consolidate_memory({"scope": day, "day": day})))
        canonical = ms.list_episodes(self.log_dir, day=day, status="canonical", reverse=True)
        conflicts = ms.list_episodes(self.log_dir, day=day, status="conflict", reverse=True)

        self.assertEqual(len(canonical), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["status"], "conflict")
        self.assertEqual(len(report["conflict_groups"]), 1)
        self.assertEqual(len(conflicts[0]["evidence"]), 1)
        self.assertEqual(len(canonical[0]["evidence"]), 1)

    def test_consolidation_is_idempotent_and_memory_md_is_untouched(self):
        day = "2026-06-22"
        self._save_episode(
            timestamp=f"{day}T08:00:00+09:00",
            summary="廊下の灯りがついていた",
            detail="watch log",
            evidence_source="watch-1",
            tags=["light"],
        )
        self._save_episode(
            timestamp=f"{day}T08:01:00+09:00",
            summary="廊下の灯りがついていた",
            detail="watch log",
            evidence_source="watch-2",
            tags=["light"],
        )
        memory_md = Path(self.log_dir) / "memory.md"
        original_memory = "\n".join([
            "## コア記憶",
            "",
            "core note",
            "",
            "---",
            "",
            "## 最近の気づき",
            "",
            "- legacy note",
            "",
        ])
        memory_md.write_text(original_memory, encoding="utf-8")

        report1 = json.loads(self._mcp_text(self.mcp.consolidate_memory({"scope": day, "day": day})))
        report2 = json.loads(self._mcp_text(self.mcp.consolidate_memory({"scope": day, "day": day})))

        self.assertEqual(report1, report2)
        self.assertEqual(memory_md.read_text(encoding="utf-8"), original_memory)

    def test_mem_context_prioritizes_daybook_and_consolidated_episodes(self):
        day = "2026-06-23"
        self._save_episode(
            timestamp=f"{day}T09:00:00+09:00",
            summary="キッチンの窓が開いていた",
            detail="watch log",
            evidence_source="watch-a",
            tags=["window"],
        )
        self._save_episode(
            timestamp=f"{day}T09:05:00+09:00",
            summary="キッチンの窓が閉まっていた",
            detail="watch log",
            evidence_source="watch-b",
            tags=["window"],
        )
        ms.build_daybook(
            self.log_dir,
            day,
            episode_ids=[episode["id"] for episode in ms.list_episodes(self.log_dir, day=day, reverse=True)],
            summary="日次要約",
            themes=["観察", "窓"],
            highlights=[{"summary": "窓の状態変化"}],
            open_questions=["次の確認点"],
            source="watch",
        )
        memory_md = Path(self.log_dir) / "memory.md"
        memory_md.write_text(
            "\n".join([
                "## コア記憶",
                "",
                "core note",
                "",
                "---",
                "",
                "## 最近の気づき",
                "",
                "- legacy note",
                "",
            ]),
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["EHA_LOG_DIR"] = self.log_dir
        result = subprocess.run(
            ["python3", str(ROOT / "embodied_ha" / "mem-context.py"), str(memory_md), "40"],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        output = result.stdout
        self.assertLess(output.index("## 日次サマリー"), output.index("## 統合済みエピソード"))
        self.assertLess(output.index("## 統合済みエピソード"), output.index("legacy note"))

    def test_recall_prioritizes_consolidated_episodes_before_memory_md(self):
        day = "2026-06-24"
        self._save_episode(
            timestamp=f"{day}T10:00:00+09:00",
            summary="リビングの照明がついていた",
            detail="watch log",
            evidence_source="watch-a",
            tags=["light"],
        )
        self._save_episode(
            timestamp=f"{day}T10:10:00+09:00",
            summary="リビングの照明が消えていた",
            detail="watch log",
            evidence_source="watch-b",
            tags=["light"],
        )
        Path(self.log_dir, "memory.md").write_text(
            "\n".join([
                "## コア記憶",
                "",
                "core note",
                "",
                "---",
                "",
                "## 最近の気づき",
                "",
                "- 照明のlegacy note",
                "",
            ]),
            encoding="utf-8",
        )
        report = json.loads(self._mcp_text(self.mcp.consolidate_memory({"scope": day, "day": day})))
        self.assertEqual(len(report["conflict_groups"]), 1)

        env = os.environ.copy()
        env["EHA_LOG_DIR"] = self.log_dir
        result = subprocess.run(
            ["bash", str(ROOT / "embodied_ha" / "recall.sh"), "照明"],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        output = result.stdout
        canonical_index = output.index("【エピソード:observation】")
        memory_index = output.index("[記憶]")
        self.assertLess(canonical_index, memory_index)
        self.assertIn("/conflict", output)


if __name__ == "__main__":
    unittest.main()
