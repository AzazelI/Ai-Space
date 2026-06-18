import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

# Ensure the parent directory is in the path so we can import local modules
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

import agent_orchestrator as ao
import config


# ---------------------------------------------------------------------------
# Pure functions — no LLM, no subprocess, no filesystem.
# ---------------------------------------------------------------------------
class TestExtractJson(unittest.TestCase):
    """Defensive router-output parsing — must never raise, salvage from prose."""

    def test_clean_object(self):
        self.assertEqual(ao._extract_json('{"a": 1}'), {"a": 1})

    def test_salvage_from_prose(self):
        self.assertEqual(ao._extract_json('noise {"assignee": "claude"} tail'),
                         {"assignee": "claude"})

    def test_salvage_from_markdown_fence(self):
        raw = '```json\n{"mode": "plan"}\n```'
        self.assertEqual(ao._extract_json(raw), {"mode": "plan"})

    def test_nested_braces(self):
        self.assertEqual(ao._extract_json('x {"a": {"b": 1}} y'), {"a": {"b": 1}})

    def test_empty_is_none(self):
        self.assertIsNone(ao._extract_json(""))

    def test_garbage_is_none(self):
        self.assertIsNone(ao._extract_json("no json here"))

    def test_json_array_is_none(self):
        """A top-level array is valid JSON but not a dict → rejected."""
        self.assertIsNone(ao._extract_json("[1, 2, 3]"))


class TestFormatChatHistory(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(ao.format_chat_history([]),
                         "No previous conversation history.")

    def test_formats_sender_and_text(self):
        out = ao.format_chat_history([{"sender": "User", "text": "hi"}])
        self.assertEqual(out, "[User]: hi")

    def test_caps_at_last_12(self):
        history = [{"sender": "User", "text": f"m{i}"} for i in range(15)]
        out = ao.format_chat_history(history)
        self.assertNotIn("m0", out)      # first 3 dropped
        self.assertNotIn("m2", out)
        self.assertIn("m3", out)         # last 12 kept
        self.assertIn("m14", out)
        self.assertEqual(len(out.splitlines()), 12)


class TestInjectContext(unittest.TestCase):
    def test_includes_history_and_message(self):
        session = {"history": [{"sender": "User", "text": "earlier"}],
                   "temp_file_context": None}
        out = ao.inject_context(session, "current question")
        self.assertIn("earlier", out)
        self.assertIn("current question", out)

    def test_includes_attached_file_block(self):
        session = {"history": [],
                   "temp_file_context": {"filename": "spec.md", "content": "FILEBODY"}}
        out = ao.inject_context(session, "q")
        self.assertIn("spec.md", out)
        self.assertIn("FILEBODY", out)

    def test_no_file_block_when_absent(self):
        session = {"history": [], "temp_file_context": None}
        self.assertNotIn("ATTACHED WORKSPACE FILE", ao.inject_context(session, "q"))


class TestSessionStore(unittest.TestCase):
    def setUp(self):
        self._saved = ao.sessions
        ao.sessions = {}

    def tearDown(self):
        ao.sessions = self._saved

    def test_get_session_creates_with_defaults(self):
        s = ao.get_session(42)
        self.assertEqual(s["history"], [])
        self.assertIsNone(s["temp_file_context"])
        self.assertEqual(s["mode"], config.DEFAULT_MODE)

    def test_get_session_is_stable(self):
        a = ao.get_session(1)
        a["history"].append({"sender": "User", "text": "x"})
        self.assertIs(ao.get_session(1), a)
        self.assertEqual(len(ao.get_session(1)["history"]), 1)

    def test_clear_session_empties_but_keeps(self):
        s = ao.get_session(1)
        s["history"].append({"sender": "User", "text": "x"})
        s["temp_file_context"] = {"filename": "f", "content": "c"}
        ao.clear_session(1)
        self.assertEqual(s["history"], [])
        self.assertIsNone(s["temp_file_context"])
        self.assertIn(1, ao.sessions)


# ---------------------------------------------------------------------------
# Filesystem + security — confined to WORKSPACE_DIR, refuses secrets/traversal.
# ---------------------------------------------------------------------------
class WorkspaceTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self._patcher = patch.object(config, "WORKSPACE_DIR", str(self.ws))
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()


class TestSafePath(WorkspaceTestBase):
    def test_normal_file_resolves(self):
        (self.ws / "notes.md").write_text("x", encoding="utf-8")
        self.assertEqual(ao._safe_path("notes.md"), (self.ws / "notes.md").resolve())

    def test_path_traversal_rejected(self):
        self.assertIsNone(ao._safe_path("../escape.txt"))

    def test_secret_dotenv_rejected(self):
        self.assertIsNone(ao._safe_path(".env"))

    def test_secret_keys_rejected(self):
        self.assertIsNone(ao._safe_path("config_keys.json"))


class TestReadFileListDir(WorkspaceTestBase):
    def test_read_file_returns_content(self):
        (self.ws / "a.md").write_text("hello world", encoding="utf-8")
        self.assertEqual(ao.read_file("a.md"), "hello world")

    def test_read_secret_denied(self):
        (self.ws / ".env").write_text("SECRET=1", encoding="utf-8")
        self.assertIn("Access denied", ao.read_file(".env"))

    def test_read_traversal_denied(self):
        self.assertIn("Access denied", ao.read_file("../../etc/passwd"))

    def test_read_missing_file(self):
        self.assertIn("Not a file", ao.read_file("nope.md"))

    def test_read_truncates_long_file(self):
        with patch.object(ao, "_MAX_TOOL_READ_CHARS", 10):
            (self.ws / "big.md").write_text("x" * 50, encoding="utf-8")
            out = ao.read_file("big.md")
            self.assertIn("truncated", out)
            self.assertIn("50 chars total", out)

    def test_list_dir_omits_hidden_and_secret(self):
        (self.ws / "visible.md").write_text("x", encoding="utf-8")
        (self.ws / ".hidden").write_text("x", encoding="utf-8")
        (self.ws / ".env").write_text("x", encoding="utf-8")
        (self.ws / "sub").mkdir()
        out = ao.list_dir(".")
        self.assertIn("visible.md", out)
        self.assertIn("[dir] sub", out)
        self.assertNotIn(".hidden", out)
        self.assertNotIn(".env", out)

    def test_list_dir_on_file_errors(self):
        (self.ws / "a.md").write_text("x", encoding="utf-8")
        self.assertIn("Not a directory", ao.list_dir("a.md"))


class TestRenderRegistry(WorkspaceTestBase):
    def test_only_existing_entries_render(self):
        rel = ao._DEPARTMENT_REGISTRY[0][0]   # a real registry-relative path
        target = self.ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
        out = ao._render_registry()
        self.assertIn(rel, out)
        # A different registry entry that we did NOT create must be absent.
        self.assertNotIn(ao._DEPARTMENT_REGISTRY[1][0], out)

    def test_empty_workspace_fallback(self):
        self.assertIn("ვერ მოიძებნა", ao._render_registry())


# ---------------------------------------------------------------------------
# run_routed — async, but only the single LLM boundary is mocked. Exercises the
# defensive normalization/clamping that keeps a bad model reply from crashing.
# ---------------------------------------------------------------------------
class TestRunRoutedNormalization(unittest.IsolatedAsyncioTestCase):
    def _patches(self, raw):
        """Patch the router so run_routed gets `raw` as the model output, with a
        live gemini_client and the killswitch released."""
        return (
            patch.object(ao, "gemini_client", object()),
            patch.object(ao, "init_clients", lambda: None),
            patch("killswitch.is_engaged", return_value=False),
            patch.object(ao, "run_gemini_operation", new=AsyncMock(return_value=raw)),
        )

    async def _route(self, raw):
        p1, p2, p3, p4 = self._patches(raw)
        with p1, p2, p3, p4:
            return await ao.run_routed("do a thing")

    async def test_valid_full_json(self):
        res = await self._route(
            '{"reformulated_task": "build X", "assignee": "claude", '
            '"mode": "autonomous", "scope": ["a.py"], "council": false}')
        self.assertTrue(res["ok"])
        self.assertEqual(res["assignee"], "claude")
        self.assertEqual(res["mode"], "autonomous")
        self.assertEqual(res["scope"], ["a.py"])
        self.assertFalse(res["council_suggested"])

    async def test_bad_assignee_defaults_to_both(self):
        res = await self._route('{"assignee": "nobody", "mode": "plan"}')
        self.assertEqual(res["assignee"], "both")

    async def test_bad_mode_defaults_to_discuss(self):
        res = await self._route('{"assignee": "claude", "mode": "wizardry"}')
        self.assertEqual(res["mode"], "discuss")

    async def test_scalar_scope_coerced_to_list(self):
        res = await self._route('{"scope": "single.py"}')
        self.assertEqual(res["scope"], ["single.py"])

    async def test_council_truthy_coerced_to_bool(self):
        res = await self._route('{"council": 1}')
        self.assertIs(res["council_suggested"], True)

    async def test_non_json_returns_not_ok(self):
        res = await self._route("the model rambled with no json")
        self.assertFalse(res["ok"])

    async def test_killswitch_short_circuits(self):
        with patch("killswitch.is_engaged", return_value=True):
            res = await ao.run_routed("do a thing")
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
