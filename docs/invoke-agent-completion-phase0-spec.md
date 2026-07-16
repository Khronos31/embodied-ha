# invoke-agent.sh完成化 フェーズ0仕様(2026-07-16)

## 目的

`76a384c`(2026-07-01)がqueued listenの音声マルチモーダル本線(`RECENT_AUDITORY_INPUT`へWAVパスを
渡しAntigravityへ処理させる設計)を、STTテキストのみへ静かに劣化させた。この後始末の本命として
`invoke-agent.sh`(3ハーネス差分吸収ラッパー)が既に相当実装されていたが、2026-07-16のCodex監査+
ユーザーレビューで、caller配線が未完了なだけでなく、ラッパー自体にも未実装・未設計の項目が
複数残っていることが判明した。本仕様はこれらを一括して片付けるための増分計画。

## 横展開検索・呼び出し元列挙(Hard Gate 0、2026-07-16実施)

- `invoke-agent.sh`/`invoke_agent`をリポジトリ全体でgrep: 呼び出し元は`embodied_ha/loop.py`のみ
  (production)。`tests/test_invoke_agent.py`・`tests/test_loop_shadow_harness.py`がテスト側。
  `chat.py`/`chat_invoke.py`/`game-mcp.py`/`daybook_rollup.py`は現時点で未接続(想定通り)。
- `mcp-config.py`をgrep: production呼び出し元は`chat.sh`/`loop.sh`(bash旧版)・`chat_invoke.py`・
  `antigravity_setup.py`・`run.sh`・`loop.py`・`invoke-agent.sh`自身。想定外の呼び出し元なし。
- `game-mcp.py`の`_start_cpu_session()`/`_ask_cpu_word()`(374-401行付近)を確認: 現行は
  `cpu_session_id = str(uuid.uuid4())`をcaller側で生成してから`--session-id`で渡す設計
  (426-427行)。想定通り。
- 他リポジトリ(`/config/GitHub/*`)への横展開: `invoke-agent.sh`はembodied-ha固有のハーネス差分
  吸収ラッパーであり、他リポジトリに同種の仕組み・重複実装は無い(cross-repo検索は対象外と判断
  ——単一リポジトリ内の複数caller間の整合性が本題であり、他リポジトリへの影響経路が無い)。

## 受け入れ条件(MUST/SHOULDトリアージ)

### MUST

1. **[#17]** ✅完了(2026-07-16、ユーザー承認済み)。`chat.py`の多重セッション(同一
   `--agent-site chat`)がAntigravity site config(`.agents/mcp_config.json`/`.eha_project_id`)へ
   並行書き込みしうるか判定した。結論: **非該当、追加ロック不要**。
   根拠: `daemon.py:319`の`run_chat()`が`CHAT_PY`起動の唯一の経路(grep確認、他に起動箇所なし)。
   `_chat_lock.acquire(blocking=False)`(daemon.py:322)により多重起動時は即skipされる
   (キューイングも並行実行もしない、loop.pyの`_loop_lock`と同型)。`loop.py`の5サイト
   (observe/explore/reflect/web/social)と`chat.py`の`chat`サイトは名前空間が重複しない。
   **将来の懸念(未解決・記録のみ)**: この安全性は「callerが常にdaemon.py経由(`_chat_lock`/
   `_loop_lock`)でしか起動されない」という運用上の前提に依存する。`invoke-agent.sh`自体には
   並行実行を防ぐ機構が無いため、将来daemon.py以外の経路(例: Web UIからの直接呼び出し)が
   同一siteを使うcallerとして追加された場合、この前提は崩れる。新規caller追加時は
   `embodied_ha_invoke_agent_contract_2026-07-15.md`のこの節を確認すること。
2. **[#11]** ✅完了(2026-07-16、Codex gpt-5.6-sol実装、案1採用)。`--allowed-mcp-tools`が
   不正形式・存在しないserver名を指定されたとき、`mcp-config.py`が黙ってスキップせず、
   明示的にエラー終了する。
   検証: `python3 embodied_ha/mcp-config.py --format codex --allowed-mcp-tools "mcp__haa__ha_get"
   /tmp/test-out.toml ha`を自分で実行、exit 2・stderr「unknown MCP server in allowlist: haa」・
   出力ファイル未生成を確認。
3. **[#11]** ✅完了。`--allowed-builtins`と`--allowed-mcp-tools`の統合契約が設計メモへ反映済み
   (`embodied_ha_invoke_agent_contract_2026-07-15.md`「tool allowlist契約(2026-07-16改訂・
   案1採用)」節)。旧`--allowed-tools`/`--allowedTools`は削除(unknown option化、自分で実行し
   確認済み)。`SERVER_SPECS`と各MCP serverの実`tools/list`一致を実サブプロセス起動で検証する
   テストを追加(`test_server_specs_match_runtime_tools_with_default_preferences`等)、
   `python3 -m unittest discover -s tests`544件全green(532→544)。
4. **[#14]** `chat.py`のqueued listen経路が、WAVファイルを`invoke-agent.sh --sound-file`へ渡す。
   検証: `tests/test_chat_integration.py`(または新規テスト)で、queued listenが有るときに
   `chat.py`が構築するコマンドに`--sound-file <wav>`相当が含まれることを確認する。
5. **[#14]** `loop.py`が`EHA_SESSION_BIN=agy`/`EHA_AGENT_HARNESS=agy`のときSystemExitせず、
   `invoke-agent.sh`経由でagy分岐を実行する。
   検証: `python3 -m unittest tests.test_loop_py -v`および新規シャドウパリティ相当のテストで、
   SystemExitが発生しないことを確認する。
6. **[#14]** `python3 -m unittest discover -s tests`が全green(既存527テストを壊さない)。

### SHOULD

7. **[#12]** ✅完了(2026-07-16、Claude直接実装)。`invoke-agent.sh`に`--system-prompt`の
   bashケースが追加され、契約メモ通りClaude(native `--system-prompt`)/Codex
   (`model_instructions_file`一時ファイル経由)/Antigravity(`[System Instruction]/
   [User Prompt]`形式のprompt prefix近似)それぞれの吸収方針で動く。
   検証: `tests/test_invoke_agent.py`に3件追加(claude/codex/agyそれぞれ)、
   `python3 -m unittest discover -s tests`531件全green(528→531)。
8. **[#15]** ✅完了(2026-07-16、Claude直接実装)。`EHA_SESSION_BIN` basename参照を
   `invoke-agent.sh`から削除、`--sound-file`時に`EHA_AGENT_HARNESS=agy`本体ならHigh固定
   しない分岐(`harness_was_agy`)を実装。
   検証: `test_sound_file_does_not_force_high_when_harness_already_agy`追加、
   `python3 -m unittest discover -s tests`532件全green(531→532)。
   設計メモ追記: `embodied_ha_invoke_agent_contract_2026-07-15.md`の該当2箇所を実装済みに更新。
9. **[#13]** `game-mcp.py`のWordVec CPU戦が、caller指定UUIDではなく初回応答からID取得する方式に
   なる(対戦ロジック自体は変更しない)。
   検証: `tests/test_game_mcp.py`(存在すれば)相当が引き続きgreen、かつ新規契約のテストが追加される。
10. **[#16]** ✅完了(2026-07-16、ユーザー承認案A採用)。`mcp-config.py`が`output_path`に
    ディレクトリ無し相対パスを渡されてもクラッシュしない。
    検証: `tests/test_mcp_config.py::test_bare_filename_output_path_does_not_crash`追加、
    `python3 -m unittest discover -s tests`528件全green(527→528)。
    設計メモ追記: `embodied_ha_invoke_agent_caller_argument_open_items_2026-07-15.md`§5末尾。

## 非目標

- Web UIのharness選択・表示系コード(契約メモが別範囲と明記済み)。
- `game-mcp.py`のWordVec CPU対戦ロジック自体の変更(#13はID取得方式のみがスコープ)。
- Antigravity/Codexへの`--content-json`(画像添付)実装(現状「明示拒否」のまま契約通り、
  今回のスコープ外)。

## 制約

- 既存テストのassertionを変更しない(`feedback_test_discipline.md`)。#11/#12/#14/#15は
  いずれも新規契約の追加であり、既存16テスト(`test_invoke_agent.py`)への影響有無を都度確認する。
- #14(caller配線)は`feedback_design_change_gate.md`の設計変更ゲート対象。実装前に該当増分で
  red-team skillを通す(手順4のAgentツール新規spawn、除染ブリーフ必須)。
- #11も安全性(MCPツールallowlistのfail-open懸念)に関わるため、実装前に自己チェック3問
  (フェーズ2)を最低限行う。red-team本発動はユーザー判断。
- git commitは各増分の完了ごとにユーザー確認を得てから。
- 大きな仕事なので、既存TaskList(#11〜#17)を主軸に増分の進捗を追跡する。

## 増分順序(依存関係を踏まえる)

1. #17(投資小・#14のスコープに影響しうる調査を先に片付ける)
2. #16(独立・trivial、後回しにする理由がない)
3. #12(独立・設計済み、実装のみ)
4. #15(#12と同じファイル領域、まとめて触ると手戻りが少ない)
5. #11(安全性設計、#14が使うagy分岐は現状allowlist系オプションを受け付けないため#14の直接の
   前提ではないが、ラッパーの契約一貫性のため#14より先に片付ける)
6. #14(最大の増分。#17/#11/#12/#15の後。design-change-gate+red-team必須)
7. #13(独立、他の全増分と並行/後回し可能。優先度最低)

## 戻し方

各増分は`embodied_ha/invoke-agent.sh`・`embodied_ha/mcp-config.py`・`embodied_ha/game-mcp.py`・
`embodied_ha/chat.py`・`embodied_ha/loop.py`への差分としてコミット単位を分ける。問題が出た場合は
該当コミットを`git revert`する。#14は`loop.sh`本体を削除していないため、最悪の場合
`daemon.py`を`loop.sh`へ戻すロールバック手順(`docs/loop-runtime-contracts.md`記載済み)が
引き続き有効。

## フェーズ2: 決定前self-check(#11・#14以外の項目向け)

①より安い代替はないか: #12/#15/#16/#13はいずれも設計メモで既に決着している内容の実装のみで、
安く済ませる余地は無い(スコープを削るとcaller側の要求を満たせない)。
②検証されていない隠れ前提は何か: #13はClaude Code/Codex/Antigravityそれぞれが「初回応答から
再開用IDを取得できる」ことが前提。Antigravityの`last_conversations.json`読み取りは調査済みだが、
Codexの`thread_id`取得は`--json`必須という制約がある(契約メモに記録済み、game-mcp.pyが現在
`--json`を使っていない場合は追加調査が要る)。
③戻し方は本当に機能するか: 上記「戻し方」節の通り、コミット単位のrevertで機能する
(loop.sh保持によりPhase2全体のロールバックパスも健在)。
