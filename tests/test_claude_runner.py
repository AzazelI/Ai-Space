import sys
import os
import unittest
import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure the parent directory is in the path so we can import local modules
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

import claude_runner
import config
import budget
import killswitch


class TestClaudeRunnerUnit(unittest.TestCase):
    """Unit tests for utility functions in claude_runner."""

    def test_looks_secret(self):
        """Tests that _looks_secret correctly flags sensitive file names/paths."""
        secrets = [
            ".env",
            "my_keys.json",
            "credentials.txt",
            "id_rsa",
            "path/to/my_secret_token",
        ]
        non_secrets = [
            "bot.py",
            "requirements.txt",
            "README.md",
            "agents.py",
            "tests/test_claude_runner.py",
        ]

        for path in secrets:
            self.assertTrue(
                claude_runner._looks_secret(path),
                f"Expected '{path}' to be flagged as secret",
            )

        for path in non_secrets:
            self.assertFalse(
                claude_runner._looks_secret(path),
                f"Expected '{path}' not to be flagged as secret",
            )

    def test_parse_result_json_valid(self):
        """Tests _parse_result_json with valid clean JSON."""
        data = '{"result": "Success", "usage": {"input_tokens": 10}, "total_cost_usd": 0.01}'
        parsed = claude_runner._parse_result_json(data)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["result"], "Success")
        self.assertEqual(parsed["usage"]["input_tokens"], 10)

    def test_parse_result_json_with_noise(self):
        """Tests _parse_result_json when output is surrounded by console noise."""
        raw_output = """
        Windows cmd wrapper shim output...
        {"result": "Success", "usage": {"input_tokens": 10}, "total_cost_usd": 0.01}
        Some trailing logs or empty lines
        """
        parsed = claude_runner._parse_result_json(raw_output)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["result"], "Success")

    def test_parse_result_json_invalid(self):
        """Tests _parse_result_json with invalid JSON returns None."""
        self.assertIsNone(claude_runner._parse_result_json("not json"))
        self.assertIsNone(claude_runner._parse_result_json("{broken: json}"))
        self.assertIsNone(claude_runner._parse_result_json(""))

    def test_pro_env(self):
        """Tests that _pro_env filters out ANTHROPIC API keys/tokens to enforce Pro CLI usage."""
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "test-api-key",
                "ANTHROPIC_AUTH_TOKEN": "test-auth-token",
                "SOME_OTHER_VAR": "keep-me",
            },
        ):
            env = claude_runner._pro_env()
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
            self.assertEqual(env.get("SOME_OTHER_VAR"), "keep-me")

    def test_spawn_kwargs(self):
        """Tests that _spawn_kwargs returns correct process-group and window flags for isolation."""
        kwargs = claude_runner._spawn_kwargs()
        if os.name == "nt":
            self.assertIn("creationflags", kwargs)
            flags = kwargs["creationflags"]
            # Check that both flags are present
            expected_flags = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
            self.assertEqual(flags, expected_flags)
        else:
            self.assertIn("start_new_session", kwargs)
            self.assertTrue(kwargs["start_new_session"])

    def test_build_argv(self):
        """Tests that _build_argv constructs the CLI arguments correctly."""
        with patch("config.CLAUDE_PERMISSION_MODE", "acceptEdits"), patch(
            "config.MAX_TASK_COST_USD", 2.0
        ):
            argv = claude_runner._build_argv("claude")
            # For Windows, it prepends cmd /c
            if os.name == "nt":
                self.assertEqual(argv[:3], ["cmd", "/c", "claude"])
                args_start = 3
            else:
                self.assertEqual(argv[0], "claude")
                args_start = 1

            self.assertIn("-p", argv[args_start:])
            self.assertIn("--permission-mode", argv[args_start:])
            self.assertIn("acceptEdits", argv[args_start:])
            self.assertIn("--max-budget-usd", argv[args_start:])
            self.assertIn("2.0", argv[args_start:])

        # Test bypassPermissions adds the dangerously-skip-permissions flag
        with patch("config.CLAUDE_PERMISSION_MODE", "bypassPermissions"), patch(
            "config.MAX_TASK_COST_USD", 0.0
        ):
            argv = claude_runner._build_argv("claude")
            self.assertIn("--dangerously-skip-permissions", argv)
            self.assertNotIn("--max-budget-usd", argv)


class TestClaudeRunnerIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration and behavior tests for subprocess spawning and communication."""

    async def test_subprocess_spawning_behavior(self):
        """Verify that spawning a process using our _spawn_kwargs works and successfully exits.

        This directly validates that the CREATE_NEW_PROCESS_GROUP and CREATE_NO_WINDOW
        flags do not crash the execution or cause exit code 3221225786 (0xC000013A).
        """
        # We spawn python itself to execute a simple print statement
        python_exe = sys.executable
        kwargs = claude_runner._spawn_kwargs()

        # Let's run a simple echo task using the same create_subprocess_exec logic
        proc = await asyncio.create_subprocess_exec(
            python_exe,
            "-c",
            "import sys; print('subprocess_spawn_ok'); sys.exit(0)",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **kwargs,
        )

        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=10.0,
            )
            output = stdout.decode("utf-8").strip()
            self.assertEqual(proc.returncode, 0)
            self.assertIn("subprocess_spawn_ok", output)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            self.fail("Subprocess execution timed out")

    async def test_run_claude_task_killswitch(self):
        """Tests that run_claude_task respects the killswitch without spawning any process."""
        with patch("killswitch.is_engaged", return_value=True):
            res = await claude_runner.run_claude_task("test task")
            self.assertFalse(res["ok"])
            self.assertIn("Kill-switch ჩართულია", res["error"])

    async def test_run_claude_task_budget_exceeded(self):
        """Tests that run_claude_task respects budget limits and refuses to spawn."""
        with patch("budget.budget_exceeded", return_value=True), patch(
            "killswitch.is_engaged", return_value=False
        ):
            res = await claude_runner.run_claude_task("test task")
            self.assertFalse(res["ok"])
            self.assertIn("დღიური ტოკენ-ბიუჯეტი ამოწურულია", res["error"])


if __name__ == "__main__":
    unittest.main()
