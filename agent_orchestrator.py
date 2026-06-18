import asyncio
import json
import re
import logging
from pathlib import Path
from google import genai
from google.genai import types
from anthropic import Anthropic
import config
import claude_runner
import budget
import killswitch

logger = logging.getLogger("telegram_bot.orchestrator")

# Initialize LLM Clients
gemini_client = None
claude_client = None
_current_key_index = 0

def init_clients():
    """Initializes LLM clients if keys are available."""
    global gemini_client, claude_client, _current_key_index
    
    if config.GEMINI_API_KEYS and not gemini_client:
        try:
            _current_key_index = 0
            gemini_client = genai.Client(api_key=config.GEMINI_API_KEYS[0])
            logger.info("Gemini client initialized successfully with key index 0.")
        except Exception as e:
            logger.error(f"Failed to initialize Gemini client: {e}")
            
    if config.ANTHROPIC_API_KEY and not claude_client:
        try:
            claude_client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
            logger.info("Claude client initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize Claude client: {e}")

def get_next_gemini_client():
    global _current_key_index, gemini_client
    if not config.GEMINI_API_KEYS:
        return None
    
    _current_key_index = (_current_key_index + 1) % len(config.GEMINI_API_KEYS)
    key = config.GEMINI_API_KEYS[_current_key_index]
    gemini_client = genai.Client(api_key=key)
    logger.info(f"Rotated Gemini API Key. Now using key index {_current_key_index}.")
    return gemini_client

# In-memory session store
# Structure: {chat_id: {"history": [...], "temp_file_context": {"filename": str, "content": str}, "mode": str}}
sessions = {}

def get_session(chat_id: int) -> dict:
    """Retrieves or creates a session for the given chat_id."""
    if chat_id not in sessions:
        sessions[chat_id] = {
            "history": [],
            "temp_file_context": None,
            "mode": config.DEFAULT_MODE
        }
    return sessions[chat_id]

def clear_session(chat_id: int):
    """Clears history and temp context for the session."""
    session = get_session(chat_id)
    session["history"] = []
    session["temp_file_context"] = None

def format_chat_history(history: list) -> str:
    """Formats list of message dicts into a plain text transcript."""
    if not history:
        return "No previous conversation history."
    formatted = []
    # Limit history to last 12 messages to prevent token bloating
    for msg in history[-12:]:
        formatted.append(f"[{msg['sender']}]: {msg['text']}")
    return "\n".join(formatted)

# --- Read-only filesystem tools exposed to Gemini (Antigravity) via Function Calling ---
# Both are confined to WORKSPACE_DIR and refuse secret-looking paths, so Antigravity
# can open department prompts and source but can never read credentials or escape the
# repo. Mutation tools are deliberately NOT provided — discussion stays read-only
# (ROUTER_PROTOCOL.md §2.2). claude_runner._looks_secret() is reused as the secret filter.
_MAX_TOOL_READ_CHARS = 16000


def _safe_path(rel_path: str) -> Path | None:
    """Resolve rel_path under WORKSPACE_DIR. Returns None if it escapes the workspace
    or looks like a secret file (.env, keys, credentials)."""
    base = Path(config.WORKSPACE_DIR).resolve()
    try:
        target = (base / rel_path).resolve()
    except Exception:
        return None
    if target != base and base not in target.parents:
        return None  # path traversal — escaped the workspace
    if claude_runner._looks_secret(str(target)):
        return None  # never expose .env / keys / credentials
    return target


def read_file(path: str) -> str:
    """Read a UTF-8 text file from the project workspace and return its contents.

    Use this to open department master-prompts and source files. When the User asks
    for a department's prompt, read its .md and return the real content — never say
    "no access". Paths are relative to the project root.

    Args:
        path: File path relative to the project root, e.g. "Translation Department.md".
    """
    target = _safe_path(path)
    if target is None:
        return f"⛔ Access denied or path escapes workspace: {path}"
    if not target.is_file():
        return f"⚠️ Not a file: {path}"
    try:
        text = target.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return f"⚠️ Could not read {path}: {e}"
    if len(text) > _MAX_TOOL_READ_CHARS:
        return text[:_MAX_TOOL_READ_CHARS] + f"\n\n…(truncated; {len(text)} chars total)"
    return text


def list_dir(path: str = ".") -> str:
    """List files and folders inside a project directory (one entry per line).

    Use this to discover what exists before reading. Hidden and secret-looking
    entries are omitted. Paths are relative to the project root.

    Args:
        path: Directory relative to the project root. Defaults to the project root.
    """
    target = _safe_path(path)
    if target is None:
        return f"⛔ Access denied or path escapes workspace: {path}"
    if not target.is_dir():
        return f"⚠️ Not a directory: {path}"
    try:
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except Exception as e:
        return f"⚠️ Could not list {path}: {e}"
    lines = []
    for p in entries:
        if p.name.startswith(".") or claude_runner._looks_secret(p.name):
            continue
        lines.append(f"[dir] {p.name}" if p.is_dir() else p.name)
    return "\n".join(lines) or "(empty)"


# --- Department registry: real paths Antigravity can read_file() on demand ---
# Only entries whose file actually exists are rendered, so the registry never lies.
_DEPARTMENT_REGISTRY = [
    ("Central AI Team/Agent Organization.md", "მასტერ-რუკა: 9 დეპარტამენტი + Council (ყველაფრის ინდექსი)"),
    ("Central AI Team/Agent Registry.md", "აგენტების რეესტრი"),
    ("Central AI Team/Council.md", "აგენტების საბჭო — 5 მრჩეველი, გადაწყვეტილებების წნეხის-ტესტი"),
    ("Central AI Team/Departments/Translation Department.md", "თარჯიმნების დეპარტამენტი — 4-აგენტიანი კონვეიერი + ლექსიკონი"),
    ("Central AI Team/Departments/Data Tech Architecture.md", "მონაცემთა ინჟინერია & სისტემური არქიტექტურა"),
    ("Central AI Team/Departments/Cybersecurity Compliance.md", "კიბერუსაფრთხოება & შესაბამისობა"),
    ("Central AI Team/Departments/RD Lab.md", "ინოვაციებისა და კვლევების ლაბორატორია (R&D)"),
    ("Central AI Team/Departments/Innovation Department.md", "ტექნოლოგიური ოპტიმიზაცია & ჰოსტინგ-სკაუტინგი"),
    ("Central AI Team/Departments/Archive Department.md", "არქივის დეპარტამენტი — სესიების Ledger"),
    ("Central AI Team/Departments/Cockpit UI UX Studio.md", "UI/UX სტუდია — 8 აგენტი, პრემიუმ ესთეტიკა"),
    ("Central AI Team/Departments/Marketing Department.md", "მარკეტინგის დეპარტამენტი"),
    ("Central AI Team/Departments/Aftersales CX Analytics.md", "მომხმარებლის გამოცდილება & ანალიტიკა"),
]


def _render_registry() -> str:
    base = Path(config.WORKSPACE_DIR)
    lines = [f"- `{rel}` — {purpose}"
             for rel, purpose in _DEPARTMENT_REGISTRY if (base / rel).is_file()]
    return "\n".join(lines) or "(რეესტრის ფაილები ვერ მოიძებნა)"


# Built-in fallback used only if telegram_bot/ANTIGRAVITY_PERSONA.md is missing.
_ANTIGRAVITY_FALLBACK_PROMPT = """You are Antigravity, the strategic Master Architect of the Porsche Aftersales AI Software House.
Your focus is overall system architecture, database schema design, and compliance with the premium Porsche design code.
ALWAYS respond in Georgian (ქართული ენა). Speak as a critical-thinking advisor, not a servile assistant: do not open with agreement, tag confidence ([Certain]/[Likely]/[Guessing]), lead with the uncomfortable truth, and disagree only with a concrete reason and an alternative. Match length to the message.
"""


def _load_antigravity_persona() -> str:
    """Build Antigravity's live system prompt: the canonical persona authored by
    Antigravity itself (ANTIGRAVITY_PERSONA.md), plus the operational context and the
    read-only department registry. Falls back to a built-in prompt if the file is
    missing. See ROUTER_PROTOCOL.md §6."""
    persona_path = Path(__file__).parent / "ANTIGRAVITY_PERSONA.md"
    try:
        canonical = persona_path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception as e:
        logger.warning(f"ANTIGRAVITY_PERSONA.md not loaded ({e}); using built-in fallback persona.")
        return _ANTIGRAVITY_FALLBACK_PROMPT
    return (
        canonical
        + "\n\n--- საოპერაციო კონტექსტი ---\n"
        + "შენ ესაუბრები User-ს და Claude-ს (Lead Developer). მიმართე ორივეს. უპასუხე Markdown-ში.\n"
        + "სიგრძე შეუსაბამე შეტყობინებას: მარტივ მისალმებას/საუბარს უპასუხე 1–2 ბუნებრივი წინადადებით — "
        + "არანაირი სქემა, დიზაინ-ტოკენი ან არქიტექტურა, თუ User-მა ნამდვილად არ ჰკითხა.\n\n"
        + "--- ფაილებზე წვდომა (READ-ONLY) ---\n"
        + "გაქვს ორი ხელსაწყო: `read_file(path)` და `list_dir(path)` (path = პროექტის ფესვიდან).\n"
        + "როცა User ჰკითხავს დეპარტამენტის პრომფთს ან პროექტის ფაილს — **წაიკითხე ხელსაწყოთი და "
        + "დააბრუნე ნამდვილი შიგთავსი**, არასდროს თქვა „წვდომა არ მაქვს“. ზუსტი სახელი თუ არ იცი, ჯერ "
        + "`list_dir`-ით მონახე. ფაილებს ვერ ცვლი — მხოლოდ კითხულობ.\n\n"
        + "დეპარტამენტების რეესტრი:\n"
        + _render_registry()
    )


# System Prompts
ANTIGRAVITY_SYSTEM_PROMPT = _load_antigravity_persona()

CLAUDE_SYSTEM_PROMPT = """You are Claude, the Lead Developer and Senior Code Auditor of the Porsche Aftersales AI Software House.
Your focus is writing clean, high-performance, and secure code, building FastAPI backend APIs, writing Vanilla JavaScript, styling with raw CSS, and database indexing.
You are chatting with the User and Antigravity (who acts as the Master Architect).

When replying:
1. ALWAYS respond in Georgian (ქართული ენა) — this is mandatory for every message.
2. Speak in a detail-oriented, practical, and execution-focused tone.
3. Address both the User and Antigravity.
4. Review Antigravity's architectural proposals and point out potential implementation issues, optimization suggestions, or exact code blocks.
5. Output your responses in clean Markdown with appropriate code blocks.
6. Match your response length to the message. If it is a simple greeting, social chat, or not a technical request, reply in 1–2 short, polite sentences in Georgian — NO code blocks, NO schemas, NO headers, NO long reviews. Only write code or detailed analysis when the user actually asks for technical work.
6. If the user's message is a simple greeting, social chat, or not related to technical tasks, DO NOT write code blocks, database migrations, or audit reports. Just respond shortly, politely, and appropriately in Georgian.
"""

def _call_gemini(prompt: str) -> str:
    """Synchronous Gemini SDK call. Runs off the event loop via asyncio.to_thread."""
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=ANTIGRAVITY_SYSTEM_PROMPT,
            temperature=0.3,
            # Georgian is token-heavy (non-Latin script). 2048 truncated debate
            # replies mid-word, including the Final Consensus — give ample room.
            max_output_tokens=8192,
            # Read-only project access via automatic Function Calling. The SDK executes
            # these locally and loops until Antigravity has a final answer.
            tools=[read_file, list_dir],
        )
    )
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        usage = {
            "input_tokens": response.usage_metadata.prompt_token_count,
            "output_tokens": response.usage_metadata.candidates_token_count,
        }
        cost = (usage["input_tokens"] * 0.075 / 1_000_000) + (usage["output_tokens"] * 0.30 / 1_000_000)
        budget.add_tokens(usage, cost)
    return response.text

def _call_claude(prompt: str) -> str:
    """Synchronous Claude SDK call. Runs off the event loop via asyncio.to_thread."""
    message = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        temperature=0.3,
        system=CLAUDE_SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )
    if hasattr(message, "usage") and message.usage:
        usage = {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
        }
        # Cost estimate for Claude 3.5 Sonnet: input $3/M, output $15/M
        cost = (usage["input_tokens"] * 3.0 / 1_000_000) + (usage["output_tokens"] * 15.0 / 1_000_000)
        budget.add_tokens(usage, cost)
    return message.content[0].text

_GEMINI_TRANSIENT = ("503", "UNAVAILABLE", "overloaded", "500", "INTERNAL", "deadline")

async def run_gemini_operation(api_call_func, *args, retries=2, **kwargs):
    """Executes a Gemini synchronous API function with backoff for transient errors
    and automatic API key rotation for rate/quota limits."""
    global gemini_client
    if not gemini_client:
        init_clients()
    if not gemini_client:
        raise ValueError("Gemini client not configured.")

    keys_count = len(config.GEMINI_API_KEYS)
    attempts_per_key = retries + 1
    total_keys_to_try = max(1, keys_count)
    
    for key_idx in range(total_keys_to_try):
        delay = 2.0
        for attempt in range(attempts_per_key):
            try:
                return await asyncio.to_thread(api_call_func, *args, **kwargs)
            except Exception as e:
                last_err = str(e)
                # Check for rate/quota limits
                is_quota = any(tok in last_err for tok in ["429", "RESOURCE_EXHAUSTED", "quota"])
                # Check for transient errors
                is_transient = any(tok in last_err for tok in _GEMINI_TRANSIENT)
                
                if is_quota and keys_count > 1:
                    logger.warning(
                        f"Gemini call hit quota/rate limit. Rotating API Key. "
                        f"Error: {last_err[:200]}"
                    )
                    get_next_gemini_client()
                    # Break out of the inner retry loop to try the new key immediately
                    break
                
                if is_transient and attempt < retries:
                    logger.warning(
                        f"Gemini call hit transient error (attempt {attempt + 1}/{attempts_per_key}). "
                        f"Retrying in {delay}s. Error: {last_err[:200]}"
                    )
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                
                # If we have other keys left to try, we can rotate and try them.
                if keys_count > 1 and key_idx < total_keys_to_try - 1:
                    logger.warning(
                        f"Gemini call failed on current key. Rotating to next key. "
                        f"Error: {last_err[:200]}"
                    )
                    get_next_gemini_client()
                    break
                
                # Otherwise, raise the exception
                raise e

async def query_gemini(prompt: str) -> str:
    """Queries Gemini (Antigravity) without blocking the bot's event loop."""
    if killswitch.is_engaged():
        return "🛑 Kill-switch ჩართულია — Gemini (Antigravity) გაყინულია. გაათავისუფლე `/resume`-ით."
    if budget.budget_exceeded():
        return f"⛔ დღიური ლიმიტი ამოიწურა. ხვალ განახლდება.\n{budget.summary()}"

    global gemini_client
    if not gemini_client:
        return "⚠️ Gemini API key is not configured or client failed to initialize."

    try:
        return await run_gemini_operation(_call_gemini, prompt, retries=1)
    except Exception as e:
        logger.error(f"Error querying Gemini: {e}")
        return f"⚠️ Gemini Error: {e}"

async def query_claude(prompt: str) -> str:
    """Queries Claude without blocking the bot's event loop.

    When CLAUDE_DISCUSSION_VIA_CLI is set (default), Claude answers through the
    local Claude Code CLI on your Claude Pro subscription — no Anthropic API key
    or credits needed. Otherwise it falls back to the Anthropic API."""
    if killswitch.is_engaged():
        return "🛑 Kill-switch ჩართულია — Claude (Pro CLI) გაყინულია. გაათავისუფლე `/resume`-ით."
    if budget.budget_exceeded():
        return f"⛔ დღიური ლიმიტი ამოიწურა. ხვალ განახლდება.\n{budget.summary()}"

    if config.CLAUDE_DISCUSSION_VIA_CLI:
        return await claude_runner.run_claude_chat(prompt, CLAUDE_SYSTEM_PROMPT)

    global claude_client
    if not claude_client:
        return "⚠️ Claude API key is not configured or client failed to initialize."

    try:
        # Offload the blocking SDK call so the Telegram poll loop stays responsive
        return await asyncio.to_thread(_call_claude, prompt)
    except Exception as e:
        logger.error(f"Error querying Claude: {e}")
        return f"⚠️ Claude Error: {e}"

# --- Stage 1 Router (Facilitator): structured-JSON dispatch ---
# Gemini reformulates the task neutrally and decides routing, returning STRICT JSON.
# Parsing is defensive: response_mime_type forces JSON, a regex fallback salvages a
# JSON object from any stray prose, and every failure path degrades to {"ok": False}
# so the bot can fall back to normal chat instead of crashing (ROUTER_PROTOCOL.md §5.1).
ROUTER_SYSTEM_PROMPT = """You are the Facilitator/Router for a two-agent system: Antigravity (Master Architect) and Claude (Lead Developer).
Restate the user's task neutrally, then decide routing. Respond with STRICT JSON ONLY — no prose, no markdown fences, nothing before or after the object:
{"reformulated_task": "<neutral one-line restatement>", "assignee": "antigravity|claude|both", "mode": "discuss|plan|autonomous", "scope": ["likely/file.py"], "council": false}
Rules:
- mode = "autonomous" ONLY when the user clearly asks to modify, build, implement, fix, or refactor code. Reading, explaining, reviewing, or designing is "discuss" or "plan".
- assignee = "claude" for code/implementation, "antigravity" for architecture/design/brand, "both" for review-style or cross-cutting work.
- scope = list of likely file paths, or [] if unknown.
- council = true ONLY when the task is a high-stakes, hard-to-reverse DECISION or a fundamental architectural/strategic choice worth stress-testing from many angles (e.g. "should we migrate the DB", "pick an auth model", "is this product direction right"). A council run is expensive, so default to false for ordinary coding, reading, or small fixes. This is only a SUGGESTION — a human still decides whether to spend it.
Output the JSON object and nothing else."""


def _route_gemini(task: str) -> str:
    """Synchronous router call. JSON mode makes Gemini emit a bare JSON object."""
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=task,
        config=types.GenerateContentConfig(
            system_instruction=ROUTER_SYSTEM_PROMPT,
            temperature=0.0,
            max_output_tokens=512,
            response_mime_type="application/json",
        )
    )
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        usage = {
            "input_tokens": response.usage_metadata.prompt_token_count,
            "output_tokens": response.usage_metadata.candidates_token_count,
        }
        cost = (usage["input_tokens"] * 0.075 / 1_000_000) + (usage["output_tokens"] * 0.30 / 1_000_000)
        budget.add_tokens(usage, cost)
    return response.text


def _extract_json(text: str) -> dict | None:
    """Parse a JSON object from model output. Tries a direct load, then salvages the
    first {...} block if the model wrapped it in prose/fences. Never raises."""
    if not text:
        return None
    for candidate in (text, ):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
    return None


async def run_routed(task: str) -> dict:
    """Route a task via the Facilitator. Returns a normalized dict:
    {ok, reformulated_task, assignee, mode, scope, raw} or {ok: False, error, raw}.
    NEVER raises — the caller falls back to plain chat on {ok: False}."""
    if killswitch.is_engaged():
        return {"ok": False, "error": "🛑 Kill-switch ჩართულია — მარშრუტიზაცია გაყინულია.", "raw": ""}

    init_clients()  # idempotent: the client is lazily initialized, like the chat paths
    if not gemini_client:
        return {"ok": False, "error": "Gemini client not configured", "raw": ""}
    try:
        raw = await run_gemini_operation(_route_gemini, task, retries=1)
    except Exception as e:
        logger.error(f"Router call failed: {e}")
        return {"ok": False, "error": f"router call failed: {e}", "raw": ""}

    data = _extract_json(raw)
    if not isinstance(data, dict):
        return {"ok": False, "error": "router did not return valid JSON", "raw": raw or ""}

    assignee = str(data.get("assignee", "both")).lower().strip()
    if assignee not in ("antigravity", "claude", "both"):
        assignee = "both"
    mode = str(data.get("mode", "discuss")).lower().strip()
    if mode not in ("discuss", "plan", "autonomous"):
        mode = "discuss"
    scope = data.get("scope") or []
    if not isinstance(scope, list):
        scope = [str(scope)]
    reformulated = str(data.get("reformulated_task") or task).strip() or task
    council_suggested = bool(data.get("council", False))

    return {
        "ok": True,
        "reformulated_task": reformulated,
        "assignee": assignee,
        "mode": mode,
        "scope": [str(s) for s in scope][:20],
        "council_suggested": council_suggested,
        "raw": raw or "",
    }


def inject_context(session: dict, user_msg: str) -> str:
    """Formats the current user prompt with history and file context."""
    history_str = format_chat_history(session["history"])
    
    context_str = ""
    if session["temp_file_context"]:
        ctx = session["temp_file_context"]
        context_str = f"--- ATTACHED WORKSPACE FILE: {ctx['filename']} ---\n{ctx['content']}\n---------------------------------------------\n\n"
        
    full_prompt = f"""Conversation History:
{history_str}

{context_str}User's Current Message:
"{user_msg}"
"""
    return full_prompt

async def run_solo_gemini(chat_id: int, user_msg: str) -> str:
    """Runs a single prompt targeting only Gemini (Antigravity)."""
    init_clients()
    session = get_session(chat_id)
    
    prompt = inject_context(session, user_msg)
    response = await query_gemini(prompt)
    
    # Save to history
    session["history"].append({"sender": "User", "text": user_msg})
    session["history"].append({"sender": "Antigravity", "text": response})
    
    # Clear temp context after query
    session["temp_file_context"] = None
    
    return response

async def run_solo_claude(chat_id: int, user_msg: str) -> str:
    """Runs a single prompt targeting only Claude."""
    init_clients()
    session = get_session(chat_id)
    
    prompt = inject_context(session, user_msg)
    response = await query_claude(prompt)
    
    # Save to history
    session["history"].append({"sender": "User", "text": user_msg})
    session["history"].append({"sender": "Claude", "text": response})
    
    # Clear temp context after query
    session["temp_file_context"] = None
    
    return response

async def run_collaborative(chat_id: int, user_msg: str):
    """
    Runs Gemini (Antigravity) first, then feeds Gemini's response 
    to Claude for review. Returns both responses.
    """
    init_clients()
    session = get_session(chat_id)
    
    prompt_gemini = inject_context(session, user_msg)
    gemini_resp = await query_gemini(prompt_gemini)
    
    # Construct prompt for Claude, including Gemini's live response
    history_str = format_chat_history(session["history"])
    context_str = ""
    if session["temp_file_context"]:
        ctx = session["temp_file_context"]
        context_str = f"--- ATTACHED WORKSPACE FILE: {ctx['filename']} ---\n{ctx['content']}\n---------------------------------------------\n\n"
        
    prompt_claude = f"""Conversation History:
{history_str}

{context_str}User's Current Message:
"{user_msg}"

[Antigravity (Gemini) Response]:
{gemini_resp}

Claude, respond in Georgian (ქართული ენა). Match your length to the user's message: if it is a greeting or casual/non-technical message, reply in 1–2 short, natural sentences with NO code, NO review of Antigravity, NO headers. ONLY when the user is asking for technical work should you review Antigravity's response and add concrete improvements, code, or corrections.
"""
    claude_resp = await query_claude(prompt_claude)
    
    # Save to history
    session["history"].append({"sender": "User", "text": user_msg})
    session["history"].append({"sender": "Antigravity", "text": gemini_resp})
    session["history"].append({"sender": "Claude", "text": claude_resp})
    
    # Clear temp context
    session["temp_file_context"] = None
    
    return gemini_resp, claude_resp

async def run_debate(chat_id: int, user_msg: str):
    """
    Multi-round, hard-capped debate loop (ROUTER_PROTOCOL.md §7 Stage 2).

    Structure: Gemini makes an initial proposal, then for each round Claude
    critiques the latest proposal and Gemini refines in response. The last
    Gemini refinement is the "final consensus".

    The round count is clamped to [1, config.DEBATE_ROUNDS_CEILING] via
    config.MAX_DEBATE_ROUNDS, so this agent<->agent exchange can NEVER
    ping-pong unbounded without a human (§5.2 — the protocol bans exactly that).
    A daily-budget check before each (expensive) Claude turn stops the loop
    early if the day's token budget is already spent (§5.4).

    With MAX_DEBATE_ROUNDS=1 this reproduces the original 3-turn behavior
    (Gemini → Claude → Gemini).

    Returns an ordered list of (icon, label, text) turns for the caller to
    render in sequence.
    """
    init_clients()
    session = get_session(chat_id)

    rounds = max(1, min(config.MAX_DEBATE_ROUNDS, config.DEBATE_ROUNDS_CEILING))

    history_str = format_chat_history(session["history"])
    context_str = ""
    if session["temp_file_context"]:
        ctx = session["temp_file_context"]
        context_str = f"--- ATTACHED WORKSPACE FILE: {ctx['filename']} ---\n{ctx['content']}\n---------------------------------------------\n\n"

    turns: list[tuple[str, str, str]] = []

    # Gemini's initial proposal (full session context injected).
    current_proposal = await query_gemini(inject_context(session, user_msg))
    turns.append(("🤖", "Antigravity (Initial Proposal)", current_proposal))

    last_critique = ""
    for r in range(1, rounds + 1):
        # Budget guard BEFORE the expensive Claude turn (§5.4). Stop, don't crash.
        if budget.budget_exceeded():
            turns.append(("🧮", f"Debate stopped (round {r})",
                          f"დღიური ტოკენ-ბიუჯეტი ამოწურულია — დებატი შეწყდა.\n{budget.summary()}"))
            break

        # Claude critiques the latest Gemini proposal.
        prompt_claude = f"""Conversation History:
{history_str}

{context_str}User's Current Message:
"{user_msg}"

[Antigravity (Gemini) Proposal — round {r} of {rounds}]:
{current_proposal}

Claude, audit and critique this proposal. Point out weaknesses, optimization points, or security issues. Be specific and concise. Respond in Georgian (ქართული ენა).
"""
        last_critique = await query_claude(prompt_claude)
        critique_label = "Claude (Audit & Critique)" if rounds == 1 else f"Claude (Critique — round {r}/{rounds})"
        turns.append(("👤", critique_label, last_critique))

        # Gemini refines in response to Claude.
        is_final = (r == rounds)
        instruction = ("respond to his critiques and make a refined FINAL architectural recommendation"
                       if is_final else
                       "respond to his critiques and refine your proposal for the next round")
        prompt_gemini = f"""Conversation History:
{history_str}

{context_str}User's Current Message:
"{user_msg}"

[Your Latest Proposal (Antigravity) — round {r} of {rounds}]:
{current_proposal}

[Claude's Audit & Critique — round {r}]:
{last_critique}

Please review Claude's audit, {instruction}. Respond in Georgian (ქართული ენა).
"""
        current_proposal = await query_gemini(prompt_gemini)
        refine_label = "Antigravity (Final Consensus)" if is_final else f"Antigravity (Refinement — round {r}/{rounds})"
        turns.append(("🏁" if is_final else "🤖", refine_label, current_proposal))

    # Persist a compact trace of the debate to session history.
    session["history"].append({"sender": "User", "text": user_msg})
    session["history"].append({"sender": "Claude", "text": last_critique})
    session["history"].append({"sender": "Antigravity", "text": current_proposal})

    # Clear temp context
    session["temp_file_context"] = None

    return turns


# ============================================================================
# Full Council (ROUTER_PROTOCOL.md §7 Stage 3 — Central AI Team/Council.md)
# ============================================================================
# Five advisors, each a thinking *style* that leans fully into its angle, run as
# INDEPENDENT cold calls: every advisor sees only the neutrally-reformulated
# question and its own persona — no session history, no other advisors' answers,
# no file context. That independence is the whole point; a council where one
# model role-plays five voices is a council in costume only.
#
# Independence is reinforced by spreading the five across BOTH backends, so the
# disagreement is partly cross-model rather than five samples from one
# distribution. The Contrarian and Outsider go to Claude (Pro CLI); First
# Principles, Expansionist, and Executor go to Gemini. Advisors get NO file
# tools — the council stress-tests an idea, it does not audit the repo.
#
# Default run = 5 advisors + 1 chair = ~6 calls. With COUNCIL_PEER_REVIEW the
# advisors also peer-review the anonymized set (+5 calls) = the protocol's "~11".
import random

_COUNCIL_REFORMULATE_PROMPT = (
    "You are the Council Facilitator. Take the user's raw question/idea/decision and rewrite it as a "
    "single neutral, self-contained prompt that all five advisors will receive. Include the core "
    "decision, the key context, and what is at stake — but add NO opinion and NO advice. "
    "Respond in Georgian (ქართული). Output ONLY the reformulated question, nothing else."
)

# (key, icon, label, backend, persona-system-prompt). Display/answer order matches
# Council.md: Contrarian → First Principles → Expansionist → Outsider → Executor.
_COUNCIL_BASE = (
    "You are one advisor in a 5-member stress-test council. You are a thinking STYLE, not a job title. "
    "Lean FULLY into your angle — the other advisors cover what you ignore; do not hedge or balance. "
    "Answer in 150–300 words, in Georgian (ქართული ენა), direct, no preamble, no greeting."
)
_COUNCIL_ADVISORS = [
    ("contrarian", "🔴", "კონტრარიანი", "claude",
     _COUNCIL_BASE + "\n\nYOUR STYLE — The Contrarian: actively hunt for what is wrong, missing, or will "
     "fail. Assume the idea has a fatal flaw and try to find it. You are not a pessimist — you are the "
     "friend who protects from a bad deal by asking the questions being avoided."),
    ("first_principles", "🧱", "პირველადი პრინციპები", "gemini",
     _COUNCIL_BASE + "\n\nYOUR STYLE — First Principles: ignore the surface question. Ask 'what are we "
     "really trying to solve here?'. Strip away assumptions, rebuild from the ground up, and say so if "
     "the wrong question is being asked entirely."),
    ("expansionist", "🚀", "ექსპანსიონისტი", "gemini",
     _COUNCIL_BASE + "\n\nYOUR STYLE — The Expansionist: find the perspective everyone else leaves behind. "
     "What could be bigger? What adjacent opportunity is hiding? You don't worry about risk — you worry "
     "about what happens if this works far better than expected."),
    ("outsider", "🌫️", "აუტსაიდერი", "claude",
     _COUNCIL_BASE + "\n\nYOUR STYLE — The Outsider: you have ZERO context about this person, their domain, "
     "or their history. Respond only to what is literally in front of you. Flag the 'curse of knowledge' — "
     "things that are obvious to insiders but confusing to everyone else."),
    ("executor", "⚙️", "შემსრულებელი", "gemini",
     _COUNCIL_BASE + "\n\nYOUR STYLE — The Executor: you care only whether this can be done and the fastest "
     "path to doing it. Ignore theory and big-picture musing. View every idea through one lens: 'fine, but "
     "what do you do Monday morning?'."),
]

_CHAIR_PROMPT = (
    "You are the Council Chair. You see the reformulated question and all five de-anonymized advisor "
    "answers (plus peer reviews if present). Do NOT average the answers and do NOT paper over "
    "disagreement. Synthesize a final verdict in Georgian (ქართული) using EXACTLY these five Markdown "
    "sections, in this order:\n"
    "## რაზე თანხმდება საბჭო\n## სად არის დაპირისპირება საბჭოში\n## ბრმა წერტილები, რომლებიც საბჭომ დაიჭირა\n"
    "## რეკომენდაცია\n## პირველი რიგის ამოცანა\n"
    "The recommendation must be a real, direct answer with reasoning — not 'it depends'. You may side "
    "with a minority advisor if their reasoning is stronger. The first-priority task is exactly ONE "
    "concrete next step."
)


def _call_gemini_plain(prompt: str, system_prompt: str, temperature: float = 0.8,
                       max_tokens: int = 4096) -> str:
    """A Gemini call with an ARBITRARY system prompt and NO file tools — used for
    council advisors, peer review, reformulation, and the chair. Distinct from
    _call_gemini (which always wears Antigravity's persona and carries file tools)."""
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
    )
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        usage = {
            "input_tokens": response.usage_metadata.prompt_token_count,
            "output_tokens": response.usage_metadata.candidates_token_count,
        }
        cost = (usage["input_tokens"] * 0.075 / 1_000_000) + (usage["output_tokens"] * 0.30 / 1_000_000)
        budget.add_tokens(usage, cost)
    return response.text


async def query_gemini_plain(prompt: str, system_prompt: str, temperature: float = 0.8,
                             max_tokens: int = 4096, retries: int = 2) -> str:
    """Async wrapper for _call_gemini_plain with bounded retry+backoff on transient
    errors. Never raises — returns a ⚠️ string once retries are exhausted."""
    if killswitch.is_engaged():
        return "🛑 Kill-switch ჩართულია — Gemini (Antigravity) გაყინულია. გაათავისუფლე `/resume`-ით."
    if budget.budget_exceeded():
        return f"⛔ დღიური ლიმიტი ამოიწურა. ხვალ განახლდება.\n{budget.summary()}"

    if not gemini_client:
        return "⚠️ Gemini client not configured."

    try:
        return await run_gemini_operation(
            _call_gemini_plain, prompt, system_prompt, temperature, max_tokens, retries=retries
        )
    except Exception as e:
        return f"⚠️ Gemini Error: {e}"


async def _ask_one_advisor(advisor: tuple, reformulated: str) -> tuple[str, str, str]:
    """Run a single advisor on its assigned backend. Returns (icon, label, text).
    Each call is independent: it receives ONLY the reformulated question + persona."""
    _key, icon, label, backend, persona = advisor
    prompt = (f"ფორმულირებული კითხვა საბჭოსთვის:\n\n{reformulated}\n\n"
              f"უპასუხე შენი როლის შესაბამისად.")
    if backend == "claude":
        text = await claude_runner.run_claude_chat(prompt, persona, timeout=config.COUNCIL_ADVISOR_TIMEOUT)
    else:
        text = await query_gemini_plain(prompt, persona, temperature=0.85, max_tokens=4096)
    return (icon, label, (text or "").strip() or "(ცარიელი პასუხი)")


async def _council_peer_review(reformulated: str, answers: list[tuple[tuple, str]]
                               ) -> tuple[list[tuple[str, str, str]], str]:
    """Anonymize the five answers as Response A–E (random letters), then re-invoke
    each advisor as a reviewer answering the three Council.md review questions.
    Returns (review_turns, anonymity_map_text)."""
    letters = list("ABCDE")[:len(answers)]
    random.shuffle(letters)
    # lettered: [(letter, advisor, text)] sorted A..E for a stable anonymized block
    lettered = sorted(((ltr, adv, txt) for ltr, (adv, txt) in zip(letters, answers)),
                      key=lambda x: x[0])
    anon_block = "\n\n".join(f"Response {ltr}:\n{txt}" for ltr, _adv, txt in lettered)

    async def _review(advisor: tuple) -> tuple[str, str, str]:
        _key, icon, label, backend, persona = advisor
        prompt = (
            f"ფორმულირებული კითხვა:\n{reformulated}\n\n"
            f"ქვემოთ ხუთი ანონიმური პასუხია (Response A–E):\n\n{anon_block}\n\n"
            "შენ ახლა რეცენზენტი ხარ. წაიკითხე ხუთივე და უპასუხე სამ კითხვას ქართულად, 200 სიტყვამდე, "
            "პასუხებზე ასოებით მითითებით:\n"
            "1. რომელი პასუხია ყველაზე ძლიერი და რატომ? (ერთი ასო)\n"
            "2. რომელ პასუხს აქვს ყველაზე დიდი ბრმა წერტილი და რა აკლია? (ერთი ასო)\n"
            "3. რა გამორჩა ხუთივეს, რაც საბჭომ უნდა განიხილოს?"
        )
        if backend == "claude":
            text = await claude_runner.run_claude_chat(prompt, persona, timeout=config.COUNCIL_ADVISOR_TIMEOUT)
        else:
            text = await query_gemini_plain(prompt, persona, temperature=0.5, max_tokens=2048)
        return (icon, f"{label} (რეცენზია)", (text or "").strip() or "(ცარიელი რეცენზია)")

    review_turns = list(await asyncio.gather(*[_review(a) for a in _COUNCIL_ADVISORS]))
    anon_map = "\n".join(f"Response {ltr} = {adv[2]}" for ltr, adv, _txt in lettered)
    return review_turns, anon_map


async def _chair_verdict(reformulated: str, answers: list[tuple[tuple, str]],
                         review_turns: list[tuple[str, str, str]] | None) -> str:
    """Synthesize the final 5-section verdict from the de-anonymized answers."""
    advisor_block = "\n\n".join(f"### {adv[2]}\n{txt}" for adv, txt in answers)
    review_block = ""
    if review_turns:
        review_block = "\n\n--- კოლეგების მიმოხილვა ---\n" + "\n\n".join(
            f"### {label}\n{txt}" for _icon, label, txt in review_turns)
    prompt = (
        f"ფორმულირებული კითხვა:\n{reformulated}\n\n"
        f"--- ხუთი მრჩევლის პასუხი ---\n{advisor_block}{review_block}\n\n"
        "ახლა შეასრულე თავმჯდომარის ვერდიქტი ზემოთ მითითებული ზუსტი 5-სექციიანი სტრუქტურით."
    )
    verdict = (await query_gemini_plain(prompt, _CHAIR_PROMPT, temperature=0.4, max_tokens=8192)).strip()
    # The verdict is the council's whole point — it must not die because one
    # backend is briefly down. If Gemini still failed after its retries, synthesize
    # the verdict on the OTHER backend (Claude Pro CLI) instead.
    if not verdict or verdict.startswith("⚠️"):
        logger.warning("Chair verdict via Gemini failed; falling back to Claude CLI.")
        claude_verdict = (await claude_runner.run_claude_chat(
            prompt, _CHAIR_PROMPT, timeout=config.COUNCIL_ADVISOR_TIMEOUT)).strip()
        if claude_verdict and not claude_verdict.startswith("⚠️"):
            verdict = claude_verdict + "\n\n_(ვერდიქტი Claude-მ შეადგინა — Gemini დროებით მიუწვდომელი იყო.)_"
    return verdict


async def run_council(chat_id: int, question: str, peer_review: bool | None = None
                      ) -> list[tuple[str, str, str]]:
    """Run the full 5-advisor council on a decision/idea (ROUTER_PROTOCOL.md §7).

    Pipeline: reformulate → 5 independent advisors (concurrent, cross-backend) →
    [optional anonymous peer review + reveal map] → chair verdict. Returns an
    ordered list of (icon, label, text) turns for the caller to render in
    sequence. NEVER raises — every leg degrades to a ⚠️ string instead.

    Cost: ~6 calls by default, ~11 with peer_review. A daily-budget check up
    front refuses to start once today's token budget is spent (§5.4)."""
    init_clients()
    if peer_review is None:
        peer_review = config.COUNCIL_PEER_REVIEW
    if not gemini_client:
        return [("⚠️", "Council", "Gemini client not configured — council unavailable.")]
    if budget.budget_exceeded():
        return [("🧮", "Council", f"დღიური ტოკენ-ბიუჯეტი ამოწურულია — საბჭო არ გაშვებულა.\n{budget.summary()}")]

    turns: list[tuple[str, str, str]] = []

    # 1. Neutral reformulation (Council.md variable 1).
    reformulated = (await query_gemini_plain(question, _COUNCIL_REFORMULATE_PROMPT,
                                             temperature=0.2, max_tokens=1024)).strip()
    if not reformulated or reformulated.startswith("⚠️"):
        reformulated = question
    turns.append(("🧭", "ფორმულირებული კითხვა", reformulated))

    # 2. Five independent advisors, concurrent across both backends.
    advisor_results = await asyncio.gather(
        *[_ask_one_advisor(a, reformulated) for a in _COUNCIL_ADVISORS])
    turns.extend(advisor_results)
    answers = [(adv, txt) for adv, (_icon, _label, txt) in zip(_COUNCIL_ADVISORS, advisor_results)]

    # 3. Optional anonymous peer review (the extra ~5 calls → full "~11").
    review_turns = None
    if peer_review and not budget.budget_exceeded():
        review_turns, anon_map = await _council_peer_review(reformulated, answers)
        turns.extend(review_turns)
        turns.append(("🗺️", "ანონიმურობის რუკა", anon_map))

    # 4. Chair verdict — synthesis, not averaging.
    verdict = await _chair_verdict(reformulated, answers, review_turns)
    turns.append(("👑", "თავმჯდომარის ვერდიქტი", verdict))

    # Persist a compact trace to session history.
    session = get_session(chat_id)
    session["history"].append({"sender": "User", "text": question})
    session["history"].append({"sender": "Council", "text": verdict})

    return turns
