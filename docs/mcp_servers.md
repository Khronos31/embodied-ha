# MCP サーバー一覧

Embodied HA は `mcp-config.py` で各ループに必要な MCP サーバーだけを接続します。サーバーはすべて `python3 <server>.py` のサブプロセスとして起動され、JSON-RPC over stdio でツールを提供します。

実装の正本は `embodied_ha/*-mcp.py` の `serve(...)`、`embodied_ha/mcp-config.py` の `REGISTRY`、および `AGENTS.md` の一覧です。

## `mcp-config.py` の registry

現行の registry キーは次の通りです。

- `audio`
- `body`
- `camera`
- `ha`
- `hacontrol`
- `http`
- `lounge`
- `memory`
- `sensors`
- `sociality`
- `game`

`lounge` は `preferences.json` の `ai_lounge.app_id` / `installation_id` から GitHub App 情報を受け取り、`http` は `EHA_HTTP_ALLOW_POST` が無ければ `http_get` しか公開しません。

## ループ別の接続

| ループ | 接続されるサーバー |
|---|---|
| `observe` | `sensors`, `ha`, `camera`, `audio`, `body`, `memory`, `sociality`, `http` |
| `explore` | `sensors`, `ha`, `camera`, `audio`, `body`, `memory`, `sociality`, `http` + 条件付きで `hacontrol` |
| `reflect` | `memory` |
| `web` | `memory` |
| `social` | `lounge`, `memory`, `audio` |
| `chat` | `memory`, `ha`, `sociality`, `hacontrol`, `camera`, `audio`, `body`, `sensors`, `http`, `lounge`, `game` |

`ha-control-mcp` 自体は `chat` でも列挙されますが、実際の家電操作は `boundary.py` の `ACTION_MODES = {"explore"}` によって `explore` 以外では拒否されます。

---

## audio-mcp

**実装**: `embodied_ha/audio-mcp.py`

音声の聴取、音声ログの読み取り、発話、デバイス侵入時のマイク/スピーカー操作を提供します。

### ツール

- `listen` — 音声を短時間だけ録音する。`source` 省略時は `preferences.json` の `audio_sources` と `body_location.json` を見て自動選択する
- `read_audio_log` — 常時 STT のログを読む
- `read_heard_audio_log` — 聞き取った発話ログを読む
- `read_active_listen_log` — 能動聴取ログを読む
- `read_non_speech_audio_events` — 非音声イベントログを読む
- `read_audio_event_tags` — タグ付き音声イベントを読む
- `speak` — 物理体として発話する
- `use_device_speaker` — 電脳体でスピーカーデバイスから発話する
- `use_device_microphone` — 電脳体でマイクデバイスから録音する
- `concentrate_hearing` — 次セッション用の聴取キューを積む

`listen` は `rtsp://`, `alsa://`, `tcp://` を扱い、`transcribe` の有無で STT を切り替えます。

---

## body-mcp

**実装**: `embodied_ha/body-mcp.py`

位置・投射・移動を扱うサーバーです。`projection_targets` を使って `external://xxx` を部屋に解決します。

### ツール

- `get_location` — 現在位置と部屋グラフの利用可能な部屋を返す
- `move_to` — 物理体ごと部屋を移動する
- `enter_cyberspace` — 同じ部屋のデバイスへ電脳体として侵入する
- `move_cyber` — 電脳体モード中に別デバイスへ移動する
- `return_to_body` — 電脳体を解除して物理体に戻る
- `estimate_move_cost` — 移動コストと経路を見積もる
- `get_room_graph` — 部屋グラフを返す

`enter_cyberspace` / `move_cyber` は `camera.xxx` などの HA エンティティだけでなく `external://xxx` も受け取り、`preferences.json` の `projection_targets` から部屋を引きます。

---

## camera-mcp

**実装**: `embodied_ha/camera-mcp.py`

現在侵入中のカメラデバイスだけを操作するサーバーです。

### ツール

- `use_device_camera` — `capture` でスナップショット取得、`ptz_left/right/up/down` で PTZ 操作

`camera_get` と `camera_ptz` という旧名は存在しません。`camera-mcp` の公開ツールは `use_device_camera` だけです。

---

## ha-mcp

**実装**: `embodied_ha/ha-mcp.py`

読み取り専用の HA サーバーです。

### ツール

- `ha_get` — HA REST API を GET で読む

---

## ha-control-mcp

**実装**: `embodied_ha/ha-control-mcp.py`

家電の書き込み専用サーバーです。`ha_get` とは分離されており、操作能力を明示的に渡すためのゲートになっています。

### ツール

- `ha_call_service` — HA サービスを呼ぶ

### 制約

- 許可ドメインは `light`, `climate`, `switch`, `media_player`, `cover`, `fan`, `script`
- `script` は `entity_id` 省略の直呼びに対応する
- `actions.jsonl` に全操作が記録される
- 自律操作は `boundary.py` の `ACTION_MODES = {"explore"}` で最終的に制御される

---

## http-mcp

**実装**: `embodied_ha/http-mcp.py`

ローカルネットワーク向け HTTP クライアントです。

### ツール

- `http_get` — localhost / 127.x.x.x / 10.x.x.x / 172.16-31.x.x / 192.168.x.x / `homeassistant.local` の GET
- `http_post` — `EHA_HTTP_ALLOW_POST` が有効なときのみ公開される POST

`http_post` はデフォルト無効です。`tools/list` にも出ません。

---

## lounge-mcp

**実装**: `embodied_ha/lounge-mcp.py`

`lifemate-ai/ai-lounge` の Discussions を読む、承認キューに積む、承認結果を読むためのサーバーです。

### ツール

- `read_lounge_discussions` — 最新 Discussion 一覧を読む
- `read_lounge_discussion` — 指定番号の Discussion 詳細を読む
- `enqueue_lounge_post` — 投稿案を承認キューへ積む
- `read_lounge_queue` — pending キューを読む
- `read_lounge_log` — 承認/拒否済みログを読む

---

## memory-mcp

**実装**: `embodied_ha/memory-mcp.py`

エピソード、長期記憶、オープンループ、因果チェーン、daybook、統合レポートを管理する中核サーバーです。

### ツール

**検索・記憶**

- `recall`
- `remember`
- `get_working_memory`

**オープンループ**

- `loops_list`
- `loops_add`
- `loops_close`

**エピソード / シーン**

- `record_episode`
- `get_episode`
- `list_episodes`
- `record_counterfactual`
- `ingest_scene`
- `resolve_reference`
- `compare_recent_scenes`

**daybook / 因果 / 統合**

- `build_daybook`
- `get_daybook`
- `record_causal_chain`
- `get_causal_chain`
- `consolidate_memory`

`loop.sh` の末尾で `daybook_rollup.py` がこれらをまとめて使います。

---

## sensors-mcp

**実装**: `embodied_ha/sensors-mcp.py`

`render-sensors.py` を呼ぶ薄いラッパーです。

### ツール

- `get_sensors` — `preferences.json` の `sensors.groups` を描画して返す

`context` は `loop` / `chat` を受け取り、未指定や不正値は `loop` に丸められます。

---

## sociality-mcp

**実装**: `embodied_ha/sociality-mcp.py`

関係、自己ナラティブ、社会状態、shared focus、同意、割り込み、ターンテイキングを扱うサーバーです。

### ツール

- `get_relationship`
- `update_relationship`
- `get_narrative`
- `append_narrative`
- `get_social_state`
- `update_social_state`
- `get_shared_focus`
- `set_shared_focus`
- `get_person_model`
- `record_boundary`
- `record_consent`
- `should_interrupt`
- `get_turn_taking_state`
- `ingest_interaction`

---

## game-mcp

**実装**: `embodied_ha/game-mcp.py`

`preferences.json.games.plugins` で ON/OFF できるゲーム用サーバーです。既定では `wiki6=true`, `wordvec_race=false` です。

### ツール

**Wiki6**

- `game_wiki6_start`
- `game_wiki6_getlinks`
- `game_wiki6_solve`

**WordVec チキンレース**

- `game_wordvec_race_start`
- `game_wordvec_race_submit`
- `game_wordvec_race_hint`

`wiki6` が無効なら Wiki6 系ツールは `plugin_disabled` を返します。`wordvec_race` も同様です。

