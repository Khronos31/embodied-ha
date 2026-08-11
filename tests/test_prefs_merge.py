"""`prefs_merge` の単体テスト。

守っている性質は「**保存のたびに設定が消えない**」（findings F-21）。
UI がフォームに持っていないキーは `POST /api/preferences` の body に載らないため、
全置換すると黙って失われる。実際に `cameras[].ptz` / `ha_entity` / `preset` / `direction` と
`speakers[].media_player` が消え、本番の `ptz` は0個になっていた。

同時に、**消せるべきものは消せる**ことも固定する——項目の削除やフィールドのクリアを
「引き継ぎ」で復活させてしまうと、今度は別の壊れ方になる。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
EHA_DIR = ROOT / "embodied_ha"
for p in (str(EHA_DIR), str(EHA_DIR / "web")):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("HA_URL", "http://supervisor/core/api")

import prefs_merge
import server


class KeyPreservationTests(unittest.TestCase):
    def test_untouched_top_level_key_survives(self):
        existing = {"tts_provider": "voicevox", "stt_language": "ja"}
        incoming = {"tts_provider": "cloud"}
        merged = prefs_merge.merge_preferences(existing, incoming)
        self.assertEqual(merged["tts_provider"], "cloud", "UI の値が正になっていない")
        self.assertEqual(merged["stt_language"], "ja", "言及されなかったキーが消えた")

    def test_nested_dict_keys_survive(self):
        existing = {"tts_options": {"speaker": 12, "speed": 1.1, "pitch": 0.0}}
        incoming = {"tts_options": {"speaker": 30}}
        merged = prefs_merge.merge_preferences(existing, incoming)
        self.assertEqual(merged["tts_options"], {"speaker": 30, "speed": 1.1, "pitch": 0.0})

    def test_f21_camera_keys_survive(self):
        """F-21 の実際の症状。UI は dataset の5キーだけでカメラを作り直していた。"""
        existing = {"cameras": [{
            "source": "camera_living", "label": "リビング", "room": "living_room",
            "ptz": {"left": "button.a", "right": "button.b"},
            "ha_entity": "camera.living", "preset": "wide", "direction": "north",
        }]}
        incoming = {"cameras": [{
            "source": "camera_living", "label": "リビング", "room": "living_room",
            "note": "", "entity": "",
        }]}
        cam = prefs_merge.merge_preferences(existing, incoming)["cameras"][0]
        self.assertEqual(cam["ptz"], {"left": "button.a", "right": "button.b"})
        self.assertEqual(cam["ha_entity"], "camera.living")
        self.assertEqual(cam["preset"], "wide")
        self.assertEqual(cam["direction"], "north")

    def test_speaker_media_player_survives(self):
        existing = {"speakers": [{"entity": "sp1", "label": "居間", "media_player": "media_player.x"}]}
        incoming = {"speakers": [{"entity": "sp1", "label": "居間", "room": "living_room"}]}
        sp = prefs_merge.merge_preferences(existing, incoming)["speakers"][0]
        self.assertEqual(sp["media_player"], "media_player.x")
        self.assertEqual(sp["room"], "living_room")


class DeletionStillWorksTests(unittest.TestCase):
    def test_missing_ui_managed_camera_field_is_removed(self):
        existing = {"cameras": [{"source": "a", "note": "remove", "ptz": {"left": "x"}}]}
        merged = prefs_merge.merge_preferences(existing, {"cameras": [{"source": "a"}]})
        self.assertNotIn("note", merged["cameras"][0])
        self.assertEqual(merged["cameras"][0]["ptz"], {"left": "x"})

    def test_removing_a_list_item_removes_it(self):
        existing = {"mics": [{"source": "a", "note": "keep"}, {"source": "b", "note": "gone"}]}
        incoming = {"mics": [{"source": "a"}]}
        merged = prefs_merge.merge_preferences(existing, incoming)
        self.assertEqual([m["source"] for m in merged["mics"]], ["a"], "項目の削除が効いていない")
        self.assertEqual(merged["mics"][0]["note"], "keep")

    def test_emptying_a_list_empties_it(self):
        existing = {"cameras": [{"source": "a", "ptz": {"left": "x"}}]}
        merged = prefs_merge.merge_preferences(existing, {"cameras": []})
        self.assertEqual(merged["cameras"], [])

    def test_explicit_empty_value_is_respected(self):
        # 「キーが無い」と「値を空にした」は別物。後者は UI の意図として尊重する。
        existing = {"tts_entity": "tts.old"}
        merged = prefs_merge.merge_preferences(existing, {"tts_entity": ""})
        self.assertEqual(merged["tts_entity"], "")

    def test_explicit_null_is_respected(self):
        existing = {"sing_speaker": {"name": "x", "style_id": 1}}
        merged = prefs_merge.merge_preferences(existing, {"sing_speaker": None})
        self.assertIsNone(merged["sing_speaker"])

    def test_new_list_item_is_added_as_is(self):
        existing = {"mics": [{"source": "a", "note": "keep"}]}
        incoming = {"mics": [{"source": "a"}, {"source": "b", "label": "新規"}]}
        merged = prefs_merge.merge_preferences(existing, incoming)
        self.assertEqual(merged["mics"][1], {"source": "b", "label": "新規"})


class IdentityResolutionTests(unittest.TestCase):
    def test_identity_priority(self):
        # id が最優先。source しか無ければ source。
        self.assertEqual(prefs_merge._identity({"id": "i", "source": "s"}), ("id", "i"))
        self.assertEqual(prefs_merge._identity({"source": "s", "label": "l"}), ("source", "s"))
        self.assertEqual(prefs_merge._identity({"entity": "e"}), ("entity", "e"))
        self.assertIsNone(prefs_merge._identity({"note": "x"}))
        self.assertIsNone(prefs_merge._identity({"id": "   "}))

    def test_reordering_does_not_mix_items(self):
        existing = {"mics": [{"source": "a", "note": "A"}, {"source": "b", "note": "B"}]}
        incoming = {"mics": [{"source": "b"}, {"source": "a"}]}
        merged = prefs_merge.merge_preferences(existing, incoming)
        self.assertEqual([(m["source"], m["note"]) for m in merged["mics"]], [("b", "B"), ("a", "A")])

    def test_unidentifiable_list_is_taken_as_is(self):
        # 取り違えて混ぜるくらいならマージしない
        existing = {"policies": [{"note": "old"}]}
        incoming = {"policies": [{"note": "new"}, {"note": "new2"}]}
        merged = prefs_merge.merge_preferences(existing, incoming)
        self.assertEqual(merged["policies"], [{"note": "new"}, {"note": "new2"}])

    def test_scalar_lists_are_taken_as_is(self):
        existing = {"policies": ["静かにする", "確認する"]}
        merged = prefs_merge.merge_preferences(existing, {"policies": ["確認する"]})
        self.assertEqual(merged["policies"], ["確認する"])


class DegenerateInputTests(unittest.TestCase):
    def test_no_existing_file(self):
        self.assertEqual(prefs_merge.merge_preferences({}, {"a": 1}), {"a": 1})

    def test_non_dict_existing_is_ignored(self):
        self.assertEqual(prefs_merge.merge_preferences(None, {"a": 1}), {"a": 1})
        self.assertEqual(prefs_merge.merge_preferences([1, 2], {"a": 1}), {"a": 1})

    def test_type_change_takes_incoming(self):
        existing = {"sensors": {"groups": []}}
        merged = prefs_merge.merge_preferences(existing, {"sensors": ["a"]})
        self.assertEqual(merged["sensors"], ["a"])

    def test_existing_is_not_mutated(self):
        existing = {"tts_options": {"speaker": 1}, "keep": True}
        prefs_merge.merge_preferences(existing, {"tts_options": {"speaker": 2}})
        self.assertEqual(existing, {"tts_options": {"speaker": 1}, "keep": True})


class PreferencesEndpointMergeTests(unittest.TestCase):
    """`POST /api/preferences` が全置換ではなくマージになっていること（配線の裏付け）。"""

    def _handler(self, body: object):
        raw = json.dumps(body).encode()
        handler = object.__new__(server.Handler)
        handler.path = "/api/preferences"
        handler.headers = {"Content-Length": str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        handler.send_json = mock.Mock()
        return handler

    def test_save_does_not_drop_keys_the_ui_omitted(self):
        with tempfile.TemporaryDirectory() as temp:
            prefs_file = Path(temp) / "preferences.json"
            prefs_file.write_text(json.dumps({
                "cameras": [{"source": "cam_a", "label": "居間", "ptz": {"left": "button.a"}}],
                "stt_language": "ja",
            }, ensure_ascii=False), encoding="utf-8")
            handler = self._handler({"cameras": [{"source": "cam_a", "label": "居間"}]})
            with mock.patch.object(server, "PREFS_FILE", str(prefs_file)):
                handler.do_PUT()
            self.assertEqual(handler.send_json.call_args.args[0], {"ok": True})
            saved = json.loads(prefs_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["cameras"][0]["ptz"], {"left": "button.a"}, "ptz が消えた")
            self.assertEqual(saved["stt_language"], "ja", "言及されなかったキーが消えた")

    def test_save_drops_retired_audio_keys_even_when_merge_would_preserve_them(self):
        with tempfile.TemporaryDirectory() as temp:
            prefs_file = Path(temp) / "preferences.json"
            prefs_file.write_text(json.dumps({
                "wake_words": ["legacy"],
                "mics": [{
                    "source": "rtsp://example/mic",
                    "label": "Mic",
                    "stt_enabled": True,
                    "background_hearing_enabled": True,
                }],
            }), encoding="utf-8")
            handler = self._handler({
                "mics": [{"source": "rtsp://example/mic", "label": "Mic"}],
            })
            with mock.patch.object(server, "PREFS_FILE", str(prefs_file)):
                handler.do_PUT()

            saved = json.loads(prefs_file.read_text(encoding="utf-8"))
            self.assertNotIn("wake_words", saved)
            self.assertNotIn("stt_enabled", saved["mics"][0])
            self.assertNotIn("background_hearing_enabled", saved["mics"][0])

    def test_save_is_refused_when_existing_file_is_corrupt(self):
        # 壊れたファイルを黙って上書きしない（読めない=空 として扱わない）
        with tempfile.TemporaryDirectory() as temp:
            prefs_file = Path(temp) / "preferences.json"
            prefs_file.write_text("{壊れている", encoding="utf-8")
            handler = self._handler({"stt_language": "ja"})
            with mock.patch.object(server, "PREFS_FILE", str(prefs_file)):
                handler.do_PUT()
            self.assertEqual(handler.send_json.call_args.args[1], 500)
            self.assertEqual(prefs_file.read_text(encoding="utf-8"), "{壊れている", "壊れたファイルが上書きされた")

    def test_first_save_without_existing_file_works(self):
        with tempfile.TemporaryDirectory() as temp:
            prefs_file = Path(temp) / "preferences.json"
            handler = self._handler({"stt_language": "ja"})
            with mock.patch.object(server, "PREFS_FILE", str(prefs_file)):
                handler.do_PUT()
            self.assertEqual(handler.send_json.call_args.args[0], {"ok": True})
            self.assertEqual(json.loads(prefs_file.read_text(encoding="utf-8")), {"stt_language": "ja"})


if __name__ == "__main__":
    unittest.main()
