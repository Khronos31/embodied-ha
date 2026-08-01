# Changelog / 変更履歴

このアドオンの主な変更点を記録します。**2.0.0 以降**を対象とし、それ以前の履歴は git ログを参照してください。
Notable changes to this add-on. Tracked from **2.0.0** onward; for earlier history see the git log.

形式は [Keep a Changelog](https://keepachangelog.com/) に準拠します。
Format is based on [Keep a Changelog](https://keepachangelog.com/).

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
