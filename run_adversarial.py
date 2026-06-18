"""
Adversarial review loop: Claude (Coder) <-> Gemini (Reviewer) over a shared folder.

Neither agent is a background daemon, so THIS script is the orchestrator that
wakes each one in turn. One process drives bounded rounds:

    Coder (claude -p, mutates WORKSPACE_DIR)
      -> Reviewer (Gemini API, text-only, fed the git diff because it CANNOT
         read the filesystem)
      -> repeat until the reviewer APPROVES or the round ceiling is hit.

Roles are fixed by capability, not preference (see ROUTER_PROTOCOL.md discussion):
Claude is the only worker wired to edit files; Gemini can only produce text, so
it reviews. This script reuses the telegram bot's hardened workers rather than
re-implementing them:

  * claude_runner.run_claude_task   - Claude edits files (budget + killswitch guarded)
  * agent_orchestrator.query_gemini - Gemini review (budget + killswitch guarded)
  * claude_runner.auto_commit_push  - secret-scanned commit + push on approval

State lives in shared/ as plain files so the human (and either agent) can read
exactly where the loop stands at any moment:

  shared/task.md    - the spec (human writes this, or pass it as a CLI arg)
  shared/work.md    - Coder's latest output
  shared/review.md  - Reviewer's latest verdict
  shared/state.json - machine state {turn, round, status, history}

Usage:
  python run_adversarial.py "build a CSV export endpoint"   # task as arg
  python run_adversarial.py                                 # reads shared/task.md
  python run_adversarial.py --rounds 4
"""
import os
import sys
import json
import asyncio
import argparse
import subprocess
from datetime import datetime
from pathlib import Path

import config
import claude_runner
import agent_orchestrator

# The Windows console defaults to cp1252, which can't encode the status emoji
# below; force utf-8 so prints never crash the loop. No-op if already utf-8 or
# if stdout doesn't support reconfigure (e.g. a redirected pipe).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).parent.resolve()
SHARED = ROOT / "shared"
TASK_FILE = SHARED / "task.md"
WORK_FILE = SHARED / "work.md"
REVIEW_FILE = SHARED / "review.md"
STATE_FILE = SHARED / "state.json"

# Hard loop-killer: the agent<->agent exchange can never ping-pong unbounded
# without a human, no matter what is asked for (mirrors DEBATE_ROUNDS_CEILING).
CEILING = 5
DEFAULT_ROUNDS = max(1, min(int(os.getenv("ADVERSARIAL_ROUNDS", "3")), CEILING))
DIFF_CHAR_CAP = 12000


# --------------------------------------------------------------------------- #
# small file / git helpers
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def _git(args: list[str]) -> tuple[int, str]:
    """Run git inside WORKSPACE_DIR. Bytes->utf-8 (errors ignored) so Georgian
    or other non-ASCII content in a diff never crashes decoding."""
    try:
        p = subprocess.run(["git", "-C", config.WORKSPACE_DIR, *args],
                           capture_output=True, timeout=30)
        out = (p.stdout or p.stderr).decode("utf-8", errors="ignore")
        return p.returncode, out
    except Exception as e:
        return 1, str(e)


def _filter_files(files_text: str) -> str:
    ignore_prefixes = (".obsidian/", ".claude/", ".gemini/", "shared/")
    filtered = []
    for line in files_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if any(line.startswith(p) for p in ignore_prefixes):
            continue
        filtered.append(line)
    return "\n".join(filtered)


def _filter_diff(diff_text: str) -> str:
    """Filter out diff blocks for metadata folders like .obsidian, .claude, etc."""
    ignore_prefixes = (".obsidian/", ".claude/", ".gemini/", "shared/")
    blocks = diff_text.split("diff --git ")
    filtered = []
    if blocks[0].strip():
        filtered.append(blocks[0])

    for block in blocks[1:]:
        lines = block.splitlines()
        if not lines:
            continue
        header = lines[0]
        is_ignored = False
        for prefix in ignore_prefixes:
            if f"a/{prefix}" in header or f"b/{prefix}" in header or f"/{prefix}" in header:
                is_ignored = True
                break
        if not is_ignored:
            filtered.append("diff --git " + block)

    return "\n".join(filtered)


def _workspace_diff() -> tuple[str, str]:
    """Return (changed_files, diff) for everything the coder changed since the
    last commit. The reviewer (Gemini) gets ONLY this — it can't open files."""
    # Stage everything (including untracked files) to compare
    _git(["add", "-A"])

    code, files = _git(["diff", "HEAD", "--name-only"])
    if code != 0:
        return "", ("(workspace is not a git repo, or git failed — reviewer is "
                    "reviewing blind. Init a repo in WORKSPACE_DIR for real diffs.)")
    code, diff = _git(["diff", "HEAD"])

    # Unstage changes to leave index clean for next round
    _git(["reset"])

    diff = diff.strip() or "(no changes detected since last commit)"
    
    # Filter out metadata directories from the list of files and diff
    files = _filter_files(files)
    diff = _filter_diff(diff)
    
    if len(diff) > DIFF_CHAR_CAP:
        diff = diff[:DIFF_CHAR_CAP] + f"\n...(truncated; {len(diff)} chars total)"
    return files.strip(), diff


def _run_tests() -> tuple[bool, str]:
    """Run python unit tests in the workspace (config.WORKSPACE_DIR).
    Specifically targets 'tests' or 'scripts' subdirectories if present,
    otherwise falls back to the workspace root.
    Returns (success, output)."""
    workspace = Path(config.WORKSPACE_DIR).resolve()
    test_dirs = []
    if (workspace / "tests").is_dir():
        test_dirs.append("tests")
    if (workspace / "scripts").is_dir():
        test_dirs.append("scripts")
        
    if not test_dirs:
        test_dirs = ["."]

    outputs = []
    all_success = True
    for t_dir in test_dirs:
        # Run discovery in the directory relative to workspace root
        cmd = [sys.executable, "-m", "unittest", "discover", "-s", t_dir]
        try:
            p = subprocess.run(
                cmd,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=60
            )
            out = f"--- running tests in: {t_dir} ---\n"
            if p.stdout:
                out += f"STDOUT:\n{p.stdout}\n"
            if p.stderr:
                out += f"STDERR:\n{p.stderr}\n"
            outputs.append(out)
            if p.returncode != 0:
                all_success = False
        except Exception as e:
            all_success = False
            outputs.append(f"Failed to execute tests in {t_dir}: {e}\n")

    return all_success, "\n".join(outputs)


# --------------------------------------------------------------------------- #
# prompt builders
# --------------------------------------------------------------------------- #
def _coder_prompt(task: str, last_review: str, round_no: int) -> str:
    parts = [
        "You are the CODER in an adversarial Claude<->Gemini loop.",
        f"Implement the task by editing files in the workspace ({config.WORKSPACE_DIR}).",
        "",
        "## TASK",
        task.strip(),
    ]
    if last_review:
        parts += [
            "",
            f"## REVIEWER FEEDBACK (round {round_no - 1}) — address EVERY point",
            last_review.strip(),
        ]
    parts += [
        "",
        "Make the changes now. End with a 3-5 line summary of what you changed and why.",
    ]
    return "\n".join(parts)


def _reviewer_prompt(task: str, changed_files: str, diff: str, round_no: int) -> str:
    return "\n".join([
        "You are the REVIEWER in an adversarial Claude<->Gemini loop. You are Gemini.",
        "You CANNOT read the filesystem — review ONLY from the diff below.",
        "Be adversarial: your job is to find real problems, not to be agreeable.",
        "Politeness that approves broken code is a failure of your role.",
        "",
        "Your reply MUST begin with exactly one of these two lines:",
        "  VERDICT: CHANGES_REQUESTED",
        "  VERDICT: APPROVED",
        "Use APPROVED only when you genuinely cannot find a substantive issue.",
        "If CHANGES_REQUESTED, list each issue as a concrete, actionable bullet",
        "(what is wrong + where + how to fix). Vague comments are not allowed.",
        "",
        "## TASK (what the code must achieve)",
        task.strip(),
        "",
        f"## CHANGED FILES (round {round_no})",
        changed_files or "(none — the coder changed no files, which is itself a problem)",
        "",
        "## DIFF (git diff HEAD)",
        "```diff",
        diff,
        "```",
    ])


def _verdict(review_text: str) -> str:
    """Read the reviewer's machine-parsable verdict line. Anything that isn't an
    explicit APPROVED counts as changes-requested — fail safe, never auto-approve
    on ambiguous output."""
    for line in review_text.strip().splitlines():
        s = line.strip().upper()
        if s.startswith("VERDICT:"):
            return "approved" if "APPROV" in s else "changes"
    return "changes"


# --------------------------------------------------------------------------- #
# main loop
# --------------------------------------------------------------------------- #
async def main() -> int:
    ap = argparse.ArgumentParser(description="Claude(Coder) <-> Gemini(Reviewer) loop")
    ap.add_argument("task", nargs="?", help="task text; if omitted, read shared/task.md")
    ap.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS,
                    help=f"max rounds (clamped to 1..{CEILING})")
    args = ap.parse_args()

    SHARED.mkdir(exist_ok=True)
    rounds = max(1, min(args.rounds, CEILING))

    # Resolve the task: a CLI arg wins and is persisted to task.md; else read it.
    if args.task:
        _write(TASK_FILE, args.task.strip() + "\n")
    task = _read(TASK_FILE).strip()
    if not task or task.startswith("<!--"):
        print("⛔ No task. Write the spec in shared/task.md, or pass it as an argument.")
        return 1

    agent_orchestrator.init_clients()
    if not agent_orchestrator.gemini_client:
        print("⛔ Gemini client failed to init — check GEMINI_API_KEYS in .env.")
        return 1

    state = {
        "task": task,
        "turn": "coder",
        "round": 0,
        "max_rounds": rounds,
        "status": "running",
        "started": _now(),
        "history": [],
    }
    _save_state(state)
    print(f"▶ Adversarial loop — ceiling {rounds} rounds. Workspace: {config.WORKSPACE_DIR}")

    last_review = ""
    for rnd in range(1, rounds + 1):
        state["round"] = rnd

        # ---- CODER (Claude) -------------------------------------------------
        state["turn"] = "coder"
        _save_state(state)
        print(f"\n=== Round {rnd}: CODER (Claude) ===")
        result = await claude_runner.run_claude_task(_coder_prompt(task, last_review, rnd))
        coder_out = (result.get("output") or "").strip() or result.get("error") or "(no output)"
        _write(WORK_FILE, f"# Round {rnd} — Coder (Claude)\n_{_now()}_\n\n{coder_out}\n")
        if not result.get("ok"):
            state["status"] = "coder_error"
            _save_state(state)
            print(f"⛔ Coder failed: {result.get('error')}")
            return 1
        print(coder_out[:600])

        # ---- collect the diff the reviewer will see -------------------------
        changed_files, diff = _workspace_diff()

        # ---- REVIEWER (Gemini) ----------------------------------------------
        state["turn"] = "reviewer"
        _save_state(state)
        print(f"\n=== Round {rnd}: REVIEWER (Gemini) ===")
        review = (await agent_orchestrator.query_gemini(
            _reviewer_prompt(task, changed_files, diff, rnd))).strip()
        _write(REVIEW_FILE, f"# Round {rnd} — Reviewer (Gemini)\n_{_now()}_\n\n{review}\n")
        verdict = _verdict(review)
        print(review[:600])

        if verdict == "approved":
            print(f"⚙ Running automated unit tests in {config.WORKSPACE_DIR}...")
            test_success, test_output = _run_tests()
            if not test_success:
                print("❌ Tests failed! Overriding approval to CHANGES_REQUESTED.")
                verdict = "changes"
                test_feedback = (
                    f"\n\n❌ AUTOMATED TEST RUNNER: TEST FAILURE OVERRIDE\n"
                    f"Gemini approved the changes, but automated unit tests failed:\n\n"
                    f"```\n{test_output}\n```\n"
                    f"Please resolve these test failures."
                )
                review += test_feedback
                _write(REVIEW_FILE, f"# Round {rnd} — Reviewer (Gemini) [TESTS FAILED]\n_{_now()}_\n\n{review}\n")
            else:
                print("✅ All unit tests passed.")

        state["history"].append({"round": rnd, "verdict": verdict, "at": _now()})
        last_review = review

        if verdict == "approved":
            state["status"] = "approved"
            state["turn"] = "user"
            _save_state(state)
            print(f"\n✅ Reviewer APPROVED on round {rnd}.")
            if config.AUTO_PUSH:
                push = await claude_runner.auto_commit_push(
                    f"adversarial: approved round {rnd} — {task[:60]}")
                print(f"   {push.get('summary')}")
            else:
                print("   AUTO_PUSH off — review shared/work.md + the diff, then commit manually.")
            return 0

    # Ceiling hit without approval — hand back to the human.
    state["status"] = "ceiling_reached"
    state["turn"] = "user"
    _save_state(state)
    print(f"\n⚠ Hit the {rounds}-round ceiling without approval. "
          f"Your call now — see shared/review.md for the open issues.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n(interrupted)")
        sys.exit(130)
