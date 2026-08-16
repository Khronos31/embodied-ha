# Embodied HA — ドキュメント索引

このディレクトリには、Embodied HA アドオンの現在の実装に合わせた設計ドキュメントを置いています。推測ではなく、`embodied_ha/` の実装を読んで書いたものです。

| ファイル | 内容 |
|---|---|
| [architecture.md](architecture.md) | `run.sh` → `daemon.py` → `loop.py` / `chat.py` の起動フロー、MQTT 連携、常駐スレッド、保守パイプライン |
| [loops.md](loops.md) | `loop.py` の 5 モード（observe / explore / reflect / web / social）と `chat.py`、`daemon.py` の役割分担 |
| [mcp_servers.md](mcp_servers.md) | 現行の MCP サーバー一覧、`mcp-config.py` の registry、各ツール定義 |
| [body_state.md](body_state.md) | `body_state.py` の係数、`advance_tick` / `apply_feedback` / `apply_action_effect` / `compute_run_chance` |
| [preferences_schema.md](preferences_schema.md) | `preferences.json` の現行スキーマと `chat.py` の自動更新オペレーション |
| [cyber_body_model.md](cyber_body_model.md) | `body-mcp.py` の位置・投射モデル、`projection_targets`、`external://` の解決 |
| [how-to/README.md](how-to/README.md) | Core・first-party companion・第三者Recipeの境界と、周辺連携How-Toの公開状況 |
| [privacy.md](privacy.md) | 第三者CLI、家庭内データの送信条件、カメラ・音声、同居人への配慮 |
| [data_and_retention.md](data_and_retention.md) | ローカル保存、外部提供元の学習利用・保持、層ごとの削除 |

## 補助ファイル

以下は本体ドキュメントの補助に使う実装ファイルです。

- `embodied_ha/boundary.py` — `speak` / `action` の境界判定
- `embodied_ha/desire_state.py` + `desires.json` — 欲求システム
- `embodied_ha/anomaly_state.py` — 異常検知と explore 緊急度
- `embodied_ha/sociality_state.py` — 関係・同意・ターンテイキング状態
- `embodied_ha/audio-mcp.py` — 能動聴取、音声履歴の参照、発話
