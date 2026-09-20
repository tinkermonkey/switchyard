"""Tests for the reply_to_discussion / get_discussion_feedback MCP tools (#267)."""
import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_mcp_dir = os.path.join(_repo_root, 'mcp')
sys.path.insert(0, _repo_root)
sys.path.insert(0, _mcp_dir)
try:
    import server as mcp_server
finally:
    sys.path.remove(_mcp_dir)
    sys.path.remove(_repo_root)

from services.github_discussions import GitHubDiscussions, analyze_agent_discussion_state

SIG = "_Processed by the business_analyst agent_"


def _c(id, body, author, ts, replies=()):
    return {
        "id": id, "body": body, "createdAt": ts,
        "author": {"login": author} if author is not None else None,
        "replies": {"nodes": list(replies)},
    }


def _svc(**kw):
    svc = MagicMock()
    for k, v in kw.items():
        getattr(svc, k).side_effect = v if isinstance(v, Exception) else None
        if not isinstance(v, Exception):
            getattr(svc, k).return_value = v
    return svc


def _tree(comments, truncated=False, total=None):
    return {"comments": comments, "truncated": truncated,
            "total_comments": len(comments) if total is None else total}


class TestReplyToDiscussion:
    @pytest.mark.asyncio
    async def test_success(self):
        svc = _svc(add_discussion_comment="DC_1")
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.reply_to_discussion("D_1", "hello")
        assert r == {"posted": True, "comment_id": "DC_1"}
        svc.add_discussion_comment.assert_called_once_with("D_1", "hello", None, None, "unknown")

    @pytest.mark.asyncio
    async def test_threaded_reply_and_attribution(self):
        svc = _svc(add_discussion_comment="DC_2")
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.reply_to_discussion(
                "D_1", "hi", reply_to_id="DC_0", pipeline_run_id="run-1", repo="o/r")
        assert r["posted"] is True
        svc.add_discussion_comment.assert_called_once_with("D_1", "hi", "DC_0", "run-1", "o/r")

    @pytest.mark.asyncio
    async def test_service_returns_none(self):
        svc = _svc(add_discussion_comment=None)
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.reply_to_discussion("D_1", "hello")
        assert r["posted"] is False and r["reason"]

    @pytest.mark.asyncio
    async def test_service_raises(self):
        svc = _svc(add_discussion_comment=RuntimeError("boom"))
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.reply_to_discussion("D_1", "hello")
        assert r["posted"] is False and "boom" in r["reason"]

    @pytest.mark.asyncio
    async def test_client_factory_raises(self):
        with patch.object(mcp_server, "_discussions_client", side_effect=RuntimeError("no client")):
            r = await mcp_server.reply_to_discussion("D_1", "hello")
        assert r["posted"] is False and "no client" in r["reason"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", [("", "x"), ("  ", "x"), ("D_1", ""), ("D_1", "   ")])
    async def test_empty_inputs_do_not_call_service(self, args):
        svc = _svc(add_discussion_comment="DC_1")
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.reply_to_discussion(*args)
        assert r["posted"] is False
        svc.add_discussion_comment.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_blank_reply_to_id_is_rejected_not_downgraded(self, bad):
        svc = _svc(add_discussion_comment="DC_1")
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.reply_to_discussion("D_1", "hi", reply_to_id=bad)
        assert r["posted"] is False and "reply_to_id" in r["reason"]
        svc.add_discussion_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_string_inputs_do_not_raise(self):
        r = await mcp_server.reply_to_discussion(None, 5)
        assert r["posted"] is False

    @pytest.mark.asyncio
    async def test_oversized_body_is_split_and_only_first_part_is_threaded(self):
        svc = MagicMock()
        svc.add_discussion_comment.side_effect = ["DC_a", "DC_b", "DC_c", "DC_d"]
        body = ("line\n" * 30000)  # 150k chars
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.reply_to_discussion("D_1", body, reply_to_id="DC_0")
        calls = svc.add_discussion_comment.call_args_list
        assert r["posted"] is True and r["comment_id"] == "DC_a" and r["parts"] == len(calls) > 1
        assert calls[0].args[2] == "DC_0"
        assert all(c.args[2] is None for c in calls[1:])
        assert all(len(c.args[1]) <= 65000 for c in calls)

    @pytest.mark.asyncio
    async def test_partial_post_is_not_reported_as_posted(self):
        svc = MagicMock()
        svc.add_discussion_comment.side_effect = ["DC_a", None]
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.reply_to_discussion("D_1", "line\n" * 30000)
        assert r["posted"] is False and r["partial"] is True
        assert r["comment_id"] == "DC_a" and r["parts_posted"] == 1

    @pytest.mark.asyncio
    async def test_exception_after_first_part_reports_partial(self):
        svc = MagicMock()
        svc.add_discussion_comment.side_effect = ["DC_a", RuntimeError("rate limited")]
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.reply_to_discussion("D_1", "line\n" * 30000)
        assert r["posted"] is False and r["partial"] is True
        assert r["comment_id"] == "DC_a" and "rate limited" in r["reason"]


class TestGetDiscussionFeedback:
    @pytest.mark.asyncio
    async def test_tree_without_agent(self):
        comments = [_c("C1", "hi", "alice", "2026-01-01T00:00:00Z")]
        svc = _svc(fetch_discussion_comment_tree=_tree(comments))
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.get_discussion_feedback("D_1")
        assert r["success"] is True and r["comments"] == comments
        assert r["discussion_id"] == "D_1" and r["truncated"] is False
        assert "agent" not in r
        svc.fetch_discussion_comment_tree.assert_called_once_with("D_1")

    @pytest.mark.asyncio
    async def test_agent_still_last_word(self):
        tree = [_c("C1", f"out\n{SIG}", "orchestrator-bot", "2026-01-01T00:00:00Z",
                   replies=[_c("R1", "bot note", "github-actions[bot]", "2026-01-02T00:00:00Z")])]
        svc = _svc(fetch_discussion_comment_tree=_tree(tree))
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.get_discussion_feedback("D_1", "business_analyst")
        a = r["agent"]
        assert a["name"] == "business_analyst"
        assert a["has_posted"] is True
        assert a["superseded"] is False
        assert a["still_last_word"] is True
        assert a["last_agent_at"] == "2026-01-01T00:00:00Z"
        assert a["last_human_at"] is None

    @pytest.mark.asyncio
    async def test_agent_superseded_by_nested_human_reply(self):
        tree = [_c("C1", f"out\n{SIG}", "orchestrator-bot", "2026-01-01T00:00:00Z",
                   replies=[_c("R1", "please change X", "alice", "2026-01-03T00:00:00Z")])]
        svc = _svc(fetch_discussion_comment_tree=_tree(tree))
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.get_discussion_feedback("D_1", "business_analyst")
        assert r["agent"]["superseded"] is True and r["agent"]["still_last_word"] is False
        assert r["agent"]["last_human_at"] == "2026-01-03T00:00:00Z"

    @pytest.mark.asyncio
    async def test_agent_never_posted(self):
        svc = _svc(fetch_discussion_comment_tree=_tree([_c("C1", "hi", "alice", "2026-01-01T00:00:00Z")]))
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.get_discussion_feedback("D_1", "business_analyst")
        assert r["agent"]["has_posted"] is False and r["agent"]["still_last_word"] is False

    @pytest.mark.asyncio
    async def test_truncation_is_reported(self):
        svc = _svc(fetch_discussion_comment_tree=_tree([], truncated=True, total=250))
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.get_discussion_feedback("D_1")
        assert r["truncated"] is True and r["total_comments"] == 250

    @pytest.mark.asyncio
    async def test_fetch_exception_returns_error_dict(self):
        svc = _svc(fetch_discussion_comment_tree=RuntimeError("nope"))
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.get_discussion_feedback("D_1", "business_analyst")
        assert r["success"] is False and "nope" in r["error"]

    @pytest.mark.asyncio
    async def test_failed_fetch_is_not_reported_as_empty_thread(self):
        svc = _svc(fetch_discussion_comment_tree=None)
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.get_discussion_feedback("D_1")
        assert r["success"] is False

    @pytest.mark.asyncio
    async def test_analysis_error_is_distinguished_from_fetch_error(self):
        svc = _svc(fetch_discussion_comment_tree=_tree(["not-a-dict"]))
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.get_discussion_feedback("D_1", "business_analyst")
        assert r["success"] is False and r["error"].startswith("analysis failed")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["", "  ", None])
    async def test_bad_id(self, bad):
        r = await mcp_server.get_discussion_feedback(bad)
        assert r["success"] is False


class TestAnalyzeAgentDiscussionState:
    def test_empty(self):
        s = analyze_agent_discussion_state([], "business_analyst")
        assert s["agent_posted"] is False and s["still_last_word"] is False
        assert s["last_agent_at"] is None and s["last_human_at"] is None

    def test_order_is_by_timestamp_not_position(self):
        # The human reply is listed after the agent comment but is older.
        comments = [
            _c("C1", f"out\n{SIG}", "orchestrator-bot", "2026-01-05T00:00:00Z",
               replies=[_c("R1", "old feedback", "alice", "2026-01-01T00:00:00Z")]),
        ]
        assert analyze_agent_discussion_state(comments, "business_analyst")["still_last_word"] is True

    def test_later_toplevel_comment_sorts_after_earlier_nested_reply(self):
        comments = [
            _c("C1", f"out\n{SIG}", "orchestrator-bot", "2026-01-01T00:00:00Z",
               replies=[_c("R1", "late nested", "alice", "2026-01-09T00:00:00Z")]),
            _c("C2", f"newer out\n{SIG}", "orchestrator-bot", "2026-01-10T00:00:00Z"),
        ]
        assert analyze_agent_discussion_state(comments, "business_analyst")["still_last_word"] is True

    @pytest.mark.parametrize("author,is_human", [
        ("alice", True),
        ("orchestrator-bot", False),
        ("github-actions[bot]", False),
        (None, False),
    ])
    def test_author_classification_after_agent(self, author, is_human):
        comments = [
            _c("C1", f"out\n{SIG}", "orchestrator-bot", "2026-01-01T00:00:00Z"),
            _c("C2", "later", author, "2026-01-02T00:00:00Z"),
        ]
        s = analyze_agent_discussion_state(comments, "business_analyst")
        assert s["superseded"] is is_human

    def test_null_author_on_nested_reply_is_a_bot(self):
        comments = [_c("C1", f"out\n{SIG}", "orchestrator-bot", "2026-01-01T00:00:00Z",
                       replies=[_c("R1", "ghost", None, "2026-01-02T00:00:00Z")])]
        assert analyze_agent_discussion_state(comments, "business_analyst")["still_last_word"] is True

    def test_human_quoting_signature_counts_as_agent_message(self):
        comments = [
            _c("C1", f"out\n{SIG}", "orchestrator-bot", "2026-01-01T00:00:00Z"),
            _c("C2", f"> {SIG}\nquoted", "alice", "2026-01-02T00:00:00Z"),
        ]
        assert analyze_agent_discussion_state(comments, "business_analyst")["still_last_word"] is True

    def test_null_fields_tolerated(self):
        comments = [{"body": None, "author": None, "createdAt": None, "replies": None}]
        assert analyze_agent_discussion_state(comments, "business_analyst")["agent_posted"] is False


class TestFetchDiscussionCommentTree:
    def _svc(self, result):
        svc = GitHubDiscussions.__new__(GitHubDiscussions)
        svc._execute_graphql = lambda *a, **k: result
        return svc

    @pytest.mark.parametrize("result", [None, {}, {"node": None}, {"node": {}}])
    def test_failure_shapes_return_none(self, result):
        svc = self._svc(result)
        assert svc.fetch_discussion_comment_tree("D_bad") is None
        assert svc.fetch_discussion_comments("", "", "D_bad") is None
        assert svc.get_discussion_comments("", "", "D_bad") == []

    def test_success(self):
        nodes = [{"id": "C1", "replies": {"totalCount": 0, "nodes": []}}]
        svc = self._svc({"node": {"comments": {"totalCount": 1, "nodes": nodes}}})
        assert svc.fetch_discussion_comment_tree("D_1") == {
            "comments": nodes, "truncated": False, "total_comments": 1}
        assert svc.get_discussion_comments("", "", "D_1") == nodes

    def test_truncated_by_top_level_count(self):
        svc = self._svc({"node": {"comments": {"totalCount": 101, "nodes": [{"id": "C1"}]}}})
        assert svc.fetch_discussion_comment_tree("D_1")["truncated"] is True

    def test_truncated_by_reply_count(self):
        nodes = [{"id": "C1", "replies": {"totalCount": 51, "nodes": [{"id": "R1"}]}}]
        svc = self._svc({"node": {"comments": {"totalCount": 1, "nodes": nodes}}})
        assert svc.fetch_discussion_comment_tree("D_1")["truncated"] is True

    def test_query_uses_newest_window(self):
        seen = {}
        svc = GitHubDiscussions.__new__(GitHubDiscussions)
        svc._execute_graphql = lambda q, v: seen.setdefault("q", q) and {"node": None}
        svc.fetch_discussion_comment_tree("D_1")
        assert "comments(last: 100)" in seen["q"] and "replies(last: 50)" in seen["q"]


class TestHasAgentProcessedDiscussion:
    """The refactored method: each outcome, on its own."""

    def _run(self, graphql):
        from services.github_integration import GitHubIntegration
        gi = GitHubIntegration.__new__(GitHubIntegration)
        app = MagicMock()
        app.graphql_request.side_effect = graphql if isinstance(graphql, Exception) else None
        if not isinstance(graphql, Exception):
            app.graphql_request.return_value = graphql
        with patch("services.github_app.github_app", app):
            return asyncio.run(gi.has_agent_processed_discussion("D_1", "business_analyst"))

    @staticmethod
    def _wrap(comments):
        return {"node": {"comments": {"nodes": comments}}}

    def test_processed_and_last_word(self):
        assert self._run(self._wrap([_c("C1", SIG, "orchestrator-bot", "2026-01-01T00:00:00Z")])) is True

    def test_never_processed(self):
        assert self._run(self._wrap([_c("C1", "hi", "alice", "2026-01-01T00:00:00Z")])) is False

    def test_superseded(self):
        comments = [_c("C1", SIG, "orchestrator-bot", "2026-01-01T00:00:00Z"),
                    _c("C2", "more", "alice", "2026-01-02T00:00:00Z")]
        assert self._run(self._wrap(comments)) is False

    def test_null_author_after_agent_does_not_supersede(self):
        comments = [_c("C1", SIG, "orchestrator-bot", "2026-01-01T00:00:00Z"),
                    _c("C2", "ghost", None, "2026-01-02T00:00:00Z")]
        assert self._run(self._wrap(comments)) is True

    @pytest.mark.parametrize("graphql", [None, {}, {"node": None}, RuntimeError("down")])
    def test_errors_return_false(self, graphql):
        assert self._run(graphql) is False
