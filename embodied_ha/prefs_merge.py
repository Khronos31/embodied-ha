"""`preferences.json` の保存を「全置換」から「言及されなかったキーは残す」へ変える。

**なぜ要るか**（findings F-21）: Web UI の保存経路は、フォームから作り直した JSON を
`POST /api/preferences` へ送り、サーバーが `atomic_write` で**全置換**していた。
UI がフォームに持っていないキーは送られないので、**保存のたびに黙って消えていた**——
実際に `cameras[].ptz` / `ha_entity` / `preset` / `direction`、`speakers[].media_player` が失われ、
本番の `ptz` キーは0個になっていた。JSONエディタで書き戻しても、次に設定タブから保存すると再び消える。

同じ失敗の型は [[feedback_never_clobber_addon_options]] でアドオンoptionsでも起きている。
**UI側だけ直しても、次にUIへ項目を足し忘れたときにまた起きる**ので、サーバー側で構造的に止める。

## 方針: UIが扱うキーを明示し、それ以外を残す

特に欠損が起きた `cameras` / `speakers` は、UIが編集する項目キーを明示する。
UI管理キーが送られなければ削除の意図として扱い、UI非管理キーは既存値を残す。
それ以外の構造では従来どおり、

- **incoming に**無い**キー → existing から引き継ぐ**
- incoming に**ある**キー → incoming が正（空文字も「クリアした」として尊重する）

とする。UI は値をクリアするとき空文字や null を**送る**ので、
「キーごと消える」と「値を空にした」を区別できる。

## リストの扱い

`cameras` / `mics` / `speakers` などは dict のリスト。**項目の削除は壊してはいけない**ので、
「existing にあって incoming に無い項目」は**引き継がない**（消えるのが正しい）。
引き継ぐのは**同じ項目の中のキーだけ**。同一性は `id` → `source` → `entity_id` → `entity` → `label`
の順で最初に見つかった非空文字列で判定する（本番データではこの優先順で全セクションが一意になる）。

同一性を決められない項目があるリストは、**取り違えて混ぜるくらいなら**マージせず incoming をそのまま使う。
"""
from typing import Any

# 同一性の判定に使うキー。前にあるものほど優先。
_IDENTITY_KEYS = ("id", "source", "entity_id", "entity", "label")

# The form reconstructs these two item types from the listed fields. Anything
# else is an extension field that the server must preserve.
_UI_ITEM_KEYS = {
    "cameras": frozenset({"source", "room", "entity", "label", "note"}),
    "speakers": frozenset({
        "room", "type", "label", "entity", "note", "host", "port", "sink",
    }),
}


def _identity(item: Any) -> tuple[str, str] | None:
    """リスト項目の同一性キーを返す。決められなければ None。"""
    if not isinstance(item, dict):
        return None
    for key in _IDENTITY_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return (key, value.strip())
    return None


def _merge_item(existing: dict, incoming: dict, section: str) -> dict:
    handled = _UI_ITEM_KEYS.get(section)
    if handled is None:
        return merge_preferences(existing, incoming)
    merged = dict(incoming)
    for key, old_value in existing.items():
        if key in handled:
            continue
        if key not in merged:
            merged[key] = old_value
        elif isinstance(old_value, dict) and isinstance(merged[key], dict):
            merged[key] = merge_preferences(old_value, merged[key])
    return merged


def _merge_list(existing: list, incoming: list, section: str) -> list:
    if not incoming or not all(isinstance(item, dict) for item in incoming):
        return incoming
    incoming_ids = [_identity(item) for item in incoming]
    if any(i is None for i in incoming_ids):
        return incoming
    existing_by_id: dict[tuple[str, str], dict] = {}
    for item in existing:
        ident = _identity(item)
        if ident is not None and ident not in existing_by_id:
            existing_by_id[ident] = item
    merged = []
    for item, ident in zip(incoming, incoming_ids):
        old = existing_by_id.get(ident)
        merged.append(_merge_item(old, item, section) if isinstance(old, dict) else item)
    return merged


def merge_preferences(existing: Any, incoming: Any) -> Any:
    """`incoming` を正としつつ、`incoming` が言及しなかったキーを `existing` から引き継ぐ。"""
    if not isinstance(existing, dict) or not isinstance(incoming, dict):
        return incoming
    merged = dict(incoming)
    for key, old_value in existing.items():
        if key not in merged:
            # UI が触れなかったキー。消さずに残す。
            merged[key] = old_value
            continue
        new_value = merged[key]
        if isinstance(old_value, dict) and isinstance(new_value, dict):
            merged[key] = merge_preferences(old_value, new_value)
        elif isinstance(old_value, list) and isinstance(new_value, list):
            merged[key] = _merge_list(old_value, new_value, key)
    return merged
