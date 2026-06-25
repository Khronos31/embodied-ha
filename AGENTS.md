# Embodied HA — Project Instructions

This is a **Home Assistant OS add-on** that runs as an autonomous agent inside the home.
It observes the home through sensors and cameras, speaks up, chats, and operates devices.

## Repository layout

```
embodied_ha/          # Add-on source (scripts, MCP servers, web UI)
  daemon.py           # Main daemon: 4 threads (watch/chat/explore schedulers + MQTT)
  watch.sh            # Observation loop: phase1(haiku) → phase2(sonnet+camera)
  chat.sh             # Conversation loop: chat + home appliance control
  explore.sh          # Exploration loop: read-only HA entity browsing
  ha-mcp.py           # MCP server: ha_get / ha_call_service
  ha-control-mcp.py   # MCP server: home appliance control (autonomous_control gate)
  memory-mcp.py       # MCP server: recall / remember / loops
  sociality-mcp.py    # MCP server: relationship / narrative / social_state / shared_focus
  sensors-mcp.py      # MCP server: get_sensors
  camera-mcp.py       # MCP server: camera snapshot
  tts-mcp.py          # MCP server: speak (TTS / push notification routing)
  mcp_lib.py          # Shared stdio JSON-RPC base for all MCP servers
  mcp-config.py       # MCP config generator (env injection per loop)
  discover.py         # Auto-discovery: generates preferences.json scaffolding
  render-sensors.py   # Renders sensor list from preferences.json
  motion-history.py   # Fetches motion sensor history from HA History API
  mem-context.py      # Trims memory.md to core + recent 40 entries for LLM context
  recall.sh           # Full-text search across observations/explore/chat_log/memory
  loops.sh            # Open loops (unfinished tasks / promises) manager
  speak.py            # TTS routing logic
  feature-flags.py    # Tracks which add-on features have been shown to the user
  web/                # Web UI (Ingress): settings, soliloquy room, chat
  config.yaml         # Supervisor manifest (version bump required for permission changes)
  run.sh              # Container entrypoint: env setup, auth check, daemon start
  config.sh           # Dev environment path defaults (overridden by run.sh in add-on)
tests/                # Test suite
personal_data/        # Personal config (excluded from public repo)
```

## Runtime environment (production)

- Runs as a **Home Assistant OS add-on** (slug `ff8b9363_embodied_ha`, currently v1.0.2+)
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
| Claude | `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>` |

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
- **`claude -p` tool restriction caveat**: `--allowedTools` per-tool filtering does NOT work
  when MCP servers are attached — the entire server's tools become available. Design
  access control at the MCP server level, not the tool-name level.

## Multi-agent setup

This project is developed collaboratively by multiple AI agents:
- **Claude** (SCS terminal): HA config, daily ops, overall add-on management
- **Codex** (SCS terminal): code implementation, architecture analysis, code review
- **Antigravity** (SCS terminal): design, image generation, Web UI improvements

When in doubt about the broader environment (HAOS persistence rules, HA safety constraints,
git SSH setup), refer to the global instructions file loaded by your tool.
