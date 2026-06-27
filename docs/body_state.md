# ホメオスタシスモデル（body_state）

実装ファイル: `embodied_ha/body_state.py`（472行）, `embodied_ha/embodied_action.py`（111行）

body_state は「あかねちゃんの内部状態」を数値ベクターで表現するモジュール。ループの起動確率・発話のトーン・行動コストに影響を与えるが、Claude に直接渡されるのは公開フィールドのみで、実装上の詳細（remote 関連）は隠蔽される。

状態は `EHA_DATA_DIR/body_state.json` に JSON として永続化される。

<!-- TODO: diagram — 5軸のホメオスタシスと各ループ・アクションの影響を示す図 -->

## 公開フィールド（5軸）

ループのプロンプトに含まれる公開軸。すべて 0.0〜1.0 の浮動小数点数。

| フィールド | 初期値 | 意味 |
|---|---|---|
| `curiosity` | 0.52 | 探索・発見への衝動。高いほど watch/explore の実行確率が上がる |
| `energy` | 0.68 | 処理・活動のリソース。低いとループ確率が下がる |
| `stress` | 0.24 | 緊張・負荷の蓄積。高いとループ確率が下がり、発話を抑制する傾向 |
| `confidence` | 0.56 | 行動・判断への確信度。低いと提案・発話に慎重になる |
| `social_openness` | 0.50 | 他者との相互作用への開放性。chat での応答の積極性に影響 |

## 非公開フィールド（プロンプトに渡さない）

Claude には見せず、内部処理のみに使用するフィールド。

| フィールド | 初期値 | 意味 |
|---|---|---|
| `embodiment_tension` | 0.0 | 電脳体使用時の「身体からの遊離感」。remote_avatar モードで上昇 |
| `return_to_body_pressure` | 0.0 | 物理身体に戻りたいという圧力。remote_avatar 継続で上昇 |

非公開にしている理由: Claude がこれらの値を直接読んで判断するより、stress/confidence の変動として「間接的に感じる」ほうが自然な内省につながるという設計判断。

## リモート状態フィールド

電脳体の位置を管理するフィールド群。`body_location.json` とは別に body_state.json に保存される。

| フィールド | 型 | 意味 |
|---|---|---|
| `remote_mode` | string | 現在のモード（`""`=身体内, `"remote_avatar"`=電脳体） |
| `remote_room` | string | 電脳体がいる部屋 ID |
| `remote_since` | string | 電脳体モードに入った時刻（ISO 8601） |
| `remote_updated_at` | string | 最後にリモート状態が更新された時刻 |
| `remote_move_cost` | float | 現在位置までの移動コストの累積 |
| `remote_avatar_host` | string | 電脳体投影先のホスト |
| `last_action_mode` | string | 最後に実行したアクションのモード |
| `last_action_at` | string | 最後にアクションを実行した時刻 |
| `last_action_cost` | float | 最後のアクションのコスト |
| `last_target_room` | string | 最後のアクションのターゲット部屋 |

## 関数リファレンス

### `advance_tick(state, *, loop_name, trigger_reason, active_desires, now)`

`body_state.py` 実装。スケジューラがループを起動する直前に呼ばれる「時間経過ドリフト」関数。

**基本ドリフトのルール:**

```python
# curiosity は時間と欲求数に比例してゆっくり上昇
curiosity += 0.01 + min(0.06, elapsed_hours * 0.012) + min(0.03, desire_count * 0.004)

# energy・stress・confidence・social_openness はベースライン（0.66/0.22/0.58/0.50）に向けて回帰
energy += (0.66 - energy) * min(0.18, 0.04 + elapsed_hours * 0.02)
stress += (0.22 - stress) * min(0.16, 0.03 + elapsed_hours * 0.02)
```

**remote_avatar モードの追加ドリフト:**

電脳体モードで離れているほど（`remote_move_cost` が大きいほど）stress・embodiment_tension・return_to_body_pressure が増加する。ただし `curiosity` が高いほどこのドリフトが抑制される（積極的に探索している状態と解釈する）:

```python
distance_factor = max(0.15, min(1.0, distance / 3.0))
raw_drift = min(0.024, elapsed_hours * 0.016 * distance_factor)
# curiosity 0.8 → factor ≈ 0.36 / curiosity 0.3 → factor ≈ 0.86
curiosity_drift_factor = max(0.2, 1.0 - current["curiosity"] * 1.0)
remote_drift = raw_drift * curiosity_drift_factor
stress += remote_drift * 0.7
confidence -= remote_drift * 0.55
```

**ループ種別による追加変化:**
- `explore`: `curiosity += 0.015`
- `watch`: `stress += 0.004`
- `chat`: `social_openness += 0.012`, `confidence += 0.006`

**予期しないトリガーによる追加変化:**

`trigger_reason` が「定期実行」「手動実行」以外の場合（センサートリガーや MQTT からの特定メッセージ）:
```python
curiosity += 0.015
stress += 0.010
```

---

### `apply_feedback(state, *, loop_name, success, duration_seconds, spoke, action_taken)`

`body_state.py` 実装。ループ完了後に呼ばれる「結果反映」関数。`daemon.py` の `finish_body_state()` から呼ばれる。

**共通の energy コスト（全ループ）:**
```python
energy_cost = 0.018 + min(0.080, duration / 1800.0 * 0.045)
energy -= energy_cost
```

**watch ループの成功時:**
```python
curiosity -= 0.040   # 観察で好奇心が消費される
stress    -= 0.012   # 観察が完了すると緊張が和らぐ
confidence += 0.020  # 成功した観察が自信につながる
```

**watch ループの失敗時:**
```python
curiosity += 0.010   # 未解決のまま残り好奇心が高まる
stress    += 0.080   # 失敗でストレスが大きく上昇
confidence -= 0.050  # 失敗で自信が低下
```

**explore ループの成功時:**（好奇心消費が watch より大きい）
```python
curiosity -= 0.060
stress    -= 0.010
confidence += 0.030
```

**chat ループの成功時:**
```python
social_openness += 0.040
confidence      += 0.020
curiosity       += 0.010  # 会話で新しいことを知る
```

**追加フラグの効果:**
- `spoke=True` → `social_openness += 0.010`
- `action_taken=True` かつ成功 → `confidence += 0.015`
- `action_taken=True` かつ失敗 → `confidence -= 0.015`, `stress += 0.010`
- 成功共通 → `stress -= 0.006`
- 失敗共通 → `stress += 0.020`

---

### `apply_action_effect(state, *, action_mode, action_cost, target_room, target_host, move_cost)`

`body_state.py` 実装。家電操作やカメラ閲覧など「アクション」を実行したときに呼ばれる。`embodied_action.apply_action_to_body_state()` 経由で `ha-control-mcp` や `camera-mcp` から呼ばれる。

3つのアクションモードで効果が異なる:

**`direct_in_room` モード**（身体と同じ部屋での直接操作）:
```python
stress           -= 0.007   # 直接操作は安心感をもたらす
confidence       += 0.006
embodiment_tension      -= 0.060  # 身体に戻ってくる感覚
return_to_body_pressure -= 0.070
# remote 状態をクリア
```

**`physical_move` モード**（物理的な部屋間移動）:
```python
stress           -= 0.012   # 身体を動かすことでリフレッシュ
confidence       += 0.010
embodiment_tension      -= 0.090  # 移動で tension が大きく回復
return_to_body_pressure -= 0.100
# remote 状態をすべてクリア
```

**`remote_avatar` モード**（電脳体越しのリモートアクセス）:
```python
# 距離に比例してストレス・テンションが上昇
raw_bump = min(0.028, 0.004 + min(0.018, distance * 0.006))
curiosity_factor = max(0.2, 1.0 - current["curiosity"] * 1.0)
bump = raw_bump * curiosity_factor

stress           += bump * 0.65
confidence       -= bump * 0.50
embodiment_tension      += bump
return_to_body_pressure += bump * 0.85

# 電脳体アクションで好奇心が少し充足される
curiosity_satisfaction = 0.008 if cost <= 0.01 else 0.015
curiosity -= curiosity_satisfaction
```

---

### `compute_run_chance(base_chance, state, loop_name)`

`body_state.py` 実装。スケジューラがループを実行するかどうかを確率的に決める。返り値は 5〜100 の整数で、`random.randint(1, 100)` との比較に使う。

**watch ループ（base_chance = 60）:**
```python
chance += round((curiosity - 0.5) * 26)   # curiosity が高いほど +13 まで
chance += round((confidence - 0.5) * 8)
chance += round((energy - 0.5) * 10)
chance -= round(max(0.0, stress - 0.35) * 24)  # stress > 0.35 から減少開始
```

**explore ループ（base_chance = 50）:**
```python
chance += round((curiosity - 0.5) * 34)   # watch より curiosity の影響が大きい
chance += round((energy - 0.55) * 16)
chance += round((confidence - 0.5) * 6)
chance -= round(max(0.0, stress - 0.30) * 30)  # stress > 0.30 から減少開始
```

**共通ガード条件:**
```python
if energy < 0.30:    chance -= 15
if stress > 0.70:    chance -= 12
if curiosity > 0.75: chance += 8
```

実際の確率例（初期値 curiosity=0.52, energy=0.68, stress=0.24, confidence=0.56 のとき）:
- watch: `60 + 1 + 0 + 1 - 0 = 62%`
- explore: `50 + 1 + 2 + 0 - 0 = 53%`

## embodied_action.py

**実装**: `embodied_ha/embodied_action.py`（111行）

身体位置・操作対象の位置関係からアクションモードとコストを算出するヘルパー。

### `action_mode_for_rooms(body_room, target_room, projected_room)`

3モードを判定:
```python
if body_room == target_room:       return "direct_in_room"
if projected_room == target_room:  return "cyber_in_room"
return                             "remote_avatar"
```

### `action_cost_for_mode(action_mode, move_cost)`

モード別コスト:
```python
"physical_move"   → move_cost（距離そのまま）
"direct_in_room"  → 0.05
"cyber_in_room"   → 0.05
"remote_avatar"   → 0.35 + min(0.20, distance * 0.05)  # 最大 0.55
```

### `apply_action_to_body_state(...)`

`body_state_path()` を読み込み、`body_state.apply_action_effect()` を実行して上書き保存するワンショット関数。`ha-control-mcp` と `camera-mcp` から呼ばれる。

## on_audio_session()

`body_state.py` 実装。`audio_daemon.py` が能動聴取セッションを実行したときに呼ばれる:
```python
energy -= 0.08
stress += 0.03
```

## 状態の on-disk フォーマット

`body_state.json` には公開・非公開・リモート状態フィールドをすべて保存するが、`public_state()` で公開フィールドだけに絞ったビューを生成する。ループスクリプトには `EHA_BODY_STATE` 環境変数として `public_state()` の JSON が渡される（非公開フィールドはプロンプトに含まれない）。
