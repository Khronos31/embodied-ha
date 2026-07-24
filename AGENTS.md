# Embodied HA — Project Instructions

This is a **Home Assistant OS add-on** that runs as an autonomous agent inside the home.
It observes the home through sensors and cameras, speaks up, chats, and operates devices.

## Repository layout

**Do not treat this as an exhaustive file index** — read the directory when you need the full list.
This groups files by role so you know where to look.

```
embodied_ha/
  # --- entrypoints / core loop ---
  daemon.py           # Main process: loop_scheduler thread (autonomous loop timing/probability),
                       #   MQTT listener threads (chat/loop triggers), audio_daemon watchdog thread,
                       #   body/desire/anomaly state ticking. Single-instance guarded via flock.
  chat.py             # Direct conversation loop (voice or text) — replies + home appliance control
  loop.py             # Autonomous loop dispatcher — MODE selects: observe / explore / reflect / web / social
  run.sh              # Container entrypoint: env setup, auth check, daemon start
  config.sh           # Historical migration snapshot; eha_config.py is authoritative

  # --- MCP servers (stdio JSON-RPC via mcp_lib.py; wired per-loop by mcp-config.py) ---
  audio-mcp.py        # listen / speak / use_device_microphone / use_device_speaker / concentrate_hearing
  body-mcp.py         # get_location / move_to / enter_cyberspace / move_cyber / return_to_body
  camera-mcp.py       # camera snapshot (HA camera_proxy + go2rtc frame API)
  ha-mcp.py           # read-only ha_get
  ha-control-mcp.py   # ha_call_service (gated by autonomous_control / boundary.py)
  http-mcp.py         # http_get / http_post
  lounge-mcp.py       # AI Lounge read + post-approval queue
  memory-mcp.py       # recall / remember / episodes / causal chains / loops
  sensors-mcp.py      # get_sensors
  sociality-mcp.py    # relationship / narrative / social_state / shared_focus / turn-taking
  game-mcp.py         # Wiki6, WordVecチキンレース等（preferences.json の games.plugins でON/OFF）
  mcp_lib.py          # Shared stdio JSON-RPC base for all MCP servers
  mcp-config.py       # MCP config generator — server registry + per-server env injection

  # --- persistent state (all writes atomic via os.replace()) ---
  body_state.py / desire_state.py / anomaly_state.py / sociality_state.py
  memory_state.py / scene_state.py / counterfactual_state.py

  # --- sensory / body-location model ---
  sensory_origin.py       # per-event attenuation_db / sensory_origin / move_cost classification
  body-context.py         # hearing_attenuation() + ambient "距離減衰" prompt block
  auditory_context.py     # recent auditory events prompt block + source_filter by current_entity
  listen_queue.py         # concentrate_hearing queued-listen request/consume lifecycle
  embodied_action.py      # applies action effects (move/speak/etc.) to body_state
  state_utils.py          # shared helpers incl. get_device_capabilities() (mic/speaker/camera lookup)
  boundary.py             # autonomous action / interruption boundary checks

  # --- audio ---
  audio_daemon.py     # always-on background listening daemon
  audio_stt.py        # speech-to-text transcription helpers

  # --- misc / support ---
  antigravity_setup.py    # agy (Antigravity CLI) install/auth state helpers
  discover.py             # Auto-discovery: generates preferences.json scaffolding
  render-sensors.py       # Renders sensor list from preferences.json
  motion-history.py       # Fetches motion sensor history from HA History API
  mem-context.py          # Trims memory.md to core + recent N entries for LLM context
  recent_chat_context.py  # Recent same-day chat context for prompts
  recall.sh / loops.sh    # Full-text search across logs / open-loops (unfinished tasks) manager
  daybook_rollup.py       # Rolls up daily logs
  init_fts.py             # Full-text-search index initialization
  speak.py                # TTS routing logic
  feature-flags.py        # Tracks which add-on features have been shown to the user

  web/                # Web UI (Ingress): settings (incl. loop_schedule, games tab), soliloquy room, chat
  config.yaml         # Supervisor manifest (version bump required for permission changes)

tests/                # pytest suite (test_*.py, one per module roughly)
personal_data/        # Personal config (excluded from public repo)
```

## Runtime environment (production)

- Runs as a **Home Assistant OS add-on** (slug `ff8b9363_embodied_ha`; check `config.yaml`'s `version` field for the current version — do not hardcode a number here, it changes often)
- Container entrypoint: `run.sh` → `daemon.py`
- Persistent data: `/config/embodied-ha/` (EHA_DATA_DIR, `config:rw` mount)
  - `character.md`, `preferences.json`, `extra_context.conf`, `log/`
- Add-on internal data: `/data/` — Claude auth only (`/data/.claude/`)
- HA API: `http://supervisor/core/api` + `SUPERVISOR_TOKEN` (requires `homeassistant_api: true` in config.yaml)
- MQTT: `core-mosquitto` — publishes 5 entities (sensor×3, text×1, button×1) via MQTT Discovery

## Development workflow

**Local dev path** (Studio Code Server on HAOS):
```
/config/GitHub/embodied-ha/   ← this repo (git remote: Khronos31/embodied-ha)
```

Edit files here, then sync to the installed add-on:
```bash
# Sync a file into the running add-on container
ha addons info ff8b9363_embodied_ha   # get ip_address
scp -i /config/.ssh/id_haos -P 22 embodied_ha/<file> root@<ip>:/app/

# Or restart the add-on to pick up rebuilt image
ha addons restart ff8b9363_embodied_ha
```

**⚠️ config.yaml permission changes require a version bump** — Supervisor does not re-evaluate
permissions on rebuild, only on version update + `ha addons update`.

## Commit convention

すべてのコミットメッセージの末尾に、作業したエージェントの `Co-Authored-By` トレーラーを付けること。

| エージェント | トレーラー |
|---|---|
| Codex | `Co-Authored-By: Codex <noreply@openai.com>` |
| Antigravity | `Co-Authored-By: Antigravity <noreply@google.com>` |
| Claude | `Co-Authored-By: Claude Code <noreply@anthropic.com>` |

例:
```
feat: add counterfactual state logging

Co-Authored-By: Codex <noreply@openai.com>
```

## Key constraints

- **Python style**: follow `ruff.toml` (E401/E701 disabled; focus on bug detection)
- **MCP protocol**: `mcp_lib.py` stdio JSON-RPC — do not break the wire format
- **Atomic writes**: use `os.replace()` for all JSON/Markdown file writes (see existing code)
- **No hardcoded personal data**: personal names, entity IDs, IPs belong in `preferences.json`
  or `personal_data/` (excluded from public repo via .gitignore)
- **`claude -p` tool restriction caveat**: `--allowedTools` enforces MCP tool-level execution
  restrictions when MCP servers are attached (verified with Claude Code 2.1.211), but it does
  not hide the connected server's tool list or schemas. Keep server-level connection controls
  and MCP-side gates as defense in depth; re-run the live canary after Claude CLI upgrades.

## Multi-agent setup

This project is developed collaboratively by multiple AI agents:
- **Claude** (SCS terminal): HA config, daily ops, overall add-on management, final deploy/version-bump gate
- **Codex** (SCS terminal): code implementation, architecture analysis, code review, bug investigation
- **Antigravity** (SCS terminal): design, image generation, **and all `web/` frontend implementation**
  (`index.html`/`app.js`/`style.css`) — this is full implementation ownership, not just mockups/CSS tweaks

**Web UI changes (2026-07-01 policy):** if you're Codex and a task touches `embodied_ha/web/`, hand the
frontend part off to Antigravity rather than implementing it yourself — backend routes in `web/server.py`
are still yours. Before any Web UI change ships, Claude verifies it locally (dev server + Playwright) as
the final gate; do not report a Web UI task "done" without that verification having happened.

When in doubt about the broader environment (HAOS persistence rules, HA safety constraints,
git SSH setup), refer to the global instructions file loaded by your tool.
