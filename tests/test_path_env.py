import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "embodied_ha"))

from path_env import build_tools_path


class BuildToolsPathTests(unittest.TestCase):
    def test_uses_portable_default(self):
        self.assertEqual(build_tools_path({"PATH": "/usr/bin:/bin"}), "/usr/local/bin:/usr/bin:/bin")

    def test_preserves_order_and_removes_duplicates(self):
        env = {
            "EHA_TOOLS_PATH": "/usr/local/bin:/opt/eha/bin",
            "PATH": "/usr/local/bin:/usr/bin:/opt/eha/bin:/bin",
        }
        self.assertEqual(build_tools_path(env), "/usr/local/bin:/opt/eha/bin:/usr/bin:/bin")


if __name__ == "__main__":
    unittest.main()
