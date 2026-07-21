# Changelog / 変更履歴

このアドオンの主な変更点を記録します。**2.0.0 以降**を対象とし、それ以前の履歴は git ログを参照してください。
Notable changes to this add-on. Tracked from **2.0.0** onward; for earlier history see the git log.

形式は [Keep a Changelog](https://keepachangelog.com/) に準拠します。
Format is based on [Keep a Changelog](https://keepachangelog.com/).

## [2.0.0] - Unreleased 🚧

> 🚧 **開発中のドラフト。リリースまで内容は変動します。**
> Draft under active development; contents may change before release.

複数のハーネス（あかねを動かす AI）に対応しました。初回セットアップで **Claude Code / Codex / Antigravity** から選べます。
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
