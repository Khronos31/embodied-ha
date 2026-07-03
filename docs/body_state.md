# ホメオスタシスモデル（body_state）

実装ファイル: `embodied_ha/body_state.py`, `embodied_ha/embodied_action.py`

`body_state` は内部状態を数値ベクターとして保持します。公開されるのは 5 軸で、`remote_*` や `embodiment_tension` などは内部用です。

状態は `body_state.json` に保存されます。

## 公開フィールド

| フィールド | 初期値 | 意味 |
|---|---|---|
| `curiosity` | 0.52 | 探索・発見への衝動。`loop` の実行確率に効く |
| `energy` | 0.68 | 活動リソース |
| `stress` | 0.24 | 緊張・負荷 |
| `confidence` | 0.56 | 判断の確信 |
| `social_openness` | 0.50 | 他者との相互作用への開放性 |

## 内部フィールド

| フィールド | 初期値 | 意味 |
|---|---|---|
| `embodiment_tension` | 0.0 | 電脳体使用時の遊離感 |
| `return_to_body_pressure` | 0.0 | 物理体へ戻りたい圧 |

## リモート状態

| フィールド | 意味 |
|---|---|
| `remote_mode` | `""`, `"remote_avatar"` など |
| `remote_room` | 電脳体がいる部屋 |
| `remote_since` | 電脳体に入った時刻 |
| `remote_updated_at` | 最終更新時刻 |
| `remote_move_cost` | ここまでの移動コスト |
| `remote_avatar_host` | 投射先ホスト |
| `last_action_mode` | 最後のアクションモード |
| `last_action_at` | 最後のアクション時刻 |
| `last_action_cost` | 最後のアクションコスト |
| `last_target_room` | 最後の対象部屋 |

## `advance_tick`

`daemon.py` がループ開始前に呼びます。係数は実装どおりです。

```python
curiosity += 0.01 + min(0.06, elapsed_hours * 0.012) + min(0.03, desire_count * 0.004)
energy += (0.66 - energy) * min(0.18, 0.04 + elapsed_hours * 0.02)
stress += (0.22 - stress) * min(0.16, 0.03 + elapsed_hours * 0.02)
confidence += (0.58 - confidence) * min(0.10, 0.02 + elapsed_hours * 0.01)
social_openness += (0.50 - social_openness) * min(0.08, 0.02 + elapsed_hours * 0.01)
embodiment_tension += (0.0 - embodiment_tension) * min(0.24, 0.06 + elapsed_hours * 0.05)
return_to_body_pressure += (0.0 - return_to_body_pressure) * min(0.16, 0.04 + elapsed_hours * 0.03)
```

`remote_mode == "remote_avatar"` のときは追加で次のドリフトがあります。

```python
distance_factor = max(0.15, min(1.0, remote_move_cost / 3.0))
raw_drift = min(0.024, elapsed_hours * 0.016 * distance_factor)
curiosity_drift_factor = max(0.2, 1.0 - curiosity)
remote_drift = raw_drift * curiosity_drift_factor
stress += remote_drift * 0.7
confidence -= remote_drift * 0.55
embodiment_tension += remote_drift
return_to_body_pressure += remote_drift * 0.8
```

`remote_avatar_host` が `camera.` で始まると、視覚疲労として `return_to_body_pressure` がさらに増えます。

```python
visual_bump = min(0.015, 0.005 + elapsed_hours * 0.006)
return_to_body_pressure += visual_bump
```

ループ種別ごとの追加変化:

- `loop` → `curiosity += 0.012`, `stress += 0.004`
- `chat` → `social_openness += 0.012`, `confidence += 0.006`

`trigger_reason` が定期実行や手動実行以外なら `curiosity += 0.015`, `stress += 0.010` です。

## `apply_feedback`

ループ終了後の反映です。

```python
energy_cost = 0.018 + min(0.080, duration / 1800.0 * 0.045)
energy -= energy_cost
```

### `loop`

成功時:

```python
curiosity -= 0.060
stress -= 0.010
confidence += 0.030
```

失敗時:

```python
curiosity += 0.015
stress += 0.080
confidence -= 0.050
```

### `chat`

成功時:

```python
social_openness += 0.040
confidence += 0.020
curiosity += 0.010
```

失敗時:

```python
social_openness -= 0.020
confidence -= 0.030
```

### 追加フラグ

- `spoke=True` → `social_openness += 0.010`
- `action_taken=True` 成功 → `confidence += 0.015`
- `action_taken=True` 失敗 → `confidence -= 0.015`, `stress += 0.010`
- 成功共通 → `stress -= 0.006`
- 失敗共通 → `stress += 0.020`

## `apply_action_effect`

`embodied_action.py` 経由で呼ばれます。

### `remote_avatar`

```python
raw_bump = min(0.028, 0.004 + min(0.018, distance * 0.006))
curiosity_factor = max(0.2, 1.0 - curiosity)
bump = raw_bump * curiosity_factor
stress += bump * 0.65
confidence -= bump * 0.50
embodiment_tension += bump
return_to_body_pressure += bump * 0.85
curiosity -= 0.008 if action_cost <= 0.01 else 0.015
```

### `direct_in_room`

```python
stress -= 0.007
confidence += 0.006
embodiment_tension -= 0.060
return_to_body_pressure -= 0.070
```

### `physical_move`

```python
stress -= 0.012
confidence += 0.010
embodiment_tension -= 0.090
return_to_body_pressure -= 0.100
```

`cyber_in_room` は `apply_action_effect` では専用ブランチを持たないため、`last_action_*` を除けばほぼ中立です。

## `compute_run_chance`

`daemon.py` が実行確率を決めるときの式です。返り値は 5〜100 の整数です。

```python
if loop_name == "loop":
    chance += round((curiosity - 0.5) * 34)
    chance += round((energy - 0.55) * 16)
    chance += round((confidence - 0.5) * 6)
    chance -= round(max(0.0, stress - 0.30) * 30)
else:
    chance += round((social_openness - 0.5) * 12)

if energy < 0.30:
    chance -= 15
if stress > 0.70:
    chance -= 12
if curiosity > 0.75:
    chance += 8
```

`loop` は好奇心・エネルギー・確信・ストレスの影響を受け、`chat` は主に `social_openness` で揺れます。

