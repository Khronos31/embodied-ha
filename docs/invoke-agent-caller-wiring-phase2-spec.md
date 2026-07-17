# invoke-agent.sh caller配線(#14) フェーズ2仕様(2026-07-16)

## 目的

`loop.py`(5モード: observe/explore/reflect/web/social)と`chat.py`(通常応答・queued listen)の
Claude Code直接呼び出し(`invoke_loop_claude()`等)を、`invoke-agent.sh`経由の呼び出しへ置換する。
`game-mcp.py`は対象外(#13で別途、stateful session設計が別問題のため)。

`76a384c`(2026-07-01)が壊した音声マルチモーダル本線(queued listen WAV→Antigravity)の復旧が
最終目的の一つ。Finding 1(音声マルチモーダルが実際に機能するか)・Finding 2(hacontrol権限境界の
マッピング)はいずれも解決済み([[embodied_ha_allowedtools_bash_boundary_investigation_2026-07-16]]、
[[embodied_ha_invoke_agent_contract_2026-07-15]]のFinding 2最終マッピング節)。

## 前提・決定済み事項(2026-07-16、ゆの承認済み。2026-07-16 dual red-team REVISE対応で改訂)

1. **cwd統一(移行期間は二重export)**: `invoke-agent.sh`に全ハーネス共通`EHA_AGENT_CWD`を新設し、
   Claude/Codex/Antigravity全てこれを見る。現状`run_claude()`は`EHA_AGENT_CWD`を一切参照して
   いない(`run_codex()`/`run_agy()`はフォールバックとして参照済み)ため、`run_claude()`にも
   同様のcwd解決を新規追加する必要がある(増分1のスコープ)。
   `run.sh`は`EHA_CLAUDE_CWD`と`EHA_AGENT_CWD`を**同じ値で両方export**する。
   **訂正(2026-07-17、増分7完了時点)**: 当初は「増分7で旧経路コードを削除するのと同時に
   `EHA_CLAUDE_CWD`のexportも止める」という一体の移行として計画していたが、
   `daybook_rollup.py`・ロールバック経路として温存される`loop.sh`/`chat.sh`は
   これら自体が削除されるまで(#14のスコープ外、別途将来の増分)引き続き
   `EHA_CLAUDE_CWD`のみを読み続けるため、**二重exportは増分7完了後も維持する**
   (ゆの承認済み)。詳細な訂正履歴は
   `docs/invoke-agent-caller-wiring-phase2-spec-changelog.md`参照。
2. **MCP設定生成の完全wrapper移行**: `loop.py`/`chat.py`は`mcp-config.py`を直接呼ばない。
   `apply_boundary_gate()`が返す`(allowed_tools CSV, mcp_servers tuple)`を、`mcp__`prefix
   の有無で`--allowed-mcp-tools`/`--allowed-builtins`に分け、`mcp_servers`はそのまま
   `--mcp-servers`へ渡す。`--mcp-config`(PATH直渡し)は新規呼び出し箇所では使わない
   (既存の`build_mcp_config()`/`--mcp-config`パスは削除)。
   **例外(2026-07-16 red-team必須修正)**: この呼び出しが`--sound-file`を伴う(=queued listen、
   Antigravityへの強制ルーティングが既に確定している)場合、callerは`--allowed-builtins`自体を
   invoke-agent.shへ渡さない。理由: `run_agy()`は`--allowed-builtins`が非空で渡されると
   無条件で`die`する(Antigravityにはbuiltin単位の制限機構がなく、`invoke-agent.sh`は
   「制限を要求されたが実現できない」ことを明示的に拒否する設計になっている)。
   `chat_invoke.py`の`_COMMON_TOOLS`には`"Read"`が含まれており、機械的prefix分割だけでは
   queued listen呼び出しが即死する(dual red-teamの両批評者が独立に発見、実コードで確認済み)。
   `--sound-file`を渡すこと自体が既に「Antigravityへ強制ルーティングする」というcaller側の
   既存の意思決定であるため、この除外はharness知識の新規漏洩ではない。`invoke-agent.sh`側の
   `die`は変更しない(fail-closedのバックストップとして温存)。
3. **段階的移行(shadow parity伴う)**: `loop.sh→loop.py`cutoverと同じ手法。各モード/経路ごとに
   「旧(直接claude呼び出し)のargv・MCP設定」と「新(invoke-agent.sh経由)のargv・MCP設定」を
   比較するshadow parityテストを先に作り、一つずつ切り替えて検証する。
   **限界(2026-07-16 red-team必須修正)**: shadow parityは「旧動作が既に存在する経路」にしか
   機能しない。queued listen(増分5・6)は現行`loop.py`/`chat_invoke.py`がEHA_SESSION_BIN/agy
   分岐を一切見ず常にClaude Codeを直接呼ぶため、比較対象の「旧動作」がそもそも存在しない。
   増分5・6では、shadow parity一致とは**別建ての独立した受け入れ条件**として、実際のWAV
   ファイルを使った実CLI音声smoke test(内容理解の確認)を必須とする。
4. **`concentrate_hearing`のbody-stateチェック位置バグ**(2026-07-16発見、
   [[project_embodied_ha_todo]]記載)は**このフェーズの対象外**。別途修正する。
5. **observeモードのcontent_blocks(カメラ画像)は当面Claude専用のまま**。`--sound-file`による
   agyフォールバックと画像contentの同時発生は、上記4のバグ修正後は発生しにくくなる想定のため、
   今回は対応しない(複数`@<path>`添付の実機検証も次フェーズ以降)。
6. **不可逆性の訂正(2026-07-16 red-team必須修正)**: 「データ移行は伴わない」という記述は
   誤り。`EHA_CLAUDE_CWD`→`EHA_AGENT_CWD`統一は、Claude Code組み込み自動メモリ
   (`.claude/projects/<slug>/`配下、あかねの自己像・経験の蓄積、25ファイル規模)への
   継続アクセスを断絶しうるリスクを伴う([[embodied_ha_claude_builtin_memory_2026-07-16]]、
   過去に実際に一度発生: コミット`0aacd19`)。増分1に、Claude用・agy用双方の`EHA_AGENT_CWD`
   解決結果が現行`EHA_CLAUDE_CWD`解決結果とbyte-identicalであることを機械的に検証するテストと、
   `.claude/projects/<slug>/`配下の既存メモリファイルへの継続アクセスを確認する手順を追加する。
7. **増分順序の妥当性(2026-07-16 red-team指摘・ゆの判断で現行順序を維持)**: 調査の結果、
   `run_codex()`/`run_agy()`は既に`EHA_AGENT_CWD`をフォールバックで参照しており、
   cwd統一・MCP設定wrapper化(増分1-4)が音声復旧(増分5-6、本epicの一次目的)の技術的前提に
   なっている根拠はない。それでも増分順序は「インフラ整備→5モード移行→音声復旧」の現行順を
   維持する(ゆの判断、2026-07-16)。実装上の都合(shadow parity基盤・cwd統一を先に固めてから
   最も複雑な音声経路に取り組む)による選択であり、技術的必然ではないことを明記しておく。
8. **`--content-json`のargv長制限(2026-07-16、observeモード移行の設計中に発見)**: Linuxの
   単一argv要素あたり128KB(`MAX_ARG_STRLEN`)上限により、inline JSONのまま`--content-json`を
   渡すとobserveモードの実カメラ画像(本番2台構成でも超過)で`Argument list too long`になる。
   `invoke-agent.sh`の`--content-json`にcurl `-d @file`と同じ`@<path>`形式を追加し、
   callerは一時ファイル経由で渡す(詳細:
   [[embodied_ha_invoke_agent_contract_2026-07-15]]Claude harness契約節)。

## 非目標

- `game-mcp.py`のstateful session配線(#13で別途)。
- Codex/Antigravityを初回ハーネス選択として使う機能(将来のWeb UI機能、未着手)。
- `concentrate_hearing`のbody-stateバグ修正。
- observeモードのcontent_blocksをCodex/Antigravity経由でも扱えるようにすること。

## 増分順序

1. **`EHA_AGENT_CWD`統一の実装・移行(二重export・拡張スコープ)**: `invoke-agent.sh`の
   `run_claude()`に`EHA_AGENT_CWD`解決を新規追加(現状未対応)。`run.sh`は移行期間中
   `EHA_CLAUDE_CWD`と`EHA_AGENT_CWD`を同じ値で両方export。`daybook_rollup.py`・
   ロールバック経路`loop.sh`/`chat.sh`は増分7完了まで引き続き`EHA_CLAUDE_CWD`のみ読む前提を
   維持し、この時点では変更しない(増分7でまとめて扱う)。
   受け入れ条件: (a) Claude用・agy用双方の`EHA_AGENT_CWD`解決結果が現行`EHA_CLAUDE_CWD`
   解決結果とbyte-identicalであることを機械的に検証するテストがgreen (b)
   `.claude/projects/<slug>/`配下の既存メモリファイルへ継続アクセスできることを実機確認
   (c) `daybook_rollup.py`・`loop.sh`/`chat.sh`が二重export後も従来通り動作することを確認。
2. **shadow parityテスト基盤の一般化**: `tests/test_loop_shadow_harness.py`のモデルを、
   任意モード/chat.pyでも使える形に拡張する(旧argv生成 vs
   新invoke-agent.sh呼び出しのargv+生成MCP設定を比較)。
3. **`loop.py`モード単位の切り替え**(最もリスクの低いモードから): 各モードごとに
   (a) shadow parityテストで旧新一致を確認 (b) 実際にinvoke-agent.sh経由へ切り替え
   (c) `python3 -m unittest discover -s tests`全green (d) 隔離環境での実CLIスモークテスト。
   モードの順序はリスク評価後に確定する(observeはcontent_blocksがあり複雑なため最後が妥当)。
4. **`chat.py`通常応答経路の切り替え**: 同様の手順。
5. **`chat.py` queued listen経路の切り替え**: `EHA_QUEUED_LISTEN_FILE` →
   `invoke-agent.sh --sound-file`変換をcaller側で実装。**`--sound-file`を渡す呼び出しでは
   `--allowed-builtins`を渡さない**(前提・決定済み事項2参照、`--allowed-builtins`+agy即死の
   回避策)。WAV削除責務・`RECENT_AUDITORY_INPUT`との併用条件
   ([[embodied_ha_invoke_agent_caller_argument_open_items_2026-07-15]]項目8)を確定する。
   受け入れ条件は shadow parity一致に加えて、**実際のWAVファイルを使った実CLI音声smoke test
   (内容理解の確認)を独立の必須基準として満たすこと**(shadow parityの緑だけでは音声経路の
   正しさを証明しない、前提・決定済み事項3参照)。
   **実装済み(2026-07-17)の追加事項**: 実機検証でagyのGo content-sniffがWAV/MP3/FLACの
   MIMEを誤判定しGemini APIに拒否されることが判明したため、`invoke-agent.sh`の
   `--sound-file`処理はWAV→WebM(opus)へ`ffmpeg`変換した上でプロンプトに埋め込む
   (caller側=`chat.py`/`chat_invoke.py`は無変更、WAVパスを渡すインターフェースのまま)。
   併せてプロンプトへ「command/shell/Pythonなどの実行ツールや外部スクリプトは使わず、
   view_fileだけで読み込め」という明示指示を追加し、`--dangerously-skip-permissions`は
   使わない(実機検証済み)。これはAntigravity側のGo content-sniffバグに対する暫定
   ワークアラウンドであり、Antigravity側で修正されたら不要になる。詳細:
   [[embodied_ha_agy_audio_mime_investigation_2026-07-17]]。
6. **`loop.py`のqueued listen経路の切り替え**: 同様(増分5と同じ`--allowed-builtins`除外
   ルール・独立smoke test基準を適用)。`loop.py:836`の`EHA_SESSION_BIN=agy`
   SystemExitブロックをここで撤去する。
7. **旧経路の削除**: `invoke_loop_claude()`の直接claude呼び出しコード、
   `build_mcp_config()`/`--mcp-config`直渡しパス、使われなくなった`EHA_CLAUDE_CWD`参照を削除。
   **訂正(2026-07-17、変更履歴参照)**: `run.sh`の二重export(増分1)はこのタイミングでは
   解消しない。`daybook_rollup.py`・`loop.sh`/`chat.sh`(ロールバック経路として温存)は
   これら自体が削除されるまで(#14のスコープ外、別途将来の増分)引き続き`EHA_CLAUDE_CWD`
   のみを読むため、二重exportは増分7完了後も維持し続ける。増分7では
   `daybook_rollup.py`・`loop.sh`/`chat.sh`が引き続き正しいcwdを取得できることを
   最終確認してから完了とする。
8. **全体検証**: 全テストgreen、実CLIスモークテスト(observeの画像・queued listenのWAV
   それぞれ)、実機での本番相当smoke test。

## 受け入れ条件(2026-07-17、増分7完了時点で確定・訂正版。訂正履歴は
`docs/invoke-agent-caller-wiring-phase2-spec-changelog.md`参照)

- `loop.py`が`EHA_AGENT_HARNESS=agy`でSystemExitしない。✅(増分6)
- `chat.py`のqueued listenが`--sound-file`を渡す。✅(増分5)
- `--sound-file`を伴う呼び出しで`--allowed-builtins`が渡されていないことをテストで固定している。✅
- `EHA_AGENT_CWD`/`EHA_CLAUDE_CWD`解決結果のbyte-identical検証テストがgreen、かつ
  Claude組み込み自動メモリへの継続アクセスを実機確認済み。✅(増分1)
- 増分5・6それぞれで、shadow parityとは独立の実CLI音声smoke testが合格している。✅
- daybook_rollup.py・loop.sh/chat.sh(ロールバック経路)が増分7完了後も正しいcwdで動作する
  ことを確認済み。✅(増分7、`EHA_CLAUDE_CWD`供給元がbyte-identicalであることを静的確認)
- **訂正・削除**: 「全モード・chat.py両経路で、shadow parityテストが旧新一致を確認している」
  「全既存テストgreen(件数減なし)」は、増分7で旧経路自体を削除する方針(承認済み)と
  構造的に両立しないため削除。旧経路が存在しない以上、新旧一致比較は不可能。件数は
  旧経路専用テスト削除により572→549件に減少(意図的、[[embodied_ha_phase1_shadow_parity_tests_obsolete_2026-07-17]]参照)。
- 実CLIスモークテスト(隔離環境)で音声マルチモーダル・画像contentがそれぞれ機能することを確認。
  ✅(増分8、2026-07-17完了。camera-mcp.pyのagyハング修正・agy headless MCP権限グラント
  自動配布の実装を経て、`chat.py`・`loop.py --mode observe`双方のPython caller経路での
  実マイク録音E2E完走まで確認。経緯と既知の残存リスクは
  `docs/invoke-agent-caller-wiring-phase2-spec-changelog.md`の「増分8完了」エントリ参照)
- `AGENTS.md`/契約メモの該当箇所が新設計と矛盾しない。
- 実機での本番相当smoke testは、デプロイ済み実機への反映は増分8の範囲外(ゆの判断、
  隔離環境での実CLI検証まで)。

## 制約

- design-change-gate対象。実装着手前にこの仕様でred-team相当を通すか、
  各増分の完了ごとに小さくレビューを挟むかは要判断。
- 既存テストのassertion変更は意図的な仕様変更として明記する。
- git commitは各増分の完了ごとにユーザー確認を得てから。
- 実装はCodex(既定terra)、レビューはsol。ただし数行規模の修正はClaude本体が直接行ってよい。
