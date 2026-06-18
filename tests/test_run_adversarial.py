import sys
import os
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure the parent directory is in the path so we can import local modules
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

import run_adversarial
import config

class TestRunAdversarial(unittest.TestCase):
    @patch('run_adversarial.subprocess.run')
    @patch('run_adversarial.Path')
    def test_run_tests_success(self, mock_path_class, mock_run):
        # Setup workspace mock
        mock_workspace = MagicMock()
        # Mocking tests/ and scripts/ existing
        mock_workspace.__truediv__.return_value.is_dir.return_value = True
        mock_path_class.return_value.resolve.return_value = mock_workspace
        
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Test run OK"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc
        
        success, output = run_adversarial._run_tests()
        self.assertTrue(success)
        self.assertIn("STDOUT:\nTest run OK", output)
        self.assertIn("--- running tests in: tests ---", output)
        self.assertIn("--- running tests in: scripts ---", output)

    @patch('run_adversarial.subprocess.run')
    @patch('run_adversarial.Path')
    def test_run_tests_failure(self, mock_path_class, mock_run):
        # Setup workspace mock
        mock_workspace = MagicMock()
        # Mocking tests/ and scripts/ existing
        mock_workspace.__truediv__.return_value.is_dir.return_value = True
        mock_path_class.return_value.resolve.return_value = mock_workspace
        
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "AssertionError: 1 != 2"
        mock_run.return_value = mock_proc
        
        success, output = run_adversarial._run_tests()
        self.assertFalse(success)
        self.assertIn("STDERR:\nAssertionError: 1 != 2", output)

    @patch('run_adversarial.subprocess.run')
    @patch('run_adversarial.Path')
    def test_run_tests_no_dirs(self, mock_path_class, mock_run):
        # Simulate that neither tests/ nor scripts/ exists, so it falls back to "."
        mock_workspace = MagicMock()
        # when (workspace / "tests").is_dir() is called:
        mock_workspace.__truediv__.return_value.is_dir.return_value = False
        mock_path_class.return_value.resolve.return_value = mock_workspace
        
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Fallback OK"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc
        
        success, output = run_adversarial._run_tests()
        self.assertTrue(success)
        self.assertIn("--- running tests in: . ---", output)

if __name__ == '__main__':
    unittest.main()
