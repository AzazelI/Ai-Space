import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure the parent directory is in the path so we can import local modules
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

import budget
import config


class BudgetTestBase(unittest.TestCase):
    """Isolates every test from the real on-disk ledger.

    budget.py references the module-global LEDGER_PATH directly, so we repoint it
    at a throwaway temp file per test. Without this the suite would mutate the
    bot's live .budget_ledger.json and corrupt today's real spend stats.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._ledger = Path(self._tmp.name) / ".budget_ledger.json"
        self._patcher = patch.object(budget, "LEDGER_PATH", self._ledger)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def _write_ledger(self, obj):
        self._ledger.write_text(json.dumps(obj), encoding="utf-8")


class TestLedgerAccounting(BudgetTestBase):
    def test_fresh_when_file_missing(self):
        """No ledger file → zero spent, summary still renders."""
        self.assertFalse(self._ledger.exists())
        self.assertEqual(budget.today_tokens(), 0)
        self.assertIsInstance(budget.summary(), str)

    def test_add_tokens_accumulates(self):
        budget.add_tokens({"input_tokens": 10, "output_tokens": 5})
        budget.add_tokens({"input_tokens": 3, "output_tokens": 2})
        self.assertEqual(budget.today_tokens(), 20)

    def test_cache_tokens_summed_from_both_keys(self):
        """cache_tokens = cache_creation_input_tokens + cache_read_input_tokens."""
        budget.add_tokens({
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 25,
        })
        self.assertEqual(budget.today_tokens(), 125)

    def test_record_counts_a_task_but_add_tokens_does_not(self):
        budget.add_tokens({"input_tokens": 1})          # sub-call, not a task
        budget.record({"input_tokens": 1}, 0.0)         # one autonomous task
        d = budget._load()
        self.assertEqual(d["tasks"], 1)

    def test_cost_accumulates(self):
        budget.add_tokens({}, cost_usd=0.10)
        budget.add_tokens({}, cost_usd=0.05)
        self.assertAlmostEqual(budget._load()["cost_usd"], 0.15, places=5)

    def test_cost_rounded_to_five_places(self):
        """Cost is stored rounded to 5 decimals. Note: rounding is per-step, so a
        cumulative total can drift sub-cent from the exact mathematical sum."""
        budget.add_tokens({}, cost_usd=0.1234567)
        self.assertEqual(budget._load()["cost_usd"], 0.12346)


class TestBudgetCeilings(BudgetTestBase):
    def test_budget_exceeded_boundary_is_inclusive(self):
        with patch.object(config, "DAILY_TOKEN_BUDGET", 100):
            budget.add_tokens({"input_tokens": 99})
            self.assertFalse(budget.budget_exceeded())     # 99 < 100
            budget.add_tokens({"input_tokens": 1})         # now exactly 100
            self.assertTrue(budget.budget_exceeded())      # 100 >= 100
            budget.add_tokens({"input_tokens": 50})        # over
            self.assertTrue(budget.budget_exceeded())

    def test_unlimited_budget_never_exceeds(self):
        with patch.object(config, "DAILY_TOKEN_BUDGET", 0):
            budget.add_tokens({"input_tokens": 10_000_000})
            self.assertFalse(budget.budget_exceeded())
            self.assertIsNone(budget.remaining_tokens())

    def test_alert_at_80_percent(self):
        with patch.object(config, "DAILY_TOKEN_BUDGET", 100):
            budget.add_tokens({"input_tokens": 79})
            self.assertFalse(budget.budget_alert_reached())
            budget.add_tokens({"input_tokens": 1})         # 80
            self.assertTrue(budget.budget_alert_reached())

    def test_remaining_clamped_to_zero(self):
        with patch.object(config, "DAILY_TOKEN_BUDGET", 100):
            budget.add_tokens({"input_tokens": 250})
            self.assertEqual(budget.remaining_tokens(), 0)


class TestLedgerResilience(BudgetTestBase):
    def test_daily_rollover_resets_old_ledger(self):
        """A ledger dated before today is treated as zero (daily rollover)."""
        self._write_ledger({
            "date": "2000-01-01", "input_tokens": 5000, "output_tokens": 5000,
            "cache_tokens": 0, "cost_usd": 9.9, "tasks": 7,
        })
        self.assertEqual(budget.today_tokens(), 0)

    def test_corrupt_ledger_self_heals(self):
        self._ledger.write_text("not json {{{", encoding="utf-8")
        self.assertEqual(budget.today_tokens(), 0)

    def test_add_tokens_safe_on_empty_and_none_usage(self):
        budget.add_tokens(None)        # must not raise
        budget.add_tokens({})          # must not raise
        self.assertEqual(budget.today_tokens(), 0)

    def test_save_failure_never_raises(self):
        """If the ledger path is unwritable, record() must log and continue, not
        crash the task that called it."""
        with patch.object(budget, "LEDGER_PATH", Path(self._tmp.name)):  # a dir
            try:
                budget.record({"input_tokens": 5}, 0.01)
            except Exception as e:
                self.fail(f"record() raised on unwritable ledger: {e!r}")


class TestFormatting(BudgetTestBase):
    def test_fmt_tokens(self):
        self.assertEqual(budget.fmt_tokens(999), "999")
        self.assertEqual(budget.fmt_tokens(1500), "1k")
        self.assertEqual(budget.fmt_tokens(2_000_000), "2.00M")

    def test_summary_shows_infinity_when_unlimited(self):
        with patch.object(config, "DAILY_TOKEN_BUDGET", 0):
            self.assertIn("∞", budget.summary())


if __name__ == "__main__":
    unittest.main()
