"""
Autonomous task runner: drives the local `claude` CLI in headless (-p/--print)
mode against the workspace, so a task issued from Telegram actually edits code,
runs commands, and (optionally) commits + pushes — with no "Allow" popups.

Security note: this executes real work on the machine with no interactive
approval. The Telegram layer MUST gate it behind config.is_authorized(user_id).
"""
import os
import json
import shutil
import asyncio
import logging
import subprocess
import config
import budget
import killswitch

logger = logging.getLogger("telegram_bot.claude_runner")


def _kill_tree(pid: int) -> None:
    """Kill a process AND its children. On Windows the `cmd /c` wrapper spawns
    claude/node as separate PIDs, so killing only the wrapper leaves orphans
    still editing the workspace — taskkill /T kills the whole tree."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(pid), 9)
    except Exception as e:
        logger.error(f"Failed to kill process tree {pid}: {e}")


def _resolve_claude() -> str | None:
    """Find the claude CLI executable (handles Windows .cmd shim)."""
    return shutil.which(config.CLAUDE_CLI_PATH)


def _pro_env() -> dict:
    """Environment for the claude CLI that forces it onto the Claude Pro login
    instead of API billing. The bot loads ANTHROPIC_API_KEY from .env into the
    process env; if the CLI inherits it, it bills API credits ("balance too low")
    instead of using the Pro subscription. Strip it (and the auth-token var) so
    the CLI falls back to the interactive Pro session."""
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def _spawn_kwargs() -> dict:
    """Detach the child from the bot's console so a Ctrl+C / console-control
    event aimed at the bot (or a sibling leg's `taskkill /T`) can't take down an
    in-flight `claude`. Without this, a shared-console run dies with
    STATUS_CONTROL_C_EXIT (0xC000013A = exit 3221225786) before it emits a byte.
    `_kill_tree` still targets by PID, so intentional kills keep working."""
    if os.name == "nt":
        return {"creationflags": (subprocess.CREATE_NEW_PROCESS_GROUP
                                  | subprocess.CREATE_NO_WINDOW)}
    # POSIX: own session/process group, same isolation goal.
    return {"start_new_session": True}


def _build_argv(exe: str) -> list[str]:
    """Static flag args only — the task prompt is fed via stdin, so no user
    content ever reaches a shell parser (no injection, no quoting issues)."""
    args = [
        "-p",
        "--permission-mode", config.CLAUDE_PERMISSION_MODE,
        # JSON (not text) so we can read usage/cost and feed the daily ledger.
        "--output-format", "json",
    ]
    # Per-task pre-emptive hard ceiling (ROUTER_PROTOCOL.md §5.4). The CLI stops
    # the run once computed cost crosses this — fires under Pro auth too, since
    # total_cost_usd is computed regardless of who pays. 0 = unlimited.
    if config.MAX_TASK_COST_USD > 0:
        args += ["--max-budget-usd", str(config.MAX_TASK_COST_USD)]
    if config.CLAUDE_PERMISSION_MODE == "bypassPermissions":
        args.append("--dangerously-skip-permissions")
    else:
        # acceptEdits auto-accepts file edits but, in headless -p mode, anything
        # else that needs approval is auto-DENIED — which is why the coder could
        # not run its own smoke tests. Grant LEAST-privilege execution: only
        # test-runner commands, never arbitrary Bash (that's what bypass is for).
        # Edit/Write/MultiEdit are listed explicitly so this allow-list can never
        # accidentally override acceptEdits' edit rights.
        args += [
            "--allowed-tools",
            "Edit", "Write", "MultiEdit",
            "Bash(python:*)", "Bash(python3:*)", "Bash(py:*)", "Bash(pytest:*)",
        ]
    full = ["cmd", "/c", exe, *args] if os.name == "nt" else [exe, *args]
    return full


def _parse_result_json(raw: str) -> dict | None:
    """Extract the single result object from `claude -p --output-format json`.
    Tolerates stray wrapper output (e.g. the Windows `cmd /c` shim) by falling
    back to the outermost {...} span. Returns None if nothing parses."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(raw[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


async def run_claude_task(task: str) -> dict:
    """Run a single autonomous task through `claude -p`. Returns
    {ok, output, error, budget}. The prompt is passed on stdin.

    Budget caps (ROUTER_PROTOCOL.md §5.4):
      * Pre-task: refuse before spawning if today's DAILY_TOKEN_BUDGET is spent.
      * In-task: --max-budget-usd (in _build_argv) caps a single run's cost.
      * Post-task: this run's usage is added to the daily ledger (budget.py),
        even on failure — tokens spent still count."""
    # Emergency stop — checked FIRST, before any spend guard: if the owner has
    # engaged the kill-switch, never spawn anything (ROUTER_PROTOCOL.md §5).
    if killswitch.is_engaged():
        return {"ok": False, "output": "",
                "error": ("🛑 Kill-switch ჩართულია — ავტონომიური სამუშაო გაყინულია. "
                          "გაათავისუფლე `/resume`-ით."),
                "budget": budget.summary()}

    # Pre-task daily ceiling — cheapest possible guard: never even spawn.
    if budget.budget_exceeded():
        return {"ok": False, "output": "",
                "error": (f"⛔ დღიური ტოკენ-ბიუჯეტი ამოწურულია "
                          f"({config.DAILY_TOKEN_BUDGET:,} tokens). განახლდება ხვალ."),
                "budget": budget.summary()}

    exe = _resolve_claude()
    if not exe:
        return {"ok": False, "output": "", "error": f"claude CLI not found (CLAUDE_CLI_PATH={config.CLAUDE_CLI_PATH})"}

    argv = _build_argv(exe)
    logger.info(f"Running autonomous task in {config.WORKSPACE_DIR}: {task[:80]!r}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=config.WORKSPACE_DIR,
            env=_pro_env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **_spawn_kwargs(),
        )
        # Register the live PID so /kill can terminate this run mid-flight, not
        # just block the next one. Unregistered in finally once it has exited.
        killswitch.register_pid(proc.pid)
        try:
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(input=task.encode("utf-8")),
                    timeout=config.AUTONOMOUS_TIMEOUT,
                )
            except asyncio.TimeoutError:
                _kill_tree(proc.pid)
                return {"ok": False, "output": "", "error": f"Task exceeded {config.AUTONOMOUS_TIMEOUT}s and was killed.",
                        "budget": budget.summary()}

            raw = stdout.decode("utf-8", errors="ignore").strip()
            parsed = _parse_result_json(raw)

            # Record usage first — tokens were spent regardless of outcome.
            if parsed is not None:
                budget.record(parsed.get("usage") or {}, parsed.get("total_cost_usd") or 0.0)

            if parsed is None:
                # No parseable JSON — surface raw text, trust the exit code.
                ok = proc.returncode == 0
                return {"ok": ok, "output": raw,
                        "error": "" if ok else f"claude exited with code {proc.returncode} (unparseable output)",
                        "budget": budget.summary()}

            result_text = (parsed.get("result") or "").strip()
            # claude's own JSON verdict is authoritative when present. The Windows
            # `cmd /c` shim can exit nonzero on a clean run (STATUS_CONTROL_C_EXIT,
            # a sibling leg's `taskkill /T`, child process-group signals), so a
            # nonzero returncode alone must NOT flip a declared success to failure —
            # that surfaced as the misleading "claude reported an error (success)".
            # Only trust the return code when there is no is_error field to read.
            if "is_error" in parsed:
                is_error = bool(parsed.get("is_error"))
            else:
                is_error = proc.returncode != 0
            if is_error:
                subtype = parsed.get("subtype") or parsed.get("stop_reason") or f"exit {proc.returncode}"
                return {"ok": False, "output": result_text,
                        "error": f"claude reported an error ({subtype}).",
                        "budget": budget.summary()}
            return {"ok": True, "output": result_text, "error": "", "budget": budget.summary()}
        finally:
            killswitch.unregister_pid(proc.pid)
    except Exception as e:
        logger.error(f"Autonomous task failed: {e}")
        return {"ok": False, "output": "", "error": str(e), "budget": budget.summary()}


async def run_claude_chat(prompt: str, system_prompt: str = "", timeout: int = 180) -> str:
    """Pure-text Claude response via the local CLI — uses Claude Pro, NO API key.

    Runs in the real ``WORKSPACE_DIR`` with READ-ONLY filesystem tools enabled
    (Read/Grep/Glob), so in discussion mode Claude can actually open the
    project's department prompts and source instead of saying "no access" — but
    it still cannot mutate anything. Two independent guards enforce read-only:

    * ``--allowed-tools Read Grep Glob`` — only these run without a prompt; in
      headless ``-p`` mode any other tool that needs approval is auto-denied.
    * ``--disallowed-tools Edit Write NotebookEdit Bash`` — a hard block on the
      mutating tools (deny wins over allow), so mutation is impossible even if
      the allow list were ever loosened.

    Mutation stays exclusive to the autonomous path (``run_claude_task``) behind
    the human gate. See ROUTER_PROTOCOL.md §2.2 and §5.3."""
    if killswitch.is_engaged():
        return "🛑 Kill-switch ჩართულია — Claude (Pro CLI) გაყინულია. გაათავისუფლე `/resume`-ით."

    exe = _resolve_claude()
    if not exe:
        return "⚠️ Claude CLI not found (install Claude Code or set CLAUDE_CLI_PATH)."

    args = [
        "-p",
        "--permission-mode", "default",
        "--allowed-tools", "Read", "Grep", "Glob",
        "--disallowed-tools", "Edit", "Write", "NotebookEdit", "Bash",
    ]
    if system_prompt:
        args += ["--append-system-prompt", system_prompt]
    full = ["cmd", "/c", exe, *args] if os.name == "nt" else [exe, *args]

    try:
        proc = await asyncio.create_subprocess_exec(
            *full, cwd=config.WORKSPACE_DIR, env=_pro_env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **_spawn_kwargs(),
        )
        # Register so /kill terminates an in-flight discussion/council leg too.
        killswitch.register_pid(proc.pid)
        try:
            try:
                out, _ = await asyncio.wait_for(
                    proc.communicate(input=prompt.encode("utf-8")), timeout=timeout
                )
            except asyncio.TimeoutError:
                _kill_tree(proc.pid)
                return "⚠️ Claude (Pro CLI) timed out."
            text = out.decode("utf-8", errors="ignore").strip()
            if proc.returncode != 0:
                return f"⚠️ Claude (Pro CLI) error: {text[-500:] or ('exit ' + str(proc.returncode))}"
            return text or "(no response)"
        finally:
            killswitch.unregister_pid(proc.pid)
    except Exception as e:
        logger.error(f"run_claude_chat failed: {e}")
        return f"⚠️ Claude (Pro CLI) error: {e}"


async def _run_git(args: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=config.WORKSPACE_DIR,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode("utf-8", errors="ignore").strip()


_SECRET_TOKENS = (".env", "secret", "credential", "_keys", "id_rsa")


def _looks_secret(path: str) -> bool:
    p = path.lower()
    return any(tok in p for tok in _SECRET_TOKENS)


async def auto_commit_push(message: str) -> dict:
    """Stage tracked changes, commit, and push. Returns {ok, summary}.
    No-ops cleanly if there is nothing to commit. Aborts (and unstages) if any
    secret-looking file got staged — guards against an autonomous run that may
    have rewritten .gitignore under bypassPermissions."""
    code, status = await _run_git(["status", "--porcelain"])
    if code != 0:
        return {"ok": False, "summary": f"git status failed: {status}"}
    if not status.strip():
        return {"ok": True, "summary": "No changes to commit."}

    code, _ = await _run_git(["add", "-A"])
    if code != 0:
        return {"ok": False, "summary": "git add failed."}

    # Secret scan: never let secrets reach the remote.
    code, staged = await _run_git(["diff", "--cached", "--name-only"])
    if code == 0 and staged.strip():
        flagged = [f for f in staged.splitlines() if _looks_secret(f)]
        if flagged:
            await _run_git(["reset"])  # unstage everything; let the owner review
            return {"ok": False, "summary": f"⛔ Push aborted — secret-looking files were staged: {', '.join(flagged[:5])}. Unstaged for your review."}

    code, out = await _run_git(["commit", "-m", message])
    if code != 0:
        return {"ok": False, "summary": f"git commit failed: {out}"}

    code, out = await _run_git(["push"])
    if code != 0:
        return {"ok": False, "summary": f"Committed locally, but push failed: {out}"}

    return {"ok": True, "summary": "Committed and pushed ✅"}
