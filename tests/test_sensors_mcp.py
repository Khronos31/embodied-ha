import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "embodied_ha" / "sensors-mcp.py"


def load_sensors_mcp_module():
    module_name = "sensors_mcp_test"
    sys.modules.pop(module_name, None)
    pkg = str(ROOT / "embodied_ha")
    if pkg not in sys.path:
        sys.path.insert(0, pkg)
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SensorsMcpGuardTests(unittest.TestCase):
    """serve() が if __name__ == "__main__": の外で呼ばれていないことの回帰テスト。

    ガードが無いとこのモジュールを import した瞬間に mcp_lib.serve() の
    stdin 待ち JSON-RPC ループへ入ってしまい、このテスト自体がハングする。
    """

    def test_import_does_not_hang(self):
        module = load_sensors_mcp_module()
        self.assertTrue(hasattr(module, "get_sensors"))


class GetSensorsTests(unittest.TestCase):
    def setUp(self):
        self.sensors_mcp = load_sensors_mcp_module()

    def _json(self, result):
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        return result[0]["text"]

    @mock.patch("subprocess.run")
    def test_default_context_is_loop(self, mock_run):
        mock_run.return_value = mock.Mock(returncode=0, stdout="室温 23度")
        result = self.sensors_mcp.get_sensors({})
        self.assertEqual(self._json(result), "室温 23度")
        called_args = mock_run.call_args[0][0]
        self.assertIn("--context", called_args)
        self.assertEqual(called_args[called_args.index("--context") + 1], "loop")

    @mock.patch("subprocess.run")
    def test_invalid_context_falls_back_to_loop(self, mock_run):
        mock_run.return_value = mock.Mock(returncode=0, stdout="ok")
        self.sensors_mcp.get_sensors({"context": "bogus"})
        called_args = mock_run.call_args[0][0]
        self.assertEqual(called_args[called_args.index("--context") + 1], "loop")

    @mock.patch("subprocess.run")
    def test_chat_context_is_passed_through(self, mock_run):
        mock_run.return_value = mock.Mock(returncode=0, stdout="ok")
        self.sensors_mcp.get_sensors({"context": "chat"})
        called_args = mock_run.call_args[0][0]
        self.assertEqual(called_args[called_args.index("--context") + 1], "chat")

    @mock.patch("subprocess.run")
    def test_failure_with_no_stdout_is_error(self, mock_run):
        mock_run.return_value = mock.Mock(returncode=1, stdout="", stderr="boom")
        result, is_error = self.sensors_mcp.get_sensors({})
        self.assertTrue(is_error)
        self.assertIn("boom", self._json(result))

    @mock.patch("subprocess.run")
    def test_empty_stdout_success_returns_placeholder(self, mock_run):
        mock_run.return_value = mock.Mock(returncode=0, stdout="")
        result = self.sensors_mcp.get_sensors({})
        self.assertIn("未設定", self._json(result))


if __name__ == "__main__":
    unittest.main()
