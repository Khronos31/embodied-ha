# システムアーキテクチャ

実装ファイル: `embodied_ha/run.sh`, `embodied_ha/daemon.py`

## 起動フロー

```
HAOS アドオン起動
    ↓
embodied_ha/run.sh      ← アドオンのエントリポイント
    ↓ (exec)
embodied_ha/daemon.py   ← 常駐プロセス（フォアグラウンド）
    ├── MQTT 購読スレッド × 2
    ├── watch スケジューラスレッド
    ├── explore スケジューラスレッド
    └── audio_daemon.py (オプション、サブプロセス)
```

## run.sh の処理内容

`run.sh` はアドオンの初期化をすべて担い、最後に `exec daemon.py` でデーモンに引き継ぐ。

1. `/data/options.json` から設定を読み込み、環境変数にエクスポート:
   - `RESIDENT` — 居住者名（デフォルト「ユーザー」）
   - `EHA_AUTONOMOUS` — 自律操作ゲート（`0`/`1`）
   - `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASS`
   - `HA_URL`, `SUPERVISOR_TOKEN`
   - `GO2RTC_BASE`, `EHA_DATA_DIR`, `EHA_LOG_DIR`, `EHA_PREFS_FILE` など多数

2. PulseAudio ソケットを探索してセット（`/run/audio/native` 等）

3. 永続データディレクトリの決定:
   - `/config/embodied-ha/` がマウントされていればそこを使用
   - 未マウント時は `/data/embodied-ha/` にフォールバック

4. `discover.py` を実行して HA のセンサー・メディアプレーヤーを自動発見

5. MQTT discovery パケットを送信して HA に7つのエンティティを登録:
   - `sensor.embodied_ha_observation` — 最後の観察内容
   - `sensor.embodied_ha_last_speak` — 最後の発話
   - `sensor.embodied_ha_emotion` — 現在の感情状態
   - `text.embodied_ha_chat` — チャット入力（set トピックで受信）
   - `button.embodied_ha_observe` — 手動観察トリガー
   - `sensor.embodied_ha_body_physical_room` — 物理的な身体の部屋
   - `sensor.embodied_ha_body_current_place` — 現在の存在場所（電脳体含む）

6. Claude API 認証の確認（API キー直接指定または `.credentials.json`）

7. `exec python3 daemon.py` でデーモンに制御を移譲

## daemon.py の構造

`daemon.py` はシングルプロセスで複数のスレッドを管理する。

```python
SCHEDULE_INTERVAL = 1200  # watch: 20分
EXPLORE_INTERVAL  = 1800  # explore: 30分
SENSOR_COOLDOWN   = 300   # センサートリガーのクールダウン: 5分
WATCH_TIMEOUT     = 600   # watchの最大実行時間: 10分
CHAT_TIMEOUT      = 300   # chatの最大実行時間: 5分
EXPLORE_TIMEOUT   = 600   # exploreの最大実行時間: 10分
```

### 多重起動ガード

`flock` によるロックファイル (`log/daemon.lock`) で二重起動を防止。

### MQTT 購読スレッド

2つのバックグラウンドスレッドがそれぞれ `mosquitto_sub` を永続実行し、切断時は5秒後に再接続する:

| トピック | ハンドラ | 処理 |
|---|---|---|
| `embodied_ha/chat/set` | `on_chat_trigger()` | payload をユーザー発言として `run_chat()` を呼ぶ |
| `embodied_ha/observe/trigger` | `on_observe_trigger()` | payload を trigger_reason として `run_watch()` を呼ぶ |

`embodied_ha/chat/set` の payload は JSON（`{"message": "...", "source": "chat"}`）でも生テキストでも受け付ける。

### スケジューラスレッド

2つのスケジューラが独立して動く:

**watch スケジューラ**
```python
while True:
    time.sleep(SCHEDULE_INTERVAL)  # 20分待機
    active_desires, pressure = tick_desires(...)
    body = tick_body_state("watch", ...)
    chance = body_state.compute_run_chance(60, body, "watch")
    if random.randint(1, 100) <= chance:
        run_watch(...)
```

**explore スケジューラ**
```python
while True:
    time.sleep(EXPLORE_INTERVAL)  # 30分待機
    anomaly_urgency = _anomaly_urgency()
    active_desires, pressure = tick_desires(...)
    body = tick_body_state("explore", ...)
    chance = body_state.compute_run_chance(50, body, "explore")
    if random.randint(1, 100) <= chance:
        run_explore(...)
```

各スケジューラは、ループ開始前に `body_state.advance_tick()` と `desire_state.decay_tick()` を呼んで状態を更新し、`compute_run_chance()` の結果で実行の可否を確率的に決定する。

### ループの排他制御

各ループ（watch/chat/explore）には `threading.Lock()` が1つずつあり、`acquire(blocking=False)` で多重起動をブロック。すでに実行中の場合は即座にスキップしてログに記録する。

### 状態の受け渡し

daemon からループスクリプトへの情報はすべて**環境変数**で渡される:

```bash
TRIGGER_REASON=...          # watch のトリガー経緯
EHA_BODY_STATE='{...}'      # body_state の公開フィールド JSON
ACTIVE_DESIRES='["..."]'    # 発火した欲求のプロンプト配列 JSON
CHAT_MESSAGE=...            # ユーザーの発言（chat.sh のみ）
CHAT_SOURCE=...             # 発言ソース（chat.sh のみ）
```

### audio_daemon watchdog

`preferences.json` の `audio_sources` に `stt_enabled: true` のソースがあれば、`audio_daemon.py` をサブプロセスとして起動する。

<!-- TODO: diagram — run.sh → daemon.py → 3ループの起動フロー図 -->

## 全体的なデータフロー

```
HA (MQTT)
  │ embodied_ha/chat/set      → daemon → chat.sh  → claude CLI
  │ embodied_ha/observe/trigger → daemon → watch.sh → claude CLI
  ↑                                            ↓
  MQTT publish                         MCP servers
  (observation/state,                  (sensors, ha, camera, audio,
   last_speak/state,                    body, memory, sociality...)
   emotion/state 等)
```

ループスクリプト（watch.sh / explore.sh / chat.sh）は `claude` CLI のサブプロセスとして Claude を起動し、結果を JSON で受け取って MQTT に publish する。Claude は MCP サーバー経由で HA のセンサー・カメラ・音声・記憶などにアクセスする。

## 永続データの配置

すべての永続データは `EHA_DATA_DIR`（通常 `/config/embodied-ha/`）以下に置かれる:

| パス | 内容 |
|---|---|
| `preferences.json` | ユーザー設定 |
| `body_state.json` | ホメオスタシスベクター |
| `body_location.json` | 電脳体位置状態 |
| `desire_state.json` | 欲求状態ランタイム |
| `anomaly_state.json` | 異常検知状態 |
| `floorplan_room_graph_draft.json` | 部屋グラフ（移動コスト計算用） |
| `log/memory.md` | 長期記憶 |
| `log/open_loops.jsonl` | オープンループ（やりかけ） |
| `log/episodes/` | 構造化エピソード |
| `log/daybooks/` | 日次サマリー |
| `log/actions.jsonl` | 家電操作ログ |
| `log/body_location_log.jsonl` | 電脳体移動ログ |
| `log/audio_log.jsonl` | 常時 STT ログ |
