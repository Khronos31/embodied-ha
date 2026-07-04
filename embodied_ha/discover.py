#!/usr/bin/env python3
"""discover.py — HA Template API でセンサーを走査し、preferences.json の
sensors / source マニフェストの「下書き」を生成する（公開アドオンのゼロ設定初回起動用）。

下書きはあくまで出発点。area 誤り・ペアリング曖昧・要選別バッテリ等は会話で補正する前提。

ヒューリスティック（実機調査 2026-06-22 で確定）:
  - motion / temperature / humidity は「area が付いているものだけ」採用
    （device_class=temperature には phone battery temp・天気予報温度などのノイズが
      混じるが、それらは area=None なので area フィルタで自然に落ちる）
  - temperature + humidity は同一 area でペアにして「X℃ / Y%」の複合行に
    （1つの area に温度センサーが複数ある場合はペアにせず個別出力＋警告）
  - battery は area を持たない（デバイス付属）ため area フィルタ非適用。
    数が多くノイズになりがちなので全件を下書きに出しつつ「要選別」を警告

env: HA_URL, SUPERVISOR_TOKEN, EHA_PREFS_FILE, RESIDENT
引数:
  （なし）       下書きを JSON で stdout に出力（dry-run。確認用）
  --write       preferences.json の sensors を下書きで置き換える（presence 行も付与）
"""

import argparse
import json
import os
import re
import subprocess
import sys

from migrate_source_schema import build_source_draft, classify_source  # type: ignore  # noqa: F401  (classify_source re-exported for shared-classifier use/tests)


def get_token():
    return os.environ.get("SUPERVISOR_TOKEN", "")


DISCOVERY_TEMPLATE = (
    "{% for s in states.binary_sensor %}"
    "{% if s.attributes.device_class == 'motion' %}"
    "motion|{{ s.entity_id }}|{{ area_name(s.entity_id) }}|{{ s.name }}\n"
    "{% endif %}{% endfor %}"
    "{% for s in states.sensor %}{% set dc = s.attributes.device_class %}"
    "{% if dc in ['temperature','humidity','battery'] %}"
    "{{ dc }}|{{ s.entity_id }}|{{ area_name(s.entity_id) }}|{{ s.name }}\n"
    "{% endif %}{% endfor %}"
    # スピーカー候補（media_player）と TTS エンジン
    "{% for s in states.media_player %}"
    "mp|{{ s.entity_id }}|{{ area_name(s.entity_id) }}|{{ s.name }}\n"
    "{% endfor %}"
    "{% for s in states.tts %}"
    "tts|{{ s.entity_id }}||{{ s.name }}\n"
    "{% endfor %}"
    # 操作できる家電（エンティティ対応表の下書き用）
    "{% for dom in ['light','switch','climate','cover','fan','script'] %}"
    "{% for s in states[dom] %}"
    "ctl|{{ s.entity_id }}|{{ area_name(s.entity_id) }}|{{ s.name }}\n"
    "{% endfor %}{% endfor %}"
)

# media_player から「喋れるスピーカー」を推定するための語彙（entity_id / name に対して）。
# 下書き用のヒューリスティック。誤りは会話で修正する前提。
SPEAKER_EXCLUDE = ("tv", "terehi", "テレビ", "switch", "nintendo", "recorder", "レコーダー",
                   "spotify", "chromecast", "全部", "playstation", "streamer", "google_tv", "fire")
SPEAKER_PREFER = ("nest", "mini", "alexa", "アレクサ", "echo", "homepod", "sonos",
                  "voice", "speaker", "home")


def fetch_entities(ha_url, token):
    r = subprocess.run(
        ["curl", "-sf", "--max-time", "15", "-X", "POST",
         "-H", f"Authorization: Bearer {token}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps({"template": DISCOVERY_TEMPLATE}),
         f"{ha_url.rstrip('/')}/template"],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return None
    rows = []
    for line in r.stdout.splitlines():
        parts = line.split("|")
        if len(parts) == 4:
            dc, eid, area, name = parts
            rows.append({"dc": dc, "eid": eid,
                         "area": area if area not in ("None", "") else None,
                         "name": name})
    return rows


def strip_battery_label(name):
    return re.sub(r"\s*battery\s*level\s*$", "", name, flags=re.IGNORECASE).strip() or name


def build_draft(rows, resident):
    warnings = []
    groups = []

    # presence（preferences.presence から。無ければ汎用ラベルのみ）
    presence_entity = None
    try:
        prefs = json.load(open(os.environ.get("EHA_PREFS_FILE", ""), encoding="utf-8"))
        presence_entity = prefs.get("presence", {}).get("entity")
    except Exception:
        pass
    if presence_entity:
        groups.append({
            "title": "在宅状態", "contexts": ["loop"],
            "items": [{"label": f"{resident}さん", "entity": presence_entity}]
        })

    # motion（area ありのみ）
    motion = [r for r in rows if r["dc"] == "motion" and r["area"]]
    if motion:
        area_counts = {}
        for r in motion:
            area_counts[r["area"]] = area_counts.get(r["area"], 0) + 1
        items = []
        for r in motion:
            # 同一 area に複数あれば entity 末尾で区別
            label = r["area"]
            if area_counts[r["area"]] > 1:
                label = f"{r['area']}({r['eid'].split('.')[-1][:12]})"
            items.append({"label": label, "entity": r["eid"]})
        groups.append({"title": "人感センサー", "contexts": ["loop"], "items": items})

    # temperature + humidity（area でペア）
    temps = [r for r in rows if r["dc"] == "temperature" and r["area"]]
    hums = {r["area"]: r for r in rows if r["dc"] == "humidity" and r["area"]}
    if temps:
        by_area = {}
        for r in temps:
            by_area.setdefault(r["area"], []).append(r)
        items = []
        for area, trs in by_area.items():
            if len(trs) == 1:
                t = trs[0]["eid"]
                h = hums.get(area)
                if h:
                    items.append({"label": area,
                                  "template": f"{{{{ states('{t}') }}}}℃ / {{{{ states('{h['eid']}') }}}}%"})
                else:
                    items.append({"label": area, "template": f"{{{{ states('{t}') }}}}℃"})
            else:
                warnings.append(f"area『{area}』に温度センサーが{len(trs)}個。ペアにせず個別出力。")
                for tr in trs:
                    items.append({"label": f"{area}({tr['eid'].split('.')[-1][:12]})",
                                  "template": f"{{{{ states('{tr['eid']}') }}}}℃"})
        groups.append({"title": "温湿度", "contexts": ["loop"], "items": items})

    # battery（area なし・要選別）
    batts = [r for r in rows if r["dc"] == "battery"]
    if batts:
        warnings.append(f"バッテリーを{len(batts)}個検出。ノイズが多いので会話で選別推奨"
                        f"（「○○のバッテリーは要らない」等）。")
        items = [{"label": strip_battery_label(r["name"]),
                  "template": f"{{{{ states('{r['eid']}') }}}}%"} for r in batts]
        groups.append({"title": "デバイスバッテリー", "contexts": ["loop"], "items": items})

    return {"groups": groups}, warnings


def _default_tts(rows):
    tts = [r["eid"] for r in rows if r["dc"] == "tts"]
    for t in tts:                       # HA Cloud があれば優先（高品質・設定不要なことが多い）
        if "home_assistant_cloud" in t:
            return t
    return tts[0] if tts else ""


def build_speakers_draft(rows):
    """media_player をエリア別に集約し、スピーカーらしきものを1つ選んで speakers 下書きを作る。
    type は tts 固定。Alexa を notify で鳴らす等は会話で調整する前提。"""
    warnings = []
    default_tts = _default_tts(rows)
    by_area = {}
    for r in rows:
        if r["dc"] != "mp" or not r["area"]:
            continue
        hay = (r["eid"] + " " + r["name"]).lower()
        if any(k in hay for k in SPEAKER_EXCLUDE):   # TV・ゲーム機・レコーダー等は除外
            continue
        by_area.setdefault(r["area"], []).append(r)

    speakers = []
    for area, cands in by_area.items():
        preferred = [c for c in cands
                     if any(k in (c["eid"] + " " + c["name"]).lower() for k in SPEAKER_PREFER)]
        chosen = preferred or cands
        pick = chosen[0]
        if len(chosen) > 1:
            warnings.append(f"area『{area}』にスピーカー候補が複数。{pick['eid']} を仮選択"
                            f"（「{area}は○○で喋って」で変更可）。")
        speakers.append({"type": "tts", "entity": pick["eid"], "room": area, "label": area})

    if speakers and not default_tts:
        warnings.append("TTS エンジンが見つかりません。グローバル設定の tts_entity を会話で設定してください。")
    return speakers, default_tts, warnings


def build_entities_draft(rows):
    """操作できる家電（light/switch/climate/media_player/cover/fan/script）を
    friendly_name 付きで「エンティティ対応表」の下書きにする。
    name は friendly_name をそのまま採用（口語名に近い）。同名が複数あれば note で警告。"""
    warnings = []
    ents = [r for r in rows if r["dc"] in ("ctl", "mp")]
    name_counts = {}
    for r in ents:
        name_counts[r["name"]] = name_counts.get(r["name"], 0) + 1
    out = []
    for r in ents:
        row = {"name": r["name"], "entity_id": r["eid"]}
        if name_counts[r["name"]] > 1:
            row["note"] = "要確認（同名複数）"
        out.append(row)
    dup = sum(1 for v in name_counts.values() if v > 1)
    if dup:
        warnings.append(f"操作できる家電に同名が{dup}種あります。Web UIで重複を整理してください。")
    return out, warnings


def build_source_draft_from_preferences(prefs):
    """preferences.json の source 系列を 4 バケツへ正規化する。"""
    if isinstance(prefs.get("audio_sources"), list):
        return build_source_draft(prefs)
    return {
        "cameras": list(prefs.get("cameras") or []),
        "mics": list(prefs.get("mics") or []),
        "video_media": list(prefs.get("video_media") or []),
        "audio_media": list(prefs.get("audio_media") or []),
    }, []


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--write", action="store_true",
                   help="preferences.json の sensors を下書きで置き換える")
    args = p.parse_args()

    ha_url = os.environ["HA_URL"]
    resident = os.environ.get("RESIDENT", "ユーザー")

    rows = fetch_entities(ha_url, get_token())
    if rows is None:
        print("[discover] Template API 呼び出しに失敗しました", file=sys.stderr)
        sys.exit(1)

    draft, warnings = build_draft(rows, resident)
    speakers_draft, default_tts, speaker_warnings = build_speakers_draft(rows)
    entities_draft, entity_warnings = build_entities_draft(rows)
    prefs_file = os.environ.get("EHA_PREFS_FILE", "")
    source_draft = {"cameras": [], "mics": [], "video_media": [], "audio_media": []}
    source_warnings = []
    if prefs_file and os.path.exists(prefs_file):
        try:
            prefs = json.load(open(prefs_file, encoding="utf-8"))
        except Exception:
            prefs = {}
        if isinstance(prefs, dict):
            source_draft, source_warnings = build_source_draft_from_preferences(prefs)

    for w in warnings + speaker_warnings + entity_warnings + source_warnings:
        print(f"[discover][warn] {w}", file=sys.stderr)

    if args.write:
        prefs_file = os.environ.get("EHA_PREFS_FILE", "")
        try:
            prefs = json.load(open(prefs_file, encoding="utf-8"))
        except Exception:
            prefs = {}
        if not prefs.get("sensors"):
            prefs["sensors"] = draft
            print("[discover] sensors 下書きを書き込みました", file=sys.stderr)
        if not prefs.get("tts_entity") and default_tts:
            prefs["tts_entity"] = default_tts
        # speakers は既存ユーザー設定を尊重し、未設定（空）のときだけ下書きを入れる。
        if not prefs.get("speakers") and speakers_draft:
            prefs["speakers"] = speakers_draft
            print(f"[discover] speakers 下書きを書き込みました（{len(speakers_draft)}部屋）", file=sys.stderr)
        # entities（操作できる家電）も未設定のときだけ下書きを入れる。
        if not prefs.get("entities") and entities_draft:
            prefs["entities"] = entities_draft
            print(f"[discover] entities 下書きを書き込みました（{len(entities_draft)}件）", file=sys.stderr)
        source_keys = ("cameras", "mics", "video_media", "audio_media")
        if not any(prefs.get(key) for key in source_keys) and any(source_draft.values()):
            for key in source_keys:
                prefs[key] = source_draft[key]
            total = sum(len(source_draft[key]) for key in source_keys)
            print(f"[discover] source 下書きを書き込みました（{total}件）", file=sys.stderr)
        tmp = prefs_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(prefs, f, ensure_ascii=False, indent=2)
        os.replace(tmp, prefs_file)
        if prefs.get("sensors") == draft:
            n = sum(len(g["items"]) for g in draft["groups"])
            print(f"[discover] preferences.json に sensors を書き込みました（{len(draft['groups'])}グループ / {n}項目）",
                  file=sys.stderr)
    else:
        output = {
            "sensors": draft,
            "speakers": speakers_draft,
            "entities": entities_draft,
            **source_draft,
        }
        if default_tts:
            output["tts_entity"] = default_tts
        print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
