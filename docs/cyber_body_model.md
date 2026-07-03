# 電脳体モデル設計

実装ファイル: `embodied_ha/body-mcp.py`, `embodied_ha/embodied_action.py`, `embodied_ha/sensory_origin.py`

ここでいう「電脳体」は、物理体とは別に HA エンティティや外部デバイスへ意識を投射した状態を指します。現在の実装では `project_to` という独立ツールはなく、`enter_cyberspace` / `move_cyber` / `return_to_body` / `move_to` が統合された位置モデルを扱います。

## `body_location.json`

`body-mcp.py` が扱う主な状態は次の通りです。

| フィールド | 意味 |
|---|---|
| `current_room` | 物理体のいる部屋 |
| `projected_room` | 電脳体が投射されている部屋 |
| `current_entity` | 現在侵入中のデバイス entity |
| `source` | 現在の音声ソースやデバイスの出どころ |
| `previous_room` | 直前の部屋 |
| `last_move_cost` | 最後の移動コスト |
| `last_move_path` | 最後の移動経路 |
| `updated_at` | 更新時刻 |
| `projection_updated_at` | 投射更新時刻 |

`get_location` はこの状態を読みやすく整形して返します。

## `projection_targets`

`external://xxx` 形式は `preferences.json` の `projection_targets` で解決されます。

| フィールド | 意味 |
|---|---|
| `id` | `external://xxx` の識別子 |
| `room` | 投射先の room_id |
| `label` | 表示名 |
| `note` | 補足 |

`enter_cyberspace` と `move_cyber` は `external://xxx` を受け取り、`resolve_external_room()` で `room` を引きます。

## ツール

- `get_location` — 現在位置を返す
- `move_to` — 物理体そのものを移動する
- `enter_cyberspace` — 同室のデバイスに電脳体として侵入する
- `move_cyber` — 電脳体で別デバイスへ移動する
- `return_to_body` — 電脳体を解除して物理体に戻る
- `estimate_move_cost` — 部屋間コストを見積もる
- `get_room_graph` — 部屋グラフを返す

## モード判定

`embodied_action.py` の `action_mode_for_rooms()` は次の 3 値を返します。

| 値 | 意味 |
|---|---|
| `direct_in_room` | 物理体と同じ部屋での直接操作 |
| `cyber_in_room` | 投射先の部屋での操作 |
| `remote_avatar` | 離れた部屋やデバイスへのリモート操作 |

`body_state.py` では `direct_in_room` と `physical_move` がグラウンディング寄り、`remote_avatar` が不安定化寄りです。`cyber_in_room` は実装上ほぼ中立です。

## 実装上の注意

- `enter_cyberspace` は物理体と同じ部屋にあるデバイスにしか入れません
- `move_cyber` は電脳体モード中のみ使えます
- `return_to_body` は `projected_room` が物理体の部屋と一致しているときに戻れます
- `move_to` は物理体を部屋グラフ上で実際に動かし、`projected_room` をクリアします
- `external://xxx` は `projection_targets` にあるものだけ解決できます

