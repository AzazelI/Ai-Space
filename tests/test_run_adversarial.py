import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure the parent directory is in the path so we can import local modules
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

import run_adversarial


def _mock_workspace(tests_dir_exists: bool) -> MagicMock:
    """Build a workspace mock whose `(workspace / "tests").is_dir()` answers
    `tests_dir_exists`. _run_tests only ever probes the tests/ subdir now."""
    mock_workspace = MagicMock()
    mock_workspace.__truediv__.return_value.is_dir.return_value = tests_dir_exists
    return mock_workspace


class TestRunTests(unittest.TestCase):
    @patch('run_adversarial.subprocess.run')
    @patch('run_adversarial.Path')
    def test_passed_when_suite_green(self, mock_path_class, mock_run):
        mock_path_class.return_value.resolve.return_value = _mock_workspace(True)
        mock_run.return_value = MagicMock(returncode=0, stdout="Ran 3 tests\nOK", stderr="")

        status, output = run_adversarial._run_tests()
        self.assertEqual(status, "passed")
        self.assertIn("--- running tests in: tests ---", output)

    @patch('run_adversarial.subprocess.run')
    @patch('run_adversarial.Path')
    def test_failed_on_real_failure(self, mock_path_class, mock_run):
        mock_path_class.return_value.resolve.return_value = _mock_workspace(True)
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="FAILED (failures=1)\nAssertionError: 1 != 2")

        status, output = run_adversarial._run_tests()
        self.assertEqual(status, "failed")
        self.assertIn("AssertionError", output)

    @patch('run_adversarial.subprocess.run')
    @patch('run_adversarial.Path')
    def test_skipped_when_no_tests_dir(self, mock_path_class, mock_run):
        # No tests/ directory: must short-circuit to "skipped" WITHOUT running.
        mock_path_class.return_value.resolve.return_value = _mock_workspace(False)

        status, _ = run_adversarial._run_tests()
        self.assertEqual(status, "skipped")
        mock_run.assert_not_called()

    @patch('run_adversarial.subprocess.run')
    @patch('run_adversarial.Path')
    def test_skipped_on_empty_discover_exit5(self, mock_path_class, mock_run):
        # The regression that wedged the loop: Python 3.12+ exits 5 on an empty
        # discover. That is NOT a failure — it must be "skipped" so APPROVED holds.
        mock_path_class.return_value.resolve.return_value = _mock_workspace(True)
        mock_run.return_value = MagicMock(returncode=5, stdout="", stderr="NO TESTS RAN")

        status, _ = run_adversarial._run_tests()
        self.assertEqual(status, "skipped")

    @patch('run_adversarial.subprocess.run')
    @patch('run_adversarial.Path')
    def test_skipped_on_zero_tests_legacy_exit0(self, mock_path_class, mock_run):
        # Older Pythons exit 0 but print "Ran 0 tests" — also a skip, not a pass.
        mock_path_class.return_value.resolve.return_value = _mock_workspace(True)
        mock_run.return_value = MagicMock(returncode=0, stdout="Ran 0 tests in 0.000s\n\nOK", stderr="")

        status, _ = run_adversarial._run_tests()
        self.assertEqual(status, "skipped")


class TestPreexistingDirt(unittest.TestCase):
    @patch('run_adversarial._git')
    def test_filters_metadata_and_parses_paths(self, mock_git):
        mock_git.return_value = (0, "\n".join([
            " M scripts/export_boilerplate.py",
            "?? newfile.py",
            " M .obsidian/graph.json",      # ignored metadata
            " M shared/state.json",          # ignored loop artifact
            "?? .claude/settings.json",      # ignored metadata
        ]))
        dirt = run_adversarial._preexisting_dirt()
        self.assertEqual(dirt, ["scripts/export_boilerplate.py", "newfile.py"])

    @patch('run_adversarial._git')
    def test_clean_tree_is_empty(self, mock_git):
        mock_git.return_value = (0, "")
        self.assertEqual(run_adversarial._preexisting_dirt(), [])

    @patch('run_adversarial._git')
    def test_git_failure_returns_empty(self, mock_git):
        mock_git.return_value = (1, "not a git repo")
        self.assertEqual(run_adversarial._preexisting_dirt(), [])


if __name__ == '__main__':
    unittest.main()
