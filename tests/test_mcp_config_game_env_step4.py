"""Step4 増分1b: game-mcp の nested invoke へ選択ハーネス+CLI環境を明示注入する配線のテスト。

MCP サーバーは mcp-config の明示 env からのみ起動され親環境を継承しない(sol H3)。game-mcp は
CPU 戦で invoke-agent.sh 経由に選択ハーネスを再起動する唯一の MCP なので、選択ハーネス+CLI
パス/ホーム/認証を game 限定で注入する。認証(ANTHROPIC_API_KEY 等)は全 MCP へ広げない。
"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
EHA_DIR = ROOT / "embodied_ha"

_GAME_KEYS = (
    "EHA_AGENT_HARNESS", "EHA_AGENT_CWD",
    "EHA_CLAUDE_BIN", "CLAUDE_BIN", "EHA_CLAUDE_CWD", "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY",
    "EHA_CODEX_BIN", "CODEX_HOME",
    "EHA_ANTIGRAVITY_BIN", "EHA_ANTIGRAVITY_BIN_DIR", "EHA_ANTIGRAVITY_HOME",
)


def load_mcp_config(name: str, env: dict):
    """env を厳密に反映して mcp-config を import する(GAME_NESTED_ENV/COMMON_ENV は import 時計算)。

    テスト非決定性を避けるため、env に無い game 関連キーは import 前に確実に外す
    (patch.dict が終了時に元へ復元する)。
    """
    with mock.patch.dict(os.environ, env, clear=False):
        for key in _GAME_KEYS:
            if key not in env and key in os.environ:
                del os.environ[key]
        spec = importlib.util.spec_from_file_location(name, EHA_DIR / "mcp-config.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


class GameNestedEnvTests(unittest.TestCase):
    # 各ハーネスの nested invoke に要る全キーを網羅する(sol Low3: 1つ落ちても検出できるよう
    # table-driven)。EHA_AGENT_HARNESS/EHA_AGENT_CWD は共通、以下がハーネス固有。
    _PER_HARNESS_KEYS = {
        "claude": ("EHA_CLAUDE_BIN", "CLAUDE_BIN", "EHA_CLAUDE_CWD", "CLAUDE_CONFIG_DIR"),
        "codex": ("EHA_CODEX_BIN", "CODEX_HOME"),
        "agy": ("EHA_ANTIGRAVITY_BIN", "EHA_ANTIGRAVITY_BIN_DIR", "EHA_ANTIGRAVITY_HOME"),
    }

    def test_game_env_carries_selected_harness_and_all_cli_paths(self):
        for harness, keys in self._PER_HARNESS_KEYS.items():
            with self.subTest(harness=harness):
                env = {"EHA_AGENT_HARNESS": harness, "EHA_AGENT_CWD": "/data/workdir"}
                for i, key in enumerate(keys):
                    env[key] = f"/data/{harness}/val{i}"
                m = load_mcp_config(f"mcp_config_game_{harness}", env)
                self.assertEqual(m.GAME_NESTED_ENV.get("EHA_AGENT_HARNESS"), harness)
                game_env = m.SERVER_SPECS["game"].build()["env"]
                self.assertEqual(game_env.get("EHA_AGENT_HARNESS"), harness)
                self.assertEqual(game_env.get("EHA_AGENT_CWD"), "/data/workdir")
                for key in keys:
                    self.assertEqual(game_env.get(key), env[key], f"{harness}: {key} が game env に無い")

    def test_auth_and_harness_are_scoped_to_game_not_other_servers(self):
        env = {
            "EHA_AGENT_HARNESS": "claude",
            "ANTHROPIC_API_KEY": "sk-secret",
            "CLAUDE_CONFIG_DIR": "/data/claude-home",
            "EHA_CLAUDE_BIN": "/data/claude-cli/bin/claude",
        }
        m = load_mcp_config("mcp_config_game_scope", env)
        game_env = m.SERVER_SPECS["game"].build()["env"]
        self.assertEqual(game_env.get("ANTHROPIC_API_KEY"), "sk-secret")
        self.assertEqual(game_env.get("EHA_AGENT_HARNESS"), "claude")
        self.assertEqual(game_env.get("CLAUDE_CONFIG_DIR"), "/data/claude-home")

        # 非 game サーバーには認証もハーネスも渡らない。
        body_env = m.SERVER_SPECS["body"].build()["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", body_env)
        self.assertNotIn("EHA_AGENT_HARNESS", body_env)
        self.assertNotIn("CLAUDE_CONFIG_DIR", body_env)

        # COMMON_ENV 自体にも入らない(全 MCP へ広がらない)。
        self.assertNotIn("ANTHROPIC_API_KEY", m.COMMON_ENV)
        self.assertNotIn("EHA_AGENT_HARNESS", m.COMMON_ENV)

    def test_absent_keys_are_omitted_not_broadcast_empty(self):
        m = load_mcp_config("mcp_config_game_absent", {})
        for key in _GAME_KEYS:
            self.assertNotIn(key, m.GAME_NESTED_ENV)
        game_env = m.SERVER_SPECS["game"].build()["env"]
        self.assertNotIn("EHA_AGENT_HARNESS", game_env)
        self.assertNotIn("ANTHROPIC_API_KEY", game_env)


if __name__ == "__main__":
    unittest.main()
