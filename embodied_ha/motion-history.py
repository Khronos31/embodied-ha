#!/usr/bin/env python3
"""motion-history.py [minutes] — 直近N分の人感センサー「動きの流れ」を HA recorder から組む。

人感センサー履歴を HA の History API（recorder）から直接取得する。

人感センサーの特定はハイブリッド:
  (b) preferences.json の sensors マニフェストに人感センサー（binary_sensor.*_motion 等）が
      列挙されていればそれを使う。label を部屋名にし、note も拾う。除外もユーザーが制御できる。
  (a) 無ければ device_class が motion/occupancy/presence の binary_sensor を自動発見し、
      friendly_name を部屋名に使う（ゼロ設定で動く）。

出力: "HH:MM 部屋名, HH:MM 部屋名, ..."（時刻昇順）。該当なし or 失敗時は "なし"。

env: EHA_PREFS_FILE, HA_URL, SUPERVISOR_TOKEN
引数: minutes（省略時 15）
"""
import sys, json, os, subprocess, datetime

MOTION_CLASSES = ("motion", "occupancy", "presence")


def get_token():
    return os.environ.get("SUPERVISOR_TOKEN", "")


def _curl_get(url, token):
    r = subprocess.run(
        ["curl", "-sf", "--max-time", "10",
         "-H", f"Authorization: Bearer {token}", url],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def _looks_like_motion(entity, group_title):
    if not entity.startswith("binary_sensor."):
        return False
    if entity.endswith("_motion"):
        return True
    t = (group_title or "").lower()
    return "人感" in (group_title or "") or "motion" in t


def motion_map_from_prefs(prefs):
    """(b) preferences の sensors マニフェストから 人感センサー entity→部屋名 を集める。"""
    out = {}
    for g in prefs.get("sensors", {}).get("groups", []):
        title = g.get("title", "")
        for it in g.get("items", []):
            ent = it.get("entity", "")
            if _looks_like_motion(ent, title):
                out[ent] = it.get("label") or ent
    return out


def _strip_name(fn):
    for suf in (" モーション", " motion", " Motion"):
        if fn.endswith(suf):
            return fn[: -len(suf)]
    return fn


def motion_map_from_discovery(ha_url, token):
    """(a) device_class で人感センサーを自動発見し entity→friendly_name を作る。"""
    states = _curl_get(f"{ha_url.rstrip('/')}/states", token)
    if not states:
        return {}
    out = {}
    for s in states:
        eid = s.get("entity_id", "")
        if not eid.startswith("binary_sensor."):
            continue
        attrs = s.get("attributes", {})
        if attrs.get("device_class") in MOTION_CLASSES:
            out[eid] = _strip_name(attrs.get("friendly_name", eid))
    return out


def fetch_on_events(entities, minutes, ha_url, token):
    """History API で直近minutes分を取得し、on になった (datetime, entity) を返す。"""
    start = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    csv = ",".join(entities)
    url = (f"{ha_url.rstrip('/')}/history/period/{start}"
           f"?filter_entity_id={csv}&minimal_response")
    data = _curl_get(url, token)
    if not data:
        return []
    events = []
    for series in data:
        if not series:
            continue
        eid = series[0].get("entity_id", "")  # minimal_response: 先頭要素のみ entity_id を持つ
        for st in series:
            if st.get("state") != "on":
                continue
            ts = st.get("last_changed") or st.get("last_updated")
            if not ts:
                continue
            try:
                dt = datetime.datetime.fromisoformat(ts)
            except Exception:
                continue
            events.append((dt, eid))
    return events


def main():
    minutes = 15
    if len(sys.argv) > 1:
        try:
            minutes = int(sys.argv[1])
        except ValueError:
            pass

    ha_url = os.environ["HA_URL"]
    token = get_token()

    try:
        prefs = json.load(open(os.environ.get("EHA_PREFS_FILE", ""), encoding="utf-8"))
    except Exception:
        prefs = {}

    name_map = motion_map_from_prefs(prefs)
    if not name_map:
        name_map = motion_map_from_discovery(ha_url, token)

    if not name_map:
        print("なし")
        return

    events = fetch_on_events(list(name_map), minutes, ha_url, token)
    events.sort(key=lambda e: e[0])

    lines = []
    for dt, eid in events:
        hhmm = dt.astimezone().strftime("%H:%M")  # UTC→ローカルに変換して表示
        lines.append(f"{hhmm} {name_map.get(eid, eid)}")
    print(", ".join(lines) if lines else "なし")


if __name__ == "__main__":
    main()
