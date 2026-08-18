# データの置き場と保持

> 実装の確認日: **2026-08-16** ／ 外部規約の確認日: **2026-08-11**

データは、Embodied HAのローカル領域、Home Assistantが接続するサービス、選択したエージェントCLIの
提供元に分かれて保持されます。アドオンを停止または削除しても、別の層に保存済みのデータまで一括で
削除されるわけではありません。

## Embodied HAがローカルに置くもの

主な永続データは、通常 `/config/embodied-ha/` 以下にあります。Home Assistantのバックアップ対象に
含まれ得るため、バックアップの保存先と保持期間も確認してください。

| 種類 | 主な場所 | 現在の保持 |
|---|---|---|
| キャラクター、設定、家のルール | `character.md`、`preferences.json`、`home_policy.md`等 | 利用者が変更・削除するまで |
| 長期記憶、日誌、エピソード、検索索引 | `log/memory.md`、`log/memory/` | 自動要約や整理はあるが、全体を一律に期限削除する機能はない |
| 会話、観察、探索、操作、位置等のログ | `log/*.jsonl` | 種類ごとに異なる。多くは明示削除まで残る |
| 音声チャットの非公開内省 | `log/voice_introspection.jsonl` | 内容があるターンだけ追記。明示削除まで残る |
| 能動聴取の結果 | `log/active_listen_log.jsonl` | 既定24時間を目安に同じ音源の次回追記時に古い行を整理する。時刻だけで確実に消すタイマーではない |
| 歌声合成で作ったWAV | `wav/` | 一意な名前で保存。自動削除しない |
| ファイル指定で再生した音声 | `/media/embodied-ha/` | 内容ハッシュ名で保存。自動削除しない |
| 旧常時STT・背景音ログ | `log/auditory_events.jsonl`等 | EHA本体は新規生成しないが、移行前の既存ファイルは自動削除しない |
| 周辺の会話の文字起こし（有効にした場合のみ） | 追加アドオン側の非公開ディレクトリ。EHA個体のデータ内ではない | 既定24時間。あわせて2,048件・32 MiBの上限があり、超えた分は古いものから消える。音声そのものは保存しない |

歌声WAVと `/media/embodied-ha/` の再生用ファイルは、**意図的に永続保持する設計**です。
TTLでの削除、再生後の削除、自動整理は実装していません。作った音を勝手に消さないための選択なので、
不要になったものは利用者が消してください。`wav/` には、旧構成の非音声聴覚イベントの録音が
残っている場合もあります。

### 短期の一時データ

- カメラ履歴はオプトインです。有効時は `/tmp/embodied-ha-camera-history/` に静止画を保存し、設定した
  1〜60分の範囲で古い画像を整理します。無効化またはアドオン再起動で消える一時バッファで、アーカイブではありません。
- `listen`等の通常録音は一時WAVとして作られ、音量解析と任意のSTT処理が終わると削除されます。
- Antigravityの`concentrate_hearing`が作るWebMは `/tmp` に置かれ、既定では15分経過後に清掃されます。

一時ファイルの削除と、外部サービスへすでに送信されたコピーの削除は別です。

## 提供元側の学習利用と保持

次表は、2026-08-11に確認した代表的な条件の要約です。規約は変更され、地域、アカウント、契約、設定、
安全上または法令上の例外で条件が変わります。**表だけで判断せず、利用開始前と設定変更後に公式ページを
再確認してください。**

| 利用形態 | 学習利用と保持の要点 | 公式情報 |
|---|---|---|
| Claude Free / Pro / MaxでClaude Codeを使う | モデル改善を許可した新規・再開セッションは、匿名化された形で学習系統に最大5年保持される場合がある。会話を削除すると通常30日以内にバックエンドから削除される。安全・法令等の例外あり | [保持期間](https://privacy.claude.com/en/articles/10023548-how-long-do-you-store-my-data)、[モデル改善設定](https://privacy.claude.com/en/articles/12109829-how-do-i-change-my-model-improvement-privacy-settings)、[Consumer Terms](https://www.anthropic.com/legal/consumer-terms) |
| Anthropic API / 商用契約 | 入出力は既定で学習に使われない。APIの入出力は通常30日以内に削除されるが、別契約、機能、安全、法令等の例外がある | [商用データの保持](https://privacy.anthropic.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data)、[商用データと学習](https://privacy.anthropic.com/en/articles/7996868-is-my-data-used-for-model-training) |
| 個人向けChatGPTアカウントでCodexを使う | 設定により内容がモデル改善に使われる場合がある。保持したCodex会話は削除するまでアカウントに残り、削除後は通常30日以内に削除予定。安全・法令・すでに匿名化済み等の例外あり | [学習利用](https://openai.com/policies/how-your-data-is-used-to-improve-model-performance/)、[データ設定](https://help.openai.com/en/articles/7730893-data-controls-faq)、[Codex会話の削除](https://help.openai.com/en/articles/20001333-how-to-archive-and-delete-chats-in-codex)、[Terms of Use](https://openai.com/policies/terms-of-use/) |
| OpenAI API / Business | 入出力は既定で学習に使われない。APIはエンドポイントごとに保持が異なり、既定の不正利用監視ログは多くの経路で最大30日。対象顧客向けの追加制御もある | [OpenAI API data controls](https://developers.openai.com/api/docs/guides/your-data)、[Business data privacy](https://openai.com/business-data/) |
| Google Antigravity | Interactions（利用者データ、操作データ、メタデータ、フィードバック）を記録・保存し、設定を変更しない場合は製品・研究・機械学習技術の評価・開発・改善に使用すると規約にある。従業員・請負業者がアクセスする場合があり、削除はサポートへ依頼できる。追加規約には一律の保持日数が書かれていない | [Google Antigravity Additional Terms](https://antigravity.google/terms)、[Google Privacy Policy](https://policies.google.com/privacy) |

EHAのCodexセットアップは通常、OpenAIのデバイス認証でChatGPTアカウントへ接続します。Claude Codeは
Claude.aiの個人アカウントとAnthropic APIキーの両方を選べるため、同じCLI名でも適用条件が変わります。
認証したアカウントと契約を基準に確認してください。

Home AssistantのSTT・TTSは、選んだ統合ごとにローカル処理かクラウド処理かが変わります。EHAは
プロバイダー固有の保持期間を決めません。各統合とその提供元の説明を確認してください。

## 削除は層ごとに行う

1. **新しい処理を止める**: Embodied HAアドオンを停止する。外部のウェイクワード／音声ゲートウェイを
   使っている場合は、それも別に停止する。
2. **認証を止める**: EHAのセットアップから対象CLIをログアウトし、必要なら提供元アカウント側で
   セッションやトークンも失効させる。
3. **ローカルデータを確認する**: `/config/embodied-ha/`、`/media/embodied-ha/`、Home Assistantの
   バックアップを必要に応じて退避・削除する。削除前に対象アドオンを停止する。
   - **周辺の会話を有効にしている場合**は、追加アドオン側の
     `/config/embodied-ha-extensions/apps/ambient_speech_context/` にある
     `auditory_events.jsonl` と `recent_auditory_events.jsonl` を削除する。
     **必ず追加アドオンを停止してから消すこと。** 稼働中に消しても、次の発話が届いた時点で
     メモリ上の履歴からファイルが書き戻される。停止して削除し、起動し直すと件数はゼロに戻る。
     専用の削除ボタンはまだ無い。
4. **提供元側を削除する**: Claude、ChatGPT/Codex、Antigravity、STT・TTS提供元の各画面または
   サポート手順で、会話・Interactions等を削除する。

ローカルファイルを消しても提供元側のコピーは消えず、提供元側の会話を消してもHome Assistantの
バックアップやEHAのローカル記憶は消えません。
