#!/usr/bin/env python3
"""Initialize/rebuild the SQLite FTS5 memory index."""

from __future__ import annotations

import hashlib
import os

import memory_state as ms
from state_utils import clean

_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.environ.get("EHA_LOG_DIR", os.path.join(_DIR, "log"))


def import_memory_md(log_dir: str | None = None) -> int:
    log_dir = log_dir or LOG_DIR
    path = os.path.join(log_dir, "memory.md")
    if not os.path.exists(path):
        return 0
    count = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            text = clean(line)
            if not text or text.startswith("#") or text.startswith("---"):
                continue
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
            ms.index_episode_to_fts(
                log_dir,
                {
                    "id": f"mem_{digest}",
                    "timestamp": "",
                    "kind": "memory",
                    "source": "memory.md",
                    "summary": text,
                    "detail": text,
                },
            )
            count += 1
    return count


def main() -> None:
    episodes = ms.rebuild_fts_index(LOG_DIR)
    memory_lines = import_memory_md(LOG_DIR)
    print(f"fts_index rebuilt: episodes={episodes} memory_lines={memory_lines}")


if __name__ == "__main__":
    main()
