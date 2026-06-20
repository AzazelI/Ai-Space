"""
Council Room — configuration.

Loads .env from this folder. Deliberately small: no Telegram, and Claude never
bills the Anthropic API (it runs on the local `claude` CLI / your Pro login).
Every symbol here is consumed by agents.py, claude_runner.py, budget.py,
killswitch.py, or run_adversarial.py — nothing speculative.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# --- Gemini (Antigravity's voice) ---
# A pool is supported so a 429-exhausted key can rotate; a single key still works.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_API_KEYS = [k.strip() for k in os.getenv("GEMINI_API_KEYS", "").replace(";", ",").split(",") if k.strip()]
if not GEMINI_API_KEYS and GEMINI_API_KEY:
    GEMINI_API_KEYS = [GEMINI_API_KEY]

# --- Build workspace: where the loop edits the project under construction ---
# Defaults to ./workspace inside this repo so a fresh clone builds in isolation.
WORKSPACE_DIR = os.getenv("WORKSPACE_DIR", "").strip() or str((Path(__file__).parent / "workspace").resolve())

# --- Claude worker (local `claude` CLI on your Pro login — no API key/credits) ---
CLAUDE_CLI_PATH = os.getenv("CLAUDE_CLI_PATH", "claude").strip()
# Headless permission mode: acceptEdits (auto-accept file edits) or
# bypassPermissions (skip ALL prompts — most autonomous, highest risk).
CLAUDE_PERMISSION_MODE = os.getenv("CLAUDE_PERMISSION_MODE", "acceptEdits").strip()
# Hard ceiling (seconds) for a single build task before it is killed.
AUTONOMOUS_TIMEOUT = int(os.getenv("AUTONOMOUS_TIMEOUT", "600"))
# Auto commit + push after the build loop's reviewer APPROVES. Off by default —
# you push the finished project to its repo on purpose, not automatically.
AUTO_PUSH = os.getenv("AUTO_PUSH", "false").strip().lower() in ("1", "true", "yes", "on")

# --- Budget guards ---
#  * MAX_TASK_COST_USD  — per build-task pre-emptive ceiling (claude --max-budget-usd). 0 = off.
#  * DAILY_TOKEN_BUDGET — cumulative daily token ledger (budget.py). 0 = off.
MAX_TASK_COST_USD = float(os.getenv("MAX_TASK_COST_USD", "2.0"))
DAILY_TOKEN_BUDGET = int(os.getenv("DAILY_TOKEN_BUDGET", "2000000"))


def is_configured() -> tuple[bool, list[str]]:
    """True if the minimum to run a council is present. Claude needs no key
    (Pro CLI); only Gemini requires one."""
    missing = []
    if not GEMINI_API_KEYS:
        missing.append("GEMINI_API_KEY")
    return len(missing) == 0, missing
