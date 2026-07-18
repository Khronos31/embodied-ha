# loop.py移行 ランタイム契約

loop.py移行は2026-07-16、専用のカットオーバーred-teamパスとフォローアップ検証の
完了を経て、本番daemonの経路を`loop.sh`から`loop.py`へ切り替えた。`loop.sh`は
ロールバック用としてリポジトリに残している。

本ドキュメントは、この移行にあたり**形状(スキーマ)互換を維持しなければならない
ランタイムファイル**を追跡する。shadow parityテスト自体は#14増分7で旧経路コードと
ともに削除済み([[embodied_ha_phase1_shadow_parity_tests_obsolete_2026-07-17]])だが、
ここに記す契約(書き手・読み手・保持フィールド)は現行実装にも引き続き適用される。

## カットオーバー状況

- カットオーバーは2026-07-16に完了: `daemon.py`は`python3`で`loop.py`を起動する。
- `loop.sh`は意図的に温存しており、まだ削除してはならない。カットオーバーを
  巻き戻す場合のロールバック経路である。
- 過去のブロッカーはすべてクローズ済み。`loop.py`はかつて、5モード全てが
  end-to-endのshadow parityテストに合格するまでカットオーバー不可
  (not cutover-ready)とマークされていた。
- **解決済み(2026-07-17、#14増分6)**: `loop.py`の`run()`にあった`EHA_SESSION_BIN=agy`
  時の`SystemExit`ガードは撤去された。loop.pyのqueued listenターンにおける
  Antigravity音声対応は、`invoke_loop_claude()`が`--sound-file`/`--agent-site <mode>`を
  `invoke-agent.sh`へ転送する方式で実装済み(`chat.py`の#14増分5と同型)。
  `EHA_SESSION_BIN`自体はレガシー変数
  ([[embodied_ha_invoke_agent_caller_argument_open_items_2026-07-15]]項目9、削除予定)
  であり、`loop.py`はもうハーネス選択にこれを読まない——ハーネス選択は
  `EHA_AGENT_HARNESS`を介した`invoke-agent.sh`の責務であり、`--sound-file`指定時は
  呼び出し元の意図に関わらず常に`agy`が強制される。
- カットオーバー後も、`agy --project <uuid>` / `agy --new-project`が
  `invoke-agent.sh`のMCP allowlist設計に記録したとおりに振る舞うこと
  (ワークスペースローカルな`.agents/mcp_config.json`の解決、`--project`の冪等性)、
  およびAntigravityの`includeTools`が実際にツール可視性を制限することを、実機テストで
  再確認し続けること。これらの挙動は特定バージョンのAntigravity CLIに対する実機検証で
  確認したものであり、公式ドキュメント由来ではないため、変わっている可能性がある。

## 読み書きインベントリ

| ファイル | 書き手 | 読み手 | 契約 |
|---|---|---|---|
| `observations.jsonl` | `loop.py` observe(現行)、`chat.py`系コンテキスト経路、ロールバック温存の`loop.sh`/`chat.sh` | `chat_context.py`、`recent_chat_context.py`、`recall.sh`、`daybook_rollup.py`、`web/server.py`、memory系テスト | JSONL。`timestamp`・`emotion`・`private`を保持。任意フィールド: `facts`・`ungrounded_speech_claim`・`ungrounded_visual_claim`。パース失敗をここへ書き込んではならない。 |
| `explore.jsonl` | `loop.py`非observeモード(現行)、ロールバック温存の`loop.sh` | `chat_context.py`、`recent_chat_context.py`、`recall.sh`、`web/server.py` | JSONL。`timestamp`・`mode`・`emotion`・`private`・`topic`を保持。任意でgroundingフラグ。パース失敗をここへ書き込んではならない。 |
| `loop_parse_errors.jsonl` | `loop.py`(現行)、ロールバック温存の`loop.sh` | 診断・テスト | JSONL。`timestamp`・`mode`・`reason`・`raw`を保持。パース失敗の生テキストを永続化してよいのはここだけ。 |
| `pending_proposal.json` | `loop.py`(現行)、ロールバック温存の`loop.sh` | `chat_context.py`、`chat_postprocess.py`、chatプロンプト組み立て | JSONオブジェクト。`timestamp`・`proposal`・`action`を保持。actionが`domain`・`service`・`entity_id`を持つときのみ書き込む。 |
| `chat_log.jsonl` | `loop.py`/`loop.sh`、`chat.py`/`chat.sh`、`audio-mcp.py` | `recent_chat_context.py`、`chat_context.py`、`recall.sh`、`web/server.py` | JSONL。ループ由来レコードは`timestamp`・`source`・`claude`・`user: null`を保持。 |
| observeのscene/watch成果物 | `loop.py` observe(現行)、ロールバック温存の`loop.sh` | `scene_state.py`、memoryのscene取り込み、observe系テスト | `scene_objects`・`scene_people`・`scene_changes`の取り込み挙動を保持。見守り(watch)レポートはプロンプトコンテキストであり、それ単体では通常のmemory行にしない。 |
| socialityの状態・関係性ログ | sociality MCP、将来のsocialループ経路 | `sociality-mcp.py`、sociality系テスト、将来のプロンプトコンテキスト | ツール引数の厳格な検証を維持する。不正なペイロードは診断のみに留め、関係性の状態を変更してはならない。 |

## shadow parityの比較範囲(履歴)

Phase1(カットオーバー前)では、移行した各モードについて実shell経路とPython経路を
同一のfixture入力で比較していた:

- Claudeのargvと主要な環境変数値。
- MCP設定生成器への入力。
- 上記ファイル群へのランタイム副作用。
- パース失敗時・内省が空のときに通常の永続化が起きないこと。

この比較テスト群は、旧経路(直接claude呼び出し)の削除(#14増分7)に伴い、比較対象が
存在しなくなったため削除された。経緯は
[[embodied_ha_phase1_shadow_parity_tests_obsolete_2026-07-17]]を参照。
