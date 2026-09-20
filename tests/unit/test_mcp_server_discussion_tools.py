"""Tests for the reply_to_discussion / get_discussion_feedback MCP tools (#267)."""
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

SIG = "_Processed by the business_analyst agent_"


def _c(id, body, author, ts, replies=()):
    return {
        "id": id, "body": body, "createdAt": ts,
        "author": {"login": author},
        "replies": {"nodes": list(replies)},
    }


def _svc(**kw):
    svc = MagicMock()
    for k, v in kw.items():
        getattr(svc, k).side_effect = v if isinstance(v, Exception) else None
        if not isinstance(v, Exception):
            getattr(svc, k).return_value = v
    return svc


class TestReplyToDiscussion:
    @pytest.mark.asyncio
    async def test_success(self):
        svc = _svc(add_discussion_comment="DC_1")
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.reply_to_discussion("D_1", "hello")
        assert r == {"posted": True, "comment_id": "DC_1"}
        svc.add_discussion_comment.assert_called_once_with("D_1", "hello", None)

    @pytest.mark.asyncio
    async def test_threaded_reply_passes_reply_to_id(self):
        svc = _svc(add_discussion_comment="DC_2")
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.reply_to_discussion("D_1", "hi", reply_to_id="DC_0")
        assert r["posted"] is True
        svc.add_discussion_comment.assert_called_once_with("D_1", "hi", "DC_0")

    @pytest.mark.asyncio
    async def test_service_returns_none(self):
        svc = _svc(add_discussion_comment=None)
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.reply_to_discussion("D_1", "hello")
        assert r["posted"] is False and r["reason"]

    @pytest.mark.asyncio
    async def test_exception_never_raises(self):
        svc = _svc(add_discussion_comment=RuntimeError("boom"))
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.reply_to_discussion("D_1", "hello")
        assert r["posted"] is False and "boom" in r["reason"]

    @pytest.mark.asyncio
    async def test_client_construction_failure_never_raises(self):
        with patch.object(mcp_server, "_discussions_client", side_effect=RuntimeError("init")):
            r = await mcp_server.reply_to_discussion("D_1", "hello")
        assert r["posted"] is False and "init" in r["reason"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("did,body", [("", "x"), ("  ", "x"), ("D_1", ""), ("D_1", "   ")])
    async def test_empty_inputs_rejected_without_calling_service(self, did, body):
        with patch.object(mcp_server, "_discussions_client") as factory:
            r = await mcp_server.reply_to_discussion(did, body)
        assert r["posted"] is False
        factory.assert_not_called()


class TestGetDiscussionFeedback:
    @pytest.mark.asyncio
    async def test_returns_tree_without_agent(self):
        tree = [_c("1", "q", "alice", "2026-01-01T00:00:00Z")]
        svc = _svc(fetch_discussion_comments=tree)
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.get_discussion_feedback("D_1")
        assert r["success"] is True and r["comments"] == tree
        assert "agent" not in r

    @pytest.mark.asyncio
    async def test_agent_still_last_word(self):
        tree = [
            _c("1", "q", "alice", "2026-01-01T00:00:00Z"),
            _c("2", f"answer\n{SIG}", "orchestrator-bot", "2026-01-01T01:00:00Z",
               replies=[_c("3", "auto", "github-actions[bot]", "2026-01-01T02:00:00Z")]),
        ]
        svc = _svc(fetch_discussion_comments=tree)
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.get_discussion_feedback("D_1", "business_analyst")
        a = r["agent"]
        assert a["has_posted"] and a["still_last_word"] and not a["superseded"]

    @pytest.mark.asyncio
    async def test_agent_superseded_by_nested_human_reply(self):
        tree = [
            _c("2", f"answer\n{SIG}", "orchestrator-bot", "2026-01-01T01:00:00Z",
               replies=[_c("3", "actually no", "alice", "2026-01-01T03:00:00Z")]),
        ]
        svc = _svc(fetch_discussion_comments=tree)
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.get_discussion_feedback("D_1", "business_analyst")
        a = r["agent"]
        assert a["superseded"] and not a["still_last_word"]
        assert a["last_human_at"] == "2026-01-01T03:00:00Z"

    @pytest.mark.asyncio
    async def test_agent_never_posted(self):
        tree = [_c("1", "q", "alice", "2026-01-01T00:00:00Z")]
        svc = _svc(fetch_discussion_comments=tree)
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.get_discussion_feedback("D_1", "business_analyst")
        a = r["agent"]
        assert not a["has_posted"] and not a["still_last_word"] and not a["superseded"]

    @pytest.mark.asyncio
    async def test_matches_has_agent_processed_discussion(self):
        """The shared helper must give the same answer as the original method."""
        from services.github_integration import GitHubIntegration
        cases = [
            [_c("1", "q", "alice", "2026-01-01T00:00:00Z")],
            [_c("2", SIG, "orchestrator-bot", "2026-01-01T01:00:00Z")],
            [_c("2", SIG, "orchestrator-bot", "2026-01-01T01:00:00Z",
                replies=[_c("3", "hm", "bob", "2026-01-01T02:00:00Z")])],
        ]
        for tree in cases:
            svc = _svc(fetch_discussion_comments=tree)
            with patch.object(mcp_server, "_discussions_client", return_value=svc):
                r = await mcp_server.get_discussion_feedback("D_1", "business_analyst")
            gh = GitHubIntegration.__new__(GitHubIntegration)
            fake_app = MagicMock()
            fake_app.graphql_request.return_value = {"node": {"comments": {"nodes": tree}}}
            with patch("services.github_app.github_app", fake_app):
                expected = await gh.has_agent_processed_discussion("D_1", "business_analyst")
            assert r["agent"]["still_last_word"] is expected

    @pytest.mark.asyncio
    async def test_failure_returns_error_dict(self):
        svc = _svc(fetch_discussion_comments=RuntimeError("nope"))
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.get_discussion_feedback("D_1", "business_analyst")
        assert r["success"] is False and "nope" in r["error"]

    @pytest.mark.asyncio
    async def test_null_author_is_tolerated(self):
        svc = _svc(fetch_discussion_comments=[{"body": "x", "author": None, "createdAt": "t"}])
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.get_discussion_feedback("D_1", "business_analyst")
        assert r["success"] is True
        assert r["agent"]["has_posted"] is False

    @pytest.mark.asyncio
    async def test_failed_fetch_is_not_reported_as_empty_thread(self):
        svc = _svc(fetch_discussion_comments=None)
        with patch.object(mcp_server, "_discussions_client", return_value=svc):
            r = await mcp_server.get_discussion_feedback("D_1")
        assert r["success"] is False

    @pytest.mark.asyncio
    async def test_empty_id(self):
        r = await mcp_server.get_discussion_feedback("")
        assert r["success"] is False
