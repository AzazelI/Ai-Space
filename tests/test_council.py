import sys
import unittest
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch, AsyncMock

# Ensure the parent directory is in the path so we can import local modules
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

import agent_orchestrator as ao
import claude_runner
import budget


class TestRunCouncil(unittest.IsolatedAsyncioTestCase):
    """Orchestration tests for run_council — the two LLM boundaries
    (query_gemini_plain, run_claude_chat) are mocked, so these assert the
    pipeline shape (reformulate → 5 advisors → [peer review] → chair), the
    cross-backend split, and the resilience guards — never model content."""

    def setUp(self):
        self._saved_sessions = ao.sessions
        ao.sessions = {}

    def tearDown(self):
        ao.sessions = self._saved_sessions

    def _council_env(self, stack, gemini="G", claude="C", budget_exceeded=False):
        """Apply all patches needed to run the council offline. Returns the two
        AsyncMocks (gemini_plain, claude_chat) for call-count assertions."""
        gp = AsyncMock(return_value=gemini)
        cc = AsyncMock(return_value=claude)
        stack.enter_context(patch.object(ao, "gemini_client", object()))
        stack.enter_context(patch.object(ao, "init_clients", lambda: None))
        stack.enter_context(patch.object(budget, "budget_exceeded", lambda: budget_exceeded))
        stack.enter_context(patch.object(ao, "query_gemini_plain", gp))
        stack.enter_context(patch.object(claude_runner, "run_claude_chat", cc))
        return gp, cc

    async def test_happy_path_structure(self):
        with ExitStack() as stack:
            self._council_env(stack)
            turns = await ao.run_council(1, "Q", peer_review=False)
        # reformulated + 5 advisors + chair = 7 turns
        self.assertEqual(len(turns), 7)
        self.assertEqual(turns[0][0], "🧭")          # reformulated question
        self.assertEqual(turns[-1][0], "👑")         # chair verdict
        advisor_labels = [t[1] for t in turns[1:6]]
        self.assertEqual(advisor_labels, [adv[2] for adv in ao._COUNCIL_ADVISORS])

    async def test_advisor_backend_routing(self):
        """Contrarian + Outsider must come from the Claude backend, the other
        three from Gemini — the cross-model split is the council's whole point."""
        with ExitStack() as stack:
            self._council_env(stack, gemini="FROM_GEMINI", claude="FROM_CLAUDE")
            turns = await ao.run_council(1, "Q", peer_review=False)
        by_label = {t[1]: t[2] for t in turns[1:6]}
        for _key, _icon, label, backend, _persona in ao._COUNCIL_ADVISORS:
            expected = "FROM_CLAUDE" if backend == "claude" else "FROM_GEMINI"
            self.assertEqual(by_label[label], expected, f"{label} took the wrong backend")

    async def test_peer_review_adds_review_and_map_turns(self):
        with ExitStack() as stack:
            self._council_env(stack)
            turns = await ao.run_council(1, "Q", peer_review=True)
        # 7 base + 5 reviews + 1 anonymity map = 13
        self.assertEqual(len(turns), 13)
        self.assertTrue(any(t[1].endswith("(რეცენზია)") for t in turns))
        self.assertTrue(any(t[0] == "🗺️" for t in turns))

    async def test_budget_exceeded_short_circuits(self):
        with ExitStack() as stack:
            gp, cc = self._council_env(stack, budget_exceeded=True)
            turns = await ao.run_council(1, "Q", peer_review=False)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0][0], "🧮")
        gp.assert_not_awaited()      # never even reformulated
        cc.assert_not_awaited()

    async def test_no_gemini_client_returns_unavailable(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(ao, "gemini_client", None))
            stack.enter_context(patch.object(ao, "init_clients", lambda: None))
            turns = await ao.run_council(1, "Q", peer_review=False)
        self.assertEqual(len(turns), 1)
        self.assertIn("not configured", turns[0][2])

    async def test_verdict_persisted_to_session(self):
        with ExitStack() as stack:
            self._council_env(stack, gemini="VERDICT_TEXT")
            await ao.run_council(7, "my question", peer_review=False)
        history = ao.get_session(7)["history"]
        senders = [m["sender"] for m in history]
        self.assertIn("User", senders)
        self.assertIn("Council", senders)

    async def test_chair_falls_back_to_claude_when_gemini_fails(self):
        """If the Gemini chair verdict errors out, the verdict is synthesized on
        the Claude CLI instead — the council must not die because one backend is
        briefly down."""
        def gp_side(prompt, system_prompt, *a, **k):
            return "⚠️ gemini down" if system_prompt == ao._CHAIR_PROMPT else "G"

        with ExitStack() as stack:
            stack.enter_context(patch.object(ao, "gemini_client", object()))
            stack.enter_context(patch.object(ao, "init_clients", lambda: None))
            stack.enter_context(patch.object(budget, "budget_exceeded", lambda: False))
            stack.enter_context(patch.object(ao, "query_gemini_plain",
                                             AsyncMock(side_effect=gp_side)))
            stack.enter_context(patch.object(claude_runner, "run_claude_chat",
                                             AsyncMock(return_value="CLAUDE_VERDICT")))
            turns = await ao.run_council(1, "Q", peer_review=False)
        verdict = turns[-1][2]
        self.assertIn("CLAUDE_VERDICT", verdict)
        self.assertIn("Claude-მ შეადგინა", verdict)   # fallback marker


if __name__ == "__main__":
    unittest.main()
