"""
Emergency kill-switch — the human hard-stop (ROUTER_PROTOCOL.md §5 loop-killer).

A single owner command (/kill) FREEZES all agent activity that spends money or
acts on the machine: autonomous /build, approved /route mutations, /council,
/debate, and every chat path that calls an LLM. While engaged the bot still
answers the cheap control commands (/budget, /status, /kill, /resume, /help) so
the owner can see state and lift the freeze.

Two properties make this a real switch, not a flag:

  * STICKY — the engaged state is persisted to a tiny JSON file, so a panicked
    owner who hits /kill and then restarts the bot stays frozen. Only an explicit
    /resume clears it. The read is fail-open (a corrupt/missing file reads as "not
    engaged") so a disk glitch can never lock the bot out forever.
  * IMMEDIATE — engaging also TERMINATES any in-flight CLI subprocess. claude_runner
    registers each live process PID here while it runs; engage() kills the whole
    process tree, so a runaway task stops mid-flight rather than "no new tasks".

Dependency direction is one-way: claude_runner imports this module, never the
reverse. The PID registry is populated by claude_runner and drained here, so
there is no import cycle.
"""
import os
import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("telegram_bot.killswitch")

STATE_PATH = Path(__file__).parent / ".killswitch.json"

# Live CLI process PIDs, registered by claude_runner while a subprocess runs.
# engage() kills these trees so an in-flight task is actually stopped, not just
# "no new tasks". In-memory only — a restart clears it, which is correct: the
# processes died with the old bot anyway.
_live_pids: set[int] = set()


def register_pid(pid: int) -> None:
    """Track a live subprocess so the kill-switch can terminate it on /kill."""
    if pid:
        _live_pids.add(pid)


def unregister_pid(pid: int) -> None:
    """Stop tracking a subprocess that has exited (called from a finally block)."""
    _live_pids.discard(pid)


def _kill_tree(pid: int) -> None:
    """Kill a process AND its children. On Windows the `cmd /c` wrapper spawns
    claude/node as separate PIDs, so killing only the wrapper leaves orphans —
    taskkill /T kills the whole tree. Mirrors claude_runner._kill_tree (kept
    local to preserve the one-way import)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(pid), 9)
    except Exception as e:
        logger.error(f"kill-switch could not kill process tree {pid}: {e}")


def _read_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(d: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(d), encoding="utf-8")
    except Exception as e:
        # A state-write failure must not crash a command. Log it — but note this
        # means engage() may not survive a restart if the disk is unwritable.
        logger.error(f"Failed to write kill-switch state: {e}")


def is_engaged() -> bool:
    """True if the kill-switch is currently engaged.

    Fail-open: a missing or corrupt state file reads as 'not engaged'. A disk
    glitch must never silently freeze the bot forever — the owner can always
    /kill again. The engaged state is only ever trusted when it parses cleanly."""
    data = _read_state()
    if not data:
        return False
    return bool(data.get("engaged"))


def engage(reason: str = "", by: str = "") -> dict:
    """Engage the freeze and terminate any in-flight CLI subprocess.

    Returns {already, killed}: `already` if it was already engaged, `killed` =
    number of process trees signalled."""
    already = is_engaged()
    _write_state({
        "engaged": True,
        "reason": (reason or "").strip(),
        "by": str(by),
        "ts": datetime.now().isoformat(timespec="seconds"),
    })
    # Stop whatever is running right now, not just future tasks.
    pids = list(_live_pids)
    for pid in pids:
        _kill_tree(pid)
    _live_pids.clear()
    logger.warning(
        f"KILL-SWITCH ENGAGED by={by!r} reason={reason!r}; "
        f"killed {len(pids)} process tree(s)."
    )
    return {"already": already, "killed": len(pids)}


def release(by: str = "") -> dict:
    """Lift the freeze. Returns {was_engaged}."""
    was = is_engaged()
    _write_state({
        "engaged": False,
        "released_by": str(by),
        "ts": datetime.now().isoformat(timespec="seconds"),
    })
    logger.warning(f"Kill-switch RELEASED by={by!r} (was_engaged={was}).")
    return {"was_engaged": was}


def status() -> str:
    """Short human status for a Telegram report footer."""
    if not is_engaged():
        return "🟢 Kill-switch: *OFF* — აგენტი აქტიურია"
    d = _read_state()
    reason = d.get("reason") or "—"
    ts = d.get("ts") or "?"
    by = d.get("by") or "?"
    return (f"🛑 Kill-switch: *ON* — გაყინულია\n"
            f"   მიზეზი: {reason}\n   ვინ: {by} · დრო: {ts}")
