# 3ループモデル

実装ファイル: `embodied_ha/watch.sh`, `embodied_ha/explore.sh`, `embodied_ha/chat.sh`, `embodied_ha/daemon.py`

Embodied HA は3つの独立したループで動作する。各ループは `bash <script>.sh` で起動するシェルスクリプトであり、`claude` CLI を呼び出して Claude Sonnet（または Haiku）に処理させ、返ってきた JSON を解析して副作用を実行する。

<!-- TODO: diagram — 3ループのトリガー・実行・出力の関係図 -->

## watch ループ（観察）

**実装**: `embodied_ha/watch.sh`（781行）

### トリガー条件

| 種別 | 条件 | 備考 |
|---|---|---|
| 定期実行 | 20分ごと（`SCHEDULE_INTERVAL = 1200`） | `compute_run_chance()` による確率判定あり（基準値 60/100） |
| MQTT 手動 | `embodied_ha/observe/trigger` に publish | クールダウンなし。`_watch_lock` で多重実行防止 |
| センサートリガー | 人感センサー等のオートメーションから | 5分クールダウン（`SENSOR_COOLDOWN = 300`） |

タイムアウト上限: 600秒（10分）

### 処理フロー（2フェーズ）

**フェーズ1: カメラ選択（claude haiku）**

センサー状態・人感履歴・anomaly 状態・身体位置を入力として、軽量な haiku モデルに「今どのカメラを見るべきか」を JSON で判断させる。複数カメラが有効な場合でも、不要なカメラ画像を取得しないための最適化。

**フェーズ2: 本観察（claude sonnet）**

フェーズ1で選択したカメラ画像＋センサー状態＋長期記憶＋身体状態＋発火した欲求を入力として Sonnet に観察させる。

MCP サーバー:
- 常時: `sensors`, `ha`, `camera`, `audio`, `body`, `memory`, `sociality`, `http`
- `EHA_AUTONOMOUS=1` のとき追加: `hacontrol`（家電操作ツール）

### 出力（JSON フィールド）

| フィールド | 型 | 処理 |
|---|---|---|
| `private` | string | ログにのみ記録。MQTT には出さない |
| `emotion` | string | MQTT `embodied_ha/emotion/state` に publish |
| `speak` | string | 発話内容。`boundary.py` で可否判定後に `speak.py` で TTS 実行 |
| `speak_room` | string | 発話先の部屋（未指定時は preferences の最初のスピーカー） |
| `schedule` | object | 次回実行のヒント（`schedule.json` に保存してスケジューラが参照） |
| `observation` | string | MQTT `embodied_ha/observation/state` に publish |

発話後、`boundary.py` で拒否された場合は `counterfactual_state` に「話そうとしたが止まった」を記録する。

### 日次サマリー（daybook rollup）

watch 完了後、当日の観察ログが一定数に達すると `daybook_rollup.py` が起動し、`memory-mcp` の `build_daybook` を呼んで日次圧縮（daybook）を生成・保存する。

---

## explore ループ（自律探索）

**実装**: `embodied_ha/explore.sh`（499行）

### トリガー条件

| 種別 | 条件 | 備考 |
|---|---|---|
| 定期実行 | 30分ごと（`EXPLORE_INTERVAL = 1800`） | `compute_run_chance()` による確率判定あり（基準値 50/100） |

タイムアウト上限: 600秒（10分）

watch とは異なり、MQTT からの手動トリガーはない。anomaly urgency が高いと `compute_run_chance()` への加算が増え、実行確率が上がる。

### 3モードのランダム選択

body_state の値で重み付けしたうえでランダムにモードを選ぶ:

```
explore: 50%  (好奇心が高いほど weight 増加)
reflect: 30%  (ストレスが高いほど weight 増加)
web:     20%  (confidence が中程度のとき weight 増加)
```

**explore モード** — `get_sensors`, `ha_get`, `camera_get`, `listen` で家を自由に調査

MCP サーバー: `sensors`, `ha`, `camera`, `audio`, `body`, `memory`, `sociality`, `http`（`EHA_AUTONOMOUS=1` なら `hacontrol` も）

**reflect モード** — `recall` で過去のエピソードを思い返す内省

MCP サーバー: `memory`, `body`, `sociality`

**web モード** — 好奇心に従って `WebSearch` で調べ物

MCP サーバー: `memory`, `body`（Web 検索は claude CLI の組み込みツール `WebSearch` を使用）

### 出力（JSON フィールド）

| フィールド | 型 | 処理 |
|---|---|---|
| `private` | string | ログにのみ記録 |
| `emotion` | string | MQTT `embodied_ha/emotion/state` に publish |
| `speak` | string | `boundary.py` で可否判定後に TTS |
| `speak_room` | string | 発話先の部屋 |
| `proposal` | object | 家電操作提案。`pending_proposal.json` に保存。chat.sh が `proposal_resolved` で消化する |
| `observation` | string | MQTT `embodied_ha/observation/state` に publish |

---

## chat ループ（会話）

**実装**: `embodied_ha/chat.sh`（668行）

### トリガー条件

| 種別 | 条件 |
|---|---|
| MQTT 経由 | `embodied_ha/chat/set` にメッセージが publish される |
| HA テキストエンティティ | `text.embodied_ha_chat` への入力（MQTT 経由で同一トピックに流れる） |

タイムアウト上限: 300秒（5分）

### コンテキスト構築

chat.sh は Claude へのプロンプトを組み立てる前に以下を収集する:

1. **最近の活動** — 観察ログ（watch）と探索ログ（explore）を時系列でマージした直近の活動サマリー
2. **長期記憶** — `log/memory.md` の全文
3. **会話履歴** — `log/chat_log.jsonl` から直近10往復
4. **センサー状態** — `render-sensors.py` の出力（`context=chat`）
5. **オープンループ** — `loops.sh list` の出力
6. **保留中の提案** — `pending_proposal.json`（explore が保存した家電操作候補）
7. **turn-taking state** — sociality_state から割り込み許可の状態

### MCP サーバー

`memory`, `ha`, `sociality`, `hacontrol`, `camera`, `audio`, `body`, `sensors`, `http` に加え、組み込みの `Read` ツール（ファイル読み込み）も利用可能。

hacontrol は chat ループでは常に使用可能（`EHA_AUTONOMOUS` の影響を受けない）。

### 出力（JSON フィールド）

| フィールド | 型 | 処理 |
|---|---|---|
| `reply` | string | ユーザーへの返答。`speak.py` で TTS 実行後、MQTT `embodied_ha/last_speak/state` に publish |
| `emotion` | string | MQTT `embodied_ha/emotion/state` に publish |
| `preferences_update` | object | `preferences.json` を直接更新（下記参照） |
| `proposal_resolved` | string | 指定 ID の保留提案を削除 |
| `open_loop` | string | 新しいオープンループを `loops.sh add` で追加 |
| `close_loop` | string | ループ ID を指定して `loops.sh close` で閉じる |

### preferences の自律更新

`preferences_update` フィールドで Claude が `preferences.json` を直接変更できる。対応するキー:

- `cameras_add` / `cameras_remove` — カメラの追加・削除
- `speakers_update` — スピーカー設定の更新
- `presence_update` — 在宅判定エンティティの変更
- `policies_add` / `policies_remove` — 行動ポリシーの追加・削除
- `sensors_groups_update` — センサーグループの更新
- `entities_add` / `entities_remove` — 操作対象エンティティの追加・削除

---

## ループ間の比較

| 項目 | watch | explore | chat |
|---|---|---|---|
| トリガー | 定期 + MQTT + センサー | 定期のみ | MQTT（ユーザー発言） |
| 基本頻度 | 20分 | 30分 | イベント駆動 |
| 確率判定 | あり（base 60） | あり（base 50） | なし（必ず実行） |
| モード数 | 1（2フェーズ） | 3（explore/reflect/web） | 1 |
| hacontrol | `EHA_AUTONOMOUS=1` のとき | `EHA_AUTONOMOUS=1` のとき | 常に |
| curiosity への影響 | −0.040（成功時） | −0.060（成功時） | +0.010 |
| 主な記憶操作 | record_episode, build_daybook | recall, record_episode, loops | recall, remember, loops |

## body_state によるスケジューリング

`compute_run_chance()` は `body_state.py` に実装されており、基準値に body_state 各軸のバイアスを加算して最終確率（5〜100%）を返す:

```python
# watch の場合
chance += round((curiosity - 0.5) * 26)
chance += round((confidence - 0.5) * 8)
chance += round((energy - 0.5) * 10)
chance -= round(max(0.0, stress - 0.35) * 24)

# explore の場合
chance += round((curiosity - 0.5) * 34)
chance += round((energy - 0.55) * 16)
chance += round((confidence - 0.5) * 6)
chance -= round(max(0.0, stress - 0.30) * 30)

# 共通の補正
if energy < 0.30:   chance -= 15
if stress > 0.70:   chance -= 12
if curiosity > 0.75: chance += 8
```

curiosity が高いほど explore の実行確率が大きく上がる。stress が高い・energy が低いと確率が下がる。詳細は [body_state.md](body_state.md) 参照。
