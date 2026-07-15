# loop.py Migration Runtime Contracts

`loop.py` migration switched the production daemon path from `loop.sh` to
`loop.py` on 2026-07-16 after the separate cutover red-team pass and follow-up
verification were completed. `loop.sh` remains in the repository for rollback.

This document tracks runtime files whose shape must remain compatible while the
Python port is shadow-tested against the shell loop.

## Cutover Blockers

- Cutover completed on 2026-07-16: `daemon.py` now invokes `loop.py` with
  `python3`.
- `loop.sh` is intentionally retained and must not be deleted yet; it is the
  rollback path if the cutover has to be reverted.
- Historical blockers are closed. `loop.py` was previously marked
  not cutover-ready until all five modes passed end-to-end shadow parity tests.
- `EHA_SESSION_BIN=agy` is not reimplemented in `loop.py`. The daemon cutover
  required operator/runtime use of `EHA_SESSION_BIN` to be audited or an
  `invoke-agent.sh` abstraction to be wired and tested.
  Phase1 audit on 2026-07-16 found no `EHA_SESSION_BIN` assignment in the main
  loop startup path: `embodied_ha/run.sh` sets `EHA_AUDIO_SESSION_BIN` and
  `EHA_ANTIGRAVITY_BIN` for audio/Antigravity support but not `EHA_SESSION_BIN`,
  `embodied_ha/config.yaml` has no `EHA_SESSION_BIN`, production
  `/config/embodied-ha/preferences.json` has no `EHA_SESSION_BIN`/`session_bin`
  entry, and the current Studio Code Server environment has no
  `EHA_SESSION_BIN`. Because `daemon.py` inherits its process environment, the
  running add-on/container environment was re-checked before cutover; an
  externally injected `EHA_SESSION_BIN=agy` would still hit the explicit
  `loop.py` cutover guard.
- After cutover, keep re-verifying that `agy --project <uuid>` / `agy --new-project`
  still behave as documented in the `invoke-agent.sh` MCP allow-list design
  (workspace-local `.agents/mcp_config.json` resolution, `--project`
  idempotency), and that Antigravity's `includeTools` actually restricts tool
  visibility in a live test. These behaviors were confirmed by empirical
  testing against a specific Antigravity CLI version, not from official
  documentation, and may have changed.

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
