# Embodied HA

**[日本語版はこちら](README-ja.md)**

An **autonomous HAOS add-on** that lives inside Home Assistant.

It watches the home through sensors and cameras as if they were its own senses, then speaks up, chats, or operates devices based on what it notices.

---

## Requirements

- **Home Assistant OS** with Supervisor
- **Mosquitto Broker** add-on for MQTT integration and HA entity registration
- **Claude** authentication, either via API key or Claude.ai subscription
- Architecture: `amd64` / `aarch64` (for RPi 4/5, etc.)

---

## Installation

1. Open HA **Settings → Add-ons → Add-on Store → ⋮ → Manage repositories**
2. Add the following repository URL and press **Add**

```
https://github.com/Khronos31/embodied-ha
```

3. Reload the store, then install **Embodied HA** when it appears

---

## Setup

### 1. Claude authentication

When the add-on starts, the Web UI opens through Ingress.

| Method | Steps |
|---|---|
| **API key** | Enter `claude_api_key` in the add-on configuration tab |
| **Claude.ai subscription** | In the Web UI setup screen, click “Log in with Claude.ai” and complete browser-based authentication at the shown URL |

### 2. Configuration options

| Option | Default | Description |
|---|---|---|
| `resident_name` | `ユーザー` | The resident's name, used by the agent in conversation |
| `claude_api_key` | empty | Anthropic API key. Leave empty when using a Claude.ai subscription |
| `claude_config_dir` | empty | Claude config directory path. If empty, defaults to `/config/embodied-ha/.claude` so it survives uninstall. To reuse Claude authentication from Studio Code Server, set `/config/.tools/claude-home` |
| `claude_cwd` | empty | Working directory used when launching Claude. Setting this to `/config` together with `claude_config_dir=/config/.tools/claude-home` lets it share memory with the Studio Code Server version of Claude Code |
| `autonomous_control` | `false` | When set to `true`, the watch and explore loops can also control home devices autonomously |

### 3. Automatic startup tasks

On startup, the add-on automatically:

- **Registers MQTT Discovery entities** in HA
- **Generates sensor drafts** by scanning HA entities for the initial observation configuration
- **Starts the daemon** after authentication completes, which then launches the three loops

---

## Features

### Three loops: watch, explore, chat

| Loop | Interval | Purpose |
|---|---|---|
| **Watch** `watch` | about 20 minutes + sensor triggers | Checks cameras and sensors, then generates observations, emotions, and speech |
| **Explore** `explore` | about 30 minutes | Proactively inspects the home, reflects, and optionally performs web search |
| **Chat** `chat` | on demand | Responds to chat input; also handles device control and memory search |

### What you can do in chat

- **Add a sensor** - “Keep an eye on the living room CO2 too”
- **Add a camera** - “Use the front-door camera too”
- **Control devices** - “Turn off the living room lights” (`autonomous_control` is not required for chat)
- **Manage loops** - “Remind me later” to record a pending task and bring it back naturally in watch/chat
- **Search memory** - “What was the air conditioner setting last week?”
- **Adjust the schedule** - “Check more often” to change the observation interval yourself

### HA automation integration

Publishing a string to the MQTT topic `embodied_ha/observe/trigger` immediately triggers a watch run that takes that context into account.

```yaml
# Example automation
action:
  - service: mqtt.publish
    data:
      topic: embodied_ha/observe/trigger
      payload: "The front door opened"
```

---

## Web UI

Open it from the add-on's **Open Web UI** button. You can also access it from the robot icon in the HA sidebar.

| Room | Purpose |
|---|---|
| **Conversation** | Chat and speech history with the agent |
| **Inner monologue** | The agent's private reflections during watch/explore |

From the settings screen (⚙), you can edit the character, sensors, speakers, cameras, and policy.

---

## HA entities

The following entities are registered automatically through MQTT Discovery at startup:

| Entity | Type | Purpose |
|---|---|---|
| `sensor.embodied_ha_observation` | sensor | Latest observation |
| `sensor.embodied_ha_last_speak` | sensor | Most recent spoken output |
| `sensor.embodied_ha_emotion` | sensor | Current emotion (`curious`, `calm`, `happy`, etc.; useful for lights and other effects) |
| `text.embodied_ha_chat` | text | Chat input from the HA UI to the add-on |
| `button.embodied_ha_observe` | button | Immediate watch trigger |

---

## Personalization

All configuration files are persisted under `/config/embodied-ha/` and can also be edited through Samba or File Editor.

| File | Contents | How to edit |
|---|---|---|
| `character.md` | The agent's personality, tone, and values | Web UI settings screen or File Editor |
| `preferences.json` | Sensor, speaker, camera, and entity mapping | Web UI settings screen or conversation |
| `desires.json` | Desire types and accumulation rates | File Editor |
| `extra_context.conf` | Personal extra context (one shell command per line) | File Editor |

### Desire system

Each desire in `desires.json` accumulates over time. When the threshold (`0.6`) is exceeded, it is injected into the watch-loop prompt as an "inner drive."

```json
{
  "check_weather": {
    "growth_rate": 0.033,
    "prompt": "I haven't checked the weather outside in a while. I'm curious what it's like now."
  }
}
```

### Long-term memory

`log/memory.md` is maintained in two layers:

- **Core memory** - Structural understanding of the home. Curated information is fed back into the full context.
- **Recent findings** - A chronological note appended after each observation. The latest 40 entries are included in context.
- **Rollup** - When "Recent findings" exceeds 120 entries, older entries are summarized and promoted into core memory, while the latest 60 entries are kept.

---

## Architecture

<img src="architecture.png" width="560" alt="Embodied HA architecture diagram">

Each loop launches its own Claude CLI session every time, and continuity is maintained through files such as `memory.md` and `observations.jsonl`.

---

## Data persistence

All logs and settings are saved under `/config/embodied-ha/` and survive add-on updates and restarts.

| Path | Contents |
|---|---|
| `character.md` | Character definition |
| `preferences.json` | Sensor, speaker, camera settings |
| `desires.json` | Desire definitions |
| `extra_context.conf` | Personal extra context |
| `log/memory.md` | Long-term memory |
| `log/observations.jsonl` | Observation log |
| `log/explore.jsonl` | Explore log |
| `log/chat_log.jsonl` | Conversation history |
| `log/open_loops.jsonl` | Open tasks and promises |

---

---

> Inspired by [lifemate-ai/embodied-claude](https://github.com/lifemate-ai/embodied-claude). Respect.
