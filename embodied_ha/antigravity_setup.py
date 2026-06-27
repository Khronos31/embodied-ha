"""Helpers for optional Antigravity CLI installation and auth state."""
from __future__ import annotations

import os
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
