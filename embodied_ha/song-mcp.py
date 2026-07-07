#!/usr/bin/env python3
"""VOICEVOX Song MCP server for recording WAV files."""

from __future__ import annotations

import json

from mcp_lib import serve, text
from voicevox_song import is_installed, plugin_disabled_payload, synthesize_song

TOOL_RECORD = {
    "name": "record",
    "description": (
        "VOICEVOX Songで短い歌声WAVを生成する。音声ファイルを生成するだけで、これ単体では音は鳴らない。"
        "生成後に実際に鳴らしたい場合は、戻り値のfile_pathをspeakに渡して再生する。"
        "pitchはC4/D#4/Bb3等の音名またはrest、"
        "durationはwhole/half/quarter/eighth/sixteenthで指定する。"
        "lyricは発音そのままのべた書きで、助詞の『は』を『わ』と歌わせたい場合はlyric='わ'のように書く。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "bpm": {"type": "number", "description": "テンポ。デフォルト100"},
            "notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "pitch": {"type": "string", "description": "C4、D#4、Bb3、または rest"},
                        "duration": {"type": "string", "enum": ["whole", "half", "quarter", "eighth", "sixteenth"]},
                        "lyric": {"type": "string", "description": "発音そのままの歌詞。rest以外では必須"},
                    },
                    "required": ["pitch", "duration"],
                },
            },
        },
        "required": ["notes"],
    },
}


def sing(args: dict):
    if not is_installed():
        return [text(json.dumps(plugin_disabled_payload(), ensure_ascii=False))], True
    try:
        result = synthesize_song(args if isinstance(args, dict) else {})
        return [text(json.dumps(result, ensure_ascii=False))], False
    except ValueError as exc:
        return [text(json.dumps({"error": "invalid_score", "message": str(exc)}, ensure_ascii=False))], True
    except Exception as exc:
        return [text(json.dumps({"error": "synthesis_failed", "message": str(exc)}, ensure_ascii=False))], True


TOOLS = {"record": {"spec": TOOL_RECORD, "handler": sing}}


if __name__ == "__main__":
    serve("song-mcp", "1.0", TOOLS)
