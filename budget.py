"""
Daily token-budget ledger for the autonomous task runner (ROUTER_PROTOCOL.md
§5.4 "მყარი ჭერი"). Persists a single small JSON file next to the bot and
accumulates token usage per local calendar day. The autonomous path checks
``budget_exceeded()`` BEFORE starting a task and calls ``record()`` AFTER each
run, so a runaway loop can spend at most one task's worth past the line.

The file is deliberately tiny and self-healing: a missing, corrupt, or
stale-dated ledger is treated as "today, zero spent". Writes are serialized by
the caller's ``_build_lock`` (one task at a time), so no file locking is needed.
"""
import json
import logging
from datetime import date
from pathlib import Path

import config

logger = logging.getLogger("telegram_bot.budget")

LEDGER_PATH = Path(__file__).parent / ".budget_ledger.json"

_FRESH = {"date": "", "input_tokens": 0, "output_tokens": 0,
          "cache_tokens": 0, "cost_usd": 0.0, "tasks": 0}


def _today() -> str:
    return date.today().isoformat()


def _load() -> dict:
    """Return today's ledger, resetting to zero if the file is missing, corrupt,
    or carries an older date (daily rollover)."""
    try:
        data = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("date") != _today():
            raise ValueError("stale or malformed ledger")
        # Backfill any missing keys so arithmetic never KeyErrors.
        return {**_FRESH, **data, "date": _today()}
    except FileNotFoundError:
        return {**_FRESH, "date": _today()}
    except Exception as e:
        logger.warning(f"Resetting budget ledger ({e!r}).")
        return {**_FRESH, "date": _today()}


def _save(d: dict) -> None:
    try:
        LEDGER_PATH.write_text(json.dumps(d), encoding="utf-8")
    except Exception as e:
        # A ledger write failure must never crash a task — log and continue.
        logger.error(f"Failed to write budget ledger: {e}")


def today_tokens() -> int:
    d = _load()
    return d["input_tokens"] + d["output_tokens"] + d["cache_tokens"]


# Cache tokens (overwhelmingly cache READS from Claude CLI runs) are ~10x cheaper
# than fresh input, so counting them 1:1 against the daily ceiling drains it ~10x
# too fast — a few build runs exhaust the day. Weight them down for the CEILING
# ONLY; the ledger still records the raw count (today_tokens / summary) for honest
# accounting. 0.1 ≈ the cache-read price ratio.
CACHE_TOKEN_WEIGHT = 0.1


def billable_tokens() -> int:
    """Effective tokens counted toward the daily ceiling: input + output at full
    weight, cache discounted by CACHE_TOKEN_WEIGHT (cache reads are cheap)."""
    d = _load()
    return d["input_tokens"] + d["output_tokens"] + int(d["cache_tokens"] * CACHE_TOKEN_WEIGHT)


def budget_exceeded() -> bool:
    """True if today's BILLABLE token total has reached DAILY_TOKEN_BUDGET.
    Always False when the budget is unlimited (0)."""
    if config.DAILY_TOKEN_BUDGET <= 0:
        return False
    return billable_tokens() >= config.DAILY_TOKEN_BUDGET


def budget_alert_reached() -> bool:
    """True if today's billable token total has reached 80% of DAILY_TOKEN_BUDGET.
    Always False when the budget is unlimited (0)."""
    if config.DAILY_TOKEN_BUDGET <= 0:
        return False
    return billable_tokens() >= int(config.DAILY_TOKEN_BUDGET * 0.8)


def remaining_tokens() -> int | None:
    """Billable tokens left in today's budget, or None when unlimited."""
    if config.DAILY_TOKEN_BUDGET <= 0:
        return None
    return max(0, config.DAILY_TOKEN_BUDGET - billable_tokens())


def add_tokens(usage: dict, cost_usd: float = 0.0) -> None:
    """Add raw token/cost usage to today's ledger WITHOUT counting a task.

    Used for the many small LLM sub-calls — Gemini chat, the router, debate, and
    every council leg — so the daily budget reflects REAL spend rather than only
    autonomous /build runs. Without this the daily ceiling is blind to the bulk of
    spending (a single /council fires ~6 Gemini calls). 'tasks' stays a count of
    autonomous tasks only, so it is not bumped here. Safe on failed/partial calls —
    tokens were still spent, so they must still count. Never raises."""
    usage = usage or {}
    d = _load()
    d["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
    d["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
    d["cache_tokens"] += (
        int(usage.get("cache_creation_input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
    )
    try:
        d["cost_usd"] = round(d["cost_usd"] + float(cost_usd or 0.0), 5)
    except (TypeError, ValueError):
        pass
    _save(d)


def record(usage: dict, cost_usd: float) -> None:
    """Add one AUTONOMOUS task's usage to today's ledger and count it as a task.
    Safe to call on failed/partial runs — tokens were still spent, so they must
    still count."""
    add_tokens(usage, cost_usd)
    d = _load()
    d["tasks"] += 1
    _save(d)


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n // 1000}k"
    return str(n)


# Public alias so other modules (bot.py's /budget) can format token counts
# without reaching into a private name.
fmt_tokens = _fmt_tokens


def summary() -> str:
    """One-line human status. Shows the BILLABLE total (what actually gates)
    against the cap, with the raw token + cache counts for transparency."""
    d = _load()
    raw = d["input_tokens"] + d["output_tokens"] + d["cache_tokens"]
    billable = d["input_tokens"] + d["output_tokens"] + int(d["cache_tokens"] * CACHE_TOKEN_WEIGHT)
    cap = config.DAILY_TOKEN_BUDGET
    cap_txt = _fmt_tokens(cap) if cap > 0 else "∞"
    return (f"🧮 დღეს: {_fmt_tokens(billable)}/{cap_txt} billable "
            f"(raw {_fmt_tokens(raw)}, cache {_fmt_tokens(d['cache_tokens'])}) · "
            f"~${d['cost_usd']:.2f} · {d['tasks']} task(s)")
