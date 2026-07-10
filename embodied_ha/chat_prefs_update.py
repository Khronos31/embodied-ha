"""chat.py用のpreferences.json更新ロジック。

chat.shのpreferences.json更新ブロック（694-855行目、160行、6種類の
サブケース: cameras/speakers/presence/policies/sensors/entitiesの
追加・削除）を、importできる関数として切り出したもの
（[[embodied-ha-pythonize-chat-loop-design-2026-07-09]] 増分6）。

chat.sh側はこのブロック全体を外側のbash `2>/dev/null || true`
(694行目)で包んでおり、内部にpython try/exceptがほとんど無くても
観測可能な挙動としては「絶対にクラッシュしない」。update_preferences
はその契約を再現するため、増分5のpublish_private_to_mqttと同様に
関数全体を囲むtry/exceptを持たせている（唯一の意図的な追加）。
"""
import json
import os


def _normalize_speakers(value):
    """speakersがlist/dictいずれの形でも統一されたlist[dict]へ揃える（chat.sh:719-733と同一）。"""
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, dict):
                out.append(dict(item))
        return out
    if isinstance(value, dict):
        out = []
        for room, cfg in value.items():
            entry = dict(cfg) if isinstance(cfg, dict) else {}
            entry.setdefault("room", room)
            out.append(entry)
        return out
    return []


def _speaker_key(item):
    """スピーカーの同一性判定キー（room優先、無ければentity/media_player）（chat.sh:736-739と同一）。"""
    if not isinstance(item, dict):
        return ""
    return str(item.get("room") or item.get("entity") or item.get("media_player") or "").strip()


def _item_key(it):
    """センサー項目の同一性判定キー（chat.sh:791-792と同一）。"""
    return it.get("entity") or it.get("label") or ""


def apply_cameras_add(prefs, cameras_add):
    """cameras_add: sourceで重複排除して追加（chat.sh:744-751と同一）。"""
    changed = []
    for cam in cameras_add or []:
        src = (cam.get("source") or "").strip()
        if not src:
            continue
        prefs.setdefault("cameras", [])
        prefs["cameras"] = [c for c in prefs["cameras"] if c.get("source") != src]
        prefs["cameras"].append(cam)
        changed.append(f"cameras_add:{src}")
    return changed


def apply_cameras_remove(prefs, cameras_remove):
    """cameras_remove: sourceで削除（chat.sh:753-757と同一）。"""
    changed = []
    for src in cameras_remove or []:
        before = len(prefs.get("cameras", []))
        prefs["cameras"] = [c for c in prefs.get("cameras", []) if c.get("source") != str(src)]
        if len(prefs["cameras"]) < before:
            changed.append(f"cameras_remove:{src}")
    return changed


def apply_speakers_set(prefs, speakers_set):
    """speakers_set: room/entityキーでマージ、無ければ追加（chat.sh:742,759-778と同一）。

    prefs["speakers"]を正規化した上で書き戻す（元コードと同じ結合）。
    """
    changed = []
    speakers = _normalize_speakers(prefs.get("speakers"))
    for area, cfg in (speakers_set or {}).items():
        entry = dict(cfg) if isinstance(cfg, dict) else {}
        entry.setdefault("room", area)
        entry_room = _speaker_key(entry)
        entry_entity = str(entry.get("entity") or entry.get("media_player") or "").strip()
        for i, item in enumerate(speakers):
            if not isinstance(item, dict):
                continue
            item_room = _speaker_key(item)
            item_entity = str(item.get("entity") or item.get("media_player") or "").strip()
            if (entry_room and item_room == entry_room) or (not entry_room and entry_entity and item_entity == entry_entity):
                merged = {**item, **entry}
                merged.setdefault("room", area)
                speakers[i] = merged
                break
        else:
            speakers.append(entry)
        changed.append(f"speakers_set:{area}")
    prefs["speakers"] = speakers
    return changed


def apply_presence_set(prefs, presence_set):
    """presence_set: そのまま上書き（chat.sh:780-782と同一）。"""
    if presence_set:
        prefs["presence"] = presence_set
        return ["presence_set"]
    return []


def apply_policies_add(prefs, policies_add):
    """policies_add: 重複しない文字列のみ追加（chat.sh:784-788と同一）。"""
    changed = []
    for policy in policies_add or []:
        prefs.setdefault("policies", [])
        if policy not in prefs["policies"]:
            prefs["policies"].append(policy)
            changed.append("policies_add")
    return changed


def apply_sensors_add(prefs, sensors_add):
    """sensors_add: グループ単位でentity/label重複を置き換えつつ追加（chat.sh:794-813と同一）。"""
    changed = []
    for add in sensors_add or []:
        if not (add.get("entity") or add.get("template")):
            continue
        group_title = add.get("group", "その他")
        item = {k: v for k, v in {
            "label": add.get("label"),
            "entity": add.get("entity"),
            "template": add.get("template"),
            "note": add.get("note"),
        }.items() if v}
        contexts = add.get("contexts") or ["loop"]
        sensors = prefs.setdefault("sensors", {}).setdefault("groups", [])
        grp = next((g for g in sensors if g.get("title") == group_title), None)
        if grp is None:
            grp = {"title": group_title, "contexts": contexts, "items": []}
            sensors.append(grp)
        grp["items"] = [i for i in grp.get("items", []) if _item_key(i) != _item_key(item)]
        grp["items"].append(item)
        changed.append(f"sensors_add:{group_title}/{_item_key(item)}")
    return changed


def apply_sensors_remove(prefs, sensors_remove):
    """sensors_remove: entity_id/labelで削除、空グループは掃除（chat.sh:815-825と同一）。"""
    changed = []
    removes = [str(x) for x in (sensors_remove or [])]
    if removes:
        for grp in prefs.get("sensors", {}).get("groups", []):
            before = len(grp.get("items", []))
            grp["items"] = [i for i in grp.get("items", [])
                             if i.get("entity") not in removes and i.get("label") not in removes]
            if len(grp["items"]) < before:
                changed.append("sensors_remove")
        grps = prefs.get("sensors", {}).get("groups", [])
        prefs["sensors"]["groups"] = [g for g in grps if g.get("items")]
    return changed


def apply_entities_add(prefs, entities_add):
    """entities_add: entity_idで重複排除して追加（chat.sh:828-839と同一）。"""
    changed = []
    for add in entities_add or []:
        eid = (add.get("entity_id") or "").strip()
        if not eid:
            continue
        row = {"name": (add.get("name") or "").strip(), "entity_id": eid}
        note = (add.get("note") or "").strip()
        if note:
            row["note"] = note
        prefs.setdefault("entities", [])
        prefs["entities"] = [e for e in prefs["entities"] if e.get("entity_id") != eid]
        prefs["entities"].append(row)
        changed.append(f"entities_add:{eid}")
    return changed


def apply_entities_remove(prefs, entities_remove):
    """entities_remove: entity_id/nameで削除（chat.sh:841-847と同一）。"""
    changed = []
    ent_removes = [str(x) for x in (entities_remove or [])]
    if ent_removes:
        before = len(prefs.get("entities", []))
        prefs["entities"] = [e for e in prefs.get("entities", [])
                              if e.get("entity_id") not in ent_removes and e.get("name") not in ent_removes]
        if len(prefs.get("entities", [])) < before:
            changed.append("entities_remove")
    return changed


def update_preferences(parsed, prefs_file, print_fn=print):
    """preferences_updateフィールドを読み取り、preferences.jsonへ反映する（chat.sh:694-855と同一契約）。

    何が起きても呼び出し側へは例外を伝播しない（chat.sh側の
    `2>/dev/null || true` による観測可能な挙動と同じ。この関数を
    囲むtry/exceptは、chat.sh側が持たなかったものを移植時に追加した
    唯一の意図的な差分）。
    """
    try:
        if not prefs_file:
            return

        update = (parsed or {}).get("preferences_update") or {}
        if not update:
            return

        try:
            with open(prefs_file, encoding="utf-8") as fh:
                prefs = json.load(fh)
        except Exception:
            prefs = {"cameras": [], "speakers": [], "presence": {}, "policies": []}

        changed = []
        changed += apply_cameras_add(prefs, update.get("cameras_add"))
        changed += apply_cameras_remove(prefs, update.get("cameras_remove"))
        changed += apply_speakers_set(prefs, update.get("speakers_set"))
        changed += apply_presence_set(prefs, update.get("presence_set"))
        changed += apply_policies_add(prefs, update.get("policies_add"))
        changed += apply_sensors_add(prefs, update.get("sensors_add"))
        changed += apply_sensors_remove(prefs, update.get("sensors_remove"))
        changed += apply_entities_add(prefs, update.get("entities_add"))
        changed += apply_entities_remove(prefs, update.get("entities_remove"))

        if changed:
            tmp = prefs_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(prefs, f, ensure_ascii=False, indent=2)
            os.replace(tmp, prefs_file)
            print_fn(f"[chat][prefs] 更新: {changed}")
    except Exception:
        pass
