"""
Council Room — the two real voices.

Claude and Gemini are SEPARATE models; they cannot co-think. What this module
provides is two independent voices that can be placed in ONE transcript, each
able to see the other's last turn — that is what makes the council feel like one
room instead of two disconnected chatbots.

  * Claude  — runs on the local `claude` CLI (your Pro login, NO API key/credits).
              Read-only tools (Read/Grep/Glob) in chat mode, so the discussion
              voice can open the project but never mutate it. Mutation belongs to
              the build loop (run_adversarial.py), never to a conversation turn.
  * Gemini  — google-genai, text-only (NO function tools): a tool-carrying call
              crashes when the model hallucinates an unregistered tool such as
              `run_code` (the SDK does an unguarded function_map[name] lookup ->
              KeyError). A reasoning voice never needs tools, so we omit them.

Both paths are guarded by budget.py (daily token ledger) and killswitch.py.
"""
import asyncio
import logging

from google import genai
from google.genai import types

import config
import budget
import killswitch
import claude_runner

logger = logging.getLogger("council_room.agents")

# --- Personas: advisor tone (matches the user's global operating rules) -------
# Each voice is told it shares ONE room and can see the other's last turn, so it
# engages directly instead of answering in a vacuum.
CLAUDE_PERSONA = (
    "You are Claude, the Lead Engineer in a live Council Room shared with the User "
    "and Antigravity (Gemini, the Master Architect). ALWAYS respond in Georgian. "
    "Be an advisor, not an assistant: never open with agreement, tag confidence "
    "[Certain]/[Likely]/[Guessing] before claims, lead with the uncomfortable truth, "
    "and disagree only with a concrete reason plus an alternative. When you can see "
    "Antigravity's last message, engage with it directly — agree or push back, with "
    "reasons. Your focus is implementation, code, and feasibility. Match length to "
    "the message; a greeting gets one or two sentences, not an essay."
)
GEMINI_PERSONA = (
    "You are Antigravity, the Master Architect in a live Council Room shared with the "
    "User and Claude (Lead Engineer). ALWAYS respond in Georgian. Your focus is "
    "architecture, system design, and risk. You have NO filesystem and NO tools — "
    "reason only from the conversation.\n"
    "Calibrate to the message — this is mandatory:\n"
    "• A greeting, small talk, or a simple question gets ONE or TWO natural sentences — "
    "no architecture, no design principles, no risk analysis, no manufactured objection. "
    "Just answer like a person.\n"
    "• Only when the User actually raises a decision, a design, or a technical problem do "
    "you switch on the critical advisor: tag confidence [Certain]/[Likely]/[Guessing], lead "
    "with the uncomfortable truth, and disagree only with a concrete reason plus an alternative.\n"
    "Disagreement needs a real reason. Manufacturing a problem where there is none, or "
    "escalating a trivial message into an architecture lecture, is itself a failure — "
    "inverted sycophancy. When you can see Claude's last message, engage with it directly, "
    "but agree plainly when he is simply right."
)

# Lazily-initialised Gemini client (module global so run_adversarial can read it).
gemini_client = None


def init_gemini() -> None:
    """Idempotently build the Gemini client from the first configured key."""
    global gemini_client
    if gemini_client is not None:
        return
    if not config.GEMINI_API_KEYS:
        logger.warning("No GEMINI_API_KEY configured — Gemini voice is unavailable.")
        return
    try:
        gemini_client = genai.Client(api_key=config.GEMINI_API_KEYS[0])
    except Exception as e:
        logger.error(f"Gemini client init failed: {e}")
        gemini_client = None


def _call_gemini(prompt: str, system_prompt: str, temperature: float, max_tokens: int) -> str:
    """Synchronous, tool-free Gemini call. Runs off the event loop via to_thread."""
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            max_output_tokens=max_tokens,
        ),
    )
    meta = getattr(response, "usage_metadata", None)
    if meta:
        usage = {
            "input_tokens": meta.prompt_token_count or 0,
            "output_tokens": meta.candidates_token_count or 0,
        }
        cost = (usage["input_tokens"] * 0.075 / 1_000_000) + (usage["output_tokens"] * 0.30 / 1_000_000)
        budget.add_tokens(usage, cost)
    return response.text or ""


async def ask_gemini(prompt: str, system_prompt: str = GEMINI_PERSONA,
                     temperature: float = 0.7, max_tokens: int = 4096) -> str:
    """Antigravity's voice. Never raises — returns a ⚠️/🛑/⛔ string on any guard
    or error so the transcript degrades gracefully instead of crashing."""
    if killswitch.is_engaged():
        return "🛑 Kill-switch ჩართულია — Gemini გაყინულია."
    if budget.budget_exceeded():
        return f"⛔ დღიური ლიმიტი ამოიწურა.\n{budget.summary()}"
    init_gemini()
    if not gemini_client:
        return "⚠️ Gemini API key არ არის კონფიგურირებული (.env → GEMINI_API_KEY)."
    try:
        text = await asyncio.to_thread(_call_gemini, prompt, system_prompt, temperature, max_tokens)
        return text.strip() or "(ცარიელი პასუხი)"
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return f"⚠️ Gemini Error: {e}"


async def ask_claude(prompt: str, system_prompt: str = CLAUDE_PERSONA) -> str:
    """Claude's voice via the local Pro CLI (read-only tools). Never raises."""
    if killswitch.is_engaged():
        return "🛑 Kill-switch ჩართულია — Claude გაყინულია."
    return await claude_runner.run_claude_chat(prompt, system_prompt)
