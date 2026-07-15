# loop.sh → loop.py cutover Phase1 仕様

conductorスキルのフェーズ0成果物。作成日2026-07-16。対象は `daemon.py` が起動するメインループの
実行系を `loop.sh` から `loop.py` へ切り替える作業のうち、**Claude-onlyのパリティ確立まで**
(Phase1)。invoke-agent.sh配線・Antigravity対応・queued listen音声マルチモーダル修正は
Phase2として明示的にスコープ外(下記「非目標」参照)。

## 目的

`docs/loop-runtime-contracts.md` に定義されたcutover blockerのうち、5モード
(observe/explore/reflect/web/social)のshadow parityを達成し、`daemon.py`が最終的に
`loop.sh`ではなく`loop.py`を呼べる状態を作る。ただし本フェーズの完了は
「切り替えてよい状態を作る」ところまでで、`daemon.py`の実際の切り替え(cutover本体)は
別途red-team(Task #4)を経てからユーザー確認の上で実施する。

## 背景調査で判明した事実(2026-07-16、Explore agent+本体コード読み込みで確認済み)

`tests/loop_shadow_harness.py`の`assert_same_side_effects`/`capture_runtime_side_effects`は
定義済みだが、`loop.sh`実行結果と`loop.py`実行結果を同一fixtureで突き合わせて比較する
「本物のshadow parityテスト」はリポジトリ全体で**0件**(既存テストはloop.pyの自己整合性検証、
またはloop.shの静的テキスト検査のみで、両者を実行し比較するテストは無い)。

さらに、loop.shとloop.pyを実際に読み比べた結果、**単なる未実装ではなく実際の挙動差分が
複数見つかった**。以下、当時のコードを含めて記録する。

### 差分1(バグ・要修正): モード抽選が本番経路で古い異常検知値を使う

`daemon.py`の`loop_scheduler()`(定期実行、`mode`引数を渡さない=自動抽選、本番で実際に踏む経路)
において、loop.shは実行内で新規計算した`ANOMALY_URGENCY`をモード抽選に使うが、loop.pyは
同じ実行内で再計算した新鮮な値が死コードになり、daemon側の古いスナップショットだけで
モードが確定してしまう。

**loop.shの構造**(`loop.sh:46-158`、要約): 異常検知(fresh) → モード抽選(一度だけ、fresh値を使用)
→ thinkingステータス投稿、という一直線の流れ。「先に仮決め→後で新鮮なデータで再決定」という
二段構えの構造はそもそも存在しない。

```bash
# loop.sh:78-79 のコメント
# 今回のループで新規検出した結果を優先する（daemon から渡された env は、
# 検出が走らなかった場合のフォールバック）。空でなければ上書き。
```

```bash
# loop.sh:119-154(要約): MODE未設定なら、直前で確定したfresh ANOMALY_URGENCYを使って抽選
if [ -z "${MODE:-}" ]; then
  MODE=$(ANOMALY_URGENCY="${ANOMALY_URGENCY:-0}" EHA_BODY_STATE="${EHA_BODY_STATE:-}" python3 -c '...')
fi
```

```bash
# loop.sh:156-158: モード確定後にthinking投稿
_mode_src="loop"; [ "$MODE" = "reflect" ] && _mode_src="private"
curl ... -d "{\"status\":\"thinking\",\"source\":\"${_mode_src}\"}" ...
```

**loop.pyの構造**(`loop.py:822-858`, `632-680`): `run()`が古いcfgのままモードを仮決めし、
`web_ui_status("thinking", ...)`を先に投稿してから`build_loop_prompt_context()`内で新鮮な
異常検知をやり直す設計に変わっている(おそらく「重い処理の前に早くthinkingを出したい」という
意図での再構成)。しかしこの再決定呼び出しは、先に決めた`mode`が常に非空文字列のため
`choose_mode()`内の早期returnに必ず引っかかり、新鮮な値が一切反映されない。

```python
# loop.py:822-836(抜粋)
def run(environ=None, *, run_subprocess=subprocess.run):
    ...
    mode = choose_mode(cfg)                      # ← 古いcfgで仮決め
    ...
    web_ui_status("thinking", source, ingress_port, run=run_subprocess)  # ← 仮決めモードで先に投稿
    try:
        context = build_loop_prompt_context(cfg, mode, paths, run=run_subprocess)
```

```python
# loop.py:632-640(抜粋)
def build_loop_prompt_context(cfg, mode, paths, *, run=subprocess.run):
    ...
    anomaly_context, anomaly_urgency = update_anomaly_context(cfg, paths, sensors, open_loops_json)  # fresh計算
    cfg = {**cfg, "ANOMALY_CONTEXT": anomaly_context, "ANOMALY_URGENCY": anomaly_urgency}
    selected_mode = choose_mode({**cfg, "MODE": mode} if mode else cfg)  # ← mode(仮決め値)は常にtruthyなので必ずMODEが強制され、fresh値は無視される
```

```python
# loop.py:117-121(抜粋): MODE が設定されていれば即return、weights計算(fresh値の出番)まで到達しない
def choose_mode(environ=None, *, choices=random.choices):
    env = dict(environ if environ is not None else os.environ)
    if env.get("MODE"):
        return str(env["MODE"])
```

**修正方針**: `run()`内で、①渡された`environ`に元々`MODE`が明示指定されていたかどうかだけを
判定材料にする(仮決め`mode`のtruthyさで判定しない)、②`web_ui_status("thinking", ...)`の投稿を
`build_loop_prompt_context()`実行後・`context["mode"]`確定後に移す(loop.shの投稿順と一致させる)。
これにより「重い処理の前に早くthinkingを出したい」という最適化自体を諦める形になるが、
loop.sh自体もその最適化をしていない(thinking投稿はモード確定後)ため、パリティ上は問題ない。

### 差分2(loop.shへ揃える・実害ゼロ確認済み、将来の再検討候補): observeモードの3点

`.tools/claude-home/projects/-config/memory/project_embodied_ha_todo.md`の
「要検討TODO(将来のredesign材料): loop.py cutover Phase1でloop.shに揃えた観測モード3差分」
に当時の実コード全文を記録済み。要約:

1. 見守り要約(haiku)呼び出しへの`--json-schema`/`--allowedTools`強制付与
   (loop.py:169-198, 713-722 / loop.sh:368-389, 480-485)——loop.py側がおそらく機能バグ。
   observeの完全なloop_schema(topic/speak/private/facts等必須)がハイクへの平文要約指示
   (`WATCH_REPORT_SYSTEM`)と衝突し、見守り報告が壊れる可能性がある。
2. observeのモデル固定(loop.sh:524は`sonnet`ハードコード) vs `EHA_SESSION_MODEL`追従
   (loop.py:217)——`EHA_SESSION_MODEL`は本番未設定(grep確認済み)につき実害ゼロ。
3. `EHA_ACTOR`環境変数がloop.shのobserve分岐のみ欠落(loop.sh:364-366に無し、loop.py:134-143は
   全モード共通で`"loop"`)——`ha-control-mcp.py`/`audio-mcp.py`双方で監査ログ記録用途のみ
   (分岐条件としての参照ゼロ件、grep確認済み)につき実害ゼロ。

**方針(ゆの承認済み、2026-07-16)**: 3点ともPhase1ではloop.shの挙動に完全に揃える。
「observeも他モードと一貫させるべきか」の再検討は将来のTODOとして
`project_embodied_ha_todo.md`側で保持し、本cutoverのスコープには含めない
(後回しにする技術的コストは無いと判断済み——理由は同TODO本文参照)。

### 差分3(loop.shへ揃える・実害軽微): chat_log.jsonl書き込み順序

loop.shは queued_file削除(`loop.sh:652-654`) → feature_presented追加(`loop.sh:656-667`)の順。
loop.pyの`postprocess_loop_response()`(`loop.py:785-793`)は逆順
(`record_presented_features()`→queued_file削除)。両操作は独立しており実害は無いと見られるが、
真のパリティのため順序もloop.shに揃える。

### 差分なしと確認済み(対応不要)

explore分岐の`boundary.py --preflight`呼び出し頻度——loop.shは全モードで無条件呼び出し、
loop.pyはexploreのみ。`boundary.py:main()`の`--preflight`時は`record_counterfactual`書き込み
分岐が完全にスキップされるため、永続化への副作用差は無い(効率差のみ、コード確認済み)。

### EHA_SESSION_BINの運用監査(cutover blocker解消の根拠)

`docs/loop-runtime-contracts.md`のcutover blocker「`EHA_SESSION_BIN`の運用監査 または
invoke-agent.sh配線」について、監査結果: `daemon.py:283-296`の`run_loop()`は
`env = {**os.environ, ...}`で自環境を`bash LOOP_SH`へ継承させて呼んでおり、`run.sh`・
`config.yaml`・本番`preferences.json`のいずれにも`EHA_SESSION_BIN`の設定は無い
(2026-07-16 grep確認、0件)。よってメインループ経路で`EHA_SESSION_BIN=agy`が実際に
セットされることは現状ない。この監査結果を`docs/loop-runtime-contracts.md`へ追記することで
このblockerを解消する(invoke-agent.sh配線はPhase2)。

## 受け入れ条件

1. `tests/loop_shadow_harness.py`のヘルパーを実際に使い、5モード全てについて
   `loop.sh`(実プロセス実行)と`loop.py`(実プロセス実行)を同一fixture入力
   (env・HA API相当のモック・sensors出力)で走らせ`assert_same_side_effects`で比較する
   テストを新設する。`python3 -m unittest tests.test_loop_shadow_harness -v`実行で
   5モード分のテストがgreenになること。
2. 差分1(モード抽選のstale化)の修正後、`MODE`環境変数未指定・新鮮な異常検知が高urgencyを
   返す状況で、`loop.py`の`run()`が実際にexplore/observe寄りのモード抽選へ反映することを
   確認する回帰テストが存在しgreenであること(モックで`update_anomaly_context`の戻り値を
   制御し、`choose_mode`が呼ばれた際の実引数を検証)。
3. 差分2の3点(見守り要約への`--json-schema`/`--allowedTools`不付与、observeモデル`sonnet`
   固定、observe時`EHA_ACTOR`未設定)それぞれについて、loop.pyの実際に構築されるargv/env
   がloop.shの挙動と一致することを検証する単体テストが存在しgreenであること。
4. 差分3(chat_log書き込み順序)がloop.shと一致するよう修正され、既存テストが
   green(または新規テストで順序を明示検証)であること。
5. `docs/loop-runtime-contracts.md`に`EHA_SESSION_BIN`運用監査結果が追記されていること
   (grep等で追記内容の存在を確認可能)。
6. 既存テストスイート全件(`python3 -m unittest discover -s tests -v`)がgreenであること。
   既存assertionは変更しない(新規テスト追加のみ)。
7. `daemon.py`の`LOOP_SH`定数・`run_loop()`の呼び出し先は本フェーズでは変更しない
   (`daemon.py`が今も`loop.sh`を呼んでいることをテストで確認・現状維持)。実際の切り替えは
   Task #4(red-team)を経てユーザー承認後に別途実施する。

## 非目標

- `invoke-agent.sh`配線(loop.pyがinvoke-agent.sh経由でClaude/Codex/Antigravityを呼ぶ変更)
- Antigravity(agy)対応の復活(`EHA_SESSION_BIN=agy`時のSystemExitガードは維持する)
- queued listen音声マルチモーダル修正(`76a384c`のregression、STTテキスト→WAVパス復元+
  harness切替配線)
- `daemon.py`の実際の切り替え(cutover本体の実行)——受け入れ条件7の通り、本フェーズでは行わない

## 制約

- 既存テストのassertionを変更しない(`feedback_test_discipline.md`)。
- git commitは実装完了後、ユーザー確認の上で行う。
- `daemon.py`の実際の切り替えは、5モード全パリティ達成後もこの仕様だけでは実行しない。
  Task #4(red-team)と、その後の明示的なユーザー承認を必須とする。
- 差分1の修正(`run()`内の処理順序変更)は`loop.py`側のみに適用し、`loop.sh`は一切変更しない
  (`loop.sh`は本番稼働中のため触らない)。

## 戻し方

本フェーズでは`daemon.py`は`loop.sh`を呼び続けるため、`loop.py`側の実装が全て失敗しても
本番影響はゼロ。ロールバックは対象コミットの`git revert`のみで完結する。

## フェーズ1: 増分計画

最もリスクが高い(実プロセスを2系統モックして比較する、という前例のない構造の)増分を最初に置く。

| # | 増分 | 検証方法 | 対応する受け入れ条件 |
|---|---|---|---|
| 1(スパイク) | 5モード分のshadow parityテストハーネスを実装(loop.sh/loop.pyを同一fixtureで実プロセス実行し`assert_same_side_effects`で比較)。**実装前にまず「何も直していない現状のloop.py」に対して実行し、差分1〜3を検出してREDになることを確認してから次に進む**(red-first)。 | `python3 -m unittest tests.test_loop_shadow_harness -v` | AC1(構築)、既知差分の実在をテストが自動検出できることの確認 |
| 2 | 差分1(モード抽選のstale化)を修正: `run()`の処理順序を、fresh異常検知→モード確定→thinking投稿の順に並べ替え | 新設の回帰テスト + 増分1のharnessを再実行(該当箇所のREDが消えること) | AC2 |
| 3 | 差分2(observe 3点: json-schema/allowedTools付与・モデル固定・EHA_ACTOR)をloop.shに揃える | 新設の単体テスト(argv/env構築を直接検証) | AC3 |
| 4 | 差分3(chat_log書き込み順序)をloop.shに揃える | 新設または既存テストで順序を検証 | AC4 |
| 5 | 増分1のharnessを5モード全てで再実行し、全green化を確認 | `python3 -m unittest tests.test_loop_shadow_harness -v` | AC1(完了確認) |
| 6 | `docs/loop-runtime-contracts.md`へEHA_SESSION_BIN監査結果を追記 | grep等で追記内容の存在確認 | AC5 |
| 7 | 全体テストスイート実行+`daemon.py`が変更されていないことの確認 | `python3 -m unittest discover -s tests -v` | AC6, AC7 |

## フェーズ2: red-team自己チェック(発動対象外)

`daemon.py`は本フェーズ中一切変更しない(実際の切り替えは別途Task #4で扱う不可逆な決定)ため、
本フェーズの実装作業自体はred-team skillの発動条件(不可逆・アーキテクチャ選択)に該当しないと
判断し、自己チェック3問のみ記録する。

- ①より安い代替はないか: shadow parityテストを作らずコードレビューのみで済ませる方が安いが、
  差分1〜3は既に机上調査で発見済みの実績があり、テスト無しでは今後の変更で同種の差分が
  再び紛れ込むのを防げない。
- ②検証されていない隠れ前提は何か: `loop.sh`をテストプロセスとして安定的に(Claude CLI/HA API
  呼び出し部分をモックして)実行できる、という前提。これは増分1(スパイク)で最初に検証する。
- ③戻し方は本当に機能するか: `daemon.py`が本フェーズ中変更されないため機能する
  (`git revert`のみで完全に戻せる、本番影響ゼロ)。

## 独立レビュー(gpt-5.6-sol、2026-07-16)による指摘とAC1の実装後調整

Codex実装完了後、別インスタンス(gpt-5.6-sol)へ独立レビューを委譲したところ、Codex自身の
red-team(`/config/.tools/claude-home/red-team/20260716-loop-py-phase1.md`)では捕捉されて
いなかった懸念が追加で4件見つかった。AC1(「本物のshadow parityハーネス」)は当初の実装では
△判定(完全達成ではない)。ユーザー確認の上、以下の通り扱いを確定する:

1. **High — `EHA_ANOMALY_STATE_FILE`を親環境から継承し上書きしていない**(このSCS環境では
   未設定のため今回の実行では実害なしと確認済みだが、設定された環境では本番
   `/config/embodied-ha/log/anomaly_state.json`を書き換えうる潜在バグ)→ **修正対象**。
2. **Medium — MCP設定生成が`loop.sh`/`loop.py`とも`EHA_TMP_DIR`を無視し`/tmp/embodied-ha`を
   固定で使うため、テスト前後のクリーンアップが他プロセスの一時データと衝突しうる**→
   **修正対象**。ただし`loop.sh`のこのハードコード自体は本フェーズの制約
   (`loop.sh`は変更しない)によりテスト側の工夫で緩和する。
3. **Medium — shadow比較が`docs/loop-runtime-contracts.md`の「Shadow Parity Scope」
   (Claude argv・MCP config generator inputs)より狭く、`--allowedTools`の実際の値や
   MCP設定の中身は比較していない**(model/schema有無/allowedTools有無/actor/modeの5項目のみ)
   → **今回のスコープでは対応せず、高優先TODOとして`project_embodied_ha_todo.md`へ記録し
   別スコープで対応する**(2026-07-16ユーザー判断)。AC1は「5ランタイムファイル+5フィールドの
   比較についてはgreen、Claude argv全体/MCP設定内容の比較は別スコープ」として扱う。
4. **Medium — `web_ui_status("thinking", ...)`投稿の位置がloop.shより後ろにずれる**
   (loop.shはモード確定直後に投稿、loop.pyは`build_loop_prompt_context()`完了後)→
   **対応不要、現状のloop.py挙動を受け入れる**(2026-07-16ユーザー判断)。理由:
   `/api/status`が駆動する「入力中…」表示はloopとchat双方が共有するグローバル状態
   (`web/server.py`の`set_agent_status()`)で、ユーザーがリアルタイム性を重視するのは
   `chat.py`側の投稿でありloop側の投稿タイミング精度は優先度が低い。既存テストの
   AC(受け入れ条件2、fresh anomaly urgencyの反映)は満たしたままで良い。
