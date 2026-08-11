# Changelog / 変更履歴

このアドオンの主な変更点を記録します。**2.0.0 以降**を対象とし、それ以前の履歴は git ログを参照してください。
Notable changes to this add-on. Tracked from **2.0.0** onward; for earlier history see the git log.

形式は [Keep a Changelog](https://keepachangelog.com/) に準拠します。
Format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added / 追加

- Home Assistantが公開するTTS対応言語と音声をWeb UIから動的に選べるようにしました。
  言語・音声以外のプロバイダー固有オプションは扱わず、HA側のTTSエンティティ設定を使用します。
  Added dynamic language and voice selection from Home Assistant TTS metadata. Other provider-specific
  options remain controlled by the Home Assistant TTS entity.

### Removed / 削除

- アドオン内蔵の常時音声デーモン、常時STT、ウェイクワード検出、背景音イベント生成と、
  それらの設定項目を削除しました。音声による呼び出しには任意のRTSP Assist Gateway等を利用します。
  Removed the built-in always-on audio daemon, continuous STT, wake-word detection, background-audio event
  production, and their settings. Spoken wake-up now uses an optional external route such as RTSP Assist Gateway.

### Changed / 変更

- `listen`、`use_device_microphone`、音声チャットなどの能動聴取と既存音声履歴の参照は維持します。
  起動時に廃止設定をバックアップ付きで除去し、古い常時STT履歴は24時間を超えると自動的な
  会話コンテキストへ注入しません。
  Kept active listening, voice chat, and read-only access to existing audio history. Startup now backs up and
  removes retired settings, and frozen always-on STT history older than 24 hours is no longer auto-injected.

## [2.1.16] - 2026-08-11

### Added / 追加

- RTSP Assist Gatewayが発行するversion付きウェイクイベントをMQTTで受け取り、検証済みの部屋を
  その音声chatへ直接結び付けて処理できるようにしました。旧形式のテキスト／JSON入力は維持します。
  Added support for versioned MQTT wake events from RTSP Assist Gateway, binding the validated room directly
  to the resulting voice chat while preserving legacy text and JSON inputs.
- stale・不正・重複イベントを拒否し、再起動後も同一request IDを再実行しないboundedな永続台帳を追加しました。
  Added a bounded persistent request ledger that rejects malformed, stale, or duplicate events, including
  replay of the same request ID after an add-on restart.

### Changed / 変更

- MQTT chatを順次処理し、Gateway由来の有効な入力は実行中chatの終了を上限付きで待つようにしました。
  MQTT chat messages are now processed sequentially, and valid Gateway inputs wait for an active chat with a
  bounded timeout instead of being silently discarded.

## [2.1.15] - 2026-08-10

### Changed / 変更

- 身体的なマイク入力をRTSPストリームへ統一し、アドオン内でALSA機器や独自TCPマイクへ
  直接接続する経路を廃止しました。旧設定は消去せず、RTSPへ置き換えるまで使用しません。
  Standardized embodied microphone input on RTSP streams and removed direct add-on connections to
  ALSA devices and proprietary TCP microphones. Legacy settings are retained but inactive until replaced.
- 発話先をHome Assistantの`media_player`エンティティへ統一しました。通常読み上げは選択した
  `tts.*`エンティティのHA側設定を使うTTS Media Sourceとして再生し、アドオン固有の
  VOICEVOX話者・速度・音量設定を廃止しました。
  Standardized speech output on Home Assistant `media_player` entities. Normal speech now plays as a TTS
  Media Source using the selected `tts.*` entity's Home Assistant settings, removing add-on-specific
  VOICEVOX voice, speed, and volume overrides.
- アドオンのホスト音声権限を削除し、音声ファイルをHome Assistant Media Sourceへ渡すための
  `media:rw`マウントへ置き換えました。
  Removed host audio access and replaced it with a `media:rw` mount for Home Assistant Media Source playback.
- 起動時に全マイク・スピーカー・TTS経路を検査し、旧ALSA/TCP設定や無効なHAエンティティを
  英語のアドオンログで具体的に報告するようにしました。
  Added startup validation for every microphone, speaker, and TTS route, with actionable English add-on log
  messages for legacy ALSA/TCP settings and invalid Home Assistant entities.

## [2.1.14] - 2026-08-07

### Fixed / 修正

- Home Assistantの操作後に存在しない報告手段を案内していた問題を修正し、現在の身体状態で
  音声報告できない場合に、報告可能な身体へ移る手順を明記しました。
  Corrected post-action guidance that referenced an unavailable reporting path and documented how to
  move to a body capable of reporting when the current embodiment cannot speak.
- 長期記憶の一部をコンテキストから省略した際、実際には行われていないコア記憶への要約を
  完了済みと表示していた問題を修正しました。元の記憶データと表示件数は変更しません。
  Removed an inaccurate claim that omitted long-memory entries had already been summarized into core memory,
  without changing the stored memories or the number of entries included in context.

## [2.1.13] - 2026-08-07

### Fixed / 修正

- `should_interrupt` の `intent` を `speak` / `action` の必須選択として明文化し、
  省略時も暗黙に発話扱いせず安全側へ拒否するようにしました。
  Made `should_interrupt.intent` an explicit required choice between `speak` and `action`,
  with omitted values denied instead of being silently treated as speech.
- AntigravityへMCPツールの正規Schemaを秘密情報なしのmanifestとして渡し、
  MCP起動用の資格情報をmodel可読設定から分離しました。資格情報は権限を限定した
  呼び出し単位の一時ファイルから専用launcherだけが読み、終了時に削除します。
  Added a secret-free MCP schema manifest for Antigravity and moved MCP launch credentials out of
  model-readable configuration into restricted per-invocation files consumed only by a dedicated launcher
  and removed when the invocation ends.

## [2.1.12] - 2026-08-07

### Fixed / 修正

- カメラへの投射中にライブ画像や履歴画像を見ると、投射状態と身体へ戻りたくなる圧力が消えていた問題を
  修正しました。画像取得中に投射先が変わった場合（同じカメラへ再侵入した場合を含む）は画像を返しません。
  Fixed live and historical camera viewing so it no longer clears active camera projection state or return-to-body
  pressure. Images are withheld if the projection changes during acquisition, including re-entry into the same camera.

## [2.1.11] - 2026-08-07

### Fixed / 修正

- カメラMCPを共通のJSON-RPC処理へ統合し、不正なツール呼び出しやカメラ処理中の想定外例外で
  MCPサーバー全体が停止しないようにしました。共通処理でも入力型を検証し、内部の例外詳細は
  エージェントへ返さずアドオンのローカルログだけへ記録します。
  Migrated the camera MCP server to the shared JSON-RPC loop so malformed tool calls and unexpected camera
  handler errors no longer terminate the server. The shared boundary now validates structured inputs and keeps
  internal exception details in local add-on logs instead of returning them to the agent.

## [2.1.10] - 2026-08-07

### Fixed / 修正

- セットアップ待ちをHome Assistantへ定期的に通知し、セットアップ完了後は通知を解除するようにしました。
  Setup reminders are now repeated at a bounded interval and dismissed after runtime setup recovers.
- 日誌生成の停止を起動時だけでなく15分ごとに確認し、異常時はHome Assistantへ通知するようにしました。
  復旧後やアドオン再起動後に残った古い通知も解除します。
  Daybook liveness is now checked every 15 minutes and reported through Home Assistant, with stale warnings
  reconciled after recovery and add-on restarts.
- 音声デーモンまたはWebサーバーが短時間に5回続けて停止した場合に通知するようにしました。
  自動再起動の間隔は従来どおり変更しません。
  Repeated audio-daemon or Web-server exits now raise a notification after five short-lived failures without
  changing the existing restart interval.
- 自律ループの連続失敗3回に加え、失敗が始まってから最後の成功が4時間以上前になった場合も
  通知するようにしました。
  Loop invocation failures now alert after either three consecutive failures or an active failure streak whose
  last successful invocation was at least four hours ago.

## [2.1.9] - 2026-08-06

### Fixed / 修正

- 観察が無かった日の処理済みマーカーを、日誌ファイルだけが欠けた故障として起動時に誤警告する問題を
  修正しました。観察ログが欠損・破損していて正常な空日と確認できない場合は、従来どおり警告します。
  Fixed a startup false alarm that treated a completed zero-observation day as a missing daybook. The watchdog
  continues to warn when missing or malformed observation input prevents it from confirming a legitimate empty day.

## [2.1.8] - 2026-08-06

### Fixed / 修正

- 長い音声区間の音響特徴を解析するとき、必要なサンプルを選ぶ前に区間全体をメモリへ展開していた
  処理を修正し、常時聴取デーモンの一時的な処理時間とメモリ使用量を削減しました。
  Reduced transient processing time and memory use in the always-on audio daemon by selecting the required
  acoustic-analysis samples before materializing a long audio segment.

## [2.1.7] - 2026-08-06

### Fixed / 修正

- Home Assistantサービス呼び出しで、追加データ内の`entity_id`が明示した操作対象を上書きし、
  監査記録と実際の対象が食い違う問題を修正しました。
  Fixed Home Assistant service calls allowing an `entity_id` in additional data to override the explicit
  target and diverge from the audit record.
- センサー描画処理が途中結果を出して異常終了した場合に、成功として扱う問題を修正しました。
  Fixed sensor rendering failures with partial output being reported as successful results.
- 音声認識が設定済みのHome Assistant URLを使用するようにし、TCPスピーカーの送信先変数を明確化しました。
  利用者に関係しないVoiceS3R開発環境向けの起動時注意ログも削除しました。
  Updated speech recognition to honor the configured Home Assistant URL, clarified TCP speaker target
  handling, and removed a VoiceS3R development-only startup warning from user logs.

## [2.1.6] - 2026-08-05

### Added / 追加

- Home Assistantカメラとgo2rtc映像ソースの静止画を、既定OFF・指定した保持時間で一時保存する
  カメラ履歴を追加しました。履歴は再起動で消える専用の一時領域に置かれ、エージェントは現在
  入っているカメラからだけ、専用MCPを明示的に呼び出して参照できます。高度な設定から機能と
  保持時間（1〜60分）を設定できます。
  Added opt-in rolling camera history for configured Home Assistant cameras and go2rtc sources. Frames are
  kept in a restart-ephemeral cache for the configured 1–60 minute window, and the agent can review them only
  through an explicit MCP call while inhabiting that camera. The feature is disabled by default.

### Changed / 変更

- カメラ投射中の毎ターン画像注入と、observe時の追加LLMによる見守り要約を廃止しました。
  必要なときに現在映像または履歴を明示的に確認する方式へ変更し、トークン消費を抑えます。
  Removed passive per-turn image injection during camera projection and the extra observe-mode watch-summary
  LLM call. Camera evidence is now acquired explicitly when needed to reduce token use.
- `camera.*`以外の設定済み映像ソースもカメラ投射として扱い、身体位置・欲求・会話・ループ間で
  同じデバイス能力判定を使用するようにしました。
  Treats configured non-`camera.*` video sources as camera projections and uses the same capability lookup
  across body location, desires, chat, and autonomous loops.

### Fixed / 修正

- 成功したカメラ取得だけを視覚的主張の根拠として扱います。履歴設定を読めない場合は取得を停止して
  専用キャッシュを消去し、起動時にも前回の一時履歴を引き継ぎません。
  Only successful camera tool results now ground visual claims. Invalid history settings fail closed and clear
  the dedicated cache, which is also cleared at startup.
- 連続失敗の通知、スピーカー再生前の音声ファイル検証、WAVパスの境界検査、センサー設定読込時の
  ファイルクローズを修正しました。
  Fixed consecutive-failure notifications, audio validation before speaker playback, WAV path boundary checks,
  and file closing while rendering sensor configuration.

## [2.1.5] - 2026-08-02

### Fixed / 修正

- Antigravityで日誌を生成すると、CLIが正常終了しても空の応答になり、同じ日の日誌を繰り返し
  試してしまう問題を修正しました。日誌ではCLIの構造化出力を使い、対応前のCLIが選ばれている
  既存個体は、認証を保持したまま起動時に対応版へ更新します。更新に失敗した場合は旧CLIへ戻し、
  不完全な日誌や完了markerを保存しません。
  Fixed Antigravity daybook generation repeatedly retrying the same day after the CLI exited successfully
  with an empty response. Daybooks now use the CLI's structured output, and existing instances with an
  older selected CLI update to a compatible version at startup without changing authentication. Failed
  updates restore the previous CLI and do not save partial daybooks or advance the completion marker.

## [2.1.3] - 2026-08-02

### Fixed / 修正

- 日誌生成がClaude Codeに固定され、CodexやAntigravityを選んだ個体でも選択が無視される問題を
  修正しました。日誌は個体が選択したハーネスと既定モデルを使い、ツールを無効にしたまま
  共通schemaで検証します。空応答・不正出力・timeout時は日誌も完了markerも進めません。
  Fixed daybook generation ignoring the instance's selected harness and always invoking Claude Code.
  Daybooks now use the selected harness and default model with tools disabled and shared schema validation;
  empty, invalid, or timed-out responses leave both daybook state and the completion marker unchanged.

## [2.1.2] - 2026-08-01

### Fixed / 修正

- 以前のバージョンからAntigravity個体を更新したとき、古いHome Assistant認証情報を含む
  グローバルMCP設定が残り、現在の設定より優先されてHAへ接続できなくなる問題を修正しました。
  EHAが生成した旧形式と確認できる設定だけを、復元可能なバックアップ名へ退避します。
  Fixed upgraded Antigravity instances being unable to reach Home Assistant because a legacy global
  MCP configuration with an outdated token remained and took precedence over the current site config.
  Only files matching EHA's legacy generated format are moved to a recoverable backup name.

## [2.1.1] - 2026-07-31

### Fixed / 修正

- 定期実行が「予期しないトリガー」と誤判定され、実行間隔ごとに好奇心とストレスが
  余分に増えていた問題を修正しました。表示用の起動理由とは別にトリガーの由来を判定し、
  定期実行・手動実行と、会話・外部イベントを区別します。
  Fixed scheduled runs being misclassified as unexpected triggers, which added extra curiosity and
  stress on every interval. Trigger origin is now classified separately from the human-readable reason,
  distinguishing scheduled and manual runs from conversations and external events.

## [2.1.0] - 2026-07-31

### Changed / 変更

- Antigravityで音そのものを確認する「深聴き」を、次回セッションの予約ではなく、現在のターンで
  1〜30秒録音して確認する同期ツールへ変更しました。
  Changed Antigravity deep listening from a next-session queue into a same-turn tool that records
  for 1–30 seconds and returns the audio for immediate inspection.
- 選択中のAIがAntigravityではない個体では、使われなくなったAntigravity本体と認証情報を
  起動時に削除するようにしました。キャラクターのデータや記憶は削除しません。
  Instances that do not use Antigravity now remove the obsolete Antigravity binary and credentials
  at startup. Character data and memories are not removed.

### Fixed / 修正

- Antigravityのファイル読み取り制限が、深聴きで生成した一時音声まで拒否してしまう問題を修正しました。
  `/config`・`/data`・プロセス環境などの機密領域は引き続き明示的に拒否します。
  Fixed Antigravity file-read hardening blocking the temporary audio produced by deep listening.
  Sensitive areas such as `/config`, `/data`, and process environments remain explicitly denied.

### Removed / 削除

- Claude Code / Codexから一時的にAntigravityへ切り替えて音声を処理するフォールバックと、
  そのための聴取キュー・実験中機能UIを削除しました。
  Removed the queued-listening fallback that temporarily switched Claude Code or Codex sessions to
  Antigravity, together with its experimental UI.

## [2.0.14] - 2026-07-31

### Security / セキュリティ

- 外部サービスへ渡す入力と、エージェントが読み取れるファイルの検証を強化しました。
  Strengthened input validation for external services and boundaries around files available to agents.
- Home Assistantとの認証通信で、認証情報を子プロセスのコマンドラインへ含めないようにしました。
  Prevented Home Assistant credentials from being included in child-process command lines.

### Fixed / 修正

- 設定・記憶・関係性データの更新中に、同時保存や読み取り失敗によって既存内容が失われる問題を修正しました。
  Fixed existing settings, memories, and relationship data being lost during concurrent saves or read failures.
- 利用者固有の音声機器を前提とする既定値を撤去し、有効なマイクがない場合は理由を記録して
  待機し、設定後に回復するようにしました。
  Removed user-specific audio defaults; when no valid microphone is available, hearing now reports the
  reason, waits without repeatedly restarting, and recovers after configuration.

## [2.0.13] - 2026-07-30

### Fixed / 修正

- Claude Codeの生の実行記録を標準エラー出力へ大量に流すことで、長いツール結果を含む
  自律ループが`BlockingIOError`で失敗する問題を修正しました。実行記録は権限を制限した
  一時ファイルで受け渡し、処理後に削除します。
  Fixed autonomous Claude Code turns failing with `BlockingIOError` when large tool results were
  copied into stderr. Raw execution transcripts are now passed through restricted temporary files
  and removed after processing.
- 音声での会話中に生じた非公開の内省が保存されず、独り言画面から欠落する問題を修正しました。
  音声の内省だけを専用ログへ保存し、発言内容や返答を会話履歴へ追加せずに独り言画面へ反映します。
  Fixed private introspection from voice conversations being discarded. Voice introspection is now
  stored separately and shown in the soliloquy view without adding the spoken turn to chat history.

## [2.0.12] - 2026-07-29

### Fixed / 修正

- 起動のタイミング次第で、アドオンは動いているのに自律ループ・会話・MQTT受信が
  いつまでも始まらないことがある問題を修正しました。準備完了の確認と実際の起動の間に
  状態が変わると起動を見送るのですが、そのあと誰も再試行していませんでした。
  Fixed a startup race where the add-on could stay running while the autonomous loop, chat,
  and MQTT listeners never started. When readiness changed between the check and the actual
  start, the start was skipped and nothing retried it.

### Changed / 変更

- どのAIで動かしていても、自律ループ中にファイルを読めるようになりました。
  これまでは Claude と Antigravity では実際には読めていた一方、Codex だけ読めておらず、
  AIによって挙動が違っていました。
  Made file reading available during autonomous loops regardless of which AI runs your
  companion. Previously it effectively worked on Claude and Antigravity but not on Codex,
  so behaviour differed by harness.

## [2.0.11] - 2026-07-29

### Fixed / 修正

- Web UI から設定を保存すると、入力欄を持たない項目（カメラの PTZ 設定、スピーカーの
  `media_player` など）が毎回黙って消えていた問題を修正しました。保存で送られてこなかった
  キーは、これまでの値をそのまま残します。項目そのものの削除はこれまでどおり反映されます。
  なお、すでに失われた値は戻りません。
  Fixed the Web UI settings save silently dropping keys that have no input field (such as a camera's
  PTZ settings or a speaker's `media_player`). Keys absent from the save request now keep their
  previous values, while deleting an entry still works as before. Values already lost are not restored.
- 日誌（daybook）のまとめが実際には作られていないのに「昨日まで済んだ」印だけが進んでしまい、
  以後ずっと日誌が作られなくなる問題を修正しました。生存確認も、印だけでなく日誌ファイルの
  有無を見るようになりました。
  Fixed the daybook rollup advancing its "done through yesterday" marker even when no daybook was
  actually written, which silently stopped all later rollups. The liveness check now also looks for
  the daybook files themselves, not just the marker.

## [2.0.10] - 2026-07-29

### Security / セキュリティ

- `ha_get` の `path` と `ha_call_service` の `service` を、HA REST URL へ連結する前に検証するようにしました。
  検証していないと、curl のパス正規化により Home Assistant API の外へ到達できました。
  Validated the `path` of `ha_get` and the `service` of `ha_call_service` before they are joined into a
  Home Assistant REST URL. Without validation, curl's path normalization allowed requests to reach
  outside the Home Assistant API.

### Added / 追加

- 自律ループの起動に連続して失敗したとき、Home Assistant の通知でお知らせするようにしました。
  失敗の理由も `log/invoke_failures.jsonl` に残るので、後から原因を辿れます。
  Added a Home Assistant notification when the autonomous loop repeatedly fails to start, and
  recorded the failure reason in `log/invoke_failures.jsonl` so the cause can be traced afterwards.

## [2.0.9] - 2026-07-26

### Fixed / 修正

- Antigravityの非対話ループが、未承認の`command`ツールへ迂回して空応答になる問題を抑制しました。
  Prevented Antigravity headless loops from falling back to the unapproved `command` tool and returning an empty response.

## [2.0.4] - 2026-07-25

### Changed / 変更

- 会話欄の投稿種別ラベルを整理し、直接会話だけを「会話」と表示して、自律投稿はラベルなしに統一しました。
  Simplified conversation-room labels so only direct chat is marked as “Conversation,” while autonomous posts have no label.

### Fixed / 修正

- 既存のClaude Code設定ディレクトリが権限・I/Oエラーで読めない場合に、空の設定と誤認して新しい保存先へ切り替えないようにしました。
  Prevented unreadable legacy Claude Code configuration directories from being mistaken for empty configurations and silently switched to a new location.

## [2.0.3] - 2026-07-25

### Changed / 変更

- 会話ログのエージェント発言キーと表示を`Claude`からハーネス非依存の`Agent`へ一般化しました。既存ログは読み取り時に変換されます。
  Generalized chat-log agent messages and labels from `Claude` to harness-neutral `Agent`; existing logs are normalized when read.

### Security / セキュリティ

- Claude Code経路で組み込み`Bash`ツールを明示的に禁止しました。
  Explicitly disabled Claude Code's built-in `Bash` tool.

## [2.0.2] - 2026-07-25

### Added / 追加

- 自律チャットの未読が3件以上ある場合、緊急でない新規投稿が制限されることをエージェントへ通知するようにしました。
  Added a runtime delivery notice that discourages non-urgent autonomous chat posts when three or more remain unread.
- AI LoungeのGitHub App秘密鍵を個体ごとのデータディレクトリに保存できるようにしました。
  Added per-instance storage for the AI Lounge GitHub App private key.

### Changed / 変更

- エージェント子プロセスのPATH構築を共通化し、重複を除去して汎用的な既定値を使うようにしました。
  Unified agent subprocess PATH construction, removed duplicate entries, and replaced development-specific defaults.
- `enter_cyberspace`がTCP音源の部屋を新旧の音源設定またはスピーカー設定から解決できるようにしました。
  Enabled `enter_cyberspace` to resolve TCP device rooms from current and legacy audio-source settings or speaker settings.

## [2.0.1] - 2026-07-25

### Added / 追加

- 個体ごとにVOICEVOXの話者・音量・音高・話速を設定できるようになりました。
  Added per-instance VOICEVOX speaker, volume, pitch, and speed settings.

### Changed / 変更

- ホスト本体のlocalスピーカー出力だけを1.5倍に増幅し、リミッターで音割れを抑えるようにしました。
  Increased local host-speaker playback gain by 1.5x with limiting; TCP and Home Assistant media-player outputs are unchanged.
- Codex・Antigravityでもカメラ画像を含む自律観測を実行できるようにし、AI呼び出し失敗を成功扱いにしないよう修正しました。
  Enabled camera-image observations for Codex and Antigravity, and stopped treating failed AI invocations as successful loop turns.

### Removed / 削除

- Python実装への移行後も残っていた旧`chat.sh`・`loop.sh`を削除しました。
  Removed the obsolete `chat.sh` and `loop.sh` wrappers left after the Python migration.

## [2.0.0] - 2026-07-24

複数のハーネス（この個体を動かす AI）に対応しました。初回セットアップで **Claude Code / Codex / Antigravity** から選べます。
Multi-harness support: choose which AI runs your companion — **Claude Code / Codex / Antigravity** — during first-time setup.

### ⚠️ Breaking / 破壊的変更

- **`claude_config_dir` オプションを削除しました。** 記憶と認証の保存先はユーザーが変更できなくなります（安全のための設計是正）。
  設定していなかった既存インスタンスは、これまでの保存先（`/config/embodied-ha/.claude`）を**自動で使い続けます**（移行不要）。
  **このオプションを設定していた場合は、更新前にそのディレクトリを退避・移動してください** — 更新後は参照されず、データが孤立します。
  - **Removed the `claude_config_dir` option.** The location of memory and credentials is no longer user-configurable (a safety-driven design fix).
    Existing installs that never set it **keep using their current location** (`/config/embodied-ha/.claude`) automatically; no migration needed.
    **If you had set this option, back up / move that directory before updating** — it will no longer be read and the data would be orphaned.

### Added / 追加

- 初回セットアップのハーネス選択ウィザード（未選択 → 選択 → インストール → ログイン → 起動）。
  First-run harness selection wizard (select → install → sign in → start).

### Changed / 変更

- ログアウト／アンインストールの導線を整理（通常操作からは非公開。ハーネスの切り替えはアドオンの再インストールで）。
  Reworked logout / uninstall flows (hidden from normal use; switch harness by reinstalling the add-on).
