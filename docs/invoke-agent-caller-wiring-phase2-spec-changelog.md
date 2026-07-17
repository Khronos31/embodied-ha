# invoke-agent.sh caller配線(#14) フェーズ2仕様 変更履歴

`docs/invoke-agent-caller-wiring-phase2-spec.md`は増分1着手時点(2026-07-16)の計画を
起点としている。実装を進める過程で判明した事実により、当初の計画から変更した箇所を
ここに記録する。仕様書本体は実態に合わせて直接書き換え、この履歴で「何を・いつ・
なぜ変更したか」を追跡できるようにする。

## 2026-07-17: 増分1の二重export解消方針を訂正

**当初の記述**(増分1策定時点):
> 増分7で旧経路コードを削除するのと同時にEHA_CLAUDE_CWDのexportも止める、
> という一体の移行として扱う。

**訂正後**:
`daybook_rollup.py`・ロールバック経路として温存される`loop.sh`/`chat.sh`は、
これら自体が削除されるまで(#14のスコープ外、別途将来の増分)引き続き
`EHA_CLAUDE_CWD`のみを読む。よって`run.sh`の`EHA_CLAUDE_CWD`/`EHA_AGENT_CWD`
二重exportは、増分7(loop.py/chat.py側の旧経路コード削除)が完了した後も
**維持し続ける**。

**理由**: 当初の計画は「旧経路コードの削除」と「`loop.sh`/`chat.sh`自体の削除」を
同一のタイミングと誤って想定していた。実際には前者(#14増分7)は`loop.py`/
`chat_invoke.py`内のPython関数の削除であり、後者(`loop.sh`/`chat.sh`という
ファイル自体の削除)は別の、まだ着手していない将来の増分である。`loop.sh`/`chat.sh`は
ロールバック経路として意図的に温存されており、それらが生きている限り
`EHA_CLAUDE_CWD`のexportを止めることはできない。

**影響**: `run.sh`の実際のexport文(値・変数名)は変更していない。変更したのは
コメントのみ(「増分7まで」という誤った予告を、「`loop.sh`/`chat.sh`自体の削除まで」
という正確な依存関係の記述に修正)。

## 2026-07-17: 受け入れ条件の削除・訂正

**削除した条件**:
- 「全モード・chat.py両経路で、shadow parityテストが旧新一致を確認している」
- 「全既存テストgreen(件数減なし)」

**理由**: いずれも増分7で旧経路コード自体を削除する方針(ユーザー承認済み、
[[embodied_ha_phase1_shadow_parity_tests_obsolete_2026-07-17]])と構造的に
両立しない。旧経路が存在しない以上、新旧比較テストは実行不可能であり、
旧経路専用テストの削除によりテスト件数も意図的に減少する(572→549件)。
これらの条件は増分1〜6の計画段階(旧経路がまだ存在する前提)で書かれたドラフトであり、
増分7の実施内容が確定した時点で見直しが必要だった。

**確認**: 旧経路削除・テスト削除自体は、実施前に専用の記録ファイル
([[embodied_ha_phase1_shadow_parity_tests_obsolete_2026-07-17]])を作成し、
ユーザー承認を得てから実施した。今回の変更は、その承認済みの実施内容に
仕様書側の記述を事後的に整合させる作業である。

## 2026-07-17: 増分8で発見した重大な未解決問題(camera MCPサーバーのハング)

増分8の実CLI検証で、`chat.py`のqueued listen経路を初めてフル本番構成
(全12 MCPサーバー: memory/ha/sociality/hacontrol/camera/audio/body/sensors/http/
lounge/game/song)で実行したところ、**`camera` MCPサーバーが含まれていると
agyクライアントがハングし応答が返らない**(60秒超、150〜260秒でも完了せず)ことが
判明した。`camera-mcp.py`単体は正常動作を確認済みで、原因はagyクライアント側の
`camera`サーバーとの接続処理にあると見られるが未特定。

これまでの増分5・6の実CLI検証はいずれもMCPサーバーなし・最小構成でのみ行っており、
フル本番構成での検証はこの増分8が初めてだった。詳細・再現手順・対策案は
[[embodied_ha_camera_mcp_hangs_with_sound_file_2026-07-17]]参照。

**現時点で増分8は未完了**。ゆの承認により原因調査を継続中(案A)。

## 2026-07-17: camera MCPサーバーハングの根本原因確定・修正実装

上記「原因はagyクライアント側の`camera`サーバーとの接続処理にあると見られるが未特定」は
その後の追加調査(stdio透過プロキシによる直接観測・12パターンの再現実験)で確定した。

**確定原因**: agy 1.1.3はMCP接続開始時、標準の`initialize`より**先に**独自リクエスト
`server/discover`(id=1, protocolVersion=2026-07-28)を送り、未対応methodには
JSON-RPC `-32601 Method not found`が返ることを期待してフォールバックする
(`server/discover → -32601 → initialize → notifications/initialized → tools/list → モデル生成`)。
他11個のEHA MCPサーバーは共通`mcp_lib.py`がこれに対応済みだが、`camera-mcp.py`は独自の
`main()`ループを持ち未知methodを黙殺するため、agyが応答を待ち続けてハングしていた。
手動テストが常に成功していた理由: 手動では`server/discover`を送らず`initialize`から
始めていたため、たまたま問題を踏んでいなかった。

**実装した修正**: `camera-mcp.py`の`main()`ループ末尾に、`mcp_lib.py:107`と同じ
`-32601 Method not found`応答を追加。回帰テストとして、単発の未知method応答・notification
無応答に加え、agyが実際に送る`server/discover → initialize → notifications/initialized →
tools/list`という一連のハンドシェイクシーケンスを固定するテストを追加した。

詳細・再現実験の全記録は[[embodied_ha_camera_mcp_hangs_with_sound_file_2026-07-17]]参照。

**現時点のステータス(2026-07-17更新)**: コード修正・単体テスト・独立レビュー
(gpt-5.6-sol、指摘なし)に加え、隔離環境での実CLI検証(`invoke-agent.sh`直接呼び出し、
外側でreturncode/stdout/stderr捕捉)まで完了。camera単体(6.6秒)、chat.py本番相当フル
12サーバー構成(9.9秒)、loop.py observe相当フル9サーバー構成(9.9秒)のいずれも
returncode 0・音声内容を正しく反映したJSON応答・プロセス残留なしを確認済み。
詳細は[[embodied_ha_camera_mcp_hangs_with_sound_file_2026-07-17]]参照。

**まだ完了していない**: `chat.py`/`loop.py`のPython subprocess経由(caller E2E)での
最終検証と、呼び出し失敗時の診断情報喪失問題([[embodied_ha_increment567_rereview_findings_2026-07-17]])
への対応判断。増分8はこれらが終わるまで未完了のまま。
(→ 同日中に両方完了。後続の「増分8完了」エントリ参照)

## 2026-07-17: 診断情報喪失の修正・agy headless権限問題の解決・caller E2E完走(増分8完了)

上記「まだ完了していない」2項目に対応し、その過程で新たに発見・解決した問題を含め、
増分8の実CLI検証を完了した。

1. **診断情報喪失の修正**(コミット`b2a03ba`): `invoke_chat_claude()`/`invoke_loop_claude()`が
   `returncode!=0`または空応答時に`returncode`+`stderr`末尾400文字を`sys.stderr`へ出力する
   ようにした(daemon.py経由でSupervisorログへ到達することをレビューで確認済み)。
2. **agy headless MCP権限問題の発見と解決**(コミット`6e6ca8c`): 実caller E2E(実マイク録音)で、
   agy 1.1.3のheadlessモードがMCPツール呼び出しを自動拒否しターン全体が空応答になる問題を
   発見(上記1の診断ログが無ければ不可視だった)。切り分けの結果、settings.jsonの
   `permissions.allow`はエラーメッセージの案内に反して無視され、`config.json`の
   `userSettings.globalPermissionGrants`(grant store)だけが機能すると確定(Googleへ
   バグ報告済み)。`run_agy()`が接続サーバー単位のワイルドカードグラント`mcp(server/*)`を
   agy HOMEのconfig.jsonへ自動配布するようにした。完全一致グラント・PreToolUse/Stopフックに
   よる代替は実機検証の上で不採用(詳細な経緯・トレードオフは
   [[embodied_ha_agy_headless_mcp_tool_permission_gap_2026-07-17]]参照)。
3. **`--sound-file`プロンプト文言の確定**(コミット`4041e66`): ユーザー確定の2行構成へ変更。
4. **caller E2Eの完走確認**: `chat.py`(субprocess起動、camera込みフル12サーバー+実マイク
   録音)と`loop.py --mode observe`(9サーバー+実マイク録音)の両方が、実際のPython caller
   経路で最後まで完走することを確認。observeターンは録音→agy音声理解→MCPツール実行→
   observeスキーマ準拠JSON→observations.jsonl保存→SAY出力→daybook後処理まで一気通貫。
5. **既知の残存リスク(受容済み)**: agyのheadlessモードで、モデルが確認を要する組み込み
   ツール(write_file等)を試みた場合はターンが打ち切られる(agy側の仕様+バグの複合、
   救済手段なしと実機検証で確定)。本番では診断ログ(上記1)により発生を観測できる。
   頻度が高ければファイル書き込みグラントのパススコープ検証を再検討する。

**増分8はこれをもって完了**。#14 epic(caller配線)の全増分が完了した。

## 2026-07-17: PR#2マージ前最終レビュー(gpt-5.6-sol)への対応

1. **Claude経路の`--effort medium`は意図的な構成として承認・文書化(ユーザー決定)**:
   旧・直接呼び出し経路は`--effort`を渡さず(CLIデフォルト=high)、新経路は
   `invoke-agent.sh`のmodel契約により`--effort ${EHA_CLAUDE_EFFORT_DEFAULT:-medium}`を
   常に渡す。これは本番の全通常運転(observe/explore/reflect/web/social/chat)の
   推論品質に関わる実効挙動の変更だが、**意図的なコスト最適化として現状のmediumを
   承認する**。恒久対応として、defaultティア(liteは対象外)の推論レベルとモデルを
   構成タブまたはWeb UIでユーザーが選択できるようにするTODOを起票済み
   (環境変数`EHA_CLAUDE_EFFORT_DEFAULT`/`EHA_CLAUDE_MODEL_DEFAULT`による上書きは
   現時点でも可能)。
2. **chat経路のツール操作監査ログの復元**: 増分7の旧経路削除で失われていた
   `[chat][tool] <name>: <detail>`形式の成功ツール使用ログ(家電操作・memory更新等の
   操作監査)を、invoke-agent.sh契約でstderrへ流れる生stream-jsonからの抽出として復元
   (`chat_invoke.log_tool_use_diagnostics()`)。
3. 非ブロッキング指摘3件(テキストpromptのargv渡し・`<site>/.agents/mcp_config.json`への
   SUPERVISOR_TOKEN永続書き込み・ドキュメント微不整合)は高優先TODOとして別途起票済み
   (このPRのスコープ外)。

## 2026-07-17: 「実機での本番相当smoke test」の範囲確定

増分8着手前にユーザーへ確認し、「実機での本番相当smoke test」は本番デプロイ・
実機への反映を伴わない、**隔離環境での実CLI検証の範囲まで**と確定した。
デプロイ・再起動・バージョンバンプは別途明示指示が必要という既存の運用ルール
(`CLAUDE.md`)に基づく。
