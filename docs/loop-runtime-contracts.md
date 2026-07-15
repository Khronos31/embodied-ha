# loop.py Migration Runtime Contracts

`loop.py` migration keeps `loop.sh` as the production path until a separate
cutover conductor/red-team pass approves switching `daemon.py`.

This document tracks runtime files whose shape must remain compatible while the
Python port is shadow-tested against the shell loop.

## Cutover Blockers

- `daemon.py` must keep invoking `loop.sh` during this migration.
- `loop.py` must remain explicitly not cutover-ready until all five modes pass
  end-to-end shadow parity tests.
- `EHA_SESSION_BIN=agy` is not reimplemented in `loop.py`. Before any future
  daemon cutover, operator/runtime use of `EHA_SESSION_BIN` must be audited or
  an `invoke-agent.sh` abstraction must be wired and tested.

## Consumer Inventory

| File | Writers | Consumers | Contract notes |
|---|---|---|---|
| `observations.jsonl` | `loop.sh` observe, future `loop.py` observe, `chat.sh` context paths | `chat_context.py`, `recent_chat_context.py`, `recall.sh`, `daybook_rollup.py`, `web/server.py`, memory tests | JSONL. Preserve `timestamp`, `emotion`, `private`; optional `facts`, `ungrounded_speech_claim`, `ungrounded_visual_claim`. Parse failures must not be written here. |
| `explore.jsonl` | `loop.sh` non-observe modes, future `loop.py` non-observe modes | `chat_context.py`, `recent_chat_context.py`, `recall.sh`, `web/server.py` | JSONL. Preserve `timestamp`, `mode`, `emotion`, `private`, `topic`; optional grounding flags. Parse failures must not be written here. |
| `loop_parse_errors.jsonl` | `loop.sh`, future `loop.py` | diagnostics/tests | JSONL. Preserve `timestamp`, `mode`, `reason`, `raw`. This is the only place raw parse-failure text may be persisted. |
| `pending_proposal.json` | `loop.sh`, future `loop.py` | `chat_context.py`, `chat_postprocess.py`, chat prompt assembly | JSON object. Preserve `timestamp`, `proposal`, `action`. Write only when action has `domain`, `service`, and `entity_id`. |
| `chat_log.jsonl` | `loop.sh`, `chat.py`/`chat.sh`, `audio-mcp.py` | `recent_chat_context.py`, `chat_context.py`, `recall.sh`, `web/server.py` | JSONL. Loop-origin records preserve `timestamp`, `source`, `claude`, `user: null`. |
| observe scene/watch artifacts | `loop.sh` observe, future `loop.py` observe | `scene_state.py`, memory scene ingestion, observe tests | Preserve `scene_objects`, `scene_people`, `scene_changes` ingestion behavior. Watch reports are prompt context, not normal memory rows by themselves. |
| sociality state and relationship logs | sociality MCP, future social loop path | `sociality-mcp.py`, sociality tests, future prompt context | Keep strict tool argument validation. Invalid payloads are diagnostic-only and must not mutate relationship state. |

## Shadow Parity Scope

Each migrated mode must compare the real shell path and Python path with the
same fixture inputs:

- Claude argv and key environment values.
- MCP config generator inputs.
- Runtime side effects in the files listed above.
- Absence of normal persistence when parsing fails or introspection is empty.

