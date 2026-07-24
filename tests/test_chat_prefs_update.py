"""chat_prefs_update.py の契約テスト。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMBODIED_HA_DIR = ROOT / "embodied_ha"
sys.path.insert(0, str(EMBODIED_HA_DIR))

import chat_prefs_update  # type: ignore  # noqa: E402

class UpdatePreferencesBehaviorTests(unittest.TestCase):
    def test_all_update_operations_are_wired_through_public_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_file = Path(tmp) / "preferences.json"
            prefs_file.write_text(json.dumps({
                "cameras": [{"source": "old_cam"}],
                "speakers": [{"room": "study", "type": "notify"}],
                "presence": {},
                "policies": [],
                "sensors": {"groups": [{"title": "old", "items": [{"label": "old", "entity": "sensor.old"}]}]},
                "entities": [{"name": "old", "entity_id": "light.old"}],
            }), encoding="utf-8")
            chat_prefs_update.update_preferences({"preferences_update": {
                "cameras_add": [{"source": "new_cam"}],
                "cameras_remove": ["old_cam"],
                "speakers_set": {"study": {"type": "tcp", "host": "speaker"}},
                "presence_set": {"entity": "person.yuno"},
                "policies_add": ["静かに"],
                "sensors_add": [{"group": "new", "label": "温度", "entity": "sensor.temp"}],
                "sensors_remove": ["sensor.old"],
                "entities_add": [{"name": "new", "entity_id": "light.new"}],
                "entities_remove": ["light.old"],
            }}, str(prefs_file))
            prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
            self.assertEqual(prefs["cameras"], [{"source": "new_cam"}])
            self.assertEqual(prefs["speakers"][0]["type"], "tcp")
            self.assertEqual(prefs["presence"], {"entity": "person.yuno"})
            self.assertEqual(prefs["policies"], ["静かに"])
            self.assertEqual(prefs["sensors"]["groups"][0]["title"], "new")
            self.assertEqual(prefs["entities"], [{"name": "new", "entity_id": "light.new"}])

    def test_invalid_json_recovers_to_default_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_file = Path(tmp) / "preferences.json"
            prefs_file.write_text("not json", encoding="utf-8")
            chat_prefs_update.update_preferences(
                {"preferences_update": {"policies_add": ["新規ポリシー"]}},
                str(prefs_file),
            )
            prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
            self.assertEqual(prefs["policies"], ["新規ポリシー"])
            self.assertEqual(prefs["cameras"], [])

    def test_empty_prefs_file_string_is_noop(self):
        # 例外を投げないことの確認
        chat_prefs_update.update_preferences({"preferences_update": {"policies_add": ["x"]}}, "")

    def test_no_update_key_does_not_write_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_file = Path(tmp) / "preferences.json"
            original = {"cameras": [], "speakers": [], "presence": {}, "policies": []}
            with open(prefs_file, "w", encoding="utf-8") as fh:
                json.dump(original, fh)
            mtime_before = prefs_file.stat().st_mtime_ns
            chat_prefs_update.update_preferences({}, str(prefs_file))
            self.assertEqual(prefs_file.stat().st_mtime_ns, mtime_before)

    def test_exception_inside_apply_functions_is_swallowed(self):
        # preferences_updateの型がおかしくても(list等)クラッシュしない
        with tempfile.TemporaryDirectory() as tmp:
            prefs_file = Path(tmp) / "preferences.json"
            prefs_file.write_text("{}", encoding="utf-8")
            chat_prefs_update.update_preferences(
                {"preferences_update": {"cameras_add": "not-a-list-of-dicts"}}, str(prefs_file)
            )

    def test_prints_changed_summary_only_when_something_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_file = Path(tmp) / "preferences.json"
            prefs_file.write_text(json.dumps({"cameras": [], "speakers": [], "presence": {}, "policies": []}), encoding="utf-8")
            printed = []
            chat_prefs_update.update_preferences(
                {"preferences_update": {"policies_add": ["新方針"]}}, str(prefs_file), print_fn=printed.append
            )
            self.assertEqual(len(printed), 1)
            self.assertIn("policies_add", printed[0])


class ApplySubCaseTests(unittest.TestCase):
    def test_cameras_add_dedups_by_source(self):
        prefs = {"cameras": [{"source": "cam1", "label": "old"}]}
        changed = chat_prefs_update.apply_cameras_add(prefs, [{"source": "cam1", "label": "new"}])
        self.assertEqual(prefs["cameras"], [{"source": "cam1", "label": "new"}])
        self.assertEqual(changed, ["cameras_add:cam1"])

    def test_cameras_add_skips_entries_without_source(self):
        prefs = {"cameras": []}
        changed = chat_prefs_update.apply_cameras_add(prefs, [{"label": "no source"}])
        self.assertEqual(changed, [])

    def test_cameras_remove_reports_only_actual_removals(self):
        prefs = {"cameras": [{"source": "cam1"}]}
        changed = chat_prefs_update.apply_cameras_remove(prefs, ["not_present"])
        self.assertEqual(changed, [])
        self.assertEqual(len(prefs["cameras"]), 1)

    def test_speakers_set_merges_by_room(self):
        prefs = {"speakers": [{"room": "study", "type": "notify"}]}
        changed = chat_prefs_update.apply_speakers_set(prefs, {"study": {"type": "tts"}})
        self.assertEqual(prefs["speakers"], [{"room": "study", "type": "tts"}])
        self.assertEqual(changed, ["speakers_set:study"])

    def test_speakers_set_appends_when_no_match(self):
        prefs = {"speakers": []}
        chat_prefs_update.apply_speakers_set(prefs, {"kitchen": {"type": "tts"}})
        self.assertEqual(len(prefs["speakers"]), 1)
        self.assertEqual(prefs["speakers"][0]["room"], "kitchen")

    def test_presence_set_overwrites(self):
        prefs = {"presence": {"entity": "old"}}
        changed = chat_prefs_update.apply_presence_set(prefs, {"entity": "new"})
        self.assertEqual(prefs["presence"], {"entity": "new"})
        self.assertEqual(changed, ["presence_set"])

    def test_presence_set_empty_is_noop(self):
        prefs = {"presence": {"entity": "old"}}
        changed = chat_prefs_update.apply_presence_set(prefs, {})
        self.assertEqual(prefs["presence"], {"entity": "old"})
        self.assertEqual(changed, [])

    def test_policies_add_no_duplicates(self):
        prefs = {"policies": ["既存"]}
        changed = chat_prefs_update.apply_policies_add(prefs, ["既存", "新規"])
        self.assertEqual(prefs["policies"], ["既存", "新規"])
        self.assertEqual(changed, ["policies_add"])

    def test_sensors_add_creates_group_and_merges_duplicate_entity(self):
        prefs = {}
        chat_prefs_update.apply_sensors_add(prefs, [{"group": "G", "entity": "e1", "label": "旧"}])
        changed = chat_prefs_update.apply_sensors_add(prefs, [{"group": "G", "entity": "e1", "label": "新"}])
        items = prefs["sensors"]["groups"][0]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["label"], "新")
        self.assertEqual(changed, ["sensors_add:G/e1"])

    def test_sensors_add_requires_entity_or_template(self):
        prefs = {}
        changed = chat_prefs_update.apply_sensors_add(prefs, [{"group": "G", "label": "no entity/template"}])
        self.assertEqual(changed, [])

    def test_sensors_remove_drops_empty_groups(self):
        prefs = {"sensors": {"groups": [{"title": "G", "items": [{"entity": "e1"}]}]}}
        chat_prefs_update.apply_sensors_remove(prefs, ["e1"])
        self.assertEqual(prefs["sensors"]["groups"], [])

    def test_entities_add_dedups_by_entity_id(self):
        prefs = {"entities": [{"name": "旧", "entity_id": "light.x"}]}
        changed = chat_prefs_update.apply_entities_add(prefs, [{"name": "新", "entity_id": "light.x"}])
        self.assertEqual(prefs["entities"], [{"name": "新", "entity_id": "light.x"}])
        self.assertEqual(changed, ["entities_add:light.x"])

    def test_entities_add_skips_without_entity_id(self):
        prefs = {"entities": []}
        changed = chat_prefs_update.apply_entities_add(prefs, [{"name": "no id"}])
        self.assertEqual(changed, [])

    def test_entities_remove_by_name_or_id(self):
        prefs = {"entities": [{"name": "A", "entity_id": "light.a"}, {"name": "B", "entity_id": "light.b"}]}
        changed = chat_prefs_update.apply_entities_remove(prefs, ["light.a"])
        self.assertEqual(prefs["entities"], [{"name": "B", "entity_id": "light.b"}])
        self.assertEqual(changed, ["entities_remove"])


if __name__ == "__main__":
    unittest.main()
