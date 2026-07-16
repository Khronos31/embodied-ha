"""Shared helpers for shadow parity process tests.

The harness deliberately starts with file-contract snapshots. Mode-specific
tests add command/MCP comparisons as each loop branch is ported.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "embodied_ha"
DEFAULT_TIMESTAMP = "2026-07-15T12:00:00+09:00"
DEFAULT_TODAY = "2026-07-15"


RUNTIME_FILES = (
    "observations.jsonl",
    "explore.jsonl",
    "loop_parse_errors.jsonl",
    "pending_proposal.json",
    "chat_log.jsonl",
)


@dataclass(frozen=True)
class SideEffectSnapshot:
    files: dict[str, Any]

    def comparable(self) -> dict[str, Any]:
        return self.files


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def capture_runtime_side_effects(log_dir: str | Path) -> SideEffectSnapshot:
    root = Path(log_dir)
    files: dict[str, Any] = {}
    for name in RUNTIME_FILES:
        path = root / name
        if name.endswith(".jsonl"):
            files[name] = _read_jsonl(path)
        else:
            files[name] = _read_json(path)
    return SideEffectSnapshot(files=files)


def assert_same_side_effects(testcase, left: SideEffectSnapshot, right: SideEffectSnapshot) -> None:
    testcase.assertEqual(left.comparable(), right.comparable())


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def install_fixture_bins(bin_dir: Path) -> None:
    write_executable(
        bin_dir / "python3",
        """#!__REAL_PYTHON__
import json
import os
import subprocess
import sys

REAL_PYTHON = __REAL_PYTHON_JSON__


def append_trace(name, row):
    trace_dir = os.environ.get("EHA_SHADOW_TRACE_DIR", "")
    if not trace_dir:
        return
    os.makedirs(trace_dir, exist_ok=True)
    with open(os.path.join(trace_dir, name), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\\n")


def parse_mcp_config_args(args):
    mcp_args = args[1:]
    output_index = None
    i = 0
    while i < len(mcp_args):
        item = mcp_args[i]
        if item in ("--format", "--allowed-mcp-tools"):
            i += 2
            continue
        if item.startswith("--"):
            i += 1
            continue
        output_index = i
        break
    if output_index is None:
        return "", []
    return mcp_args[output_index], mcp_args[output_index + 1:]


args = sys.argv[1:]
is_mcp_config = bool(args and args[0].endswith("mcp-config.py"))
result = subprocess.run([REAL_PYTHON, *args])

if is_mcp_config:
    output_path, servers = parse_mcp_config_args(args)
    generated_config = None
    generated_config_text = None
    if output_path and os.path.exists(output_path):
        try:
            with open(output_path, encoding="utf-8") as fh:
                generated_config_text = fh.read()
            try:
                generated_config = json.loads(generated_config_text)
            except Exception:
                generated_config = None
        except Exception as exc:
            generated_config_text = f"<read failed: {exc}>"
    append_trace(
        "mcp_config_calls.jsonl",
        {
            "argv": ["python3", *args],
            "output_path": output_path,
            "servers": servers,
            "returncode": result.returncode,
            "generated_config": generated_config,
            "generated_config_text": generated_config_text,
        },
    )

sys.exit(result.returncode)
""".replace("__REAL_PYTHON_JSON__", json.dumps(sys.executable)).replace("__REAL_PYTHON__", sys.executable),
    )
    write_executable(
        bin_dir / "date",
        """#!/usr/bin/env python3
import sys
arg = sys.argv[1] if len(sys.argv) > 1 else ""
if arg == "-Iseconds":
    print("2026-07-15T12:00:00+09:00")
elif arg == "+%-H":
    print("12")
elif arg == "+%Y-%m-%d":
    print("2026-07-15")
else:
    print("2026-07-15T12:00:00+09:00")
""",
    )
    write_executable(
        bin_dir / "loops",
        """#!/usr/bin/env python3
import sys
if len(sys.argv) > 1 and sys.argv[1] == "list-json":
    print("[]")
else:
    print("なし")
""",
    )
    write_executable(
        bin_dir / "curl",
        """#!/usr/bin/env python3
import os
import sys
args = sys.argv[1:]
target = args[-1] if args else ""
if "/api/status" in target:
    sys.exit(0)
if "/api/camera_proxy/" in target or "/api/frame.jpeg" in target:
    os.write(1, b"JPEGFIXTURE" * 20)
    sys.exit(0)
if target.endswith("/template"):
    print("# sensors\\nfixture sensor: on")
    sys.exit(0)
sys.exit(0)
""",
    )
    write_executable(
        bin_dir / "claude-fixture",
        """#!/usr/bin/env python3
import json
import os
import sys

argv = sys.argv[1:]
stdin = sys.stdin.read()

def append_trace(name, row):
    trace_dir = os.environ.get("EHA_SHADOW_TRACE_DIR", "")
    if not trace_dir:
        return
    os.makedirs(trace_dir, exist_ok=True)
    with open(os.path.join(trace_dir, name), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\\n")

def value_after(flag, default=""):
    try:
        return argv[argv.index(flag) + 1]
    except Exception:
        return default

model = value_after("--model", "")
has_schema = "--json-schema" in argv
has_tools = "--allowedTools" in argv
allowed_tools = value_after("--allowedTools", "")
mcp_config_path = value_after("--mcp-config", "")
mcp_config = None
mcp_config_text = None
if mcp_config_path and os.path.exists(mcp_config_path):
    try:
        with open(mcp_config_path, encoding="utf-8") as fh:
            mcp_config_text = fh.read()
        try:
            mcp_config = json.loads(mcp_config_text)
        except Exception:
            mcp_config = None
    except Exception as exc:
        mcp_config_text = f"<read failed: {exc}>"
actor = os.environ.get("EHA_ACTOR", "")
mode = os.environ.get("MODE", "")
if has_schema:
    try:
        schema = json.loads(value_after("--json-schema", "{}"))
        mode = schema.get("title", mode).replace("loop_", "").replace("_response", "") or mode
    except Exception:
        pass

append_trace(
    "claude_calls.jsonl",
    {
        "argv": sys.argv,
        "allowed_tools": allowed_tools,
        "mcp_config_path": mcp_config_path,
        "mcp_config": mcp_config,
        "mcp_config_text": mcp_config_text,
        "model": model,
        "mode": mode,
        "has_schema": has_schema,
        "actor": actor,
        "cwd": os.getcwd(),
    },
)

if model == "haiku":
    text = f"watch model={model};schema={int(has_schema)};tools={int(has_tools)};actor={actor or 'unset'}"
    print(json.dumps({"type": "result", "result": text}, ensure_ascii=False))
    sys.exit(0)

watch = ""
try:
    envelope = json.loads(stdin or "{}")
    blocks = envelope.get("message", {}).get("content", [])
    texts = [str(block.get("text", "")) for block in blocks if isinstance(block, dict)]
    joined = "\\n".join(texts)
    marker = "watch model="
    if marker in joined:
        watch = joined[joined.index(marker):].splitlines()[0]
except Exception:
    pass

private = (
    f"mode={mode};model={model};actor={actor or 'unset'};"
    f"schema={int(has_schema)};tools={int(has_tools)};watch={watch or 'none'}"
)
payload = {
    "type": "result",
    "structured_output": {
        "topic": "fixture",
        "private": private,
        "emotion": "calm",
        "speak": f"say {mode}",
        "proposal": None,
        "feature_presented": None,
    },
}
print(json.dumps(payload, ensure_ascii=False))
""",
    )


def make_runtime(
    root: Path,
    name: str,
    *,
    timestamp: str = DEFAULT_TIMESTAMP,
    today: str = DEFAULT_TODAY,
) -> tuple[Path, dict[str, str]]:
    run_root = root / name
    data_dir = run_root / "data"
    log_dir = run_root / "log"
    tmp_dir = run_root / "tmp"
    workdir = run_root / "workdir"
    home = run_root / "home"
    bin_dir = run_root / "bin"
    trace_dir = run_root / "trace"
    for path in (data_dir, log_dir, tmp_dir, workdir, home, bin_dir, trace_dir):
        path.mkdir(parents=True, exist_ok=True)
    install_fixture_bins(bin_dir)

    prefs = data_dir / "preferences.json"
    prefs.write_text(
        json.dumps(
            {
                "speakers": [{"room": "living"}],
                "cameras": [{"ha_entity": "camera.fixture", "label": "Fixture"}],
                "sensors": {"groups": []},
                "policies": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    character = data_dir / "character.md"
    character.write_text("# character\n", encoding="utf-8")
    body_location = data_dir / "body_location.json"
    body_location.write_text(json.dumps({"current_entity": ""}), encoding="utf-8")
    (log_dir / ".last_daybook").write_text(today, encoding="utf-8")

    path = f"{bin_dir}:{SCRIPT_DIR}:{os.environ.get('PATH', '')}"
    env = {
        **os.environ,
        "PATH": path,
        "HOME": str(home),
        "SCRIPT_DIR": str(SCRIPT_DIR),
        "EHA_TOOLS_PATH": str(bin_dir),
        "CLAUDE_BIN": str(bin_dir / "claude-fixture"),
        "EHA_CLAUDE_BIN": str(bin_dir / "claude-fixture"),
        "CLAUDE_CONFIG_DIR": str(home / "claude"),
        "EHA_DATA_DIR": str(data_dir),
        "EHA_LOG_DIR": str(log_dir),
        "EHA_TMP_DIR": str(tmp_dir),
        "TMPDIR": str(tmp_dir),
        "EHA_ANOMALY_STATE_FILE": str(log_dir / "anomaly_state.json"),
        "EHA_PREFS_FILE": str(prefs),
        "EHA_CHARACTER_FILE": str(character),
        "EHA_BODY_LOCATION_FILE": str(body_location),
        "EHA_CLAUDE_CWD": str(workdir),
        "EHA_AGENT_CWD": str(workdir),
        "EHA_SHADOW_RUN_ROOT": str(run_root),
        "EHA_SHADOW_TRACE_DIR": str(trace_dir),
        "EHA_NEXT_LISTEN_REQUEST_FILE": str(data_dir / "runtime" / "next_listen_request.json"),
        "EHA_NEXT_LISTEN_LOG_FILE": str(log_dir / "next_listen_log.jsonl"),
        "EHA_ACTIVE_LISTEN_LOG_FILE": str(log_dir / "active_listen_log.jsonl"),
        "EHA_TEST_TIMESTAMP": timestamp,
        "EHA_TEST_HOUR": "12",
        "EHA_SESSION_MODEL": "opus",
        "HA_URL": "http://fixture.local/api",
        "SUPERVISOR_TOKEN": "fixture-token",
        "INGRESS_PORT": "18099",
        "RESIDENT": "ユーザー",
    }
    env.pop("EHA_SESSION_BIN", None)
    return log_dir, env


def run_shadow_command(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    extra_env: dict[str, str] | None = None,
    timeout: int = 60,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> subprocess.CompletedProcess:
    run_env = {**env, **(extra_env or {})}
    if runner is not None:
        return runner(cmd, cwd=cwd, env=run_env)
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def normalize_trace_value(value, env: dict[str, str]):
    run_root = env["EHA_SHADOW_RUN_ROOT"]
    if isinstance(value, str):
        return value.replace(run_root, "<RUN_ROOT>")
    if isinstance(value, list):
        return [normalize_trace_value(item, env) for item in value]
    if isinstance(value, dict):
        return {key: normalize_trace_value(value[key], env) for key in sorted(value)}
    return value


def capture_wiring_trace(env: dict[str, str]) -> dict[str, list[dict]]:
    trace_dir = Path(env["EHA_SHADOW_TRACE_DIR"])
    trace = {
        "claude_calls": _read_jsonl(trace_dir / "claude_calls.jsonl"),
        "mcp_config_calls": _read_jsonl(trace_dir / "mcp_config_calls.jsonl"),
    }
    return normalize_trace_value(trace, env)


def comparable_wiring_trace(env: dict[str, str]) -> dict[str, list[dict]]:
    trace = capture_wiring_trace(env)
    for call in trace["claude_calls"]:
        call.pop("mcp_config_text", None)
    for call in trace["mcp_config_calls"]:
        call.pop("generated_config_text", None)
    return trace


def replace_flag_value(argv: list, flag: str, value) -> bool:
    try:
        index = argv.index(flag)
    except ValueError:
        return False
    if index + 1 >= len(argv):
        return False
    argv[index + 1] = value
    return True


def remove_flag_pair(argv: list, flag: str) -> tuple[list, list | None]:
    try:
        index = argv.index(flag)
    except ValueError:
        return argv[:], None
    if index + 1 >= len(argv):
        return argv[:], None
    return argv[:index] + argv[index + 2:], [argv[index], argv[index + 1]]


def normalize_allowed_tools_order(old_argv: list, new_argv: list) -> list:
    old_without_tools, old_tools = remove_flag_pair(old_argv, "--allowedTools")
    new_without_tools, new_tools = remove_flag_pair(new_argv, "--allowedTools")
    if old_tools is not None and old_tools == new_tools and old_without_tools == new_without_tools:
        return new_argv[:]
    return old_argv


def normalize_one_extra_run_root_bin(old_path: str, new_path: str) -> str:
    if old_path == new_path:
        return old_path
    marker = "<RUN_ROOT>/bin"
    old_parts = old_path.split(":")
    new_count = new_path.split(":").count(marker)
    if old_parts.count(marker) != new_count + 1 or new_count != 2:
        return old_path
    for index, part in enumerate(old_parts):
        if part != marker:
            continue
        candidate = ":".join(old_parts[:index] + old_parts[index + 1:])
        if candidate == new_path:
            return candidate
    return old_path


def normalize_mcp_path_counts(old_config: dict | None, new_config: dict | None) -> None:
    old_servers = (old_config or {}).get("mcpServers") or {}
    new_servers = (new_config or {}).get("mcpServers") or {}
    for name, old_server in old_servers.items():
        new_server = new_servers.get(name)
        if not isinstance(old_server, dict) or not isinstance(new_server, dict):
            continue
        old_env = old_server.get("env") or {}
        new_env = new_server.get("env") or {}
        if "PATH" not in old_env or "PATH" not in new_env:
            continue
        old_env["PATH"] = normalize_one_extra_run_root_bin(old_env["PATH"], new_env["PATH"])


def normalize_known_wiring_differences(
    old_trace: dict,
    new_trace: dict,
    *,
    phase2_normalizers: tuple[Callable[[dict, dict], None], ...] = (),
) -> tuple[dict, dict]:
    old_trace = copy.deepcopy(old_trace)
    new_trace = copy.deepcopy(new_trace)

    # Phase1-specific normalization rules for the historical loop.sh -> loop.py
    # parity suite. New invoke-agent.sh Phase2 rules should be added as separate
    # normalizers via phase2_normalizers, not folded into these loop.sh rules.
    for index, old_call in enumerate(old_trace["claude_calls"]):
        if index >= len(new_trace["claude_calls"]):
            continue
        new_call = new_trace["claude_calls"][index]
        old_argv = old_call.get("argv", [])
        new_argv = new_call.get("argv", [])

        # ② 2026-07-16 user decision: loop.py's real newline system prompt is
        # correct. loop.sh has long passed literal backslash+n text because
        # Bash double quotes do not unescape \n. Do not reproduce that bug in
        # loop.py; normalize the loop.sh trace to the loop.py value for this
        # one flag only.
        try:
            old_prompt = old_argv[old_argv.index("--append-system-prompt") + 1]
            new_prompt = new_argv[new_argv.index("--append-system-prompt") + 1]
        except (ValueError, IndexError):
            old_prompt = new_prompt = None
        if isinstance(old_prompt, str) and isinstance(new_prompt, str) and "\\n" in old_prompt and "\\n" not in new_prompt:
            replace_flag_value(old_argv, "--append-system-prompt", new_prompt)

        # ① 2026-07-16 user decision: allow only argv ordering drift caused by
        # --allowedTools. That flag is planned to disappear in the invoke-agent
        # Phase 2 cutover, and moving the identical flag/value pair is not
        # considered behaviorally meaningful. Any other flag ordering drift must
        # still remain RED.
        old_call["argv"] = normalize_allowed_tools_order(old_argv, new_argv)
        normalize_mcp_path_counts(old_call.get("mcp_config"), new_call.get("mcp_config"))

    for index, old_call in enumerate(old_trace["mcp_config_calls"]):
        if index >= len(new_trace["mcp_config_calls"]):
            continue
        normalize_mcp_path_counts(
            old_call.get("generated_config"),
            new_trace["mcp_config_calls"][index].get("generated_config"),
        )

    # ③ 2026-07-16 user decision: loop.py's two EHA_TOOLS_PATH occurrences
    # are the intended current value. loop.sh has one extra global PATH export
    # before constructing Claude/MCP env. normalize_mcp_path_counts keeps this
    # whitelist narrow: only remove one extra <RUN_ROOT>/bin when the resulting
    # PATH exactly equals loop.py's PATH.
    for normalizer in phase2_normalizers:
        normalizer(old_trace, new_trace)

    return old_trace, new_trace


def summarize_argv(argv: list) -> list:
    value_flags = {
        "--model",
        "--input-format",
        "--output-format",
        "--allowedTools",
        "--append-system-prompt",
        "--json-schema",
        "--mcp-config",
    }
    summary = []
    i = 0
    while i < len(argv):
        item = argv[i]
        if item in value_flags and i + 1 < len(argv):
            value = argv[i + 1]
            if item == "--append-system-prompt":
                value = {
                    "len": len(value),
                    "newlines": value.count("\n"),
                    "literal_backslash_n": value.count("\\n"),
                    "sha12": hashlib.sha256(value.encode()).hexdigest()[:12],
                }
            elif item == "--json-schema":
                try:
                    parsed = json.loads(value)
                    value = {
                        "title": parsed.get("title"),
                        "required": parsed.get("required"),
                    }
                except Exception:
                    value = {
                        "len": len(value),
                        "sha12": hashlib.sha256(value.encode()).hexdigest()[:12],
                    }
            elif item == "--allowedTools":
                value = [part for part in value.split(",") if part]
            summary.append([item, value])
            i += 2
        else:
            summary.append(item)
            i += 1
    return summary


def summarize_mcp_config(config: dict | None) -> dict:
    servers = (config or {}).get("mcpServers") or {}
    path_counts = {}
    for name, server in servers.items():
        path_value = (server.get("env") or {}).get("PATH", "")
        path_counts[name] = path_value.count("<RUN_ROOT>/bin")
    return {
        "server_names": list(servers),
        "path_run_root_bin_counts": path_counts,
    }


def summarize_wiring_delta(left: dict, right: dict) -> dict:
    summary: dict[str, list | dict] = {
        "claude_call_count": [len(left["claude_calls"]), len(right["claude_calls"])],
        "mcp_config_call_count": [len(left["mcp_config_calls"]), len(right["mcp_config_calls"])],
        "claude_call_deltas": [],
        "mcp_config_call_deltas": [],
    }
    for index in range(max(len(left["claude_calls"]), len(right["claude_calls"]))):
        l_call = left["claude_calls"][index] if index < len(left["claude_calls"]) else {}
        r_call = right["claude_calls"][index] if index < len(right["claude_calls"]) else {}
        if l_call == r_call:
            continue
        summary["claude_call_deltas"].append(
            {
                "index": index,
                "model": [l_call.get("model"), r_call.get("model")],
                "actor": [l_call.get("actor"), r_call.get("actor")],
                "mode": [l_call.get("mode"), r_call.get("mode")],
                "allowed_tools_equal": l_call.get("allowed_tools") == r_call.get("allowed_tools"),
                "mcp_config_path": [l_call.get("mcp_config_path"), r_call.get("mcp_config_path")],
                "mcp_config": [
                    summarize_mcp_config(l_call.get("mcp_config")),
                    summarize_mcp_config(r_call.get("mcp_config")),
                ],
                "argv": [summarize_argv(l_call.get("argv", [])), summarize_argv(r_call.get("argv", []))],
            }
        )
    for index in range(max(len(left["mcp_config_calls"]), len(right["mcp_config_calls"]))):
        l_call = left["mcp_config_calls"][index] if index < len(left["mcp_config_calls"]) else {}
        r_call = right["mcp_config_calls"][index] if index < len(right["mcp_config_calls"]) else {}
        if l_call == r_call:
            continue
        summary["mcp_config_call_deltas"].append(
            {
                "index": index,
                "argv": [l_call.get("argv"), r_call.get("argv")],
                "servers": [l_call.get("servers"), r_call.get("servers")],
                "generated_config": [
                    summarize_mcp_config(l_call.get("generated_config")),
                    summarize_mcp_config(r_call.get("generated_config")),
                ],
            }
        )
    return summary
