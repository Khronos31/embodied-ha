"""Shared helpers for loop.sh -> loop.py shadow parity tests.

The harness deliberately starts with file-contract snapshots. Mode-specific
tests add command/MCP comparisons as each loop branch is ported.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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

