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
import shutil
import subprocess
import sys
import uuid
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
    # 元々の15語
    "エアコン", "図書館", "桜", "寿司", "新幹線",
    "将棋", "温泉", "台風", "選挙", "宇宙",
    "カレー", "富士山", "サッカー", "夏休み", "電車",
    # 食べ物
    "ラーメン", "すき焼き", "パン", "チョコレート", "アイスクリーム",
    "味噌汁", "焼き鳥", "納豆",
    # 自然
    "海", "山", "川", "森", "雪", "雨", "虹", "星", "月", "太陽", "火山", "滝", "砂漠", "氷河",
    # 動物
    "猫", "犬", "象", "ライオン", "パンダ", "ペンギン", "蝶", "鷹",
    # 建物・場所
    "神社", "城", "病院", "学校", "駅", "空港", "公園", "動物園", "美術館", "遊園地", "博物館", "灯台",
    # 乗り物
    "飛行機", "自転車", "船", "バス", "タクシー", "ロケット",
    # 技術・道具
    "コンピューター", "スマートフォン", "カメラ", "時計", "冷蔵庫", "テレビ", "ロボット", "鉛筆", "傘", "顕微鏡",
    # スポーツ・娯楽
    "野球", "バスケットボール", "水泳", "卓球", "映画", "音楽", "漫画", "ゲーム", "ダンス", "写真",
    # 季節・行事
    "花火", "クリスマス", "誕生日", "結婚式", "卒業式",
    # 仕事・社会
    "会社", "銀行", "警察", "消防", "医者", "先生", "農業", "漁業",
    # 自然科学
    "恐竜", "化石", "実験", "元素", "惑星", "銀河", "進化",
    # 感情・抽象
    "愛", "友情", "勇気", "平和", "自由", "幸福", "夢", "記憶",
    # 文化・芸術
    "歌舞伎", "茶道", "書道", "俳句", "落語", "浮世絵",
    # 体・健康
    "心臓", "筋肉", "睡眠", "運動", "栄養", "病気",
    # 衣類
    "着物", "靴", "帽子", "手袋", "眼鏡",
    # 家庭
    "台所", "洗濯機", "布団", "掃除機", "庭", "橋", "トンネル",
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


_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CPU_MODEL = os.environ.get("EHA_CPU_MODEL", "haiku")

_CPU_RULES = """あなたは単語連想ゲーム「WordVecレース」のCPU対戦相手。
ルール: お題となる基準語がある。プレイヤーと交互に単語を出す。
自分の番では「直前に出た単語よりも、基準語からベクトル距離が遠い」日本語の実在単語を1つ出す。
基準語に近い/同じだと負け。一気に遠くへ飛ばしすぎると後が続かず自滅しやすいので、着実に遠ざかるのが基本(読みは自由)。
返答は必ず単語1つだけ。ひらがな・カタカナ・漢字いずれかの実在語。説明・前置き・記号・引用符・メタ発言は一切書かない。
ツール（Bash/Read等）は絶対に使わない。求められているのは日本語の単語1語だけ。"""

_CPU_MOVE_COUNTS: dict[str, int] = {}


def _claude_env() -> dict[str, str]:
    env = {**os.environ}
    env["CLAUDE_CONFIG_DIR"] = os.environ.get("CLAUDE_CONFIG_DIR", "/data/.claude")
    return env


def _claude_bin() -> str:
    return shutil.which("claude") or "claude"


def _run_claude_once(cmd: list[str], *, timeout: int = 30) -> tuple[str | None, str]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=_SCRIPT_DIR,
            env=_claude_env(),
        )
    except Exception as e:
        return None, str(e)

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode == 0 and stdout:
        return stdout, ""

    reasons: list[str] = []
    if result.returncode != 0:
        reasons.append(f"exit {result.returncode}")
    if not stdout:
        reasons.append("empty output")
    if stderr:
        reasons.append(stderr)
    return None, "; ".join(reasons) if reasons else "claude call failed"


def _run_claude_with_retry(cmd: list[str], *, timeout: int = 30) -> tuple[str | None, str]:
    out, err = _run_claude_once(cmd, timeout=timeout)
    if out:
        return out, ""
    retry_out, retry_err = _run_claude_once(cmd, timeout=timeout)
    if retry_out:
        return retry_out, ""
    return None, retry_err or err or "claude call failed"


_CPU_STRIP_CHARS = '"' + "'" + "「」『』（）()[]{}<>〈〉《》,，。．・!?！？:：;；`´"
_CPU_PREFIX_CHARS = "「『（([{" + '"' + "'"
_CPU_SUFFIX_CHARS = "」』）)]}" + '"' + "'.,，。．・!?！？:：;；"

def _clean_cpu_word(raw: str) -> str:
    lines = raw.splitlines()
    token = lines[0].strip() if lines else raw.strip()
    if not token:
        return ""
    token = token.split()[0]
    token = token.strip().strip(_CPU_STRIP_CHARS)
    while token and token[0] in _CPU_PREFIX_CHARS:
        token = token[1:]
    while token and token[-1] in _CPU_SUFFIX_CHARS:
        token = token[:-1]
    return token.strip()


def _start_cpu_session(base_key: str, cpu_session_id: str) -> tuple[bool, str]:
    # --system-prompt はデフォルトのシステムプロンプトを完全に置き換える（--append-system-prompt は
    # 既定の"コーディング支援エージェント"人格に追記するだけになり、Bash承認待ち等のメタ発言を誘発した）。
    # --resume 側では system-prompt を渡さない: セッション作成時の値を引き継ぐ必要があり、
    # resume 時に再指定すると（--system-prompt/--append-system-prompt どちらでも）応答がハングする。
    cmd = [
        _claude_bin(),
        "-p",
        "--session-id",
        cpu_session_id,
        "--model",
        CPU_MODEL,
        "--system-prompt",
        _CPU_RULES,
        f"ゲーム開始。お題（基準語）は「{base_key}」。あなたはCPU側で後手。私が単語を出すたびに、基準語からより遠い実在語を1つだけ返す。準備できたら「OK」とだけ返して。",
    ]
    out, err = _run_claude_with_retry(cmd, timeout=30)
    if out:
        return True, out
    return False, err


def _ask_cpu_word(cpu_session_id: str, message: str) -> tuple[str | None, str]:
    cmd = [
        _claude_bin(),
        "-p",
        "--resume",
        cpu_session_id,
        "--model",
        CPU_MODEL,
        message,
    ]
    out, err = _run_claude_with_retry(cmd, timeout=30)
    if not out:
        return None, err
    word = _clean_cpu_word(out)
    return word or None, ""


def game_wordvec_race_start(args: dict[str, Any]):
    if not _PLUGINS.get("wordvec_race"):
        return _plugin_disabled_error("WordVecレース")
    try:
        kv = _get_kv()
        base = str(args.get("base") or "").strip() or random.choice(_RACE_BASES)
        mode = str(args.get("mode") or "human").strip() or "human"
        key = _lookup(kv, base)
        if key is None:
            return _json_error(f"「{base}」は語彙にありません")
        if mode not in {"human", "cpu"}:
            return _json_error("mode は human か cpu です")
        if mode == "cpu":
            cpu_session_id = str(uuid.uuid4())
            ok, _ = _start_cpu_session(key, cpu_session_id)
            if not ok:
                return _json_error("CPU起動に失敗")
            result = {
                "base": key,
                "mode": "cpu",
                "cpu_session_id": cpu_session_id,
                "message": (
                    f"お題は「{key}」。あなた先手。game_wordvec_race_cpu_move に start/last/answer/cpu_session_id を渡して1手ずつ進めて。"
                    "最初の手は last に base を入れて出す。"
                ),
            }
            return [text(json.dumps(result, ensure_ascii=False, indent=2))], False
        result = {
            "base": key,
            "message": f"お題は「{key}」です。より遠い単語を交互に出してください。前の単語より近づいたら負け。",
        }
        return [text(json.dumps(result, ensure_ascii=False, indent=2))], False
    except Exception as e:
        return _json_error(str(e))


def game_wordvec_race_cpu_move(args: dict[str, Any]):
    if not _PLUGINS.get("wordvec_race"):
        return _plugin_disabled_error("WordVecレース")
    cpu_session_id = str(args.get("cpu_session_id") or "").strip()
    if not cpu_session_id:
        return _json_error("cpu_session_id が空です")
    try:
        kv = _get_kv()
        start = str(args.get("start") or "").strip()
        last = str(args.get("last") or "").strip()
        answer = str(args.get("answer") or "").strip()
        move_count = int(args.get("move_count") or 0)
        for w, label in [(start, "start"), (last, "last"), (answer, "answer")]:
            if not w:
                return _json_error(f"{label} が空です")
        start_key = _lookup(kv, start)
        last_key = _lookup(kv, last)
        answer_key = _lookup(kv, answer)
        if start_key is None:
            return _json_error(f"「{start}」は語彙にありません")
        if last_key is None:
            return _json_error(f"「{last}」は語彙にありません")
        if answer_key is None:
            return _json_error(f"「{answer}」は語彙にありません")
        sim_last = float(kv.similarity(last_key, start_key))
        sim_answer = float(kv.similarity(answer_key, start_key))
        if sim_answer >= sim_last:
            _CPU_MOVE_COUNTS.pop(cpu_session_id, None)
            result = {
                "your_move": {"word": answer_key, "sim": round(sim_answer, 4)},
                "last_move": {"word": last_key, "sim": round(sim_last, 4)},
                "start": start_key,
                "game_over": True,
                "winner": "cpu",
                "reason": "あかねの手が前より近い",
                "message": "あかねの手が前より近かったのでCPUの勝ちです。結果を会話ルームに報告してください。",
            }
            return [text(json.dumps(result, ensure_ascii=False, indent=2))], False

        cpu_msg = (
            f"私は「{answer_key}」を出した（前手「{last_key}」より基準語「{start_key}」から遠い）。あなたの番。"
            f"基準語「{start_key}」からさらに遠い、実在する日本語の単語を1つだけ。単語のみ、説明・記号・引用符なし。"
        )
        cpu_word, _ = _ask_cpu_word(cpu_session_id, cpu_msg)
        if not cpu_word:
            _CPU_MOVE_COUNTS.pop(cpu_session_id, None)
            result = {
                "game_over": True,
                "winner": "aborted",
                "reason": "CPU応答に失敗",
                "message": "CPU応答に失敗したので対局を中断します。結果を会話ルームに報告してください。",
            }
            return [text(json.dumps(result, ensure_ascii=False, indent=2))], False
        cpu_key = _lookup(kv, cpu_word)
        if cpu_key is None:
            retry_msg = f"「{cpu_word}」は辞書に無い。実在する別の日本語の単語を1つだけ。"
            cpu_word, _ = _ask_cpu_word(cpu_session_id, retry_msg)
            if not cpu_word:
                _CPU_MOVE_COUNTS.pop(cpu_session_id, None)
                result = {
                    "game_over": True,
                    "winner": "aborted",
                    "reason": "CPU応答に失敗",
                }
                return [text(json.dumps(result, ensure_ascii=False, indent=2))], False
            cpu_key = _lookup(kv, cpu_word)
            if cpu_key is None:
                _CPU_MOVE_COUNTS.pop(cpu_session_id, None)
                result = {
                    "game_over": True,
                    "winner": "akane",
                    "reason": "CPUの手が語彙にありません",
                    "your_move": {"word": answer_key, "sim": round(sim_answer, 4)},
                    "cpu_move_raw": cpu_word,
                    "start": start_key,
                    "message": "CPUが語彙外の単語しか出せませんでした。あかねの勝ちです。結果を会話ルームに報告してください。",
                }
                return [text(json.dumps(result, ensure_ascii=False, indent=2))], False
        sim_cpu = float(kv.similarity(cpu_key, start_key))
        if sim_cpu >= sim_answer:
            _CPU_MOVE_COUNTS.pop(cpu_session_id, None)
            result = {
                "your_move": {"word": answer_key, "sim": round(sim_answer, 4)},
                "cpu_move": {"word": cpu_key, "sim": round(sim_cpu, 4)},
                "start": start_key,
                "game_over": True,
                "winner": "akane",
                "reason": "CPUの手が前より近い",
                "message": "CPUの手が前より近かったのであかねの勝ちです。結果を会話ルームに報告してください。",
            }
            return [text(json.dumps(result, ensure_ascii=False, indent=2))], False

        turn = max(_CPU_MOVE_COUNTS.get(cpu_session_id, 0) + 1, move_count)
        _CPU_MOVE_COUNTS[cpu_session_id] = turn
        if turn >= 16:
            _CPU_MOVE_COUNTS.pop(cpu_session_id, None)
            result = {
                "your_move": {"word": answer_key, "sim": round(sim_answer, 4)},
                "cpu_move": {"word": cpu_key, "sim": round(sim_cpu, 4)},
                "start": start_key,
                "next_last": cpu_key,
                "game_over": True,
                "winner": "draw",
                "reason": "手数上限",
                "message": "手数上限に達しました。結果を会話ルームに報告してください。",
            }
            return [text(json.dumps(result, ensure_ascii=False, indent=2))], False

        result = {
            "your_move": {"word": answer_key, "sim": round(sim_answer, 4)},
            "cpu_move": {"word": cpu_key, "sim": round(sim_cpu, 4)},
            "start": start_key,
            "next_last": cpu_key,
            "game_over": False,
            "message": (
                f"あなた: {answer_key}({sim_answer:.4f}) / CPU: {cpu_key}({sim_cpu:.4f})。"
                f"次はあなたの番。last={cpu_key} で cpu_move を続けて。"
            ),
        }
        return [text(json.dumps(result, ensure_ascii=False, indent=2))], False
    except Exception as e:
        return _json_error(str(e))


def game_wordvec_race_submit(args: dict[str, Any]):
    if not _PLUGINS.get("wordvec_race"):
        return _plugin_disabled_error("WordVecレース")
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
        return _plugin_disabled_error("WordVecレース")
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
                    "WordVecレースを開始してお題語を返す。base 省略時はランダム。"
                    "mode='cpu' でCPU戦（Haiku）を開始できる。"
                    "【ルール】お題語を基準に、交互に単語を出し合う。"
                    "自分の手番では必ず submit を呼んで判定すること。"
                    "valid=true なら採用（前より遠ざかった）、valid=false なら出した本人の負け（前より近づいた）。"
                    "相手の手番でも submit を呼んで判定し、valid=false なら相手の負けを宣言する。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "base": {"type": "string", "description": "お題語（省略可）"},
                        "mode": {"type": "string", "enum": ["human", "cpu"], "description": "human か cpu（既定: human）"},
                    },
                },
            },
            "handler": game_wordvec_race_start,
        },
        "game_wordvec_race_cpu_move": {
            "spec": {
                "name": "game_wordvec_race_cpu_move",
                "description": "WordVecレースのCPU戦を1手進める。あかねの手を判定し、CPUの手を返す。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string", "description": "お題の基準語"},
                        "last": {"type": "string", "description": "直前に出た単語"},
                        "answer": {"type": "string", "description": "今回のあかねの手"},
                        "cpu_session_id": {"type": "string", "description": "start で発行された CPU セッション ID"},
                        "move_count": {"type": "integer", "description": "この対局で呼んだ回数の目安（省略可）"},
                    },
                    "required": ["start", "last", "answer", "cpu_session_id"],
                },
            },
            "handler": game_wordvec_race_cpu_move,
        },
        "game_wordvec_race_submit": {
            "spec": {
                "name": "game_wordvec_race_submit",
                "description": (
                    "WordVecレースで単語を判定する。自他問わず単語が出るたびに必ず呼ぶこと。"
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
