import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "embodied_ha" / "ha-mcp.py"


def load_ha_mcp_module():
    module_name = "ha_mcp_test"
    sys.modules.pop(module_name, None)
    os.environ.setdefault("HA_URL", "http://127.0.0.1:8123")
    os.environ.setdefault("SUPERVISOR_TOKEN", "test-token")
    pkg = str(ROOT / "embodied_ha")
    if pkg not in sys.path:
        sys.path.insert(0, pkg)
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HaMcpGuardTests(unittest.TestCase):
    """serve() が if __name__ == "__main__": の外で呼ばれていないことの回帰テスト。

    ガードが無いとこのモジュールを import した瞬間に mcp_lib.serve() の
    stdin 待ち JSON-RPC ループへ入ってしまい、このテスト自体がハングする。
    """

    def test_import_does_not_hang(self):
        module = load_ha_mcp_module()
        self.assertTrue(hasattr(module, "ha_get"))


class HaGetTests(unittest.TestCase):
    def setUp(self):
        self.ha_mcp = load_ha_mcp_module()

    def _json(self, result):
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        return result[0]["text"]

    def test_empty_path_is_error(self):
        result, is_error = self.ha_mcp.ha_get({"path": ""})
        self.assertTrue(is_error)
        self.assertIn("path", self._json(result))

    @mock.patch("subprocess.run")
    def test_successful_get_returns_stdout(self, mock_run):
        mock_run.return_value = mock.Mock(returncode=0, stdout='{"state": "on"}')
        result = self.ha_mcp.ha_get({"path": "states/light.study"})
        self.assertEqual(self._json(result), '{"state": "on"}')
        called_url = mock_run.call_args[0][0][-1]
        self.assertTrue(called_url.endswith("/states/light.study"))

    @mock.patch("subprocess.run")
    def test_failed_get_is_error(self, mock_run):
        mock_run.return_value = mock.Mock(returncode=22, stdout="")
        result, is_error = self.ha_mcp.ha_get({"path": "states/does.not.exist"})
        self.assertTrue(is_error)
        self.assertIn("22", self._json(result))


if __name__ == "__main__":
    unittest.main()
