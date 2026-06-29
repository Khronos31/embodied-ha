#!/usr/bin/env python3
"""render-sensors.py — preferences.json の sensors マニフェストを
HA Template API で SENSORS テキストブロックに描画する。loop.sh / chat.sh 共用。

マニフェストの宣言（groups → items）から Jinja テンプレートを組み立て、
/api/template を1回叩いて整形済みテキストを得る。

env: EHA_PREFS_FILE, HA_URL, SUPERVISOR_TOKEN
引数: --context loop|chat（省略時 loop）… group.contexts でフィルタ

マニフェスト構造（preferences.json の "sensors"）:
  {
    "groups": [
      {
        "title": "人感センサー",
        "contexts": ["loop"],          # 省略時は全コンテキストで表示
        "items": [
          {"label": "リビング", "entity": "binary_sensor.xxx_motion"},
          {"label": "廊下1", "entity": "binary_sensor.yyy", "note": "リビング誤反応あり"},
          {"label": "温湿度", "template": "{{ states('sensor.t') }}℃ / {{ states('sensor.h') }}%"}
        ]
      }
    ]
  }

item は entity 形式（label: <state>）か template 形式（任意 Jinja）のどちらか。
note があれば末尾に「（note）」を付す。
"""
import sys, json, os, subprocess, argparse


def get_token():
    return os.environ.get("SUPERVISOR_TOKEN", "")


def build_template(groups, context):
    """マニフェストの groups から、HA に投げる単一の Jinja テンプレート文字列を組む。"""
    lines = []
    for g in groups:
        ctxs = g.get("contexts")
        if ctxs and context not in ctxs:
            continue
        items = g.get("items", [])
        rendered_items = []
        for it in items:
            label = it.get("label", "")
            note = it.get("note", "")
            suffix = f"（{note}）" if note else ""
            if "template" in it:
                val = it["template"]
            elif "entity" in it:
                val = "{{ states('%s') }}" % it["entity"]
            else:
                continue
            rendered_items.append(f"{label}: {val}{suffix}" if label else f"{val}{suffix}")
        if not rendered_items:
            continue
        if g.get("title"):
            lines.append(f"--- {g['title']} ---")
        lines.extend(rendered_items)
    return "\n".join(lines)


def render(template, ha_url, token):
    r = subprocess.run(
        ["curl", "-sf", "--max-time", "10", "-X", "POST",
         "-H", f"Authorization: Bearer {token}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps({"template": template}, ensure_ascii=False),
         f"{ha_url.rstrip('/')}/template"],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return None
    return r.stdout


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--context", default="loop")
    args = p.parse_args()

    prefs_file = os.environ.get("EHA_PREFS_FILE", "")
    ha_url = os.environ["HA_URL"]

    try:
        prefs = json.load(open(prefs_file, encoding="utf-8"))
    except Exception:
        prefs = {}

    groups = prefs.get("sensors", {}).get("groups", [])
    if not groups:
        # マニフェスト未設定: 空ではなく明示メッセージ（ゼロ設定の初回起動を想定）
        print("（センサー未設定。discover.py で下書きを生成するか、会話で登録してください）")
        return

    template = build_template(groups, args.context)
    if not template.strip():
        print("（このコンテキスト向けのセンサーがありません）")
        return

    out = render(template, ha_url, get_token())
    if out is None:
        print("（センサー取得失敗）", file=sys.stderr)
        sys.exit(1)
    print(out)


if __name__ == "__main__":
    main()
