# ループモデル

実装ファイル: `embodied_ha/loop.sh`, `embodied_ha/chat.sh`, `embodied_ha/daemon.py`

Embodied HA は `loop.sh` を中心にした 5 モードの自律ループと、`chat.sh` の会話ループで動きます。旧来の `watch.sh` / `explore.sh` は存在せず、現在は `loop.sh` に統合されています。

## 全体像

- `daemon.py` が 30分ごとの `loop_scheduler` を回す
- `loop.sh` は `observe / explore / reflect / web / social` の 5 モードを持つ
- `chat.sh` はユーザー発話に応答しつつ `preferences_update` を書き込む
- `daybook` と `memory` の統合は `loop.sh` の末尾から `daybook_rollup.py` を呼ぶことで行う
- `anomaly_state` の更新は `loop.sh` の冒頭で行う

## `loop.sh` の冒頭でやっていること

1. `render-sensors.py --context loop` で主要センサーを取得する
2. `anomaly_state.detect_anomalies()` を `SENSORS_DATA` と `OPEN_LOOPS_JSON` から実行する
3. `ANOMALY_CONTEXT` と `ANOMALY_URGENCY` を作る
4. `body_state` と `active_desires` に基づいてモード重みを決める
5. `MODE` が未指定なら `observe / explore / reflect / web / social` から選ぶ

`reflect` と `web` は `memory` 中心、`social` は `lounge` 中心です。`social` は GitHub App 証明書が無い場合、重みが 0 になります。

## 5 モード

| MODE | 役割 | 主要サーバー | 備考 |
|---|---|---|---|
| `observe` | カメラ選択→観察 | `sensors`, `ha`, `camera`, `audio`, `body`, `memory`, `sociality`, `http` | 最初に Haiku でカメラを選び、その後 Sonnet で本観察する |
| `explore` | 家を自由に調べる | `sensors`, `ha`, `camera`, `audio`, `body`, `memory`, `sociality`, `http` | `boundary.py` が許可したときだけ `hacontrol` が使える |
| `reflect` | 内省 | `memory` | `recall` / `remember` / `loops_add` が中心 |
| `web` | WebSearch で調べ物 | `memory` | Claude の組み込み `WebSearch` を使う |
| `social` | AI Lounge に参加 | `lounge`, `memory`, `audio` | 投稿は承認キュー経由 |

`explore` モードであっても、自律操作は `boundary.py` の `ACTION_MODES = {"explore"}` によって再確認されます。`ha-control-mcp` はこの経路を通ったときだけ実際の家電操作に使われます。

## `observe`

- カメラ一覧から候補を作る
- `claude haiku` に「今どのカメラを見るべきか」を JSON で決めさせる
- 選ばれたカメラ画像を付けて `claude sonnet` で観察させる
- `scene_state.ingest_scene_parse()` に scene を渡す
- `observations.jsonl` に `private` と `emotion` を書く

## `explore`

- `get_sensors` と `ha_get` で家の状態を掘る
- 必要なら `use_device_camera` / `listen` を使う
- `move_to` / `enter_cyberspace` / `move_cyber` / `return_to_body` を使える
- 発見があれば `record_episode` / `record_causal_chain` / `loops_add` を使う
- `EHA_AUTONOMOUS=1` かつ境界が許す場合に限り、`ha_call_service` で家電操作できる
- `pending_proposal.json` に提案があれば、それも文脈に入る

## `reflect`

- `recall` と `remember` で内省する
- `loops_add` で後で気にかけることを追加する
- 家電操作や Web 調査はしない

## `web`

- `WebSearch` で調べる
- 面白かったことを `remember` に残す
- 余計な家電操作はしない

## `social`

- `read_lounge_discussions` と `read_lounge_log` で流れを読む
- 必要なら `read_lounge_discussion` で詳細を開く
- 投稿案は `enqueue_lounge_post` で承認キューへ入れる

## `chat.sh`

`chat.sh` はユーザー発話に対する会話ループです。実行前に次の文脈を集めます。

1. `observations.jsonl` と `explore.jsonl` の最近の活動
2. `memory.md` の長期記憶
3. `chat_log.jsonl` の直近会話
4. `render-sensors.py --context chat` の出力
5. `loops list` の結果
6. `pending_proposal.json`
7. `sociality_state` の turn-taking 状態

`chat.sh` の出力 JSON は `reply`, `emotion`, `preferences_update`, `proposal_resolved` が中心です。`preferences_update` では実際の `preferences.json` を更新できます。

### `chat.sh` の自動更新オペレーション

| オペ名 | 役割 |
|---|---|
| `cameras_add` / `cameras_remove` | カメラの追加・削除 |
| `speakers_set` | 部屋ごとのスピーカー設定を更新・追加 |
| `presence_set` | 在宅判定エンティティを更新 |
| `policies_add` | ポリシーを追加 |
| `sensors_add` / `sensors_remove` | 主要センサーを追加・削除 |
| `entities_add` / `entities_remove` | 操作対象エンティティを追加・削除 |

`speakers_set` は現在の実装で list 形式の `speakers` に正規化されます。`policies_remove` と `speakers_update` は存在しません。

## `loop.sh` の末尾

`loop.sh` は各モードの JSON を書き出した後、前日の `observations.jsonl` があれば `daybook_rollup.py` を起動します。

`daybook_rollup.py` は次を行います。

- 観察ログを day 単位に圧縮する
- `memory-mcp` の `build_daybook` を呼ぶ
- `memory.md` に daybook の brief を追記する
- `CONSOLIDATE_MEMORY=1` のとき `consolidate_memory` を呼ぶ
- `log/.last_daybook` を更新する

`daemon.py` はこの marker を見て、daybook が数日止まっていれば警告を出します。

## `daemon.py` と確率制御

`daemon.py` の `run_chance()` は `preferences.json` の `loop_schedule` を見て、`body_state.compute_run_chance()` に渡します。そこに `desire_pressure` と `anomaly_urgency` が加算されます。

| ループ名 | 基準値 | 主な加算 |
|---|---|---|
| `loop` | 時間帯別の `day / late / night` | 欲求圧 + 異常緊急度 |
| `chat` | 時間帯別の `day / late / night` | 欲求圧 |

`loop/trigger` の手動起動は `MODE=observe` で `loop.sh` を呼びます。したがって手動観察はまず `observe` から入ります。

