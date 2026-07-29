"""`ha_api_path` の単体テスト。

守っている性質は「curl が送信前に `..` を正規化しても、HA_URL より上へ抜けられない」。
実装が curl のクライアント側正規化に依存しているので、**正規化される前の入力形**を
ここで固定する。percent-encode 版も含めるのは、サーバー側でデコードされる経路があるため。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = str(ROOT / "embodied_ha")
if PKG not in sys.path:
    sys.path.insert(0, PKG)

import ha_api_path


class ApiPathTests(unittest.TestCase):
    def assert_ok(self, path):
        self.assertEqual(ha_api_path.api_path_error(path), "", f"拒否された: {path!r}")

    def assert_rejected(self, path):
        self.assertNotEqual(ha_api_path.api_path_error(path), "", f"通ってしまった: {path!r}")

    def test_normal_paths_pass(self):
        for path in (
            "states",
            "services",
            "states/light.study",
            "states/climate.living",
            "history/period?filter_entity_id=sensor.x",
            "config",
            "template",
        ):
            self.assert_ok(path)

    def test_traversal_is_rejected(self):
        for path in (
            "..",
            "../addons/self/info",
            "../../addons/self/info",
            "states/../../addons/self/info",
            "a/b/../../../addons/self/info",
        ):
            self.assert_rejected(path)

    def test_percent_encoded_traversal_is_rejected(self):
        # curl はこれを正規化しないが、受け側がデコードすれば同じ場所へ届く。
        for path in (
            "%2e%2e/addons/self/info",
            "%2E%2E/addons/self/info",
            "states/%2e%2e/%2e%2e/addons/self/info",
            "%252e%252e/addons/self/info",
        ):
            self.assert_rejected(path)

    def test_absolute_url_is_rejected(self):
        self.assert_rejected("http://example.invalid/x")
        self.assert_rejected("https://example.invalid/x")

    def test_backslash_is_rejected(self):
        self.assert_rejected("..\\addons\\self\\info")

    def test_empty_path_keeps_existing_message(self):
        # 既存の ha_get の文言と同じであること（エージェントへの案内を変えない）
        self.assertEqual(
            ha_api_path.api_path_error(""),
            "path が空です（例: states, states/climate.xxx, services）",
        )

    def test_dot_in_entity_id_is_not_traversal(self):
        # `light.study` の `.` や、単独の `.` セグメントは越境ではないので通す
        self.assert_ok("states/light.study")
        self.assert_ok("states/./light.study")

    def test_query_containing_dotdot_is_not_traversal(self):
        # `?` 以降はパスではない。curl も正規化しないので拒否しない。
        self.assert_ok("history/period?filter_entity_id=../x")


class ServiceNameTests(unittest.TestCase):
    def assert_ok(self, service):
        self.assertEqual(ha_api_path.service_name_error(service), "", f"拒否された: {service!r}")

    def assert_rejected(self, service):
        self.assertNotEqual(ha_api_path.service_name_error(service), "", f"通ってしまった: {service!r}")

    def test_real_service_names_pass(self):
        for service in (
            "turn_on",
            "turn_off",
            "set_temperature",
            "select_option",
            "media_pause",
            "viewing_reservation_set",  # script ドメインの直呼び（by-design で許可）
        ):
            self.assert_ok(service)

    def test_traversal_is_rejected(self):
        for service in (
            "../../addons/self/options",
            "..",
            "turn_on/../../addons",
            "%2e%2e%2fadmin",
        ):
            self.assert_rejected(service)

    def test_slash_and_dot_are_rejected(self):
        self.assert_rejected("a/b")
        self.assert_rejected("turn.on")

    def test_empty_is_rejected(self):
        self.assert_rejected("")
        self.assert_rejected("   ")


if __name__ == "__main__":
    unittest.main()
