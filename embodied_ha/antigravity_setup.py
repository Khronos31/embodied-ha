"""Helpers for optional Antigravity CLI installation and auth state."""
from __future__ import annotations

import os
import re
import subprocess
from urllib.request import urlopen

INSTALL_URL = "https://antigravity.google/cli/install.sh"
HOME_ENV = "EHA_ANTIGRAVITY_HOME"
BIN_DIR_ENV = "EHA_ANTIGRAVITY_BIN_DIR"
BIN_ENV = "EHA_ANTIGRAVITY_BIN"


def home_dir() -> str:
    return os.environ.get(HOME_ENV, "/data/")


def bin_dir() -> str:
    return os.environ.get(BIN_DIR_ENV, os.path.join(home_dir(), "bin"))


def binary_path() -> str:
    return os.environ.get(BIN_ENV, os.path.join(bin_dir(), "agy"))


def oauth_token_path() -> str:
    return os.path.join(home_dir(), ".gemini", "antigravity-cli", "antigravity-oauth-token")


def install_script_url() -> str:
    return INSTALL_URL


def is_installed() -> bool:
    path = binary_path()
    return os.path.isfile(path) and os.access(path, os.X_OK)


def is_authenticated() -> bool:
    return os.path.exists(oauth_token_path())


def is_agy_bin(path: str | None) -> bool:
    return os.path.basename(path or "") == "agy"


def agy_prompt_text(content_blocks) -> str:
    parts = []
    for blk in content_blocks:
        if blk.get("type") == "text":
            parts.append(blk["text"])
        elif blk.get("type") == "image":
            parts.append("[カメラ画像]")
    return "\n".join(parts) + "\nJSON:\n"


def extract_agy_result(raw: str) -> str:
    raw = raw.strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    return m.group(0) if m else raw


def write_mcp_config(
    script_dir: str,
    env: dict | None = None,
    servers=("audio", "memory", "ha", "sensors", "body"),
) -> str:
    """
    agy 用 MCP config を ~/.gemini/config/mcp_config.json に書き出す。
    mcp-config.py を呼んで生成し、書き出し先パスを返す。
    失敗した場合は空文字を返す（サイレント失敗）。
    """
    config_dir = os.path.join(home_dir(), ".gemini", "config")
    os.makedirs(config_dir, exist_ok=True)
    out_path = os.path.join(config_dir, "mcp_config.json")
    gen = os.path.join(script_dir, "mcp-config.py")
    if not os.path.isfile(gen):
        return ""
    try:
        run_env = {**os.environ, **(env or {})}
        result = subprocess.run(
            ["python3", gen, out_path] + list(servers),
            env=run_env,
            capture_output=True,
            timeout=15,
        )
        if result.returncode == 0 and os.path.exists(out_path):
            return out_path
    except Exception:
        pass
    return ""


def fetch_install_script(timeout: int = 60) -> str:
    with urlopen(INSTALL_URL, timeout=timeout) as res:
        return res.read().decode("utf-8")


def state() -> dict:
    return {
        "installed": is_installed(),
        "authenticated": is_authenticated(),
        "home_dir": home_dir(),
        "bin_dir": bin_dir(),
        "binary_path": binary_path(),
        "oauth_token_path": oauth_token_path(),
        "install_url": install_script_url(),
    }
