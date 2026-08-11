# preferences.json スキーマリファレンス

実装参照: `embodied_ha/preferences.json.example`, `embodied_ha/discover.py`, `embodied_ha/chat.py`, `embodied_ha/speak.py`, `embodied_ha/render-sensors.py`, `embodied_ha/sensory_origin.py`

`preferences.json` は会話で育てる主要設定ファイルです。実体は `EHA_PREFS_FILE`（通常 `/config/embodied-ha/preferences.json`）にあり、`chat.py` の `preferences_update` から自動更新されます。

知覚系の設定は、身体的知覚とメディア受信で分けて扱います。`cameras` と `mics` は実際にその機器へ侵入して使う身体的知覚で、部屋や減衰、在室推定に影響します。一方 `video_media` と `audio_media` は侵入不要のメディア受信で、部屋は文脈としてのみ使われ、減衰や在室判定には影響しません。

## トップレベル

### `character_name`

| 型 | デフォルト |
|---|---|
| string | `"Claude"` |

キャラクター名です。プロンプトの冒頭で使われます。

---

### `tts_entity`

| 型 | デフォルト |
|---|---|
| string | `"tts.home_assistant_cloud"` 例 |

グローバルのHA TTSエンティティです。すべての`speakers`に対する通常読み上げに使います。
対応情報を公開するTTS統合では、`tts_selections`で言語・音声だけを任意指定できます。
速度・音量・ピッチ・感情など、それ以外のプロバイダー固有オプションは今後もEmbodied HAでは扱わず、
Home Assistant側のTTSエンティティ設定を使用します。
既存ファイルに残る旧`tts_options`は互換データとして保持されることがありますが、通常読み上げでは使用されません。
VOICEVOX Song用の`sing_speaker`は別機能です。

---

### `tts_selections`

| 型 | デフォルト |
|---|---|
| object | `{}` |

TTSエンティティごとの任意の言語・音声選択です。Web UIはHome Assistantが公開する対応言語と、
選択した言語に対応する音声から候補を動的に生成します。音声一覧を公開しない統合では`voice`を指定せず、
HA側のエンティティ設定を使います。

```json
{
  "tts_selections": {
    "tts.home_assistant_cloud": {
      "language": "ja-JP",
      "voice": "ja-JP-NanamiNeural"
    },
    "tts.voicevox_tts_sample": {
      "language": "ja-JP"
    }
  }
}
```

保存できるキーは`language`と`voice`だけです。エンティティを切り替えても各選択は個別に保持されます。

---

### `stt_provider`

| 型 | デフォルト |
|---|---|
| string | `"stt.home_assistant_cloud"` 例 |

`listen`、`use_device_microphone`、音声チャットなどの能動聴取で使う音声認識プロバイダーです。

---

### `stt_language`

| 型 | デフォルト |
|---|---|
| string | `"ja-JP"` 例 |

能動聴取で使う STT の言語コードです。

---

### `cameras`

| 型 | デフォルト |
|---|---|
| array of object | `[]` |

目として使うカメラ一覧です。侵入が必要な身体的知覚で、`camera-mcp` の `use_device_camera` が参照します。

各要素の主なフィールド:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `entity` | string | ○ | 侵入時に使う短い ID か HA entity_id |
| `source` | string | ○ | go2rtc ストリーム名または HA camera entity_id |
| `room` | string | ○ | 設置部屋 |
| `label` | string | ○ | 表示名 |
| `ptz` | object | — | `left/right/up/down` から button entity への対応表 |
| `note` | string | — | 補足 |

`ptz` の例:

```json
{
  "left": "button.camera_pan_left",
  "right": "button.camera_pan_right",
  "up": "button.camera_tilt_up",
  "down": "button.camera_tilt_down"
}
```

---

### `mics`

| 型 | デフォルト |
|---|---|
| array of object | `[]` |

耳として使うマイク一覧です。侵入が必要な身体的知覚で、`audio-mcp` の `listen` と `use_device_microphone` が参照します。

各要素の主なフィールド:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `entity` | string | ○ | 短い ID か HA entity_id |
| `source` | string | ○ | `rtsp://...` の音声ストリーム |
| `room` | string | ○ | 所在部屋 |
| `label` | string | ○ | 表示名 |
| `note` | string | — | 補足 |

旧 `audio_sources` からの移行は `run.sh` 起動時に `migrate_source_schema.py` が自動で行います。旧 `alsa://` / `tcp://` 項目は消失防止のため残りますが、起動ログで移行対象として通知され、RTSPへ置き換えるまで使用されません。

内蔵の常時 STT とウェイクワード検出は廃止されました。起動時の移行でトップレベルの
`wake_words` / `wake_ack` と、各マイクの `stt_enabled` / `stt_retention_hours` /
`wake_word_enabled` / `background_hearing_enabled` をバックアップ後に削除します。
音声による呼び出しには、RTSP Assist GatewayなどからMQTT chat契約へ接続する外部経路を利用してください。

---

### `video_media`

| 型 | デフォルト |
|---|---|
| array of object | `[]` |

侵入不要で観る映像ソース一覧です。部屋は文脈としてのみ持ち、在室推定や距離減衰には使いません。

各要素の主なフィールド:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `id` | string | ○ | 一意な ID |
| `source` | string | ○ | 映像ソース名、URL、または取得先の識別子 |
| `room` | string | — | 文脈上の部屋 |
| `label` | string | ○ | 表示名 |
| `note` | string | — | 補足 |

---

### `audio_media`

| 型 | デフォルト |
|---|---|
| array of object | `[]` |

侵入不要で聴く音声ソース一覧です。必要に応じて `video_media` と紐づけられますが、身体的知覚ではありません。

各要素の主なフィールド:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `id` | string | ○ | 一意な ID |
| `source` | string | ○ | go2rtcストリーム名、または `rtsp://...` URL |
| `room` | string | — | 文脈上の部屋 |
| `label` | string | ○ | 表示名 |
| `note` | string | — | 補足 |
| `video_media` | string | — | 関連する映像ソース ID |

`discover.py` は `media_player` の候補から `speakers` の下書きも作ります。

---

### `speakers`

| 型 | デフォルト |
|---|---|
| array of object | `[]` |

発話先デバイス一覧です。現在の実装は list 形式が正で、`speak.py` は旧 dict 形式も互換で受けますが、docs では list 形式を正とします。

各要素の主なフィールド:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `entity` | string | ○ | HA `media_player` entity_id |
| `room` | string | ○ | 部屋名 |
| `label` | string | — | 表示名 |
| `type` | string | — | 旧設定との互換用。省略または `tts` のみ |
| `note` | string | — | 補足 |
| `media_player` | string | — | `entity` の旧別名 |

通常発話はグローバル`tts_entity`から`cache: false`のTTS Media Source URIを組み立て、`media_player.play_media`へ渡します。音声ファイル再生は`/media/embodied-ha/`に永続配置してMedia Source URIを同じサービスへ渡します。旧`tcp` / `local`スピーカーは未対応です。

---

### `entities`

| 型 | デフォルト |
|---|---|
| array of object | `[]` |

操作できる家電の対応表です。`chat.py` が `entities_add` / `entities_remove` を使って更新します。

各要素のフィールド:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `name` | string | ○ | 口語名 |
| `entity_id` | string | ○ | HA entity_id |
| `note` | string | — | 補足 |

---

### `presence`

| 型 | デフォルト |
|---|---|---|
| object | `{}` |

在宅判定に使うエンティティです。

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `entity` | string | ○ | 例: `input_boolean.resident_home` |

---

### `policies`

| 型 | デフォルト |
|---|---|
| array of string | `[]` |

行動ポリシーの文字列一覧です。`boundary.py` の判定ロジックは固定で、policies は主にプロンプト文脈として使われます。

---

### `loop_schedule`

| 型 | デフォルト |
|---|---|
| object | `{}` |

`daemon.py` のループスケジューリングに使います。

| フィールド | 型 | 説明 |
|---|---|---|
| `loop_interval` | number | `loop.py` の実行間隔（秒） |
| `day_probability` | number | 日中の基準確率 |
| `late_probability` | number | 22-24時の基準確率 |
| `night_probability` | number | 0-6時の基準確率 |

---

### `sensors`

| 型 | デフォルト |
|---|---|
| object | `{}` |

`render-sensors.py` が描画する「おもなデバイス」です。

`sensors.groups` の各要素:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `title` | string | — | セクション名 |
| `contexts` | array of string | — | 表示先。通常は `loop` / `chat`。省略時は全コンテキスト |
| `items` | array of object | ○ | センサー項目の配列 |

`items` の各要素:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `label` | string | — | 表示ラベル |
| `entity` | string | `entity` か `template` のどちらか | HA entity_id |
| `template` | string | `entity` か `template` のどちらか | HA テンプレート文字列 |
| `note` | string | — | 補足 |

`contexts` は現在 `render-sensors.py` で `loop` と `chat` を直接フィルタします。旧 `watch` は使いません。

---

### `source_room_hints`

| 型 | デフォルト |
|---|---|
| object | `{}` |

`sensory_origin.py` が音声やカメラの文字列から部屋を推定するときに使うヒントです。

例:

```json
{
  "tv": "living_room",
  "kitchen mic": "kitchen",
  "hallway": "hallway"
}
```

キーは小文字比較され、値は room graph 上の room_id または別名として解決されます。

---

### `games`

`game-mcp.py` が `games.plugins` を読みます。既定は `wiki6: true`, `wordvec_race: false` です。

```json
{
  "plugins": {
    "wiki6": true,
    "wordvec_race": false
  }
}
```

## `chat.py` による自動更新オペレーション

`preferences_update` で実際に受け付けるキーは次の通りです。

| キー | 役割 |
|---|---|
| `cameras_add` | カメラを追加する |
| `cameras_remove` | `source` でカメラを削除する |
| `speakers_set` | 部屋ごとの発話先を更新・追加する |
| `presence_set` | 在宅判定エンティティを更新する |
| `policies_add` | ポリシーを追加する |
| `sensors_add` | 主要センサーを追加する |
| `sensors_remove` | 主要センサーを削除する |
| `entities_add` | 操作対象エンティティを追加する |
| `entities_remove` | 操作対象エンティティを削除する |

### 実装上の注意

- `speakers_set` は list 形式の `speakers` に正規化されます
- `sensors_add` は既存グループに同一 `entity` / `label` があれば置き換えます
- `sensors_add` で `contexts` を省略すると `["loop"]` になります
- `policies_remove` はありません
- `speakers_update` はありません
- `presence_update` はありません
- `sensors_groups_update` はありません
