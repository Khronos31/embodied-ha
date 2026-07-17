#!/usr/bin/env python3
"""Opt-in live canary for Claude Code MCP per-tool execution control.

This intentionally is not named ``test_*.py``: it invokes the authenticated
``claude`` CLI and may use API quota, so ``python3 -m unittest discover -s
tests`` must never run it. Run it after changing the Claude CLI version:

    EHA_RUN_CLAUDE_ALLOWLIST_CANARY=1 python3 tests/claude_allowedtools_canary.py

The canary connects only the memory MCP server, allows ``recall``, and proves
that its allowed call completes while ``remember`` is denied. Its scratch log
directory and search nonce avoid accessing persistent Embodied HA data.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
MCP_CONFIG = ROOT / "embodied_ha" / "mcp-config.py"
ALLOWED_TOOL = "mcp__memory__recall"
DENIED_TOOL = "mcp__memory__remember"
OPT_IN_ENV = "EHA_RUN_CLAUDE_ALLOWLIST_CANARY"


def fail(message: str) -> None:
    print(f"claude allowedTools canary: {message}", file=sys.stderr)
    raise SystemExit(1)


def run_claude(claude_bin: str, config_path: Path, prompt: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            claude_bin,
            "-p",
            "--model",
            "sonnet",
            "--mcp-config",
            str(config_path),
            "--allowedTools",
            ALLOWED_TOOL,
            "--output-format",
            "stream-json",
            "--verbose",
            prompt,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def has_tool_use(output: str, tool_name: str) -> bool:
    """Find a real tool_use content block, not just a tool listed at init."""
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        stack = [event]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                if value.get("type") == "tool_use" and value.get("name") == tool_name:
                    return True
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
    return False


def main() -> None:
    if os.environ.get(OPT_IN_ENV) != "1":
        fail(f"refusing live API call; set {OPT_IN_ENV}=1 to run")
    claude_bin = os.environ.get("EHA_CLAUDE_BIN", "claude")
    if not shutil.which(claude_bin):
        fail(f"Claude CLI not found: {claude_bin}")

    with tempfile.TemporaryDirectory(prefix="eha-claude-allowlist-") as tmp:
        scratch = Path(tmp)
        config_path = scratch / "memory-mcp.json"
        env = dict(os.environ)
        env["EHA_DATA_DIR"] = str(scratch / "data")
        env["EHA_LOG_DIR"] = str(scratch / "log")
        Path(env["EHA_DATA_DIR"]).mkdir()
        Path(env["EHA_LOG_DIR"]).mkdir()
        subprocess.run(
            [
                sys.executable,
                str(MCP_CONFIG),
                "--format",
                "claude",
                "--allowed-mcp-tools",
                ALLOWED_TOOL,
                str(config_path),
                "memory",
            ],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        allowed = run_claude(
            claude_bin,
            config_path,
            "Call mcp__memory__recall exactly once with keywords "
            "['claude-allowlist-canary-nonce'], then reply exactly ALLOWED_OK.",
            env,
        )
        allowed_output = (allowed.stdout or "") + "\n" + (allowed.stderr or "")
        if allowed.returncode != 0:
            fail(f"allowed recall exited {allowed.returncode}: {allowed_output[-1000:]}")
        if "ALLOWED_OK" not in allowed_output or not has_tool_use(allowed_output, ALLOWED_TOOL):
            fail(f"allowed recall did not complete: {allowed_output[-1000:]}")
        if "haven't granted it yet" in allowed_output:
            fail(f"allowed recall was denied: {allowed_output[-1000:]}")

        denied = run_claude(
            claude_bin,
            config_path,
            "Call mcp__memory__remember exactly once with text "
            "'claude-allowlist-canary-nonce', then reply exactly DENIED_CHECKED.",
            env,
        )
        denied_output = (denied.stdout or "") + "\n" + (denied.stderr or "")
        if not has_tool_use(denied_output, DENIED_TOOL) or "haven't granted it yet" not in denied_output:
            fail(
                "unallowed remember was not rejected by the Claude permission boundary "
                f"(denial-message wording may have changed): {denied_output[-1000:]}"
            )

    print("claude allowedTools canary: PASS (recall allowed; remember denied)")


if __name__ == "__main__":
    main()
