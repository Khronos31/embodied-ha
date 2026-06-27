# Embodied HA — ドキュメント索引

このディレクトリには、Embodied HA アドオンの**実装に忠実な**設計ドキュメントが置かれています。
コードから逆引きして書かれており、願望や設計案ではなく**現在実際に動いている仕組み**を記述します。

| ファイル | 内容 |
|---|---|
| [architecture.md](architecture.md) | システム全体の起動フローと各コンポーネントの連携。run.sh → daemon.py → 3ループの全体像 |
| [loops.md](loops.md) | watch / explore / chat の3ループモデル詳細。トリガー条件・頻度・処理内容・ループ間の違い |
| [mcp_servers.md](mcp_servers.md) | 全 MCP サーバーのツール一覧と入出力の概要。各サーバーがどのループで使われるかを記載 |
| [body_state.md](body_state.md) | ホメオスタシスモデルの設計。5軸の意味・初期値・tick/feedback/action_effect の挙動・スケジューリングへの影響 |
| [preferences_schema.md](preferences_schema.md) | preferences.json の全フィールドリファレンス。型・意味・デフォルト値を網羅 |
| [cyber_body_model.md](cyber_body_model.md) | 電脳体移動モデル（既存）。4アクション・コストモデルの詳細 |

## ドキュメント外の重要ファイル

以下はドキュメント化されていないが理解に必要な補助モジュール:

- `embodied_ha/boundary.py` — speak / action の実行可否を判定するゲートキーパー
- `embodied_ha/desire_state.py` + `desires.json` — 欲求システム（decay/pressure/consume サイクル）
- `embodied_ha/anomaly_state.py` — センサー急変・未解決ループの検出と explore 緊急度計算
- `embodied_ha/sociality_state.py` — ターンテイキング・同意・バウンダリーの永続状態管理
- `embodied_ha/audio_daemon.py` — 常時 STT・ウェイクワード検出のバックグラウンドプロセス
