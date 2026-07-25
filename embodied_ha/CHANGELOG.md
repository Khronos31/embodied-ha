# Changelog / 変更履歴

このアドオンの主な変更点を記録します。**2.0.0 以降**を対象とし、それ以前の履歴は git ログを参照してください。
Notable changes to this add-on. Tracked from **2.0.0** onward; for earlier history see the git log.

形式は [Keep a Changelog](https://keepachangelog.com/) に準拠します。
Format is based on [Keep a Changelog](https://keepachangelog.com/).

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
