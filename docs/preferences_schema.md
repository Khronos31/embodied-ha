# preferences.json スキーマリファレンス

実装参照: `preferences.json.example`（ルートディレクトリ）, `embodied_ha/chat.sh`, `embodied_ha/run.sh`, `embodied_ha/speak.py`, `embodied_ha/render-sensors.py`

`preferences.json` はアドオンの動作を制御する主要な設定ファイル。実際のファイルは `EHA_DATA_DIR/preferences.json`（通常 `/config/embodied-ha/preferences.json`）に置かれ、`.gitignore` でバージョン管理から除外されている。

**自律更新**: `chat.sh` が Claude の応答 JSON の `preferences_update` フィールドを解析して自動で書き込む。ユーザーが「リビングのカメラを追加して」と会話するだけで設定が更新される。

---

## トップレベルフィールド

### `character_name`

| 型 | デフォルト |
|---|---|
| string | `"Claude"` |

キャラクター名。プロンプトの冒頭で自己紹介に使われる。

---

### `cameras`

| 型 | デフォルト |
|---|---|
| array of object | `[]` |

利用可能なカメラのリスト。`camera-mcp` の `camera_get` ツールと watch.sh のカメラ選択フェーズで参照される。

各要素のフィールド:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `source` | string | ○ | go2rtc ストリーム名（例: `capture_tv`）または HA entity_id（`camera.xxx` 形式） |
| `label` | string | ○ | 表示用ラベル（例: `"テレビ"`） |
| `room` | string | 推奨 | カメラが設置されている部屋（部屋グラフの room_id に対応） |
| `preset` | string | — | カメラの初期向き・プリセット名（例: `"tv"`, `"wide"`） |
| `direction` | string | — | カメラが向いている方向（例: `"front"`, `"side"`） |
| `note` | string | — | 補足メモ（例: `"HDDレコーダー出力。今何が放送されているか確認できる"`） |

---

### `stt_provider`

| 型 | デフォルト |
|---|---|
| string または null | `null` |

音声認識プロバイダー。現在の実装では `null`（無効）が標準。有効な値は `"whisper"` など（`audio_daemon.py` が解釈する）。

---

### `stt_language`

| 型 | デフォルト |
|---|---|
| string または null | `null` |

音声認識の言語コード（例: `"ja"`, `"en"`）。`stt_provider` が有効なときに参照される。

---

### `audio_sources`

| 型 | デフォルト |
|---|---|
| array of object | `[]` |

利用可能な音声ソースのリスト。`audio-mcp` の `listen` ツール・`audio_daemon.py` で参照される。

各要素のフィールド:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `source` | string | ○ | 音声ソースの URL/パス。`rtsp://...`, `alsa://default`, `tcp://host:port` のいずれか |
| `label` | string | ○ | 表示用ラベル（例: `"スタディマイク"`） |
| `room` | string | 推奨 | ソースが置かれている部屋 |
| `note` | string | — | 補足メモ |
| `stt_enabled` | boolean | — | `true` のとき `audio_daemon.py` が常時 STT を有効化してこのソースを監視する |
| `wake_word_enabled` | boolean | — | `true` のときウェイクワード検出を有効化する |
| `background_hearing_enabled` | boolean | — | `true` のとき背景音の常時聴取を有効化する |

`daemon.py` は起動時に `stt_enabled: true` のソースが1つでもあれば `audio_daemon.py` を起動する。

---

### `speakers`

| 型 | デフォルト |
|---|---|
| object（部屋名 → 設定オブジェクト） | `{}` |

部屋ごとの発話先設定。`speak.py` と `tts-mcp` が参照する。キーは部屋名（`"study"`, `"living"` など）。

各値のフィールド:

**TTS タイプ（`type: "tts"`）:**

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `type` | string | ○ | `"tts"` 固定 |
| `tts_entity` | string | ○ | HA の TTS エンティティ ID（例: `"tts.home_assistant_cloud"`） |
| `media_player` | string | ○ | 再生先メディアプレーヤーの entity_id |

**通知タイプ（`type: "notify"`）:**

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `type` | string | ○ | `"notify"` 固定 |
| `entity` | string | ○ | HA の通知エンティティ ID（例: `"notify.living_alexa_speak"`, `"notify.mobile_app_your_phone"`） |
| `title` | string | — | 通知タイトル（モバイル通知用、例: `"Embodied HA"`） |

---

### `wake_words`

| 型 | デフォルト |
|---|---|
| array of string | `[]` |

ウェイクワードのリスト（例: `["あかね", "Claude"]`）。`audio_daemon.py` が参照する。

---

### `entities`

| 型 | デフォルト |
|---|---|
| array of object | `[]` |

操作できる家電の対応表（口語の呼び方 → entity_id）。チャットで家電操作を頼まれたとき entity_id を引くのに使う。`discover.py` で下書きを自動生成できる。Web UI でも編集可能。

各要素のフィールド:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `name` | string | ○ | 口語での呼び方（例: `"リビングのライト"`）。HA の friendly_name そのままでよい |
| `entity_id` | string | ○ | HA の entity_id（例: `"light.living_room"`） |
| `note` | string | — | 補足メモ（使い方・注意点など） |

---

### `presence`

| 型 | デフォルト |
|---|---|
| object | `{}` |

在宅状態を判定するエンティティの設定。`boundary.py` が参照して、不在時の家電操作を抑制する。

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `entity` | string | ○ | 在宅判定に使う HA entity_id（例: `"input_boolean.resident_home"`）。state が `"home"`, `"present"`, `"on"`, `"true"`, `"1"` のいずれかなら在宅と判定される |

---

### `policies`

| 型 | デフォルト |
|---|---|
| array of string | `[]` |

行動ポリシーのリスト。各要素は自然言語の文字列で、Claude のプロンプトにそのまま含まれる。

例:
```json
["深夜1〜6時は発話しない（watch/explore に組み込み済み）"]
```

`boundary.py` の `check()` 関数が policies を受け取るが、現在の実装では境界判定ロジックは固定されており（深夜時間帯・不在・AUTONOMOUS=0 など）、policies はプロンプトインジェクション用のみ。

---

### `sensors`

| 型 | デフォルト |
|---|---|
| object | `{}` |

`sensors-mcp` の `get_sensors` ツール（実体は `render-sensors.py`）が描画する「おもなデバイス」の定義。

#### `sensors.groups`

| 型 | デフォルト |
|---|---|
| array of object | `[]` |

センサーグループのリスト。各グループは `render-sensors.py` でセクションとして描画される。

各グループのフィールド:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `title` | string | — | グループタイトル（例: `"人感センサー"`）。省略時はタイトルなしで描画 |
| `contexts` | array of string | — | このグループを表示するコンテキスト。`"watch"` と `"chat"` の任意の組み合わせ。省略時は両方で表示 |
| `items` | array of object | ○ | このグループのセンサー項目リスト |

各 item のフィールド:

| フィールド | 型 | 必須（いずれか） | 説明 |
|---|---|---|---|
| `label` | string | — | 表示ラベル（例: `"リビング"`） |
| `entity` | string | `entity` か `template` のどちらか | HA entity_id。現在の `state` 値が描画される（例: `"binary_sensor.living_motion"`） |
| `template` | string | `entity` か `template` のどちらか | HA テンプレート文字列。`/api/template` エンドポイントで評価される（例: `"{{ states('sensor.living_temp') }}℃"`） |
| `note` | string | — | 補足メモ（例: `"リビング誤反応あり"`） |

---

### `projection_targets`

| 型 | デフォルト |
|---|---|
| array of object | `[]` |

外部デバイスへの投影先の定義。`body-mcp` の `project_to` ツールが `external://xxx` 形式の entity を解決するのに使う。

各要素のフィールド:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `id` | string | ○ | `external://xxx` 形式の識別子。`body-mcp` の `project_to` で指定する `entity` と一致させる |
| `room` | string | ○ | 投影先が物理的に置かれている部屋（部屋グラフの room_id） |
| `label` | string | — | 表示用ラベル |
| `note` | string | — | 補足メモ |

---

## chat.sh による自動更新フィールド

`preferences_update` JSON フィールドで Claude が自律的に更新できる操作:

| 操作キー | 説明 |
|---|---|
| `cameras_add` | カメラを追加する。`cameras` 配列に追記 |
| `cameras_remove` | `source` を指定してカメラを削除する |
| `speakers_update` | 部屋名をキーにしてスピーカー設定を更新・追加する |
| `presence_update` | `presence.entity` を更新する |
| `policies_add` | ポリシー文字列を追加する |
| `policies_remove` | 一致するポリシー文字列を削除する |
| `sensors_groups_update` | センサーグループを更新・追加する |
| `entities_add` | エンティティ対応表に追加する |
| `entities_remove` | `entity_id` を指定してエンティティ対応表から削除する |
