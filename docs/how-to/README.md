# Embodied HA エコシステム／How-To索引

| 項目 | 値 |
|---|---|
| 分類 | How-To / Recipe |
| 状態 | `reference`（索引。個別項目の状態は下表を参照） |
| 最終確認日 | 2026-08-11 |
| 検証したCore | Embodied HA 2.1.17 |
| 必須性 | 任意。Embodied HA本体のインストール手順ではない |
| 文書owner | [Khronos31/embodied-ha](https://github.com/Khronos31/embodied-ha) |

Embodied HAは、Home AssistantとLLMエージェントを連携させ、家庭内で身体性を持って活動できるようにするプロジェクトです。このリポジトリのHAOSアドオンを**Core**とし、別リポジトリの周辺実装と、Home Assistant／第三者部品を組み合わせる参考構成を区別します。

この索引に掲載されていることは、機能がCoreへ同梱されていること、全環境で動作すること、第三者部品をEmbodied HA側で保守することを意味しません。

## 区分と状態

### 区分

| 区分 | サポート境界 |
|---|---|
| **Core** | Embodied HA HAOSアドオンへ同梱。同じversion、テスト、release、不具合対応範囲で扱う |
| **First-party companion** | 同じプロジェクトが保守する別software。独立したversion、導入、release、issue境界を持つ |
| **How-To / Recipe** | Home Assistantまたは第三者部品を組み合わせる参考構成。検証範囲だけを記録し、第三者部品全体は保証しない |

### 状態

| 状態 | 意味 |
|---|---|
| `verified` | 表示したversionと条件で受入試験済み |
| `experimental` | 動作実績はあるが、仕様・導入手順・対応範囲が変わり得る |
| `reference` | 接続方針または調査入口。コピーしてそのまま動く手順としては未承認 |

## 現在のプロジェクトマップ

| 対象 | 区分 | 状態 | 最終確認 | 役割と正本 |
|---|---|---|---|---|
| [Embodied HA](https://github.com/Khronos31/embodied-ha) 2.1.17 | Core | `verified` | 2026-08-11 | センサー、カメラ、RTSPマイク、HA `media_player`、記憶、LLMエージェントを結ぶ基幹HAOSアドオン。本リポジトリが正本 |
| [RTSP Assist Gateway](https://github.com/Khronos31/rtsp-assist-gateway) 0.3.0 | First-party companion | `experimental` | 2026-08-11 | RTSP音声をHA STTへ送り、合格したprefixを汎用MQTT activation eventへ変換する。Wyoming wake-word経路は診断専用canary。導入・契約・issueはリンク先が正本 |
| [ESPHome Audio Node](https://github.com/Khronos31/esphome-audio-node) 0.1.0 | First-party companion | `experimental` | 2026-08-09 | Home Assistant `media_player`とLAN内PCMマイクを提供するsource-onlyのESPHome component。firmware binaryは配布しない |
| [Embodied HA MCP Lab](https://github.com/Khronos31/embodied-ha-mcp-lab) 0.1.1 | First-party companion | `experimental` | 2026-08-08 | LLMやresident daemonを起動せず、実MCP toolの返り値・異常系・状態変化を監査する開発者向けHA app |
| go2rtcによるRTSP音声集約 | How-To / Recipe | `reference` | 2026-08-11 | 物理マイクを一度だけ取得し、複数consumerへRTSPで配る構成。公開済みの汎用手順はまだない |
| Wyoming microWakeWord連携 | How-To / Recipe | `experimental` | 2026-08-11 | stock modelとprivate custom modelのprovider経路まで検証済み。日本語学習済みmodelと家庭内学習dataは公開しておらず、production向け汎用手順も未承認 |
| Home Assistant Automationによるactivation routing | How-To / Recipe | `experimental` | 2026-08-11 | Gatewayの固定MQTT activationを検証し、対象のEHA chatへ渡す構成。個人entity IDを除去した公開Recipeは未作成 |

`experimental`と`reference`は任意項目で、Coreには同梱されません。`reference`は検証済みの導入手順ではありません。

## 現在の安定した接続契約

Embodied HA Coreが直接扱う音声境界は次のとおりです。

- マイク入力: RTSPストリーム。CoreはALSA機器や独自TCPマイクへ直接接続しない。
- スピーカー出力: Home Assistantの`media_player`エンティティ。
- TTS: Home Assistantの`tts.*`エンティティが公開する言語・音声を使用する。
- 文字会話: MQTTの各個体用chat topic。Gateway由来のversion付きeventはCore側で形式、鮮度、room、重複を検証する。
- 自律観察: MQTTの各個体用loop trigger。音声activationとは別契約。

周辺実装はこの境界へ接続し、Coreへ物理device driver、wake-word engine、STT provider固有処理を戻さないことを基本方針とします。

## 音声経路の現在地

### 能動聴取 — Coreで利用可能

```text
physical microphone
  → optional transport/fan-out (for example go2rtc)
  → RTSP
  → Embodied HA active listen
  → selected Home Assistant STT provider
```

エージェントまたは住人が明示的に聴取を求めたときだけ使います。Coreは常時録音、常時STT、内蔵wake-word検出を行いません。

### 音声による呼び出し — 任意の外部構成

```text
RTSP microphone
  → RTSP Assist Gateway HA STT activation
  → fixed generic MQTT activation
  → Home Assistant Automation
  → selected Embodied HA chat topic
```

RTSP Assist Gateway 0.3.0のHA STT activation経路は実装済みですが、GatewayはEmbodied HAを直接呼びません。どのconsumerへ渡すかはHome Assistant Automationが所有します。

Wyoming microWakeWord経路は0.3.0時点ではdiagnostic-onlyのpassive canaryです。検出は固定の診断topicへ出るだけで、汎用activationやEHA chatへは接続されません。custom modelの品質、言語、ライセンスはmodelごとに独立して評価する必要があります。

## Security／privacy境界

- RTSP、PCM、カメラ、マイクをインターネットへ直接公開しない。LANを信頼境界にする場合も、同一LAN上のclientが家庭内音声へ到達できる可能性を明示する。
- HA STTの候補音声は選択したSTT providerへ送られる。remote／metered providerかどうかはHome Assistant側のpipeline設定に依存する。
- microWakeWordは音声をWyoming providerへstreamする。providerの配置場所と、model／学習dataの出所を別々に確認する。
- `esphome-audio-node`のPCM socketは無認証・暗号化なし。ESPHome API暗号化やOTA passwordはこのsocketを保護しない。
- MCP Labの実tool callは実HA device、カメラ、マイク、ネットワークへ作用し得る。Lab内のstate隔離は外界のsandboxではない。
- exampleへcredential、token、秘密鍵、個人名、個人entity ID、IP address、家庭内録音を含めない。

## 無効化とrollback

| 対象 | 最小rollback |
|---|---|
| RTSP Assist Gateway | 対応Automationを先に無効化し、Gatewayのactivation modeを無効化またはadd-onを停止する |
| microWakeWord | GatewayのmicroWakeWord modeを無効化してからcustom modelをproviderのdocumented pathから外し、providerの標準model一覧を確認する |
| ESPHome Audio Node | consumer側のRTSP／PCM参照を外す。deviceのAPI／OTA／network設定変更はESPHome側の手順で戻す |
| MCP Lab | add-onを停止する。過去のtool callによる外界の変更は、Lab stateのresetでは戻らない |

Coreをアンインストールしたり、Home Assistant Coreを再起動したりすることは、上記companionを止めるための通常手順ではありません。

## 不具合の報告先

1. Embodied HA Coreの再現可能な問題は[Embodied HA repository](https://github.com/Khronos31/embodied-ha)へ報告する。
2. First-party companion自身の起動、設定、契約、releaseの問題は各companion repositoryへ報告する。
3. Home Assistant、ESPHome、go2rtc、Wyoming providerの一般的な問題は、それぞれのupstreamへ報告する。
4. 複数componentをまたぐ場合は、version、データフロー、どの境界まで成功したかを添える。家庭内音声やcredentialは添付しない。

## 再検証が必要になる条件

- Embodied HAのRTSP入力、HA `media_player`、MQTT chat envelopeが変更された。
- RTSP Assist Gatewayのmajor version、MQTT payload version、VAD／STT境界が変更された。
- Home Assistant Assist pipeline、Wyoming protocol、microWakeWord model manifestの互換性が変更された。
- ESPHome Audio NodeのPCM format、同時接続数、認証境界が変更された。
- 記載した最終確認日から主要依存softwareが複数release進み、再現確認ができていない。

利用前に、各項目の状態と最終確認日を確認してください。
