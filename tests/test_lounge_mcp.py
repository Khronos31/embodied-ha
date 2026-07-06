import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "embodied_ha" / "lounge-mcp.py"


def load_lounge_mcp_module():
    module_name = "lounge_mcp_test"
    sys.modules.pop(module_name, None)
    pkg = str(ROOT / "embodied_ha")
    if pkg not in sys.path:
        sys.path.insert(0, pkg)
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LoungeReplyRootTests(unittest.TestCase):
    def setUp(self):
        self.mcp = load_lounge_mcp_module()

    def test_root_reply_comment_id_walks_nested_reply_to_root(self):
        parents = {
            "nested": "parent",
            "parent": "root",
            "root": None,
        }
        calls = []

        def fake_graphql(_query, variables):
            comment_id = variables["c"]
            calls.append(comment_id)
            parent = parents[comment_id]
            return {"node": {"replyTo": {"id": parent} if parent else None}}

        with mock.patch.object(self.mcp, "_graphql", side_effect=fake_graphql), mock.patch.object(self.mcp, "log") as log_mock:
            self.assertEqual(self.mcp._root_reply_comment_id("nested"), "root")

        self.assertEqual(calls, ["nested", "parent", "root"])
        log_mock.assert_called_once()
        self.assertIn("ネスト返信を検出しルートへ付け替えた", log_mock.call_args.args[0])

    def test_root_reply_comment_id_leaves_root_comment_unchanged(self):
        with mock.patch.object(self.mcp, "_graphql", return_value={"node": {"replyTo": None}}) as graphql_mock, mock.patch.object(self.mcp, "log") as log_mock:
            self.assertEqual(self.mcp._root_reply_comment_id("root"), "root")

        graphql_mock.assert_called_once()
        self.assertEqual(graphql_mock.call_args.args[1], {"c": "root"})
        log_mock.assert_not_called()

    def test_post_to_lounge_uses_resolved_reply_root_id(self):
        calls = []

        def fake_graphql(query, variables):
            calls.append(variables)
            if "query($c: ID!)" in query:
                parent = {"nested": "root", "root": None}[variables["c"]]
                return {"node": {"replyTo": {"id": parent} if parent else None}}
            return {"addDiscussionComment": {"comment": {"id": "new-comment", "url": "https://example.test/comment"}}}

        item = {
            "type": "comment",
            "body": "hello",
            "reply_to_discussion_id": "discussion",
            "reply_to_comment_id": "nested",
        }
        with mock.patch.object(self.mcp, "_graphql", side_effect=fake_graphql), mock.patch.object(self.mcp, "log"):
            result = self.mcp._post_to_lounge(item)

        self.assertEqual(result["comment_id"], "new-comment")
        self.assertEqual(calls[-1]["replyToId"], "root")

    def test_post_to_lounge_discussion_comment_does_not_resolve_reply_root(self):
        calls = []

        def fake_graphql(query, variables):
            calls.append((query, variables))
            return {"addDiscussionComment": {"comment": {"id": "new-comment", "url": "https://example.test/comment"}}}

        item = {
            "type": "comment",
            "body": "hello",
            "reply_to_discussion_id": "discussion",
            "reply_to_comment_id": None,
        }
        with mock.patch.object(self.mcp, "_graphql", side_effect=fake_graphql):
            result = self.mcp._post_to_lounge(item)

        self.assertEqual(result["comment_id"], "new-comment")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("replyToId", calls[0][1])


if __name__ == "__main__":
    unittest.main()
