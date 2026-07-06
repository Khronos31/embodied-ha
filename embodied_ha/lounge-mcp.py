#!/usr/bin/env python3
"""AI Lounge GitHub Discussions MCP server.

Reads lifemate-ai/ai-lounge discussions and queues Akane's proposed posts for
human approval. Approved items are posted with a GitHub App installation token.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mcp_lib import log, serve, text

OWNER = "lifemate-ai"
REPO = "ai-lounge"
REPO_NODE_ID = "R_kgDOR_xpfw"
CATEGORY_GENERAL_NODE_ID = "DIC_kwDOR_xpf84C6mmP"  # General (:speech_balloon:)
GRAPHQL_URL = "https://api.github.com/graphql"
PEM_PATH = "/config/embodied-ha/github_app.pem"
_TOKEN_LOCK = threading.Lock()
_QUEUE_LOCK = threading.Lock()
_TOKEN_CACHE: dict[str, Any] = {"token": "", "expires_at": 0.0}
_MAX_REPLY_ROOT_HOPS = 10


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _b64url(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _data_dir() -> str:
    return _clean(os.environ.get("EHA_DATA_DIR")) or "/config/embodied-ha"


def _log_dir() -> str:
    return _clean(os.environ.get("EHA_LOG_DIR")) or os.path.join(_data_dir(), "log")


def queue_path() -> str:
    return os.path.join(_log_dir(), "ai_lounge_queue.jsonl")


def log_path() -> str:
    return os.path.join(_log_dir(), "ai_lounge_log.jsonl")


def prefs_path() -> str:
    return _clean(os.environ.get("EHA_PREFS_FILE")) or os.path.join(_data_dir(), "preferences.json")


def _load_prefs() -> dict[str, Any]:
    try:
        with open(prefs_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _lounge_prefs() -> dict[str, Any]:
    data = _load_prefs().get("ai_lounge", {})
    return data if isinstance(data, dict) else {}


def _app_credentials() -> tuple[str, str]:
    prefs = _lounge_prefs()
    app_id = _clean(os.environ.get("LOUNGE_APP_ID")) or _clean(prefs.get("app_id"))
    installation_id = _clean(os.environ.get("LOUNGE_INSTALLATION_ID")) or _clean(prefs.get("installation_id"))
    if not os.path.exists(PEM_PATH):
        raise RuntimeError(f"GitHub App PEM がありません: {PEM_PATH}")
    if not app_id:
        raise RuntimeError("LOUNGE_APP_ID が未設定です")
    if not installation_id:
        raise RuntimeError("LOUNGE_INSTALLATION_ID が未設定です")
    return app_id, installation_id


def _make_jwt(app_id: str, pem_path: str = PEM_PATH) -> str:
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")))
    payload = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}, separators=(",", ":")))
    msg = f"{header}.{payload}".encode()
    tmpfile = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as f:
            f.write(msg)
            tmpfile = f.name
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", pem_path, tmpfile],
            capture_output=True,
            check=True,
        )
        sig = _b64url(result.stdout)
    finally:
        if tmpfile:
            try:
                os.unlink(tmpfile)
            except FileNotFoundError:
                pass
    return f"{header}.{payload}.{sig}"


def _request_json(url: str, payload: dict[str, Any] | None, headers: dict[str, str], *, method: str = "POST") -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as res:
            raw = res.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub HTTP {exc.code}: {detail[:800]}") from exc
    except URLError as exc:
        raise RuntimeError(f"GitHub network error: {getattr(exc, 'reason', exc)}") from exc
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"GitHub returned invalid JSON: {raw[:400]}") from exc
    if isinstance(data, dict) and data.get("errors"):
        raise RuntimeError(json.dumps(data.get("errors"), ensure_ascii=False))
    return data if isinstance(data, dict) else {"data": data}


def _installation_token() -> str:
    with _TOKEN_LOCK:
        now = time.time()
        if _TOKEN_CACHE.get("token") and now < float(_TOKEN_CACHE.get("expires_at") or 0) - 120:
            return str(_TOKEN_CACHE["token"])

        app_id, installation_id = _app_credentials()
        jwt = _make_jwt(app_id, PEM_PATH)
        data = _request_json(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            {},
            {
                "Authorization": f"Bearer {jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        token = _clean(data.get("token"))
        if not token:
            raise RuntimeError("installation access token が取得できませんでした")
        expires_at = 0.0
        try:
            expires_at = datetime.fromisoformat(_clean(data.get("expires_at")).replace("Z", "+00:00")).timestamp()
        except Exception:
            expires_at = now + 3600
        _TOKEN_CACHE.update({"token": token, "expires_at": expires_at})
        return token


def _graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    token = _installation_token()
    data = _request_json(
        GRAPHQL_URL,
        {"query": query, "variables": variables},
        {
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    return data.get("data", data)


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        rows.append(item)
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return rows


def _write_jsonl_atomic(path: str, rows: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or None, prefix=".tmp_ai_lounge_", suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _append_jsonl(path: str, row: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _json_content(data: Any) -> list[dict[str, str]]:
    return [text(json.dumps(data, ensure_ascii=False, indent=2))]


def _json_error(message: str) -> tuple[list[dict[str, str]], bool]:
    return _json_content({"error": message}), True


def read_discussions(count: int = 10) -> dict[str, Any]:
    count = max(1, min(int(count or 10), 50))
    query = """
query($owner: String!, $repo: String!, $first: Int!) {
  repository(owner: $owner, name: $repo) {
    discussions(first: $first, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number
        title
        url
        updatedAt
        author { login }
        comments { totalCount }
      }
    }
  }
}
"""
    return _graphql(query, {"owner": OWNER, "repo": REPO, "first": count})


def read_discussion(number: int) -> dict[str, Any]:
    query = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    discussion(number: $number) {
      id
      number
      title
      body
      url
      createdAt
      updatedAt
      author { login }
      comments(first: 100) {
        nodes {
          id
          body
          url
          createdAt
          author { login }
          replies(first: 20) {
            nodes {
              id
              body
              url
              createdAt
              author { login }
            }
          }
        }
      }
    }
  }
}
"""
    return _graphql(query, {"owner": OWNER, "repo": REPO, "number": int(number)})


def pending_queue() -> list[dict[str, Any]]:
    return [item for item in _read_jsonl(queue_path()) if item.get("status") == "pending"]


def resolved_log(limit: int = 20) -> list[dict[str, Any]]:
    try:
        limit = max(1, min(int(limit or 20), 200))
    except Exception:
        limit = 20
    return _read_jsonl(log_path())[-limit:]


def _first_discussion_id() -> str:
    data = read_discussions(1)
    nodes = (((data.get("repository") or {}).get("discussions") or {}).get("nodes") or [])
    if not nodes:
        raise RuntimeError("AI Lounge に投稿先ディスカッションが見つかりません")
    return _clean(nodes[0].get("id"))


def _root_reply_comment_id(comment_id: str) -> str:
    original = _clean(comment_id)
    if not original:
        return original

    query = """
query($c: ID!) {
  node(id: $c) {
    ... on DiscussionComment {
      replyTo { id }
    }
  }
}
"""
    current = original
    seen: set[str] = set()
    for _ in range(_MAX_REPLY_ROOT_HOPS):
        if current in seen:
            log(f"[lounge] replyTo chain cycle detected while resolving root: original={original} current={current}")
            return current
        seen.add(current)

        data = _graphql(query, {"c": current})
        node = data.get("node") or {}
        reply_to = node.get("replyTo") or {}
        parent_id = _clean(reply_to.get("id")) or None
        if not parent_id:
            if current != original:
                log(f"[lounge] ネスト返信を検出しルートへ付け替えた: 元ID={original}→新ID={current}")
            return current
        current = parent_id

    log(f"[lounge] replyTo chain exceeded {_MAX_REPLY_ROOT_HOPS} hops while resolving root: original={original} current={current}")
    return current


def _post_to_lounge(item: dict[str, Any]) -> dict[str, Any]:
    body = _clean(item.get("body"))
    if not body:
        raise RuntimeError("投稿本文が空です")
    post_type = _clean(item.get("type")) or "comment"

    if post_type == "new_discussion":
        title = _clean(item.get("title"))
        if not title:
            raise RuntimeError("new_discussion のとき title は必須です")
        mutation = """
mutation($repositoryId: ID!, $categoryId: ID!, $title: String!, $body: String!) {
  createDiscussion(input: {repositoryId: $repositoryId, categoryId: $categoryId, title: $title, body: $body}) {
    discussion { id number url }
  }
}
"""
        data = _graphql(mutation, {
            "repositoryId": REPO_NODE_ID,
            "categoryId": CATEGORY_GENERAL_NODE_ID,
            "title": title,
            "body": body,
        })
        discussion = ((data.get("createDiscussion") or {}).get("discussion") or {})
        return {"discussion_id": discussion.get("id"), "number": discussion.get("number"), "url": discussion.get("url")}

    else:  # comment
        discussion_id = _clean(item.get("reply_to_discussion_id"))
        if not discussion_id:
            raise RuntimeError("comment のとき reply_to_discussion_id は必須です")
        reply_to_comment_id = _clean(item.get("reply_to_comment_id")) or None
        if reply_to_comment_id:
            reply_to_comment_id = _root_reply_comment_id(reply_to_comment_id)
            mutation = """
mutation($discussionId: ID!, $body: String!, $replyToId: ID!) {
  addDiscussionComment(input: {discussionId: $discussionId, body: $body, replyToId: $replyToId}) {
    comment { id url }
  }
}
"""
            data = _graphql(mutation, {"discussionId": discussion_id, "body": body, "replyToId": reply_to_comment_id})
        else:
            mutation = """
mutation($discussionId: ID!, $body: String!) {
  addDiscussionComment(input: {discussionId: $discussionId, body: $body}) {
    comment { id url }
  }
}
"""
            data = _graphql(mutation, {"discussionId": discussion_id, "body": body})
        comment = ((data.get("addDiscussionComment") or {}).get("comment") or {})
        return {"comment_id": comment.get("id"), "url": comment.get("url"), "discussion_id": discussion_id}


def _resolve_queue_item(item_id: str, status: str, *, reason: str | None = None, post: bool = False) -> dict[str, Any]:
    with _QUEUE_LOCK:
        rows = _read_jsonl(queue_path())
        target: dict[str, Any] | None = None
        for row in rows:
            if row.get("id") == item_id:
                target = row
                break
        if target is None:
            raise RuntimeError("queue item not found")
        if target.get("status") != "pending":
            raise RuntimeError(f"queue item is already {target.get('status')}")

        post_result: dict[str, Any] | None = None
        if post:
            post_result = _post_to_lounge(target)

        target["status"] = status
        target["rejection_reason"] = reason if status == "rejected" else None
        target["resolved_at"] = _now_iso()
        if post_result:
            target["post_result"] = post_result
        _write_jsonl_atomic(queue_path(), rows)
        log_entry = dict(target)
        _append_jsonl(log_path(), log_entry)
        return log_entry


def approve_queue_item(item_id: str) -> dict[str, Any]:
    return _resolve_queue_item(item_id, "approved", post=True)


def reject_queue_item(item_id: str, reason: str | None = None) -> dict[str, Any]:
    return _resolve_queue_item(item_id, "rejected", reason=reason or None, post=False)


def enqueue_post(args: dict[str, Any]) -> dict[str, Any]:
    post_type = _clean(args.get("type")) or "comment"
    if post_type not in {"new_discussion", "comment"}:
        raise ValueError("type は new_discussion または comment です")
    body = _clean(args.get("body"))
    if not body:
        raise ValueError("body は必須です")
    if post_type == "new_discussion" and not _clean(args.get("title")):
        raise ValueError("new_discussion のとき title は必須です")
    if post_type == "comment" and not _clean(args.get("reply_to_discussion_id")):
        raise ValueError("comment のとき reply_to_discussion_id は必須です")

    preview_raw = _clean(args.get("reply_to_preview") or "")
    item = {
        "id": str(uuid.uuid4()),
        "created_at": _now_iso(),
        "type": post_type,
        "title": _clean(args.get("title")) or None,
        "reply_to_url": _clean(args.get("reply_to_url")),
        "reply_to_discussion_id": _clean(args.get("reply_to_discussion_id")),
        "reply_to_comment_id": _clean(args.get("reply_to_comment_id")) or None,
        "reply_to_preview": preview_raw[:100] if preview_raw else None,
        "body": body,
        "status": "pending",
        "rejection_reason": None,
        "resolved_at": None,
    }
    with _QUEUE_LOCK:
        _append_jsonl(queue_path(), item)

    if bool(_lounge_prefs().get("auto_approve")):
        log("AI Lounge auto_approve=true; approving queued post")
        return approve_queue_item(item["id"])
    return item


def read_lounge_discussions(args: dict[str, Any]):
    try:
        return _json_content(read_discussions(int(args.get("count") or 10)))
    except Exception as exc:
        log(f"[lounge] read_lounge_discussions failed: {exc}")
        return _json_error(str(exc))


def read_lounge_discussion(args: dict[str, Any]):
    try:
        number = int(args.get("number") or 0)
        if not number:
            return _json_error("number は必須です")
        return _json_content(read_discussion(number))
    except Exception as exc:
        log(f"[lounge] read_lounge_discussion failed: {exc}")
        return _json_error(str(exc))


def enqueue_lounge_post(args: dict[str, Any]):
    try:
        return _json_content(enqueue_post(args))
    except Exception as exc:
        log(f"[lounge] enqueue_lounge_post failed: {exc}")
        return _json_error(str(exc))


def read_lounge_queue(args: dict[str, Any]):
    return _json_content(pending_queue())


def read_lounge_log(args: dict[str, Any]):
    return _json_content(resolved_log(int(args.get("limit") or 20)))


def main() -> None:
    serve("lounge", "1.0", {
        "read_lounge_discussions": {
            "spec": {
                "name": "read_lounge_discussions",
                "description": "AI Lounge (lifemate-ai/ai-lounge) の最新Discussion一覧を読む。番号・タイトル・更新日時・コメント数のみ返す軽量版。気になるDiscussionは read_lounge_discussion で詳細を読むこと。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer", "description": "取得件数（デフォルト10、最大50）"},
                    },
                },
            },
            "handler": read_lounge_discussions,
        },
        "read_lounge_discussion": {
            "spec": {
                "name": "read_lounge_discussion",
                "description": "AI Lounge の特定Discussionを番号で開く。本文・コメント全件（最大100件）・返信も取得する。返信したいコメントのidもここで得られる。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "number": {"type": "integer", "description": "DiscussionのURLにある番号（例: /discussions/41 なら 41）"},
                    },
                    "required": ["number"],
                },
            },
            "handler": read_lounge_discussion,
        },
        "enqueue_lounge_post": {
            "spec": {
                "name": "enqueue_lounge_post",
                "description": "AI Lounge に投稿したい内容を承認キューへ積む。実際の投稿は承認後に行う。new_discussion なら title 必須・reply_to_discussion_id 不要。comment なら reply_to_discussion_id 必須。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["new_discussion", "comment"]},
                        "title": {"type": "string", "description": "投稿タイトル（new_discussion のとき必須）"},
                        "body": {"type": "string", "description": "投稿本文"},
                        "reply_to_url": {"type": "string", "description": "返信先URL（Web UIプレビュー表示用）"},
                        "reply_to_discussion_id": {"type": "string", "description": "返信先DiscussionのGraphQL node id（comment のとき必須）"},
                        "reply_to_comment_id": {"type": "string", "description": "返信先コメントのGraphQL node id（特定コメントへの返信の場合）"},
                        "reply_to_preview": {"type": "string", "description": "返信先本文冒頭（プレビュー用）"},
                    },
                    "required": ["type", "body"],
                },
            },
            "handler": enqueue_lounge_post,
        },
        "read_lounge_queue": {
            "spec": {
                "name": "read_lounge_queue",
                "description": "AI Lounge 投稿承認キューの pending アイテムを読む。",
                "inputSchema": {"type": "object", "properties": {}},
            },
            "handler": read_lounge_queue,
        },
        "read_lounge_log": {
            "spec": {
                "name": "read_lounge_log",
                "description": "AI Lounge 投稿の承認/拒否済みログを読む。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "返す件数（デフォルト20）"},
                    },
                },
            },
            "handler": read_lounge_log,
        },
    })


if __name__ == "__main__":
    main()
