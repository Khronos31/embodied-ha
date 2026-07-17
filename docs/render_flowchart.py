#!/usr/bin/env python3
"""chat_py_flowchart.mmd を、固定の配色・レイアウトでPNGへ描画する。

同じ手順で再生成することで、chat.pyの処理フローが変わったときに
見た目で(色使いや描き方に依存せず)前後を比較できるようにするための
スクリプト。Mermaidファイル自体をパースするのではなく、内容が同一の
Graphvizグラフをこのスクリプト内に直接記述している(2ファイルの対応は
手動維持。chat_py_flowchart.mmdを変更したら、このファイルのノード・
エッジも合わせて更新してから再実行すること)。

実行方法:
    python3 docs/render_flowchart.py
    (docs/chat_py_flowchart.png へ出力する。Graphviz(dot)が必要)
"""
import subprocess
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent
OUTPUT_PNG = DOCS_DIR / "chat_py_flowchart.png"

FONT = "Noto Sans CJK JP"

# 色は固定(将来また生成しても同じ見た目になるようにする)
COLOR_CRASH = "#f8d7da"       # 赤系: ガード無し、失敗で全体クラッシュ
COLOR_CRASH_BORDER = "#c0392b"
COLOR_FALLBACK = "#d6eaf8"    # 青系: フォールバックあり
COLOR_FALLBACK_BORDER = "#2980b9"
COLOR_SYSTEM = "#d5f5e3"      # 緑系: 外部システム/Web UI連携
COLOR_SYSTEM_BORDER = "#27ae60"
COLOR_DECISION = "#fdebd0"    # 黄系: 条件分岐
COLOR_DECISION_BORDER = "#d68910"
COLOR_TERMINAL = "#eaeded"    # 灰色: プロセスの開始/終了
COLOR_TERMINAL_BORDER = "#7f8c8d"

NODES = {
    "A": ("daemon.py: run_chat()\\nsubprocess.run(['python3', chat.py])", "terminal"),
    "B": ("chat.py: run(environ)\\n(エントリーポイント)", "system"),
    "C": ("CHAT_MESSAGEが空？", "decision"),
    "C1": ("ログ出力して終了\\n(Web UIステータスは変更しない)", "terminal"),
    "D": ("Web UI: status=thinking\\n(APIへステータスを通知)", "system"),
    "E": ("try: _run_chat_turn() 処理開始", "system"),
    "F": ("character.md読み込み\\n(失敗時は空文字列で継続)", "fallback"),
    "G1": ("recent_activity / current_mood\\n(フォールバックあり)", "fallback"),
    "G2": ("⚠ long_memory取得(mem-context.py)\\nガード無し＝失敗で全体クラッシュ", "crash"),
    "G3": ("pending_proposal / entity_table\\nchat_history(フォールバックあり)", "fallback"),
    "G4": ("⚠ turn_taking_state取得\\nガード無し＝失敗で全体クラッシュ", "crash"),
    "G5": ("sensors / body_location_context\\n(フォールバックあり)", "fallback"),
    "G6": ("投射カメラ画像取得\\n(失敗時は空リストで継続)", "fallback"),
    "G7": ("features.md / 紹介済み機能\\n(フォールバックあり)", "fallback"),
    "G8": ("CHAT_SOURCE == voice?", "decision"),
    "G8a": ("⚠ recent_auditory_input取得\\nガード無し＝失敗で全体クラッシュ", "crash"),
    "G8b": ("recent_auditory_input = 空文字列\\n(依存先を呼ばない)", "fallback"),
    "G9": ("queued listen(深聴き予約)解決\\n(あればenv継承)", "fallback"),
    "H": ("プロンプト構築\\nbuild_chat_prompt()", "fallback"),
    "I": ("Claude CLI起動\\ninvoke_chat_claude()\\n(invoke-agent.sh経由)", "system"),
    "J": ("レスポンス抽出\\nchat_extract()", "fallback"),
    "K1": ("紹介済み機能を記録", "fallback"),
    "K2": ("保留提案の消化", "fallback"),
    "K3": ("preferences.json更新\\n(try/except有り・握りつぶす)", "fallback"),
    "K4": ("CHAT_SOURCE == voice?", "decision"),
    "K5": ("⚠ chat_log.jsonl追記\\nガード無し＝失敗で全体クラッシュ", "crash"),
    "K6": ("チャットログには追記しない\\n(speakツールが直接返答)", "fallback"),
    "K7": ("MQTT publish(private内省)\\n(try/except有り・握りつぶす)", "fallback"),
    "L": ("finally: Web UI status=idle\\n★クラッシュしても必ず実行される", "system"),
    "M": ("daemon.py: returncodeを見る\\n(終了ステータス判定)", "terminal"),
}

EDGES = [
    ("A", "B", None),
    ("B", "C", None),
    ("C", "C1", "空"),
    ("C", "D", "非空"),
    ("D", "E", None),
    ("E", "F", None),
    ("F", "G1", None),
    ("G1", "G2", None),
    ("G2", "G3", None),
    ("G3", "G4", None),
    ("G4", "G5", None),
    ("G5", "G6", None),
    ("G6", "G7", None),
    ("G7", "G8", None),
    ("G8", "G8a", "はい"),
    ("G8", "G8b", "いいえ"),
    ("G8a", "G9", None),
    ("G8b", "G9", None),
    ("G9", "H", None),
    ("H", "I", None),
    ("I", "J", None),
    ("J", "K1", None),
    ("K1", "K2", None),
    ("K2", "K3", None),
    ("K3", "K4", None),
    ("K4", "K5", "いいえ"),
    ("K4", "K6", "はい"),
    ("K5", "K7", None),
    ("K6", "K7", None),
    ("K7", "L", None),
    ("C1", "M", None),
    ("L", "M", None),
]

# クラッシュ地点からfinallyへの「例外発生時」の経路(点線・赤)。視覚的な安心材料。
CRASH_TO_FINALLY = ["G2", "G4", "G8a", "K5"]

STYLE_BY_KIND = {
    "crash": (COLOR_CRASH, COLOR_CRASH_BORDER, "box", "bold"),
    "fallback": (COLOR_FALLBACK, COLOR_FALLBACK_BORDER, "box", ""),
    "system": (COLOR_SYSTEM, COLOR_SYSTEM_BORDER, "box", ""),
    "decision": (COLOR_DECISION, COLOR_DECISION_BORDER, "diamond", ""),
    "terminal": (COLOR_TERMINAL, COLOR_TERMINAL_BORDER, "box", ""),
}


def _esc(label):
    return label.replace('"', '\\"')


def build_dot():
    lines = [
        "digraph chat_py_flowchart {",
        '  rankdir=TB;',
        f'  fontname="{FONT}";',
        '  bgcolor="white";',
        f'  node [fontname="{FONT}", fontsize=11, style=filled];',
        f'  edge [fontname="{FONT}", fontsize=10];',
        '  label="chat.py 処理フロー図\\n(daemon.py からの実行から終了まで)";',
        '  labelloc=t; fontsize=20;',
    ]
    for node_id, (label, kind) in NODES.items():
        fill, border, shape, extra_style = STYLE_BY_KIND[kind]
        style = "filled,rounded" if shape == "box" else "filled"
        if extra_style:
            style += "," + extra_style
        penwidth = "2.5" if kind == "crash" else "1"
        lines.append(
            f'  {node_id} [label="{_esc(label)}", shape={shape}, style="{style}", '
            f'fillcolor="{fill}", color="{border}", penwidth={penwidth}];'
        )
    for src, dst, edge_label in EDGES:
        attrs = f'label="{edge_label}"' if edge_label else ""
        lines.append(f"  {src} -> {dst} [{attrs}];")
    for node_id in CRASH_TO_FINALLY:
        lines.append(
            f'  {node_id} -> L [label="例外発生(クラッシュ時)", style=dashed, '
            f'color="{COLOR_CRASH_BORDER}", fontcolor="{COLOR_CRASH_BORDER}"];'
        )

    # 凡例(色の意味。単体で見ても分かるように)
    lines.append('  subgraph cluster_legend {')
    lines.append('    label="凡例"; fontsize=13; style=dashed; color="#7f8c8d";')
    legend_items = [
        ("legend_crash", "ガード無し(依存先が失敗すると全体クラッシュ)", "crash"),
        ("legend_fallback", "フォールバックあり(失敗しても継続)", "fallback"),
        ("legend_system", "外部システム/Web UI連携", "system"),
        ("legend_decision", "条件分岐", "decision"),
        ("legend_terminal", "プロセスの開始/終了", "terminal"),
    ]
    for node_id, label, kind in legend_items:
        fill, border, shape, extra_style = STYLE_BY_KIND[kind]
        style = "filled,rounded" if shape == "box" else "filled"
        if extra_style:
            style += "," + extra_style
        lines.append(
            f'    {node_id} [label="{_esc(label)}", shape={shape}, style="{style}", '
            f'fillcolor="{fill}", color="{border}"];'
        )
    for a, b in zip([n for n, _, _ in legend_items], [n for n, _, _ in legend_items][1:]):
        lines.append(f"    {a} -> {b} [style=invis];")
    lines.append("  }")

    lines.append("}")
    return "\n".join(lines)


def main():
    dot_source = build_dot()
    subprocess.run(
        ["dot", "-Tpng", "-Gdpi=150", "-o", str(OUTPUT_PNG)],
        input=dot_source, text=True, check=True,
    )
    print(f"generated: {OUTPUT_PNG}")


if __name__ == "__main__":
    main()
