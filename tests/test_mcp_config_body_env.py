"""body サーバーの宣言 env のテスト。

キャラクター名は宣言する（宣言した env だけを渡すエージェントCLIでは、
get_location の説明が既定の「エージェント」になり、個体が自分の名前で呼ばれなくなる）。
MQTT の接続情報は宣言しない——agy 向けの設定はモデルが読めるファイルへそのまま
書き出され、秘密の退避対象は `_SENSITIVE_ENV_KEYS` に限られるため。
身体位置の publish は daemon 側から行う。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "embodied_ha" / "mcp-config.py"

MQTT_KEYS = ("MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PASS")
MQTT_ENV = {"MQTT_HOST": "core-mosquitto", "MQTT_PORT": "1883",
            "MQTT_USER": "u", "MQTT_PASS": "SUPERSECRETPW"}


def _run(extra_env, tmp, servers=("body",), fmt=None, credential_file=None):
    prefs_file = Path(tmp) / "preferences.json"
    prefs_file.write_text("{}", encoding="utf-8")
    out_path = Path(tmp) / "mcp_config.json"
    env = {"EHA_PREFS_FILE": str(prefs_file), "PATH": "/usr/bin:/bin"}
    env.update(extra_env)
    cmd = [sys.executable, str(SCRIPT)]
    if fmt:
        cmd += ["--format", fmt]
    if credential_file:
        cmd += ["--credential-file", str(credential_file)]
    cmd += [str(out_path), *servers]
    subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
    return json.loads(out_path.read_text(encoding="utf-8")), out_path


class BodyServerEnvTests(unittest.TestCase):
    def test_character_name_is_declared_for_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, _ = _run({"EHA_CHARACTER_NAME": "テスト個体"}, tmp)
        self.assertEqual(config["mcpServers"]["body"]["env"].get("EHA_CHARACTER_NAME"), "テスト個体")

    def test_character_name_unset_is_omitted_rather_than_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, _ = _run({}, tmp)
        self.assertNotIn("EHA_CHARACTER_NAME", config["mcpServers"]["body"]["env"])

    def test_mqtt_credentials_reach_no_mcp_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, _ = _run(MQTT_ENV, tmp,
                             servers=("body", "sociality", "files", "memory", "sensors", "audio"))
        for name, server in config["mcpServers"].items():
            env = server.get("env") or {}
            for key in MQTT_KEYS:
                self.assertNotIn(key, env, f"{name} に {key} が渡っている")

    def test_agy_config_is_model_readable_so_it_must_not_carry_mqtt_password(self):
        # agy 向け設定は site dir に平文で残り、モデルにパスまで教えている。
        # 秘密の退避対象は _SENSITIVE_ENV_KEYS だけなので、ここへ載せてはいけない。
        with tempfile.TemporaryDirectory() as tmp:
            cred = Path(tmp) / "cred.json"
            _, out_path = _run({**MQTT_ENV, "SUPERVISOR_TOKEN": "TOKENVALUE"}, tmp,
                               servers=("body",), fmt="agy", credential_file=cred)
            raw = out_path.read_text(encoding="utf-8")
        self.assertNotIn("SUPERSECRETPW", raw)
        self.assertNotIn("TOKENVALUE", raw)

    def test_timezone_reaches_every_server_including_the_minimal_ones(self):
        # TZ が無いサーバーはコンテナ既定の UTC で時刻を書き、同じログの中で
        # オフセットが混ざる。秘密ではないので最小 env のサーバーにも渡す。
        with tempfile.TemporaryDirectory() as tmp:
            config, _ = _run({"TZ": "Asia/Tokyo", "SUPERVISOR_TOKEN": "t"}, tmp,
                             servers=("body", "sociality", "files", "memory", "sensors", "audio"))
        for name, server in config["mcpServers"].items():
            self.assertEqual((server.get("env") or {}).get("TZ"), "Asia/Tokyo",
                             f"{name} に TZ が渡っていない")

    def test_timezone_does_not_widen_the_minimal_servers(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, _ = _run({"TZ": "Asia/Tokyo", "SUPERVISOR_TOKEN": "t", **MQTT_ENV}, tmp,
                             servers=("sociality", "files"))
        for name in ("sociality", "files"):
            env = config["mcpServers"][name]["env"]
            self.assertNotIn("SUPERVISOR_TOKEN", env)
            for key in MQTT_KEYS:
                self.assertNotIn(key, env)

    def test_sociality_still_has_no_supervisor_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, _ = _run({"SUPERVISOR_TOKEN": "t"}, tmp, servers=("body", "sociality"))
        self.assertNotIn("SUPERVISOR_TOKEN", config["mcpServers"]["sociality"]["env"])


if __name__ == "__main__":
    unittest.main()
