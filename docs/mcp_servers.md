# MCP サーバー一覧

Embodied HA は `claude` CLI の `--mcp-server` オプションで複数の MCP サーバーをループごとに接続する。各サーバーはサブプロセスとして起動され、JSON-RPC over stdio でツールを提供する。

MCP サーバーの共通基盤は `embodied_ha/mcp_lib.py` に実装されている。

<!-- TODO: diagram — ループとMCPサーバーの接続マトリクス図 -->

## ループ別の接続サーバー一覧

| サーバー | watch | explore | chat |
|---|:---:|:---:|:---:|
| sensors-mcp | ○ | ○（explore モード） | ○ |
| ha-mcp | ○ | ○（explore モード） | ○ |
| camera-mcp | ○ | ○（explore モード） | ○ |
| audio-mcp | ○ | ○（explore モード） | ○ |
| body-mcp | ○ | ○ | ○ |
| memory-mcp | ○ | ○ | ○ |
| sociality-mcp | ○ | ○ | ○ |
| http-mcp | ○ | ○（explore モード） | ○ |
| tts-mcp | — | — | — |
| ha-control-mcp | `EHA_AUTONOMOUS=1` のとき | `EHA_AUTONOMOUS=1` のとき | 常に ○ |

`tts-mcp` はループからは直接使われない。TTS は `speak.py` を直接サブプロセスで呼ぶ方式になっている（ループスクリプトが JSON の `speak` フィールドを解析して実行する）。

---

## sensors-mcp

**実装**: `embodied_ha/sensors-mcp.py`（55行）

`render-sensors.py` をサブプロセスで呼ぶ薄いラッパー。

### ツール

| ツール | 説明 | 入力 | 出力 |
|---|---|---|---|
| `get_sensors` | preferences.json の `sensors.groups` に定義された「おもなデバイス」の現在値を描画して返す | `context`: `"watch"` または `"chat"` | テキスト形式のセンサー状態 |

`context` パラメータで watch 向け（人感・温湿度など観察用）と chat 向け（在宅状態など会話用）のフィルタリングを切り替える。おもなデバイス以外の個別エンティティは `ha_get` で取得する。

---

## ha-mcp（読み取り専用）

**実装**: `embodied_ha/ha-mcp.py`（65行）

HA REST API の GET エンドポイントのみを提供する読み取り専用サーバー。家電操作は `ha-control-mcp` に分離されており、このサーバーを接続しても操作はできない（物理的な分離）。HA トークンはサーバー内に隠蔽され Claude には渡さない。

### ツール

| ツール | 説明 | 入力 | 出力 |
|---|---|---|---|
| `ha_get` | HA の REST API パスに GET リクエストを送る | `path`: API パス（例: `states`, `states/climate.living`, `history/period?filter_entity_id=xxx`, `services`） | JSON レスポンス |

---

## ha-control-mcp（書き込み専用）

**実装**: `embodied_ha/ha-control-mcp.py`（125行）

家電操作（HA サービス呼び出し）のみを提供するサーバー。`watch` / `explore` ループには `EHA_AUTONOMOUS=1` のときのみ接続される（watch/explore + `EHA_AUTONOMOUS=0` の場合はツール自体が存在しない）。全操作は `log/actions.jsonl` に記録される。

### 許可ドメイン

`light`, `climate`, `switch`, `media_player`, `cover`, `fan`, `script`

`script` ドメインは `service` にスクリプト名を直接指定可能（`entity_id` 省略可）。視聴予約スクリプト（`viewing_reservation_set` 等）の直呼びのための設計。

### ツール

| ツール | 説明 | 入力 | 出力 |
|---|---|---|---|
| `ha_call_service` | HA サービスを呼んで家電を操作する | `domain`: 許可ドメインのいずれか<br>`service`: サービス名<br>`entity_id`: 対象エンティティ（script 直呼び時は省略可）<br>`data`: 追加パラメータ（任意） | 成功/失敗メッセージ |

操作後、`embodied_action.action_fields_for_control()` でアクションモードとコストを算出し、`apply_action_to_body_state()` で body_state を更新する。

---

## camera-mcp

**実装**: `embodied_ha/camera-mcp.py`（268行）

go2rtc ストリームと HA カメラプロキシの両方に対応。`source` の形式で自動判別:
- `camera.xxx` 形式 → HA カメラプロキシ（`/api/camera_proxy/<entity_id>`）
- それ以外 → go2rtc ストリーム（`/api/frame.jpeg?src=<name>`）

スナップショット取得時に `sensory_origin.classify_sensory_origin()` でカメラの部屋・アクセスモードを判定し、`action_fields_for_sensory()` でコストを算出して `apply_action_to_body_state()` を呼ぶ。

### ツール

| ツール | 説明 | 入力 | 出力 |
|---|---|---|---|
| `camera_get` | カメラのスナップショットを取得して画像で返す | `source`: HA entity_id（`camera.xxx`）または go2rtc ストリーム名 | `camera_context` オブジェクト（JSON）＋ JPEG 画像（base64） |
| `camera_ptz` | リビングカメラをパン/チルト操作する | `direction`: `left`/`right`/`up`/`down` | 成功/失敗メッセージ |

`camera_context` には `source`, `room`, `timestamp`, `sensory_origin`, `action_mode`, `action_cost` 等が含まれる。

PTZ 操作は固定の HA button エンティティを呼ぶ（`button.rihinkunokamera_pan_*`/`tilt_*`）。direction の指定は「カメラが映す方向」で、物理的なパン方向とは逆になる点に注意（`pan_left` ボタン = 部屋の右側が映る）。

---

## audio-mcp

**実装**: `embodied_ha/audio-mcp.py`（811行）

複数の音声ソース（RTSP/ALSA/TCP）への能動的な聴取と、常時 STT ログの読み取りを提供する。

### ツール

| ツール | 説明 | 入力 | 出力 |
|---|---|---|---|
| `listen` | 指定ソースから音声を録音し、音量検出と STT（オプション）を実行する | `source`: 音声ソース（省略時はデフォルト）<br>`duration`: 録音時間（秒、最大30）<br>`stt`: STT 実行するか（デフォルト false） | 音量レベル・STT 結果・auditory_context JSON |
| `queue_next_listen` | 次セッション用に録音を予約する | `source`, `duration` | 予約 ID |
| `read_audio_log` | 常時 STT の生ログを読み取る | `lines`: 取得行数 | ログテキスト |
| `read_heard_audio_log` | 検出された発話のログを読み取る | `lines` | ログテキスト |
| `read_active_listen_log` | 能動聴取セッションのログを読み取る | `lines` | ログテキスト |
| `read_non_speech_audio_events` | 非音声イベント（効果音等）のログを読み取る | `lines` | ログテキスト |
| `read_audio_event_tags` | タグ付き音声イベントの履歴を読み取る | `lines` | ログテキスト |

`listen` ツールは内部で `audio_daemon.py` に録音リクエストを送り（PulseAudio 共有のため）、完了後に `sensory_origin.classify_sensory_origin()` でコストを算出して body_state を更新する。

---

## body-mcp

**実装**: `embodied_ha/body-mcp.py`（806行）

電脳体の位置管理・移動計算を行うサーバー。ダイクストラ法で部屋グラフ上の最短経路とコストを計算する。詳細は [cyber_body_model.md](cyber_body_model.md) 参照。

状態は `body_location.json` に永続化され、変更時に MQTT でも publish される:
- `embodied_ha/body/physical_room/state` — 物理的な身体の部屋
- `embodied_ha/body/current_place/state` — 現在の存在場所（電脳体ホスト）

### ツール

| ツール | 説明 | 入力 | 出力 |
|---|---|---|---|
| `get_location` | 現在の身体位置・電脳体位置・コストを取得する | なし | 位置状態 JSON |
| `estimate_move_cost` | 部屋間の移動コスト・経路を計算する（移動しない） | `to_room` | コスト・経路 JSON |
| `move_to` | 物理的に別の部屋に移動する | `to_room` | 移動結果 JSON |
| `enter_cyberspace` | 電脳空間に入る（現在の部屋でデバイスと接続） | `host`: 接続先デバイス | 結果 JSON |
| `move_cyber` | 電脳空間内で別の部屋に移動する | `to_room` | 結果 JSON |
| `project_to` | 外部デバイス（スマホ等）に一時投影する | `entity`: `external://xxx` 形式の投影先 | 結果 JSON |
| `return_to_body` | 電脳空間から物理身体に戻る | なし | 結果 JSON |
| `get_room_graph` | 部屋グラフ（部屋・エッジ・コスト）を取得する | なし | グラフ JSON |

投影先（`projection_targets`）は `preferences.json` から読み込む。`external://xxx` 形式の entity ID に対し、`preferences.json` の `projection_targets` リストで実際の room_id を解決する。

---

## memory-mcp

**実装**: `embodied_ha/memory-mcp.py`（719行）

エピソード記憶・長期記憶・オープンループ・因果チェーン・daybook・記憶統合を管理する中核サーバー。`recall.sh`（FTS5 全文検索）と `loops.sh` をサブプロセスで呼ぶ。

### ツール

**検索・記憶**

| ツール | 説明 | 入力 | 出力 |
|---|---|---|---|
| `recall` | 過去ログ（観察・探索・会話・長期記憶）をキーワードで全文検索（OR 検索） | `keywords`: 検索キーワード配列 | ヒットしたログ抜粋 |
| `remember` | 長期記憶（`memory.md`）に一文を追記する。重複は自動スキップ | `note`: 記憶する内容 | 成功メッセージ |
| `get_working_memory` | 直近で活性化した episode を activation の高い順に最大5件返す | なし | episode 配列 |

**オープンループ**

| ツール | 説明 | 入力 | 出力 |
|---|---|---|---|
| `loops_list` | 開いたループの一覧を見る | なし | ループ一覧テキスト |
| `loops_add` | 新しいループを追加する | `text`: 内容<br>`source`: watch/explore/chat | ループ ID |
| `loops_close` | ループをクローズする | `id`: ループ ID<br>`reason`: 理由（任意） | 成功メッセージ |

**エピソード管理**

| ツール | 説明 | 入力 | 出力 |
|---|---|---|---|
| `record_episode` | 出来事単位の episode を構造化して保存する | `summary`, `tags`, `importance`, `kind`, `entities` 等 | episode JSON（`id` 付き） |
| `get_episode` | episode を ID で取得する | `episode_id` | episode JSON |
| `list_episodes` | episode を一覧化する（day/source/kind/limit で絞り込み可） | `day`, `source`, `kind`, `limit` 等 | episode 配列 |
| `record_counterfactual` | やろうとしたが止まったことを記録する | `loop`, `intent`, `summary`, `rejected_because`, `evidence`, `confidence` | counterfactual JSON |

**シーン管理**

| ツール | 説明 | 入力 | 出力 |
|---|---|---|---|
| `ingest_scene` | カメラ観察から抽出した objects/people/changes を scene として保存する | `source`, `objects`, `people`, `changes` | `{scene_id}` |
| `resolve_reference` | 「それ」「あれ」を直近 scene と shared_focus から候補解決する | `phrase` | 解決候補 JSON |
| `compare_recent_scenes` | 同じ camera の直近2 scene を比較し差分を返す | `source` | 差分 JSON |

**daybook・統合**

| ツール | 説明 | 入力 | 出力 |
|---|---|---|---|
| `build_daybook` | 指定日の daybook を生成・保存する | `date`, `episodes`/`episode_ids`, `summary`, `themes` 等 | daybook JSON |
| `get_daybook` | 保存済み daybook を取得する | `date` | daybook JSON |
| `consolidate_memory` | episode の重複を fingerprint で統合し report を保存する | `scope`, `day` | consolidation report JSON |
| `record_causal_chain` | 出来事間の因果関係を保存する | `cause_episode_id`, `effect_episode_id`, `relation`, `summary` 等 | causal_chain JSON |
| `get_causal_chain` | 因果関係を取得する | `chain_id` または `cause/effect_episode_id` | causal_chain JSON |

---

## sociality-mcp

**実装**: `embodied_ha/sociality-mcp.py`（645行）

人間関係・自己ナラティブ・ターンテイキング・割り込み判定・同意管理を担うサーバー。永続ファイルは `EHA_LOG_DIR` 以下の各 JSON/MD ファイルに保存される。

### ツール

**人間関係・ナラティブ**

| ツール | 説明 | 入力 | 出力 |
|---|---|---|---|
| `get_relationship` | 人物の関係プロフィール・履歴を返す | `person` | 関係 JSON |
| `update_relationship` | 人物の関係ノートを追記する | `person`, `note` | 成功メッセージ |
| `get_narrative` | 現在の自己ナラティブスレッドを返す | なし | ナラティブ MD |
| `append_narrative` | 自己ナラティブに一文追加する | `text` | 成功メッセージ |

**社会状態**

| ツール | 説明 | 入力 | 出力 |
|---|---|---|---|
| `get_social_state` | 現在のソーシャルモード・最近のインタラクション状態を返す | なし | social_state JSON |
| `update_social_state` | ソーシャル状態イベントを記録する | `event`, `person` 等 | 更新結果 JSON |
| `get_shared_focus` | 現在の共同注意トピック・コンテキストを返す | なし | shared_focus JSON |
| `set_shared_focus` | 共同注意フォーカスを更新する | `topic`, `context` 等 | 結果 JSON |

**バウンダリー・割り込み判定**

| ツール | 説明 | 入力 | 出力 |
|---|---|---|---|
| `get_person_model` | 人物のバウンダリーモデル（quiet_window/consent/turn-taking）を返す | `person` | person_model JSON |
| `record_boundary` | quiet_window / consent / turn-taking / focus を更新する | `person`, `boundary_type`, `value` 等 | 結果 JSON |
| `record_consent` | speak / action への同意許可・拒否を記録する | `person`, `intent`, `granted` | 結果 JSON |
| `should_interrupt` | 今割り込んで話しかけてよいかを評価する | `mode`, `intent`, `hour`, `metadata` 等 | `{allowed, reason}` |
| `get_turn_taking_state` | 現在のターンテイキング状態を返す | `person` | turn_taking_state JSON |
| `ingest_interaction` | 人間・エージェントの最近のインタラクションを取り込む | `person`, `type`, `content` 等 | 結果 JSON |

---

## tts-mcp

**実装**: `embodied_ha/tts-mcp.py`（57行）

`speak.py` をサブプロセスで呼ぶ薄いラッパー。現在のループスクリプト（watch/explore/chat）はこのサーバーを直接使わず、JSON の `speak` フィールドを解析して `speak.py` を直接呼ぶ。

### ツール

| ツール | 説明 | 入力 | 出力 |
|---|---|---|---|
| `speak` | 指定した部屋で TTS 発話または通知を送る | `room`: `preferences.json` の `speakers` キー<br>`message`: 話す内容 | 成功/失敗メッセージ |

部屋は `preferences.json` の `speakers` に登録されている必要がある。未登録の部屋を指定すると失敗する。

---

## http-mcp

**実装**: `embodied_ha/http-mcp.py`（183行）

ローカルネットワーク内の HTTP アクセスのみを許可する汎用 HTTP クライアント。外部インターネットへのアクセスは URL バリデーションで拒否される。

### 許可されるホスト

`localhost`, `127.x.x.x`, `10.x.x.x`, `172.16-31.x.x`, `192.168.x.x`, `homeassistant.local`

リダイレクトは `_NoRedirect` ハンドラによりブロックされる。

### ツール

| ツール | 説明 | 入力 | 出力 |
|---|---|---|---|
| `http_get` | ローカルネットワーク上の URL に GET リクエストを送る | `url`: ローカル URL<br>`headers`: 追加ヘッダー（任意） | レスポンスボディ |
| `http_post` | ローカルネットワーク上の URL に POST リクエストを送る | `url`: ローカル URL<br>`body`: リクエストボディ<br>`headers`: 追加ヘッダー（任意） | レスポンスボディ |

タイムアウトは30秒。
