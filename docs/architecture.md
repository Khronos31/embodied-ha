# システムアーキテクチャ

実装ファイル: `embodied_ha/run.sh`, `embodied_ha/daemon.py`, `embodied_ha/loop.py`, `embodied_ha/chat.py`

## 起動フロー

```
Home Assistant OS
    ↓
embodied_ha/run.sh      ← アドオンのエントリポイント
    ↓ (exec)
embodied_ha/daemon.py   ← 常駐プロセス
    ├── MQTT 購読スレッド × 2（chat/loop トリガー）
    ├── loop_scheduler スレッド（30分周期の自律ループ）
    ├── web_server_watchdog スレッド（Ingress UI を常駐再起動）
    └── audio_daemon_watchdog スレッド（`mics.stt_enabled` があれば起動）
```

## `run.sh` の役割

`run.sh` はアドオンの初期化をまとめて行い、最後に `exec python3 daemon.py` へ制御を渡します。

1. `/data/options.json` を読み、`RESIDENT`, `EHA_AUTONOMOUS`, `MQTT_*`, `HA_URL`, `SUPERVISOR_TOKEN`, `EHA_PREFS_FILE` などを環境変数に展開する
2. `discover.py` で初期の `preferences.json` 下書きを整える
3. MQTT discovery で `sensor.embodied_ha_observation`, `sensor.embodied_ha_last_speak`, `sensor.embodied_ha_emotion`, `text.embodied_ha_chat`, `button.embodied_ha_observe`, `sensor.embodied_ha_body_physical_room`, `sensor.embodied_ha_body_current_place` を登録する
4. Claude 認証の有無を確認する
5. `daemon.py` を起動する

## `daemon.py` の役割

`daemon.py` は 1 プロセスで複数のスレッドを管理します。

- `embodied_ha/chat/set` を受けると `chat.py` を起動する
- `embodied_ha/loop/trigger` を受けると `loop.py` を `MODE=observe` で起動する
- 30分ごとに `loop_scheduler` が走り、`body_state.advance_tick()` と `desire_state.decay_tick()` を通して `compute_run_chance()` を評価する
- `mics` に `stt_enabled: true` があれば `audio_daemon.py` を監視起動する
- `web/server.py` は別スレッドで常駐再起動する
- `flock` で多重起動を防ぐ

### ループ確率

`loop_scheduler` は `preferences.json` の `loop_schedule` を読み、時刻帯の基準確率を決めます。

| 時帯 | 既定の基準 |
|---|---|
| 0-6時 | `night_probability` |
| 22-24時 | `late_probability` |
| それ以外 | `day_probability` |

そこに `body_state`、欲求圧、`anomaly_state.compute_explore_urgency()` を加えて最終確率を決めます。

## 主要なデータフロー

```
MQTT / HA / sensors
    ↓
daemon.py
    ├── chat.py  → selected agent harness → reply / preferences_update / speak
    └── loop.py  → selected agent harness → observe / explore / reflect / web / social
                   ↓
             MCP servers via mcp-config.py
```

`loop.py` の `observe` はカメラ観察、`explore` は自由探索、`reflect` は内省、`web` は WebSearch、`social` は AI Lounge を担当します。`loop.py` の末尾では前日の観察ログを `daybook_rollup.py` に渡し、daybook 生成と `memory.md` の統合を行います。

## 永続データ

主な永続データは `EHA_DATA_DIR`（通常 `/config/embodied-ha/`）以下に置かれます。

| パス | 内容 |
|---|---|
| `preferences.json` | 設定 |
| `body_state.json` | ホメオスタシス |
| `body_location.json` | 電脳体の位置 |
| `desire_state.json` | 欲求状態 |
| `anomaly_state.json` | 異常検知状態 |
| `log/memory.md` | 長期記憶 |
| `log/daybooks/` | 日次要約 |
| `log/actions.jsonl` | 家電操作ログ |
| `log/body_location_log.jsonl` | 位置移動ログ |
| `log/audio_log.jsonl` | 常時 STT ログ |
