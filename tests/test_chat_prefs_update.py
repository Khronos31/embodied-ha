"""chat_prefs_update.py（chat.py移植 増分6）の単体テスト＋ゴールデン比較。

ゴールデン比較テストは、chat.sh自身のpreferences.json更新コード
（696-854行目）を実際に読み取り、環境変数とハードコードされた
parsed-fileパスの読み取りを制御した状態でexec()実行し、その結果
書き込まれるpreferences.jsonの内容とchat_prefs_update.update_preferences
の出力を直接比較する。

chat.sh側は`/tmp/embodied-ha/chat_parsed.json`という本番と同一の
ハードコードパスを読むため、builtins.openをラップしてこのパスへの
読み取りだけ隔離したテスト用ファイルへ差し替え、本番/dev共有の
/tmp/embodied-haには一切触れないようにしてある。
"""
import builtins
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
EMBODIED_HA_DIR = ROOT / "embodied_ha"
sys.path.insert(0, str(EMBODIED_HA_DIR))

import chat_prefs_update  # type: ignore  # noqa: E402

CHAT_SH = EMBODIED_HA_DIR / "chat.sh"
_PREFS_SOURCE_START_LINE = 696  # import json, os
_PREFS_SOURCE_END_LINE = 854    # print(f"[chat][prefs] 更新: {changed}")
_HARDCODED_PARSED_PATH = "/tmp/embodied-ha/chat_parsed.json"


def _extract_chat_sh_prefs_source():
    lines = CHAT_SH.read_text(encoding="utf-8").splitlines()
    snippet = lines[_PREFS_SOURCE_START_LINE - 1:_PREFS_SOURCE_END_LINE]
    return "\n".join(snippet)


_CHAT_SH_PREFS_SOURCE = _extract_chat_sh_prefs_source()

_real_open = builtins.open


def _run_chat_sh_prefs_update(parsed, prefs_file):
    """chat.shの実preferences更新コードを、指定parsed/prefs_fileの下でexec実行する。

    ハードコードされた/tmp/embodied-ha/chat_parsed.jsonへの読み取りだけ、
    実ファイルに一切触れず`parsed`の内容を直接返すようすり替える。
    """
    def fake_open(path, *args, **kwargs):
        if str(path) == _HARDCODED_PARSED_PATH:
            import io
            return io.StringIO(json.dumps(parsed, ensure_ascii=False))
        return _real_open(path, *args, **kwargs)

    with patch.dict("os.environ", {"EHA_PREFS_FILE": prefs_file}, clear=False):
        with patch("builtins.open", side_effect=fake_open):
            namespace = {}
            try:
                exec(_CHAT_SH_PREFS_SOURCE, namespace)  # noqa: S102
            except SystemExit:
                pass


class PrefsUpdateGoldenComparisonTests(unittest.TestCase):
    """chat.sh実物のpreferences更新コードとupdate_preferencesの結果一致を検証。"""

    def _assert_golden_match(self, parsed, initial_prefs):
        with tempfile.TemporaryDirectory() as tmp:
            expected_file = Path(tmp) / "expected_preferences.json"
            actual_file = Path(tmp) / "actual_preferences.json"
            with open(expected_file, "w", encoding="utf-8") as fh:
                json.dump(initial_prefs, fh, ensure_ascii=False)
            with open(actual_file, "w", encoding="utf-8") as fh:
                json.dump(initial_prefs, fh, ensure_ascii=False)

            _run_chat_sh_prefs_update(parsed, str(expected_file))
            chat_prefs_update.update_preferences(parsed, str(actual_file))

            with open(expected_file, encoding="utf-8") as fh:
                expected = json.load(fh)
            with open(actual_file, encoding="utf-8") as fh:
                actual = json.load(fh)
            self.assertEqual(actual, expected)

    def test_no_preferences_update_leaves_file_untouched(self):
        self._assert_golden_match({}, {"cameras": [], "speakers": [], "presence": {}, "policies": []})

    def test_cameras_add_and_remove(self):
        self._assert_golden_match(
            {"preferences_update": {
                "cameras_add": [{"source": "capture_tv", "label": "テレビ"}],
                "cameras_remove": ["old_cam"],
            }},
            {"cameras": [{"source": "old_cam"}], "speakers": [], "presence": {}, "policies": []},
        )

    def test_speakers_set_list_shape_merge(self):
        self._assert_golden_match(
            {"preferences_update": {"speakers_set": {"study": {"type": "tts", "tts_entity": "tts.x"}}}},
            {"cameras": [], "speakers": [{"room": "study", "type": "notify"}], "presence": {}, "policies": []},
        )

    def test_speakers_set_dict_shape_and_new_entry(self):
        self._assert_golden_match(
            {"preferences_update": {"speakers_set": {"kitchen": {"type": "tcp", "host": "1.2.3.4"}}}},
            {"cameras": [], "speakers": {"living": {"type": "tts"}}, "presence": {}, "policies": []},
        )

    def test_presence_set(self):
        self._assert_golden_match(
            {"preferences_update": {"presence_set": {"entity": "input_boolean.home"}}},
            {"cameras": [], "speakers": [], "presence": {}, "policies": []},
        )

    def test_policies_add_dedup(self):
        self._assert_golden_match(
            {"preferences_update": {"policies_add": ["静かに", "静かに", "新しいルール"]}},
            {"cameras": [], "speakers": [], "presence": {}, "policies": ["静かに"]},
        )

    def test_sensors_add_new_group_and_merge_existing(self):
        self._assert_golden_match(
            {"preferences_update": {"sensors_add": [
                {"group": "人感センサー", "label": "物置", "entity": "binary_sensor.warehouse"},
            ]}},
            {"cameras": [], "speakers": [], "presence": {}, "policies": [],
             "sensors": {"groups": [{"title": "人感センサー", "contexts": ["loop"], "items": [
                 {"label": "玄関", "entity": "binary_sensor.entrance"}]}]}},
        )

    def test_sensors_remove_drops_empty_group(self):
        self._assert_golden_match(
            {"preferences_update": {"sensors_remove": ["binary_sensor.entrance"]}},
            {"cameras": [], "speakers": [], "presence": {}, "policies": [],
             "sensors": {"groups": [{"title": "人感センサー", "contexts": ["loop"], "items": [
                 {"label": "玄関", "entity": "binary_sensor.entrance"}]}]}},
        )

    def test_entities_add_and_remove(self):
        self._assert_golden_match(
            {"preferences_update": {
                "entities_add": [{"name": "リビングのライト", "entity_id": "light.living", "note": "備考"}],
                "entities_remove": ["light.old"],
            }},
            {"cameras": [], "speakers": [], "presence": {}, "policies": [],
             "entities": [{"name": "古いの", "entity_id": "light.old"}]},
        )

    def test_missing_prefs_file_content_falls_back_to_skeleton(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected_file = Path(tmp) / "expected.json"
            actual_file = Path(tmp) / "actual.json"
            # 意図的に不正なJSONを書いておき、フォールバック経路を通す
            expected_file.write_text("not json", encoding="utf-8")
            actual_file.write_text("not json", encoding="utf-8")
            parsed = {"preferences_update": {"policies_add": ["新規ポリシー"]}}
            _run_chat_sh_prefs_update(parsed, str(expected_file))
            chat_prefs_update.update_preferences(parsed, str(actual_file))
            self.assertEqual(
                json.loads(expected_file.read_text(encoding="utf-8")),
                json.loads(actual_file.read_text(encoding="utf-8")),
            )


class UpdatePreferencesBehaviorTests(unittest.TestCase):
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
