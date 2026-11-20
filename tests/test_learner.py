"""Learner plumbing with a scripted model (no API key needed). Checks that model answers are
executed, tested, fixed and verified as designed — not the model's intelligence."""
import asyncio
import copy

import pytest

from tests import recipes as R
from watchtower.db import DB
from watchtower.fetcher import Fetcher, PortalError
from watchtower.learner import Learner
from watchtower.llm import Usage


class ScriptedLLM:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []
        self.usage = Usage()

    async def ask_json(self, system, user, **kw):
        self.prompts.append(user)
        self.usage.calls += 1
        ans = self.answers.pop(0)
        return ans(user) if callable(ans) else ans


def run(coro):
    return asyncio.run(coro)


async def _learn(llm, url, **kw):
    steps = []
    async with Fetcher() as f:
        learner = Learner(DB(":memory:"), f, llm, steps, 50)
        if kw:
            return await learner.repair(url, **kw), steps
        return await learner.learn(url), steps


def test_notice_navigate_then_learn(mock_base):
    full = R.ssr_v1(mock_base)
    list_only = {k: v for k, v in full.items() if k not in ("detail", "documents")}
    broken = copy.deepcopy(list_only)
    broken["list"]["items"] = "table.wrong tr"
    llm = ScriptedLLM([
        {"action": "navigate", "url": "/ssr/tenders", "reason": "summary page"},
        {"action": "list", "recipe": broken, "reason": "table"},
        {"recipe": list_only},                                  # fix after 0 matches
        {"detail": full["detail"], "documents": full["documents"]},
        {"verdicts": {"title": "ok", "buyerName": "ok"}, "recipe": None},
    ])
    recipe, steps = run(_learn(llm, f"{mock_base}/notice/4711"))
    assert recipe["list"]["items"] == "table.results tbody tr"
    assert recipe["documents"]["links"] == "ul.docs a"
    assert "matched 0 tenders" in llm.prompts[2]
    assert "Verification round 1: all fields ok" in " ".join(steps)
    # the HTML skeleton was given to the model, compacted
    assert "more similar" in llm.prompts[1] or "<tr" in llm.prompts[1]


def test_wrong_field_dropped(mock_base):
    full = R.ssr_v1(mock_base)
    llm = ScriptedLLM([
        {"action": "list", "recipe": full},
        {"detail": full["detail"], "documents": full["documents"]},
        {"verdicts": {"buyerName": "wrong"}, "recipe": None},
    ])
    recipe, _ = run(_learn(llm, f"{mock_base}/ssr/tenders"))
    assert "buyerName" not in recipe["list"]["fields"]


def test_blocked(mock_base):
    llm = ScriptedLLM([{"action": "blocked", "kind": "login", "reason": "sign-in form"}])
    with pytest.raises(PortalError) as e:
        run(_learn(llm, f"{mock_base}/login/signin"))
    assert e.value.kind == "blocked"


def test_multistep_doc_page(mock_base):
    full = R.multi(mock_base)
    llm = ScriptedLLM([
        {"action": "list", "recipe": {k: v for k, v in full.items() if k != "documents"}},
        {"detail": full["detail"], "documents": {"pageLink": {"css": "a.btn", "attr": "href"}}},
        {"documents": {"links": "ul.fichiers a"}},
        {"verdicts": {}, "recipe": None},
    ])
    recipe, steps = run(_learn(llm, f"{mock_base}/multi/avis"))
    assert recipe["documents"]["links"] == "ul.fichiers a"
    assert recipe["documents"]["pageLink"]


def test_repair_keeps_ids(mock_base):
    import httpx
    httpx.get(mock_base + "/control/layout", params={"v": 2})
    v2 = R.ssr_v2(mock_base)
    bad_ids = copy.deepcopy(v2)
    bad_ids["list"]["fields"]["id"] = {"css": "h2.notice-title a", "attr": "href"}
    llm = ScriptedLLM([
        {"action": "list", "recipe": bad_ids},
        {"recipe": v2},   # corrected after id-continuity feedback
        {"detail": v2["detail"], "documents": v2["documents"]},
        {"verdicts": {}, "recipe": None},
    ])
    recipe, _ = run(_learn(llm, f"{mock_base}/ssr/tenders", old_recipe=R.ssr_v1(mock_base),
                           problems=["0 tenders"], sample_ids=["VG-2026-1000", "VG-2026-1001",
                                                               "VG-2026-1002"]))
    assert recipe["list"]["fields"]["id"]["css"] == "span.ref"
    assert "previously delivered ids" in llm.prompts[1]
