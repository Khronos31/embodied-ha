"""個体の応答用 --json-schema 定義（フェーズ4での実導入に向けた設計）。

「共通コアスキーマ＋モード別追加プロパティ」の合成方式。additionalProperties は
常に false（agent-hub の知見: OpenAI/codex 側の strict structured output 要件と
両対応させるため常に付ける）。

対象は6箇所:
  - chat.sh: chat_schema(voice=False) / chat_schema(voice=True)
  - loop.sh: loop_schema(mode) for mode in observe/explore/reflect/web/social
  - daybook_rollup.py: daybook_schema()

proposal/action（家電操作提案）は observe・explore のみ許可する。reflect・web・
social は物理世界の観察を主目的としないモードのため許可しない
（reflect/webを明示的に除外というred-team方針を踏まえ、observe/exploreの
「物理世界を観察するモード」という共通性からexploreも同グループとした）。
scene_objects/scene_people/scene_changes はカメラ観察を伴う observe 専用。
"""

_NULLABLE_STRING = {"type": ["string", "null"]}

_TOPIC = {"type": ["string", "null"], "description": "今回何をしたか・何に注目したかの一言メモ"}
_SPEAK = {
    "type": ["string", "null"],
    "description": (
        "住人へのショートメッセージ。会話ルームにテキストとして残る（声には出ない）。"
        "特になければ null。"
    ),
}
_PRIVATE = {
    "type": ["string", "null"],
    "description": "今この瞬間に浮かんだこと。誰も見てない前提の独り言。20〜40文字程度。",
}
_EMOTION = {
    "type": ["string", "null"],
    "enum": [
        "curious", "calm", "happy", "concerned", "amused", "surprised", "nostalgic", None,
    ],
}
_FEATURE_PRESENTED = {
    "type": ["string", "null"],
    "description": "紹介した機能があればその機能id。なければ null。",
}
_PROPOSAL = {
    "type": ["string", "null"],
    "description": "操作で直せる家の問題を見つけたときの提案を一言。なければ null。",
}
_ACTION = {
    "type": ["object", "null"],
    "description": "proposal に対応する家電操作。",
    "properties": {
        "domain": {"type": "string"},
        "service": {"type": "string"},
        "entity_id": {"type": "string"},
        "data": {"type": "object"},
    },
    "required": ["domain", "service", "entity_id"],
    "additionalProperties": False,
}

# --- loop.sh: 5モード共通コア ---
_LOOP_CORE_PROPERTIES = {
    "topic": _TOPIC,
    "speak": _SPEAK,
    "private": _PRIVATE,
    "emotion": _EMOTION,
    "feature_presented": _FEATURE_PRESENTED,
}
_LOOP_CORE_REQUIRED = ["topic", "speak", "private", "emotion", "feature_presented"]

# 物理世界を観察し、家電操作の提案を出しうるモード
_PROPOSAL_CAPABLE_MODES = ("observe", "explore")
# カメラ観察に基づくシーン情報を出しうるモード
_SCENE_CAPABLE_MODES = ("observe",)

_SCENE_OBJECTS = {"type": "array", "items": {"type": "string"}}
_SCENE_PEOPLE = {"type": "array", "items": {"type": "string"}}
_SCENE_CHANGES = {"type": "array", "items": {"type": "string"}}


def loop_schema(mode):
    """loop.sh の5モード（observe/explore/reflect/web/social）用スキーマを返す。"""
    if mode not in ("observe", "explore", "reflect", "web", "social"):
        raise ValueError(f"unknown loop mode: {mode}")

    properties = dict(_LOOP_CORE_PROPERTIES)
    required = list(_LOOP_CORE_REQUIRED)

    if mode in _PROPOSAL_CAPABLE_MODES:
        properties["proposal"] = _PROPOSAL
        properties["action"] = _ACTION
        required += ["proposal", "action"]

    if mode in _SCENE_CAPABLE_MODES:
        properties["scene_objects"] = _SCENE_OBJECTS
        properties["scene_people"] = _SCENE_PEOPLE
        properties["scene_changes"] = _SCENE_CHANGES
        required += ["scene_objects", "scene_people", "scene_changes"]

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


# --- chat.sh: chat / voice 共通コア ---
# preferences_update の許容キーは chat.sh の後続処理（L737以降）が実際に
# 参照している9キーそのもの。ネストしたアイテム（camera設定・sensor設定等）は
# 意図的に緩い object 型のまま（意味のある変更が入りうる自由形式のため厳密な
# additionalProperties: false は課さない。トップレベルのキー幻覚だけ防げれば十分）。
_PREFERENCES_UPDATE = {
    "type": "object",
    "description": "設定変更があればその内容。9キーのいずれか。なければ空の辞書。",
    "properties": {
        "cameras_add": {"type": "array", "items": {"type": "object"}},
        "cameras_remove": {"type": "array", "items": {"type": "string"}},
        "speakers_set": {"type": "object"},
        "presence_set": {"type": "object"},
        "policies_add": {"type": "array", "items": {"type": "string"}},
        "sensors_add": {"type": "array", "items": {"type": "object"}},
        "sensors_remove": {"type": "array", "items": {"type": "string"}},
        "entities_add": {"type": "array", "items": {"type": "object"}},
        "entities_remove": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "cameras_add", "cameras_remove", "speakers_set", "presence_set",
        "policies_add", "sensors_add", "sensors_remove", "entities_add", "entities_remove",
    ],
    "additionalProperties": False,
}

_CHAT_CORE_PROPERTIES = {
    "private": _PRIVATE,
    "proposal_resolved": {
        "type": "boolean",
        "description": "保留中の提案が今回の会話で承認または却下されたら true。",
    },
    "preferences_update": _PREFERENCES_UPDATE,
    "feature_presented": _FEATURE_PRESENTED,
}
_CHAT_CORE_REQUIRED = ["private", "proposal_resolved", "preferences_update", "feature_presented"]


def chat_schema(voice=False):
    """chat.sh の chat/voice 用スキーマを返す。voice=True なら reply を含めない。"""
    properties = dict(_CHAT_CORE_PROPERTIES)
    required = list(_CHAT_CORE_REQUIRED)
    if not voice:
        properties["reply"] = _NULLABLE_STRING
        required = ["reply"] + required
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


# --- daybook_rollup.py ---
_EPISODE_ITEM = {
    "type": "object",
    "properties": {
        "timestamp": {"type": "string"},
        "kind": {"type": "string"},
        "source": {"type": "string"},
        "summary": {"type": "string"},
        "detail": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "entities": {"type": "array", "items": {"type": "string"}},
        "actors": {"type": "array", "items": {"type": "string"}},
        "importance": {"type": "number"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "timestamp": {"type": "string"},
                    "private": {"type": "string"},
                },
                "required": ["timestamp", "private"],
                "additionalProperties": False,
            },
        },
        "status": {"type": "string"},
        "links": {
            "type": "object",
            "properties": {
                "causes": {"type": "array", "items": {"type": "string"}},
                "effects": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["causes", "effects"],
            "additionalProperties": False,
        },
    },
    "required": [
        "timestamp", "kind", "source", "summary", "detail", "tags",
        "entities", "actors", "importance", "evidence", "status", "links",
    ],
    "additionalProperties": False,
}

_HIGHLIGHT_ITEM = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "detail": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "importance": {"type": "number"},
    },
    "required": ["summary", "detail", "tags", "importance"],
    "additionalProperties": False,
}


def daybook_schema():
    """daybook_rollup.py の _summarize_with_claude() 用スキーマを返す。"""
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "themes": {"type": "array", "items": {"type": "string"}},
            "highlights": {"type": "array", "items": _HIGHLIGHT_ITEM},
            "open_questions": {"type": "array", "items": {"type": "string"}},
            "episodes": {"type": "array", "items": _EPISODE_ITEM},
        },
        "required": ["summary", "themes", "highlights", "open_questions", "episodes"],
        "additionalProperties": False,
    }
