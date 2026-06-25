# Embodied HA

Home Assistant の中に住み込む、**自律エージェント HAOS アドオン**。

センサー・カメラで家の様子を「自分ごと」として眺め、気づいたことをスピーカーで伝えたり、チャットで会話したり、家電を操作したりする。

---

## 必要なもの

- **Home Assistant OS**（Supervisor 付き）
- **Mosquitto Broker** アドオン（MQTT 統合）— HA エンティティの登録に使用
- **Claude** の認証（API キー、または Claude.ai サブスクリプション）
- アーキテクチャ: `amd64` / `aarch64`（RPi 4・5 等）

---

## インストール

1. HA の **設定 → アドオン → アドオンストア → ⋮ → リポジトリを管理** を開く
2. 以下の URL を追加して **追加** を押す

```
https://github.com/Khronos31/embodied-ha
```

3. ストアをリロードすると「Embodied HA」が表示されるのでインストール

---

## セットアップ

### 1. Claude 認証

アドオンを起動すると Web UI（Ingress）が開きます。

| 方法 | 手順 |
|---|---|
| **APIキー** | アドオンの設定タブで `claude_api_key` に入力 |
| **Claude.ai サブスク** | Web UI のセットアップ画面で「Claude.ai でログイン」→ 表示される URL でブラウザ認証 |

### 2. 設定オプション

| オプション | デフォルト | 説明 |
|---|---|---|
| `resident_name` | `ユーザー` | ユーザーの名前（エージェントが会話で使う） |
| `claude_api_key` | （空） | Anthropic API キー。Claude.ai サブスクリプションの場合は空 |
| `claude_config_dir` | （空） | Claude の設定ディレクトリのパス。空の場合は `/config/embodied-ha/.claude`（アンインストール後も保持）。Studio Code Server の Claude 認証を使い回す場合は `/config/.tools/claude-home` を指定 |
| `claude_cwd` | （空） | Claude 起動時の作業ディレクトリ。`/config` を指定し `claude_config_dir=/config/.tools/claude-home` と組み合わせると、Studio Code Server 版の Claude Code とメモリを共有できる |
| `autonomous_control` | `false` | `true` にすると、観察・探索ループでも自律的に家電を操作できるようになる |

### 3. 起動後の自動セットアップ

起動時に自動で行われること：

- **MQTT Discovery** — HA に 5 つのエンティティを登録（→ [HA エンティティ](#ha-エンティティ)）
- **センサー自動発見** — HA のエンティティを走査して観察対象センサーの初期設定を生成
- **デーモン起動** — 認証完了後に 3 ループが開始

---

## 機能

### 観察・探索・会話の 3 ループ

| ループ | 間隔 | 内容 |
|---|---|---|
| **観察** `watch` | 約 20 分 + センサートリガー | カメラ・センサー・聴覚ログを確認し、気づき・感情・発話を生成 |
| **探索** `explore` | 約 30 分 | 自発的に家を調べる。必要ならカメラや音声ソースを能動的に見聞きする |
| **会話** `chat` | オンデマンド | チャット入力に応答。家電操作・記憶検索・能動リスニングもここから |

### 会話でできること

- **センサー追加** — 「リビングのCO2も常に見せて」で観察コンテキストに加わる
- **カメラ追加** — 「玄関カメラも使って」で観察ループで撮影するカメラに加わる
- **聴覚利用** — 「何か聞こえた？」「テレビの音を聞いて」で常時STTログや能動listenを使える
- **家電操作** — 「リビングのライト消して」（`autonomous_control` が不要なのはチャットのみ）
- **ループ管理** — 「後で確認して」でやりかけを記録、観察・会話で自然に蒸し返す
- **記憶検索** — 「先週のエアコンの設定は？」で過去ログや聴覚ログも含めて検索できる
- **スケジュール調整** — 「もっと頻繁に見てほしい」で観察間隔を自分で変更
- **位置の扱い** — 「今どこにいることにする？」「リビングへ移動して」で現在位置や移動コストを扱える
- **社会性の反映** — 関係性や shared focus を踏まえ、割り込みや話しかけ方を調整する
- **反実仮想と記憶** — やらなかったことや直前の作業記憶も含めて、後から自然に思い出しやすい

### HAオートメーション連携

MQTT トピック `embodied_ha/observe/trigger` に文字列を送ると、その経緯を踏まえた観察をその場で実行する。

```yaml
# オートメーション例
action:
  - service: mqtt.publish
    data:
      topic: embodied_ha/observe/trigger
      payload: "玄関ドアが開いた"
```

---

## Web UI

アドオンの **「Web UI を開く」** ボタンで起動。サイドバーのロボットアイコンからもアクセス可。

| ルーム | 内容 |
|---|---|
| **会話** | エージェントとのチャット・発話履歴 |
| **独り言** | 観察・探索中の内省（エージェントの「心の内」） |
| **耳にした音** | 常時STT・背景聴覚・物音イベントの確認、再生、ラベル付け |

設定画面（⚙）からキャラクター・センサー・スピーカー・カメラ・音声ソース・ポリシーを編集できる。

---

## HA エンティティ

起動時に MQTT Discovery で自動登録されるエンティティ：

| エンティティ | 種別 | 用途 |
|---|---|---|
| `sensor.embodied_ha_observation` | センサー | 直近の観察内容 |
| `sensor.embodied_ha_last_speak` | センサー | 直近の発話 |
| `sensor.embodied_ha_emotion` | センサー | 現在の感情（`curious` / `calm` / `happy` 等。照明色変え等に活用可） |
| `text.embodied_ha_chat` | テキスト | チャット入力（HA UI → アドオン） |
| `button.embodied_ha_observe` | ボタン | 観察を即時トリガー |

---

## パーソナライズ

設定ファイルはすべて `/config/embodied-ha/` に永続化される（Samba・File Editor からも編集可）。

| ファイル | 内容 | 編集方法 |
|---|---|---|
| `character.md` | エージェントの性格・口調・価値観 | Web UI 設定画面 or File Editor |
| `preferences.json` | センサー・スピーカー・カメラ・音声ソース・エンティティ対応表 | Web UI 設定画面 or 会話 |
| `desires.json` | 欲求の種類と蓄積速度 | File Editor |
| `personal.inc` | TV番組ガイドなど個人的な追加コンテキスト（シェルスクリプト） | File Editor |

### 欲求システム

`desires.json` の各欲求は時間経過で蓄積し、閾値（0.6）を超えると観察ループの「内なる衝動」としてプロンプトに注入される。

```json
{
  "check_weather": {
    "growth_rate": 0.033,
    "prompt": "外の天気をしばらく確認していない。今どんな様子か気になる。"
  }
}
```

### 長期記憶

`log/memory.md` に 2 層で蓄積される：

- **コア記憶** — 家の構造的な理解。キュレートされた情報を全文コンテキストへ
- **最近の気づき** — 観察ごとに追記される時系列メモ。直近 40 件をコンテキストへ
- **ロールアップ** — 「最近の気づき」が 120 件を超えると古い分をコア記憶へ要約・昇格し、直近 60 件を保持

---

## アーキテクチャ

<img src="architecture.png" width="560">

各ループは毎回独立した Claude CLI セッションを起動し、`memory.md`・`observations.jsonl` 等のファイルで連続性を維持する。

---

## データ永続化

すべてのログ・設定は `/config/embodied-ha/` に保存され、アドオン更新・再起動後も保持される。

| パス | 内容 |
|---|---|
| `character.md` | キャラクター定義 |
| `preferences.json` | センサー・スピーカー・カメラ・音声ソース設定 |
| `desires.json` | 欲求定義 |
| `personal.inc` | 個人コンテキスト |
| `log/memory.md` | 長期記憶 |
| `log/observations.jsonl` | 観察ログ |
| `log/explore.jsonl` | 探索ログ |
| `log/chat_log.jsonl` | 会話履歴 |
| `log/open_loops.jsonl` | やりかけ・約束 |
| `log/auditory_events.jsonl` | 常時STTで聞こえた言葉 |
| `log/background_audio_log.jsonl` | 背景として聞こえていた音 |
| `log/active_listen_log.jsonl` | 能動的に聞きに行った音声 |
| `log/non_speech_audio_events.jsonl` | STTに乗らなかった特徴的な物音 |
| `log/audio_event_tags.jsonl` | 物音への人間・外部推論ラベル |
| `body_location.json` | 現在位置 |
| `log/body_location_log.jsonl` | 移動履歴 |

---

---

> Inspired by [lifemate-ai/embodied-claude](https://github.com/lifemate-ai/embodied-claude). Respect.
