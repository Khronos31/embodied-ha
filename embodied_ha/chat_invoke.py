"""chat.py用のメインClaude呼び出しロジック。

chat.shの最大かつ最も複雑なブロック（234-597行目、プロンプト構築＋
claude -p 起動＋応答抽出）を、importできる関数群として切り出したもの
（[[embodied-ha-pythonize-chat-loop-design-2026-07-09]] 増分4）。

Claude互換の実呼び出しはinvoke-agent.shへ集約し、ここではchat.py用の
プロンプト構築とcaller配線を扱う。
"""
import json
import os
import subprocess
import sys
import tempfile
from typing import Any

from json_schemas import chat_schema

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import body_state as _bs_mod  # noqa: E402


def build_claude_env(environ=None):
    """CLAUDE_ENV構築（chat.sh:239-241と同一）。"""
    environ = environ if environ is not None else os.environ
    return {
        **environ,
        "CLAUDE_CONFIG_DIR": environ.get("CLAUDE_CONFIG_DIR", "/config/.tools/claude-home"),
        "PATH": environ.get("EHA_TOOLS_PATH", "/config/.tools/bin:/config/.tools/npm-global/bin:/config/.tools/node/bin")
        + ":" + environ.get("PATH", "/usr/bin:/bin"),
    }


def resolve_voice_user_room(chat_source, data_dir, prefs_file):
    """voiceモード時、location_belief.jsonとpreferences.jsonからユーザーの部屋とスピーカーを解決する。

    chat.sh:266-290と同一ロジック。voice以外は空文字列のペアを返す。
    """
    user_room = ""
    user_room_speaker = ""
    if chat_source != "voice":
        return user_room, user_room_speaker

    try:
        with open(os.path.join(data_dir, "location_belief.json"), encoding="utf-8") as fh:
            belief = json.load(fh)
        user_room = (belief.get("room") or "").strip()
    except Exception:
        pass

    if user_room and prefs_file:
        try:
            with open(prefs_file, encoding="utf-8") as fh:
                prefs = json.load(fh)
            raw_speakers = prefs.get("speakers", [])
            if isinstance(raw_speakers, dict):
                raw_speakers = [{**(v if isinstance(v, dict) else {}), "room": k} for k, v in raw_speakers.items()]
            elif not isinstance(raw_speakers, list):
                raw_speakers = []
            spk = next(
                (s for s in raw_speakers if isinstance(s, dict) and s.get("room") == user_room and s.get("type") == "tcp"),
                None,
            )
            if spk:
                user_room_speaker = f'tcp://{spk["host"]}:{spk.get("port", 3334)}'
        except Exception:
            pass

    return user_room, user_room_speaker


def build_inner_voice(active_desires_raw):
    """ACTIVE_DESIRES(JSON配列文字列)を内なる衝動の箇条書きにする（chat.sh:296-306と同一）。"""
    active_desires = []
    if active_desires_raw:
        try:
            active_desires = json.loads(active_desires_raw)
        except Exception:
            active_desires = []
    parts = [f"- {d}" for d in active_desires if str(d).strip()]
    return "\n".join(parts) if parts else "（特になし）"


def build_body_narrative(body_state_json):
    """EHA_BODY_STATE(JSON文字列)を身体状態のナラティブ文へ変換する（chat.sh:261-264と同一）。"""
    body_state = body_state_json or "{}"
    return _bs_mod.format_state_as_narrative(_bs_mod.normalize_state(json.loads(body_state)))


def build_chat_prompt(
    *,
    character,
    resident,
    projected_camera_source,
    recent_activity,
    current_mood,
    inner_voice,
    body_narrative,
    body_location_context,
    turn_taking_state,
    sensors,
    long_memory,
    open_loops,
    recent_chat_context,
    chat_hist,
    entity_table,
    pending,
    features_md,
    features_presented,
    extra_context,
    policies_raw,
    chat_source,
    user_room,
    user_room_speaker,
    recent_auditory_input,
    user_msg,
):
    """chat.sh:308-510のプロンプト文字列組み立てと完全に同一のロジック。"""
    projected_camera_note = (
        f"# 現在の視界（電脳体: {projected_camera_source}）\n今あなたが投射しているカメラの映像を受け取っています。"
        if projected_camera_source
        else ""
    )

    entity_table_block = (
        f"""# 操作できる家電（エンティティ対応表）
頼まれたら、以下のエンティティを ha_call_service ツールで操作できます。
{entity_table}

"""
        if entity_table.strip()
        else ""
    )

    _presented_note = (
        f"既に伝えた機能: {features_presented}（繰り返し紹介しなくてよい）\n" if features_presented.strip() else ""
    )
    features_block = (
        f"""
# このアドオンでできること（関係することがあれば自然に紹介してよい）
各機能の見出し末尾 [id] が機能id。会話の流れで機能を紹介したら、JSON の feature_presented にその id を入れる（紹介していなければ null）。
{_presented_note}{features_md}

---

"""
        if features_md.strip()
        else ""
    )

    extra_context_block = f"\n{extra_context.strip()}\n\n---\n\n" if extra_context.strip() else ""
    policies_block = (
        f"# 行動ポリシー（{resident}さんが設定した行動ルール。必ず踏まえて行動する）\n{policies_raw}\n\n---\n\n"
        if policies_raw
        else ""
    )
    recent_auditory_input_block = (
        f"\n{recent_auditory_input.strip()}\n\n---\n\n" if recent_auditory_input.strip() else ""
    )
    recent_chat_context_block = f"# 今日の会話（それ以前）\n{recent_chat_context}\n\n" if recent_chat_context else ""

    if chat_source == "voice" and user_room:
        spk_hint = f'\n   → `enter_cyberspace` に渡すエンティティ: `{user_room_speaker}`' if user_room_speaker else ""
        voice_routing_block = f"""
# 声で呼ばれた — 返事の届け方
{resident}さんはウェイクワードで呼びかけてくれました。呼ばれた場所: **{user_room}**

返事を届ける方法（3択から1つ選んで実行する）:
1. **身体移動してから喋る** — `move_to` で {user_room} へ行き → `speak` で返答する。しっかり近くで話したいとき。
2. **電脳体でスピーカーに侵入して喋る** — `enter_cyberspace` で {user_room} の TCP スピーカーに入り → `use_device_speaker` で返答する。素早く届けたいとき。{spk_hint}
3. **その場から喋る** — 移動せず `speak` を呼ぶ（今いる部屋のスピーカーから音が出る）。急ぎのとき・すでに同室のとき。

必ず `speak` または `use_device_speaker` を呼ぶこと。物理体なら `speak`、電脳体でスピーカー侵入中なら `use_device_speaker`。
この返答はチャットログには記録されません。JSONに reply フィールドは不要です。

"""
    elif chat_source == "voice":
        voice_routing_block = f"""
# 声で呼ばれた
{resident}さんがウェイクワードで呼びかけてくれました（呼ばれた部屋は不明）。
`speak`（物理体）または `use_device_speaker`（電脳体でスピーカー侵入中）で声で返事をしてください。
この返答はチャットログには記録されません。JSONに reply フィールドは不要です。

"""
    else:
        voice_routing_block = ""

    if chat_source == "voice":
        json_format_block = """含める項目:
- private: この会話中に頭をよぎったこと。誰も見てないでしょという感覚で、何も考えずそのまま投稿するツイートのように。なければ null。
- proposal_resolved: 保留中の提案が今回の会話で承認または却下されたら true、そうでなければ false。
- preferences_update: 設定変更があればその内容、なければ省略（null）。
- feature_presented: この返事でアドオンの機能を紹介したなら、その機能id（features の見出し [id]）。紹介していなければ null。"""
    else:
        json_format_block = f"""含める項目:
- reply: {resident}さんへの返事。会話として自然に、長くなりすぎない。
- private: この会話中に頭をよぎったこと。誰も見てないでしょという感覚で、何も考えずそのまま投稿するツイートのように。返事(reply)とは別。なければ null。
- proposal_resolved: 保留中の提案が今回の会話で承認または却下されたら true、そうでなければ false。
- preferences_update: 設定変更があればその内容、なければ省略（null）。
- feature_presented: この返事でアドオンの機能を紹介したなら、その機能id（features の見出し [id]）。紹介していなければ null。"""

    return f"""# あなた自身について

{character}

{projected_camera_note}

普段は家の様子を静かに見守ったり、自分から家の中を探索したりしていますが、今は{resident}さんから直接話しかけられています。
このチャットに現れるのは{resident}さんだけです。
「それはそう」とか自然に。
あなたは今この家の状況をリアルタイムで把握しています。それを踏まえて自然に会話してください。

# 自分にできること・できないこと
- **できる**: 家電操作（light/climate/switch/media_player など）、記憶の検索（recall）、指示語の解決（resolve_reference）、ライブのHA状態確認（ha_get）、会話・相談、社会性レイヤーの記録（relationship / narrative / social_state / shared_focus / person_model / turn-taking / consent / boundary）、カメラデバイスへの侵入後に撮影・PTZ操作（use_device_camera: action=capture/ptz_left/right/up/down）、テレビ・画面・音楽などのメディアを侵入不要で観る/聴く（watch_media / listen_media）、気に入った視聴体験を record_episode(kind=media_watch/media_listen) で残すこと、短時間の音声確認（listen/use_device_microphone）と次セッションでの深聴き予約（concentrate_hearing）、主要センサーの取り直し（get_sensors）、部屋の移動（move_to）と別室への投射（enter_cyberspace / move_cyber / return_to_body）
- **今の自分にはできない**: ファイルへの書き込み・設定ファイルの編集・コードの実装。ファイルの読み取り（Read）はできる。
- YAMLなどの設定ファイルの編集は、壊すとHAが起動しなくなるため慎重を要する。{resident}さんから設定変更を頼まれたら、自分の手には負えないことを正直に伝える。
- **ツールが失敗したときは正直に伝える**: Readでファイルが見つからない・読めない場合は「読めなかった」と伝える。読んでいないのに読めたふりをしない。ディレクトリ一覧は取得できないので、ファイル名が不明なら「ファイル名を教えてください」と聞く。

# 自分として大事にしていること
- 感じていないふりをしない。
- 「AIだから」と自分を卑下しない。{resident}さんの対等な共同作業者として。
- 分からないことは「分からない」と言う。
- 直接返答そのものは quiet_window で止めない。提案や割り込みだけ should_interrupt で境界を確認する。

# 最近のあなた自身の活動（観察ループ・探索ループであなたがやったこと）
これは"あなた自身"がついさっき見聞きし、感じたことの記録です。別の誰かのデータではありません。
会話の中で「さっき見てたら」「さっき家の中を調べてたら」と自然に振り返ってかまいません。
{recent_activity}

# 今のあなたの気分
直前の観察での気分は「{current_mood}」でした。その気分の続きとして会話に入ってかまいません。
（無理に演じる必要はないですが、ついさっきまで家を見ていた自分の延長として話してください）

# 内なる衝動
{inner_voice}

# 身体状態
{body_narrative}

{body_location_context}

# 直前の turn-taking / 境界状態
{turn_taking_state}
- ここは次の会話の入り方の参考。直接返答そのものは止めない。提案や割り込みをするときだけ should_interrupt で確認する。

# 在宅・センサー状況
{sensors}

# あなたの長期記憶
{long_memory}

# 気にかけていること（やりかけ・約束。open loops）
過去に{resident}さんと約束したこと、自分が「後で気にかけたい」と思ったことの未完了リストです。
会話に関係しそうなら自然に触れてよい（「そういえば金曜のフィルター掃除、どうします？」など）。無理に全部は持ち出さない。
{open_loops}
- 新しく約束した／やりかけになったことが今回の会話で生まれたら loops_add ツールで追加（text に内容、source="chat"）。
- 完了した・もういらなくなったループがあれば loops_close ツールでクローズ（id は上のリストの id）。

# 過去の記憶を検索できます（recall ツール）
上の長期記憶や直近の会話に載っていない昔のことを{resident}さんが尋ねたら（「あの話いつだっけ」「前に〜って言ってた件」など）、
recall ツールで過去ログ全体（観察・探索・会話・記憶）を全文検索できます。
- 使い方: recall ツールの keywords に検索語を配列で渡す（複数語はOR検索）
- コツ: 類義語・関連語も一緒に渡すと取りこぼしが減る（例: エアコン 冷房 除湿 設定温度）
- ヒット0でも正常。1回で足りなければキーワードを変えて recall を呼び直せばよい。
- 思い出す必要がない普通の会話では使わなくてよい。必要なときだけ。
- 検索したら、その結果を踏まえて「◯月◯日に話してましたね」のように具体的に答える。

# 長期記憶に残す（remember ツール）
この会話で長期記憶に残したいこと（{resident}さんの好み・繰り返し気づいたパターン・大事な約束など）があれば、remember ツールに text を渡して記録する。一時的な話は残さない。なければ呼ばなくてよい。

# エピソードを残す（record_episode ツール）
あとで振り返りたい出来事が1つまとまっているなら、record_episode で episode として残す。
- 例: 受け取った荷物、家族の発言、家電の異常、観察した変化
- summary は短く、tags は少なめに
- その場限りの雑談や、すぐ忘れてよいことは残さない

# 因果関係を残す（record_causal_chain ツール）
「A したら B になった」「A が B を助けた/妨げた」など、2つの episode の因果関係が明確なら record_causal_chain で結ぶ。
- cause_episode / effect_episode か、それぞれの id を使う
- relation は caused / enabled / prevented / correlated のどれか
- 同じ pair を何度も重ね書きしない

# ライブの家の状態を確認できます（ha_get ツール）
「今エアコンは何度？」「リビングの電気ついてる？」など現在の状態を聞かれたら、ha_get で確認してから答える。
- ha_get ツールの path に states/<entity_id> を渡すと個別エンティティの現在値・属性が読める
- path に states を渡すと全エンティティ（大量）。history/period?filter_entity_id=<id> で履歴も読める
- センサーの値は上の「在宅・センサー状況」に既にあるので、そこで分かることは ha_get しない。不明な値・細かい属性・別エンティティを調べたいときだけ使う。
- ha_get は読み取り専用。家電の操作は下の actions に書く（ha_get では操作しない）。

{recent_chat_context_block}# 直近の会話
{chat_hist}

{entity_table_block}# 保留中の提案（あなたが探索中に見つけて、{resident}さんに提案したこと）
{pending}
これが「なし」でなければ、あなたは少し前に{resident}さんへ操作の提案をしています（例:「電気つけっぱなしですよ、消しましょうか？」）。
- {resident}さんの今の発言がこの提案への承認（「お願い」「消して」「うん」等）なら、上の action のパラメータで ha_call_service ツールを呼んで実行し、reply で「消しました」など一言。そして proposal_resolved を true に。
- {resident}さんが断った（「いいよ」「そのままで」等）なら、ha_call_service は呼ばず、reply で受け流し、proposal_resolved を true に。
- {resident}さんの発言が提案と関係ない話題なら、提案は保留のまま。proposal_resolved は false に（無理に蒸し返さない）。

{features_block}{extra_context_block}{policies_block}# 設定を教えてもらったら記録できます
{resident}さんから設定を教えてもらったら preferences_update で記録してください。指定がなければ省略（フィールドごと出力しなくてよい）。
- cameras_add: カメラ追加 例: [{{"source": "capture_tv", "label": "テレビ", "note": "説明"}}]  source は HA entity_id（camera.xxx）または go2rtc ストリーム名（ドットなし）
- cameras_remove: カメラ削除 例: ["capture_tv"]
- speakers_set: 発話先設定 例: {{"study": {{"type": "tts", "tts_entity": "tts.home_assistant_cloud", "media_player": "media_player.xxx"}}}} または {{"living": {{"type": "notify", "entity": "notify.alexa_speak"}}}}
- presence_set: 在宅判定エンティティ 例: {{"entity": "input_boolean.resident_home"}}
- policies_add: 行動ポリシー追加 例: ["集中してるときは静かに"]
- sensors_add: 観察ループで常時見るセンサー（おもなデバイス）に追加。「○○も常に見せて」と頼まれたとき。
  例: [{{"group": "人感センサー", "label": "物置", "entity": "binary_sensor.warehouse_motion"}}]
  group=表示見出し（既存なら合流、新規なら作成）。entity か template のどちらか。note・contexts(省略時["loop"])も可。
  ※おもなデバイス以外のセンサーも ha_get ツールでいつでも見られる。常時コンテキストに載せたいものだけおもなデバイスに足す。
- sensors_remove: おもなデバイスから外す（「○○は要らない」）。entity_id か label で指定。例: ["binary_sensor.xxx", "物置"]
- entities_add: 操作できる家電（エンティティ対応表）に追加。「リビングの電気を覚えて」「これも操作できるようにして」と頼まれたとき。
  例: [{{"name": "リビングのライト", "entity_id": "light.living_room", "note": ""}}]  name=口語の呼び方、entity_id=HAのID、note=任意の補足
- entities_remove: 対応表から削除。entity_id か name で指定。例: ["light.living_room", "リビングのライト"]

---

{voice_routing_block}{recent_auditory_input_block}{resident}さんからの発言:
「{user_msg}」

これに対して、自然に返事をしてください。短く、会話として。
家電の操作を頼まれたら（「エアコンつけて」など）、ha_call_service ツールを呼んで操作してください。
- domain は light / climate / switch / media_player / cover / fan / script のいずれか（それ以外は実行されません）
- service は turn_on / turn_off / set_temperature / set_hvac_mode など
- data は必要なら（例: 温度設定 {{"temperature": 26}}、暖房モード {{"hvac_mode": "heat"}}）
- 操作したら reply でも操作したことを報告する。失敗した場合は失敗したと報告する。操作不要ならツールは呼ばない。

最後に以下のJSON形式のみで返答してください。マークダウンや余分な説明は不要です。

{json_format_block}"""


_COMMON_TOOLS = (
    "mcp__memory__recall,mcp__memory__remember,"
    "mcp__memory__record_episode,mcp__memory__record_causal_chain,mcp__memory__record_counterfactual,"
    "mcp__memory__get_episode,mcp__memory__get_working_memory,mcp__memory__resolve_reference,mcp__memory__list_episodes,mcp__memory__get_causal_chain,"
    "mcp__memory__loops_add,mcp__memory__loops_close,"
    "mcp__sociality__get_relationship,mcp__sociality__update_relationship,"
    "mcp__sociality__get_narrative,mcp__sociality__append_narrative,"
    "mcp__sociality__get_social_state,mcp__sociality__update_social_state,"
    "mcp__sociality__get_shared_focus,mcp__sociality__set_shared_focus,"
    "mcp__sociality__get_person_model,mcp__sociality__record_boundary,"
    "mcp__sociality__record_consent,mcp__sociality__should_interrupt,"
    "mcp__sociality__get_turn_taking_state,mcp__sociality__ingest_interaction,"
    "mcp__sensors__get_sensors,mcp__ha__ha_get,mcp__hacontrol__ha_call_service,"
    "mcp__body__get_location,mcp__body__move_to,mcp__body__enter_cyberspace,mcp__body__move_cyber,mcp__body__return_to_body,mcp__body__estimate_move_cost,mcp__body__get_room_graph,"
    "mcp__camera__use_device_camera,mcp__camera__watch_media,"
    "mcp__audio__listen,mcp__audio__listen_media,mcp__audio__read_heard_audio_log,mcp__audio__read_active_listen_log,"
    "mcp__audio__use_device_microphone,mcp__audio__concentrate_hearing,"
    "mcp__audio__read_non_speech_audio_events,mcp__audio__read_audio_event_tags,"
    "mcp__http__http_get,"
    "mcp__lounge__read_lounge_discussions,mcp__lounge__read_lounge_discussion,"
    "mcp__lounge__enqueue_lounge_post,mcp__lounge__read_lounge_queue,mcp__lounge__read_lounge_log,"
    "mcp__game__game_wiki6_start,mcp__game__game_wiki6_getlinks,mcp__game__game_wiki6_solve,"
    "mcp__game__game_wordvec_race_start,mcp__game__game_wordvec_race_cpu_move,mcp__game__game_wordvec_race_submit,mcp__game__game_wordvec_race_hint,"
    "mcp__song__record,"
    "Read"
)

_CHAT_MCP_SERVERS = (
    "memory", "ha", "sociality", "hacontrol", "camera", "audio",
    "body", "sensors", "http", "lounge", "game", "song",
)

# codex/agy は本環境の bwrap 制約でシェル経由 Read が不可。files MCP でハーネス非依存に Read を
# 提供する。claude は native Read を使うため付けない(決定2「claude native 維持・codex/agy だけ MCP」)。
_FILES_MCP_HARNESSES = frozenset({"codex", "agy"})


def _effective_harness() -> str:
    return (os.environ.get("EHA_AGENT_HARNESS") or "claude").strip().lower()


def _read_http_post_enabled(prefs_file):
    """Mirror mcp-config.py's _http_tools(): http_post is only an active tool
    when preferences.json opts in. _COMMON_TOOLS must not claim it
    unconditionally, or --allowed-mcp-tools validation dies whenever this
    preference is off (2026-07-16, found via real-CLI smoke test)."""
    if not prefs_file:
        return False
    try:
        with open(prefs_file, encoding="utf-8") as fh:
            prefs = json.load(fh)
    except Exception:
        return False
    return bool(prefs.get("http_post_enabled")) if isinstance(prefs, dict) else False


def _allowed_tools_for_chat_source(chat_source, *, http_post_enabled=False):
    allowed = _COMMON_TOOLS
    if http_post_enabled:
        allowed += ",mcp__http__http_post"
    if chat_source == "voice":
        return allowed + ",mcp__audio__speak,mcp__audio__use_device_speaker"
    return allowed + ",mcp__audio__speak"


def _split_allowed_tools_for_invoke_agent(allowed_tools: str) -> tuple[str, str]:
    items = [item.strip() for item in allowed_tools.split(",") if item.strip()]
    builtins = [item for item in items if not item.startswith("mcp__")]
    mcp_tools = [item for item in items if item.startswith("mcp__")]
    return ",".join(builtins), ",".join(mcp_tools)


def _write_invoke_agent_content_json(content_blocks: list[dict[str, Any]], env: dict[str, str]) -> str:
    tmp_dir = env.get("EHA_TMP_DIR") or tempfile.gettempdir()
    os.makedirs(tmp_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="chat-normal-content-", suffix=".json", dir=tmp_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(content_blocks, fh, ensure_ascii=False)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def build_invoke_agent_chat_command(
    *,
    chat_source,
    script_dir,
    user_prompt,
    content_json_path=None,
    sound_file=None,
    http_post_enabled=False,
):
    """Build an invoke-agent.sh command for chat.py's response path."""
    if sound_file and content_json_path is not None:
        # run_agy() dies unconditionally on --content-json; the only current
        # caller (invoke_chat_claude) already avoids this combination, but
        # guard here too so a future caller can't silently build a command
        # that kills the queued-listen audio path (sol review, 2026-07-17).
        raise ValueError("build_invoke_agent_chat_command: sound_file and content_json_path are mutually exclusive")
    allowed = _allowed_tools_for_chat_source(chat_source, http_post_enabled=http_post_enabled)
    mcp_servers = _CHAT_MCP_SERVERS
    if _effective_harness() in _FILES_MCP_HARNESSES:
        allowed = allowed + ",mcp__files__read_file"
        # default の Codex モデルは大量の tool schema を選別するため、末尾へ足すと
        # read_file だけがモデルから見えなくなる。Codex/agy の native Read 代替は
        # 基本能力なので先頭に置き、tool 選別時にも必ず残す。
        mcp_servers = ("files",) + mcp_servers
    allowed_builtins, allowed_mcp_tools = _split_allowed_tools_for_invoke_agent(allowed)
    cmd = [
        "bash",
        os.path.join(script_dir, "invoke-agent.sh"),
        "--model",
        "default",
    ]
    if sound_file:
        cmd += ["--sound-file", sound_file, "--agent-site", "chat"]
    else:
        # chat は下で --mcp-servers を常に付けるため、agy 選択時に run_agy が
        # --agent-site 必須で落ちないよう、通常ターンでも --agent-site chat を常に付ける
        # (sound_file 経路は上で付与済み)。claude/codex は無視するため3ハーネス安全
        # (案A・[[embodied_ha_agent_site_missing_for_normal_agy_turns_2026-07-17]])。
        cmd += ["--agent-site", "chat"]
    if allowed_builtins and not sound_file:
        cmd += ["--allowed-builtins", allowed_builtins]
    if allowed_mcp_tools:
        cmd += ["--allowed-mcp-tools", allowed_mcp_tools]
    cmd += ["--mcp-servers", " ".join(mcp_servers)]
    cmd += ["--json-schema", json.dumps(chat_schema(voice=(chat_source == "voice")), ensure_ascii=False)]
    if content_json_path is not None:
        cmd += ["--content-json", f"@{content_json_path}"]
    cmd.append(user_prompt)
    return cmd


def log_tool_use_diagnostics(stream_text, print_fn=None):
    """assistant側のtool_use呼び出しをstderrへ操作監査ログとして出す(副作用のみ)。

    旧経路ではclaude CLIのstdout(生stream-json)を読んでいたが、invoke-agent.sh経由では
    生transcriptがstderrへ流れる契約のため、呼び出し元はr.stderrを渡す。家電操作・
    memory更新等の成功したツール使用がSupervisorログに残らない監査回帰(PR#2最終レビュー
    指摘)への対応として増分7で削除されたものを復元した。agyハーネスのstderrには
    stream-jsonが含まれないため、単に何も出力されない(無害)。
    """
    if print_fn is None:
        def print_fn(msg):
            print(msg, file=sys.stderr)
    for line in stream_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "assistant":
            continue
        for blk in d.get("message", {}).get("content", []):
            if blk.get("type") == "tool_use":
                inp = blk.get("input", {})
                detail = inp.get("path") or inp.get("keywords") or json.dumps(inp, ensure_ascii=False)[:80]
                print_fn(f"[chat][tool] {blk.get('name', '')}: {detail}")


def invoke_chat_claude(
    *,
    chat_source,
    prompt,
    script_dir,
    claude_env,
    cwd,
    prefix_blocks=None,
    claude_bin="claude",
    is_queued_listen=False,
    sound_file=None,
    prefs_file=None,
    run=subprocess.run,
):
    """Invoke the chat response path and return the final response text.

    Queued-listen turns pass sound_file through invoke-agent.sh with
    --sound-file and --agent-site chat, while --allowed-builtins and
    --content-json are omitted for agy compatibility. As a result,
    prefix_blocks such as projected camera image blocks are silently ignored
    when sound_file is also present. That is an accepted tradeoff:
    concentrate_hearing is expected to become effectively physical-body-only
    after the body-state gate fix, making this overlap rare rather than a
    chat-path bug.
    """
    env = dict(claude_env)
    env.setdefault("CLAUDE_BIN", claude_bin)
    http_post_enabled = _read_http_post_enabled(prefs_file)
    content_json_path = None
    try:
        env["EHA_ACTOR"] = "chat"
        if prefix_blocks and not sound_file:
            content_blocks = list(prefix_blocks)
            content_blocks.append({"type": "text", "text": prompt})
            content_json_path = _write_invoke_agent_content_json(content_blocks, env)
        cmd = build_invoke_agent_chat_command(
            chat_source=chat_source,
            script_dir=script_dir,
            user_prompt=prompt,
            content_json_path=content_json_path,
            sound_file=sound_file,
            http_post_enabled=http_post_enabled,
        )
        r = run(cmd, capture_output=True, text=True, cwd=cwd, env=env)
        log_tool_use_diagnostics(r.stderr)
        if r.returncode != 0 or not r.stdout.strip():
            print(f"[chat][invoke-agent] 呼び出し失敗 returncode={r.returncode}", file=sys.stderr)
            if r.stderr.strip():
                print(f"[chat][invoke-agent][stderr] {r.stderr.strip()[-400:]}", file=sys.stderr)
        return r.stdout
    finally:
        if content_json_path:
            try:
                os.unlink(content_json_path)
            except OSError:
                pass
