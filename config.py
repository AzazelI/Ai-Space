import os
from pathlib import Path
from dotenv import load_dotenv

# Locate and load the .env file in the same directory as this script
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

# Load configuration values
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

# Gemini API Key Pool for rotation on 429 quota exhaustion
GEMINI_API_KEYS = [k.strip() for k in os.getenv("GEMINI_API_KEYS", "").replace(";", ",").split(",") if k.strip()]
if not GEMINI_API_KEYS and GEMINI_API_KEY:
    GEMINI_API_KEYS = [GEMINI_API_KEY]

# Workspace path (fallback to parent of telegram_bot folder)
WORKSPACE_DIR = os.getenv("WORKSPACE_DIR", "").strip()
if not WORKSPACE_DIR:
    WORKSPACE_DIR = str(Path(__file__).parent.parent.resolve())

DEFAULT_MODE = os.getenv("DEFAULT_MODE", "collaborative").strip().lower()
if DEFAULT_MODE not in ["solo", "collaborative", "debate"]:
    DEFAULT_MODE = "collaborative"

# --- Security: Telegram User ID allowlist ---
# Comma-separated numeric Telegram user IDs allowed to command the bot.
# CRITICAL for autonomous mode: only these IDs can run real terminal work.
def _parse_ids(raw: str) -> set[int]:
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids

ALLOWED_TELEGRAM_USER_IDS = _parse_ids(os.getenv("ALLOWED_TELEGRAM_USER_IDS", ""))

# --- Autonomous "do real work" mode (drives the local `claude` CLI headlessly) ---
ENABLE_AUTONOMOUS = os.getenv("ENABLE_AUTONOMOUS", "false").strip().lower() in ("1", "true", "yes", "on")
# Permission mode passed to `claude -p`. "acceptEdits" auto-accepts file edits;
# "bypassPermissions" skips all prompts (most autonomous, highest risk).
CLAUDE_PERMISSION_MODE = os.getenv("CLAUDE_PERMISSION_MODE", "acceptEdits").strip()
# Path to the claude CLI executable (override if not on PATH).
CLAUDE_CLI_PATH = os.getenv("CLAUDE_CLI_PATH", "claude").strip()
# Auto commit + push after an autonomous /build task completes.
AUTO_PUSH = os.getenv("AUTO_PUSH", "false").strip().lower() in ("1", "true", "yes", "on")
# Hard ceiling (seconds) for a single autonomous task before it is killed.
AUTONOMOUS_TIMEOUT = int(os.getenv("AUTONOMOUS_TIMEOUT", "600"))

# --- Budget caps (ROUTER_PROTOCOL.md §5.4 "მყარი ჭერი") ---
# Two independent ceilings guard against a runaway autonomous loop spending
# unbounded resources, even though /build is human-gated:
#
#  * MAX_TASK_COST_USD — per-task PRE-EMPTIVE hard ceiling. Passed to the CLI as
#    `--max-budget-usd`; the run is stopped by the CLI once the computed cost
#    crosses it (the CLI computes total_cost_usd even on a Pro login, so this
#    fires regardless of whether API credits or the subscription pay). This is
#    the enforceable stand-in for the protocol's "per-task iteration cap": the
#    installed CLI has no --max-turns flag, so a turn count can't be capped, but
#    a cost ceiling bounds the same runaway. 0 = unlimited.
#  * DAILY_TOKEN_BUDGET — cumulative across tasks. Enforced by budget.py's
#    on-disk daily ledger: a task is refused before it starts once the day's
#    token total (input+output+cache) is exhausted. Resets at local midnight.
#    0 = unlimited.
MAX_TASK_COST_USD = float(os.getenv("MAX_TASK_COST_USD", "2.0"))
DAILY_TOKEN_BUDGET = int(os.getenv("DAILY_TOKEN_BUDGET", "2000000"))

# --- Multi-round debate (ROUTER_PROTOCOL.md §7 Stage 2) ---
# A "round" = one Claude critique + one Gemini refinement. Gemini's initial
# proposal precedes round 1. DEBATE_ROUNDS_CEILING is a HARD loop-killer (§5.2):
# the agent<->agent exchange can never ping-pong unbounded without a human, no
# matter what the env asks for. MAX_DEBATE_ROUNDS is clamped into [1, ceiling].
DEBATE_ROUNDS_CEILING = 4
MAX_DEBATE_ROUNDS = max(1, min(int(os.getenv("MAX_DEBATE_ROUNDS", "2")), DEBATE_ROUNDS_CEILING))

# --- Full Council (ROUTER_PROTOCOL.md §7 Stage 3) ---
# The 5-advisor stress-test council (Central AI Team/Council.md). The 5 advisors
# run as INDEPENDENT cold calls distributed across both backends (Gemini + Claude
# Pro CLI), so their disagreement is genuine rather than one model role-playing
# five voices. A council run is expensive, so it is never auto-spent: it fires
# only on the explicit /council command, or when the owner taps the "Council"
# button the router offers at the human gate for decision-shaped tasks.
#
#  * COUNCIL_PEER_REVIEW — when true, after the 5 advisors answer they each
#    re-read the anonymized set and peer-review it (5 extra calls), matching the
#    protocol's full "~11 LLM calls". Default false: 5 advisors + 1 chair verdict
#    = ~6 calls, which keeps the independence value at roughly half the cost.
#  * COUNCIL_ADVISOR_TIMEOUT — per-advisor ceiling (seconds) for the Claude CLI
#    leg; Gemini advisors are bounded by the SDK. Council reasoning needs no file
#    tools, so this is just guarding against a hung CLI process.
COUNCIL_PEER_REVIEW = os.getenv("COUNCIL_PEER_REVIEW", "false").strip().lower() in ("1", "true", "yes", "on")
COUNCIL_ADVISOR_TIMEOUT = int(os.getenv("COUNCIL_ADVISOR_TIMEOUT", "240"))

# Route the "Claude" voice in discussion modes (/claude /ask /debate) through the
# local `claude` CLI (Claude Pro) instead of the Anthropic API. True = no API key
# / no $5 credits needed; Claude answers via your Pro subscription.
CLAUDE_DISCUSSION_VIA_CLI = os.getenv("CLAUDE_DISCUSSION_VIA_CLI", "true").strip().lower() in ("1", "true", "yes", "on")

def is_authorized(user_id: int) -> bool:
    """True if this Telegram user may command the bot.
    If no allowlist is configured, returns False (deny-by-default) so the bot
    never runs real work for strangers. Discussion-only deployments can still
    use the bot by adding their own ID."""
    if not ALLOWED_TELEGRAM_USER_IDS:
        return False
    return user_id in ALLOWED_TELEGRAM_USER_IDS

def is_configured() -> tuple[bool, list[str]]:
    """Checks if all required credentials are present."""
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not GEMINI_API_KEYS:
        missing.append("GEMINI_API_KEY / GEMINI_API_KEYS")
    # Anthropic API key is only required when NOT routing Claude through the Pro CLI.
    if not ANTHROPIC_API_KEY and not CLAUDE_DISCUSSION_VIA_CLI:
        missing.append("ANTHROPIC_API_KEY")

    return len(missing) == 0, missing
