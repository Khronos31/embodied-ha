#!/usr/bin/env python3
"""ゲームMCPサーバー（embodied-ha 用）。

ツール:
  game_wiki6_start    … Wiki6ゲームの問題を生成してルールを返す
  game_wiki6_getlinks … Wikipediaの記事本文からリンク一覧を取得
  game_wiki6_solve    … PediaRouteで最短経路を取得（答え合わせ）

外部アクセス: ja.wikipedia.org / pediaroute.com（game-mcp内部で直接アクセス）
"""

from __future__ import annotations

import json
import os
import random
import sys
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen

from mcp_lib import serve, text

# /data/python-packages に永続インストールされた gensim 等を参照
_pkg_dir = "/data/python-packages"
if os.path.isdir(_pkg_dir) and _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)

TIMEOUT = 20
UA = "embodied-ha/game-mcp (educational use)"


def _plugin_disabled_error(name: str):
    msg = f"{name} ゲームが無効です。Web UI のゲームタブから有効にしてください。"
    return [text(json.dumps({"error": "plugin_disabled", "message": msg}, ensure_ascii=False))], True

WORD_PAIRS = [
    ("バナナ", "図書館"),
    ("猫", "宇宙"),
    ("ピザ", "江戸時代"),
    ("富士山", "インターネット"),
    ("サッカー", "茶道"),
    ("新幹線", "ピアノ"),
    ("桜", "半導体"),
    ("相撲", "フランス料理"),
    ("納豆", "オリンピック"),
    ("将棋", "深海"),
]

WIKI6_RULES = """【Wiki6 ルール】
Wikipediaのリンクだけを辿り、スタート記事からゴール記事に最短クリック数で到達してください。

ツール:
  game_wiki6_getlinks(word)        … 記事の本文リンク一覧を取得
  game_wiki6_solve(word1, word2)  … 最短経路を確認（答え合わせ用）

ヒント: 地名・年号・人名の記事はリンクが豊富なハブになりやすいです。"""

# スキップするWikipediaの特殊クラス（脚注・ナビボックス等）
_SKIP_CLASSES = {
    "reflist", "references", "navbox", "toc", "mw-editsection",
    "hatnote", "navigation-not-searchable", "sistersitebox",
    "mw-references-wrap", "mw-cite-backlink", "reference",
}


class _WikiLinkParser(HTMLParser):
    """Wikipedia HTMLの本文部分から /wiki/ リンクだけを抽出する。"""

    def __init__(self):
        super().__init__()
        self._depth = 0
        self._skip_until: int | None = None
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        cls = attrs_d.get("class", "")
        if self._skip_until is None and any(c in cls for c in _SKIP_CLASSES):
            self._skip_until = self._depth
        self._depth += 1
        if self._skip_until is not None:
            return
        if tag == "a":
            href = attrs_d.get("href", "")
            if href.startswith("/wiki/") and ":" not in href[6:]:
                title = unquote(href[6:]).replace("_", " ")
                if title and "#" not in title:
                    self.links.append(title)

    def handle_endtag(self, tag):
        self._depth -= 1
        if self._skip_until is not None and self._depth <= self._skip_until:
            self._skip_until = None


def _fetch(url: str, timeout: int = TIMEOUT) -> bytes:
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def _json_error(msg: str):
    return [text(json.dumps({"error": msg}, ensure_ascii=False))], True


# --- wiki6 ツール ---

def _pediaroute_random() -> str | None:
    """PediaRouteのランダム記事APIを呼ぶ。失敗時はNone。"""
    try:
        url = "https://pediaroute.com/api/random?lang=ja"
        body = _fetch(url).decode("utf-8")
        # レスポンスはJSON文字列 "\"バナナ\""
        return json.loads(body)
    except Exception:
        return None


def game_wiki6_start(args: dict[str, Any]):
    if not _PLUGINS.get("wiki6"):
        return _plugin_disabled_error("Wiki6")
    # PediaRouteのランダムAPIで問題を生成。失敗時はハードコードリストから選ぶ。
    start = _pediaroute_random()
    goal = _pediaroute_random()
    if not start or not goal or start == goal:
        pair = random.choice(WORD_PAIRS)
        start, goal = pair
    result = {
        "start": start,
        "goal": goal,
        "rules": WIKI6_RULES,
    }
    return [text(json.dumps(result, ensure_ascii=False, indent=2))], False


def game_wiki6_getlinks(args: dict[str, Any]):
    if not _PLUGINS.get("wiki6"):
        return _plugin_disabled_error("Wiki6")
    word = str(args.get("word") or "").strip()
    if not word:
        return _json_error("word が空です")
    try:
        url = (
            f"https://ja.wikipedia.org/w/api.php"
            f"?action=parse&page={quote(word)}&prop=text&format=json&redirects=1"
        )
        data = json.loads(_fetch(url).decode("utf-8"))
        if "error" in data:
            return _json_error(f"Wikipedia: {data['error'].get('info', 'not found')}")
        html = data["parse"]["text"]["*"]
        parser = _WikiLinkParser()
        parser.feed(html)
        seen: set[str] = set()
        links: list[str] = []
        for lnk in parser.links:
            if lnk not in seen:
                seen.add(lnk)
                links.append(lnk)
        return [text(json.dumps(
            {"word": word, "links": links, "count": len(links)},
            ensure_ascii=False, indent=2,
        ))], False
    except HTTPError as e:
        return _json_error(f"HTTP {e.code}: {e.reason}")
    except URLError as e:
        return _json_error(f"network error: {e.reason}")
    except Exception as e:
        return _json_error(str(e))


_PEDIAROUTE_ERRORS = {
    1: "スタート記事が見つかりません",
    2: "ゴール記事が見つかりません",
    3: "6クリック以内の経路が見つかりませんでした",
}


def game_wiki6_solve(args: dict[str, Any]):
    if not _PLUGINS.get("wiki6"):
        return _plugin_disabled_error("Wiki6")
    word1 = str(args.get("word1") or "").strip()
    word2 = str(args.get("word2") or "").strip()
    if not word1 or not word2:
        return _json_error("word1 と word2 が必要です")
    try:
        url = "https://pediaroute.com/api/search?lang=ja"
        body_bytes = json.dumps({"from": word1, "to": word2}).encode("utf-8")
        req = Request(url, data=body_bytes, headers={
            "User-Agent": UA,
            "Content-Type": "application/json",
        })
        with urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        error_code = data.get("error", 0)
        if error_code != 0:
            return _json_error(_PEDIAROUTE_ERRORS.get(error_code, f"error {error_code}"))
        route = data.get("route", [])
        result = {
            "word1": word1,
            "word2": word2,
            "path": route,
            "clicks": len(route) - 1,
        }
        return [text(json.dumps(result, ensure_ascii=False, indent=2))], False
    except HTTPError as e:
        return _json_error(f"HTTP {e.code}: {e.reason}")
    except URLError as e:
        return _json_error(f"network error: {e.reason}")
    except Exception as e:
        return _json_error(str(e))


# --- wordvec_race ツール ---

_kv = None  # 遅延ロード

_RACE_BASES = [
    "エアコン", "図書館", "桜", "寿司", "新幹線",
    "将棋", "温泉", "台風", "選挙", "宇宙",
    "カレー", "富士山", "サッカー", "夏休み", "電車",
]


def _get_kv():
    global _kv
    if _kv is not None:
        return _kv
    kv_path = "/data/word2vec/chive-1.3-mc90_gensim/chive-1.3-mc90.kv"
    from gensim.models import KeyedVectors
    _kv = KeyedVectors.load(kv_path)
    return _kv


def _sudachi_normalize(word: str) -> str:
    """SudachiによるchiVe正規化を試みる。失敗時は元の単語を返す。"""
    try:
        import sudachipy
        tokenizer = sudachipy.Dictionary().create()
        morphemes = tokenizer.tokenize(word)
        if morphemes:
            return morphemes[0].dictionary_form()
    except Exception:
        pass
    return word


def _lookup(kv, word: str) -> str | None:
    """単語をchiVeの語彙で検索。正規化も試みてNoneを返す。"""
    if word in kv:
        return word
    normalized = _sudachi_normalize(word)
    if normalized in kv:
        return normalized
    return None


def game_wordvec_race_start(args: dict[str, Any]):
    if not _PLUGINS.get("wordvec_race"):
        return _plugin_disabled_error("WordVecチキンレース")
    try:
        kv = _get_kv()
        base = str(args.get("base") or "").strip() or random.choice(_RACE_BASES)
        key = _lookup(kv, base)
        if key is None:
            return _json_error(f"「{base}」は語彙にありません")
        result = {
            "base": key,
            "message": f"お題は「{key}」です。より遠い単語を交互に出してください。前の単語より近づいたら負け。",
        }
        return [text(json.dumps(result, ensure_ascii=False, indent=2))], False
    except Exception as e:
        return _json_error(str(e))


def game_wordvec_race_submit(args: dict[str, Any]):
    if not _PLUGINS.get("wordvec_race"):
        return _plugin_disabled_error("WordVecチキンレース")
    try:
        kv = _get_kv()
        start = str(args.get("start") or "").strip()
        last  = str(args.get("last")  or "").strip()
        answer = str(args.get("answer") or "").strip()
        for w, label in [(start, "start"), (last, "last"), (answer, "answer")]:
            if not w:
                return _json_error(f"{label} が空です")
        start_key  = _lookup(kv, start)
        last_key   = _lookup(kv, last)
        answer_key = _lookup(kv, answer)
        if start_key is None:
            return _json_error(f"「{start}」は語彙にありません")
        if last_key is None:
            return _json_error(f"「{last}」は語彙にありません")
        if answer_key is None:
            return _json_error(f"「{answer}」は語彙にありません")
        sim_last   = float(kv.similarity(last_key, start_key))
        sim_answer = float(kv.similarity(answer_key, start_key))
        valid = sim_answer < sim_last
        result = {
            "answer": answer_key,
            "sim_answer": round(sim_answer, 4),
            "last": last_key,
            "sim_last": round(sim_last, 4),
            "start": start_key,
            "valid": valid,
            "message": (
                f"「{answer_key}」の類似度: {sim_answer:.4f}（前の手 {last_key}: {sim_last:.4f}）"
                + ("　→ 合法！さらに遠ざかりました。ゲーム続行。" if valid else "　→ 負け！前より近づいています。この単語を出したプレイヤーの負けでゲーム終了。")
            ),
        }
        return [text(json.dumps(result, ensure_ascii=False, indent=2))], False
    except Exception as e:
        return _json_error(str(e))


def game_wordvec_race_hint(args: dict[str, Any]):
    if not _PLUGINS.get("wordvec_race"):
        return _plugin_disabled_error("WordVecチキンレース")
    try:
        kv = _get_kv()
        word1 = str(args.get("word1") or "").strip()
        start = str(args.get("start") or "").strip()
        goal  = str(args.get("goal")  or "").strip()
        for w, label in [(word1, "word1"), (start, "start"), (goal, "goal")]:
            if not w:
                return _json_error(f"{label} が空です")
        word1_key = _lookup(kv, word1)
        start_key = _lookup(kv, start)
        goal_key  = _lookup(kv, goal)
        if word1_key is None:
            return _json_error(f"「{word1}」は語彙にありません")
        if start_key is None:
            return _json_error(f"「{start}」は語彙にありません")
        if goal_key is None:
            return _json_error(f"「{goal}」は語彙にありません")
        sim_word1 = float(kv.similarity(word1_key, start_key))
        # word1の近傍から、startよりも遠い（goalに近い）単語を探す
        neighbors = kv.most_similar(word1_key, topn=200)
        candidates = []
        for w, _ in neighbors:
            if w in (word1_key, start_key, goal_key):
                continue
            sim_s = float(kv.similarity(w, start_key))
            if sim_s < sim_word1:
                sim_g = float(kv.similarity(w, goal_key))
                candidates.append({"word": w, "sim_start": round(sim_s, 4), "sim_goal": round(sim_g, 4)})
        # goalに近い順にソート
        candidates.sort(key=lambda x: -x["sim_goal"])
        result = {
            "word1": word1_key,
            "sim_word1_start": round(sim_word1, 4),
            "hints": candidates[:10],
        }
        return [text(json.dumps(result, ensure_ascii=False, indent=2))], False
    except Exception as e:
        return _json_error(str(e))


def _load_plugins() -> dict[str, bool]:
    plugins: dict[str, bool] = {
        "wiki6": True,
        "wordvec_race": False,
    }
    prefs_file = os.environ.get("EHA_PREFS_FILE", "")
    if not prefs_file:
        return plugins
    try:
        with open(prefs_file, encoding="utf-8") as f:
            prefs = json.load(f)
    except Exception:
        return plugins
    if not isinstance(prefs, dict):
        return plugins
    games = prefs.get("games", {})
    if not isinstance(games, dict):
        return plugins
    saved_plugins = games.get("plugins", {})
    if not isinstance(saved_plugins, dict):
        return plugins
    for plugin_id, enabled in saved_plugins.items():
        if isinstance(enabled, bool):
            plugins[str(plugin_id)] = enabled
    return plugins


_PLUGINS = _load_plugins()


def main() -> None:
    serve("game-mcp", "1.0", {
        "game_wiki6_start": {
            "spec": {
                "name": "game_wiki6_start",
                "description": "Wiki6ゲームの問題（スタート・ゴール）とルールを返す。",
                "inputSchema": {"type": "object", "properties": {}},
            },
            "handler": game_wiki6_start,
        },
        "game_wiki6_getlinks": {
            "spec": {
                "name": "game_wiki6_getlinks",
                "description": "日本語Wikipedia記事の本文リンク一覧を返す（脚注・ナビボックス除外）。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "word": {"type": "string", "description": "記事名（例: バナナ）"},
                    },
                    "required": ["word"],
                },
            },
            "handler": game_wiki6_getlinks,
        },
        "game_wordvec_race_start": {
            "spec": {
                "name": "game_wordvec_race_start",
                "description": (
                    "WordVecチキンレースを開始してお題語を返す。base 省略時はランダム。"
                    "【ルール】お題語を基準に、交互に単語を出し合う。"
                    "自分の手番では必ず submit を呼んで判定すること。"
                    "valid=true なら採用（前より遠ざかった）、valid=false なら出した本人の負け（前より近づいた）。"
                    "相手の手番でも submit を呼んで判定し、valid=false なら相手の負けを宣言する。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "base": {"type": "string", "description": "お題語（省略可）"},
                    },
                },
            },
            "handler": game_wordvec_race_start,
        },
        "game_wordvec_race_submit": {
            "spec": {
                "name": "game_wordvec_race_submit",
                "description": (
                    "チキンレースで単語を判定する。自他問わず単語が出るたびに必ず呼ぶこと。"
                    "valid=true: answer が last より start から遠い → 合法、ゲーム続行。"
                    "valid=false: answer が last より start に近い → その単語を出したプレイヤーの負け、ゲーム終了。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "start":  {"type": "string", "description": "お題の基準語"},
                        "last":   {"type": "string", "description": "直前に出た単語"},
                        "answer": {"type": "string", "description": "今回提出する単語"},
                    },
                    "required": ["start", "last", "answer"],
                },
            },
            "handler": game_wordvec_race_submit,
        },
        "game_wordvec_race_hint": {
            "spec": {
                "name": "game_wordvec_race_hint",
                "description": "word1 の周辺で start より遠い（goal に近い）単語を返す。負けた後の確認用。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "word1": {"type": "string", "description": "直前に出た単語"},
                        "start": {"type": "string", "description": "お題の基準語"},
                        "goal":  {"type": "string", "description": "反対方向の単語"},
                    },
                    "required": ["word1", "start", "goal"],
                },
            },
            "handler": game_wordvec_race_hint,
        },
        "game_wiki6_solve": {
            "spec": {
                "name": "game_wiki6_solve",
                "description": "PediaRouteで word1→word2 の最短経路を取得する（答え合わせ用）。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "word1": {"type": "string", "description": "スタート記事名"},
                        "word2": {"type": "string", "description": "ゴール記事名"},
                    },
                    "required": ["word1", "word2"],
                },
            },
            "handler": game_wiki6_solve,
        },
    })


if __name__ == "__main__":
    main()
