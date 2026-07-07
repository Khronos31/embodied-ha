import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_voicevox_song_module():
    sys.path.insert(0, str(ROOT / "embodied_ha"))
    spec = importlib.util.spec_from_file_location(
        "voicevox_song_test", ROOT / "embodied_ha" / "voicevox_song.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_song_mcp_module():
    sys.path.insert(0, str(ROOT / "embodied_ha"))
    spec = importlib.util.spec_from_file_location(
        "song_mcp_test", ROOT / "embodied_ha" / "song-mcp.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SongScoreTests(unittest.TestCase):
    def setUp(self):
        self.song = load_voicevox_song_module()

    def test_parse_pitch_c4_standard_and_accidentals(self):
        self.assertEqual(self.song.parse_pitch("C4"), 60)
        self.assertEqual(self.song.parse_pitch("D#4"), 63)
        self.assertEqual(self.song.parse_pitch("Bb3"), 58)
        self.assertIsNone(self.song.parse_pitch("rest"))

    def test_duration_to_frames_uses_93_75fps(self):
        self.assertEqual(self.song.duration_to_frames("quarter", 100), 56)
        self.assertEqual(self.song.duration_to_frames("eighth", 100), 28)
        self.assertEqual(self.song.duration_to_frames("whole", 100), 225)

    def test_build_score_entries_adds_silence_and_lyrics(self):
        entries = self.song.build_score_entries([
            {"pitch": "C4", "duration": "quarter", "lyric": "こ"},
            {"pitch": "rest", "duration": "eighth"},
            {"pitch": "D4", "duration": "quarter", "lyric": "え"},
        ], 100)
        self.assertEqual(entries[0], {"key": None, "frame_length": 15, "lyric": ""})
        self.assertEqual(entries[-1], {"key": None, "frame_length": 15, "lyric": ""})
        self.assertEqual(entries[1], {"key": 60, "frame_length": 56, "lyric": "こ"})
        self.assertEqual(entries[2], {"key": None, "frame_length": 28, "lyric": ""})
        self.assertEqual(entries[3], {"key": 62, "frame_length": 56, "lyric": "え"})

    def test_pitched_note_requires_lyric(self):
        with self.assertRaises(ValueError):
            self.song.build_score_entries([
                {"pitch": "C4", "duration": "quarter"},
            ], 100)


class SongMcpTests(unittest.TestCase):
    def test_uninstalled_returns_plugin_disabled(self):
        mcp = load_song_mcp_module()
        mcp.is_installed = lambda: False
        result, is_error = mcp.sing({"notes": []})
        self.assertTrue(is_error)
        payload = json.loads(result[0]["text"])
        self.assertEqual(payload["error"], "plugin_disabled")


if __name__ == "__main__":
    unittest.main()
