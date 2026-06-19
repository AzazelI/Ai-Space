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

# The post-approval unit-test gate is OPT-IN and default OFF. It exists to catch
# a coder change that passes Gemini's eyes but breaks the suite — but only when
# WORKSPACE_DIR is a real codebase with a tests/ suite. Left on by default it
# wedged the loop: WORKSPACE_DIR here is an Obsidian vault with no tests, and
# Python 3.12+ exits 5 ("NO TESTS RAN") on an empty discover, which the gate read
# as failure and used to silently flip every APPROVED to CHANGES_REQUESTED —
# guaranteeing ceiling_reached forever. Turn it on only for a tested workspace.
ENABLE_TEST_GATE = os.getenv("ENABLE_TEST_GATE", "false").strip().lower() in (
    "1", "true", "yes", "on")


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


def _preexisting_dirt() -> list[str]:
    """Files already dirty in WORKSPACE_DIR BEFORE the coder runs.

    The reviewer is fed the entire uncommitted diff (Option B), so anything
    listed here would be reviewed as if the coder wrote it — which is how a
    trivial task ("are you here?") spun the loop into rewriting unrelated
    pre-existing files. Metadata dirs are filtered to match _workspace_diff so
    the check and the review agree on what counts as 'the coder's work'.
    """
    code, out = _git(["status", "--porcelain"])
    if code != 0:
        return []
    ignore_prefixes = (".obsidian/", ".claude/", ".gemini/", "shared/")
    dirty = []
    for line in out.splitlines():
        # porcelain format is 'XY <path>'; the path begins at column 3.
        path = line[3:].strip().strip('"') if len(line) > 3 else ""
        if not path:
            continue
        if any(path.startswith(p) for p in ignore_prefixes):
            continue
        dirty.append(path)
    return dirty


def _run_tests() -> tuple[str, str]:
    """Run python unit tests in WORKSPACE_DIR/tests, ONLY there.

    Returns (status, output) where status is one of:
      "passed"  - tests ran and all passed
      "failed"  - tests ran and at least one failed/errored (the real gate signal)
      "skipped" - no tests/ dir, an empty discover, or the runner itself failed

    Critically, "no tests collected" is "skipped", NOT "failed": Python 3.12+
    exits 5 ("NO TESTS RAN") on an empty discover, and an absent/empty suite must
    never flip an APPROVED verdict. Only a genuine test failure overrides approval.
    The old 'scripts/' and '.' fallbacks are gone — they imported arbitrary
    non-test modules from the workspace and were the source of the false failures.
    """
    workspace = Path(config.WORKSPACE_DIR).resolve()
    tests_dir = workspace / "tests"
    if not tests_dir.is_dir():
        return "skipped", f"(no tests/ directory in {workspace} — test gate skipped)"

    cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
    try:
        p = subprocess.run(
            cmd,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as e:
        # The runner itself failing is infrastructure noise, not a code failure —
        # skip rather than block approval on it.
        return "skipped", f"(could not run test discovery: {e})"

    output = "--- running tests in: tests ---\n"
    if p.stdout:
        output += f"STDOUT:\n{p.stdout}\n"
    if p.stderr:
        output += f"STDERR:\n{p.stderr}\n"

    combined = f"{p.stdout}\n{p.stderr}"
    # Python 3.12+ exits 5 with "NO TESTS RAN" on an empty discover; older Pythons
    # exit 0 but print "Ran 0 tests". Either way: skip, never override approval.
    if p.returncode == 5 or "NO TESTS RAN" in combined or "Ran 0 tests" in combined:
        return "skipped", output
    if p.returncode == 0:
        return "passed", output
    return "failed", output


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
    ap.add_argument("--allow-dirty", action="store_true",
                    help="proceed even if WORKSPACE_DIR has pre-existing uncommitted "
                         "changes (they WILL be reviewed as if the coder wrote them)")
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

    # Clean-tree pre-flight (fail BEFORE spending on Gemini init). The reviewer
    # sees the whole uncommitted diff, so a dirty workspace makes it review files
    # the coder never touched. Refuse on a dirty tree unless the operator opts in.
    dirt = _preexisting_dirt()
    if dirt and not args.allow_dirty:
        preview = "\n".join(f"    {f}" for f in dirt[:15])
        more = f"\n    ...(+{len(dirt) - 15} more)" if len(dirt) > 15 else ""
        print(
            f"⛔ WORKSPACE_DIR has {len(dirt)} uncommitted file(s) the reviewer would "
            f"treat as the coder's work:\n{preview}{more}\n"
            f"   Commit or stash them first, or re-run with --allow-dirty to review "
            f"them on purpose.\n   WORKSPACE_DIR = {config.WORKSPACE_DIR}")
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

        if verdict == "approved" and ENABLE_TEST_GATE:
            print(f"⚙ Test gate ON — running unit tests in {config.WORKSPACE_DIR}\\tests...")
            test_status, test_output = _run_tests()
            if test_status == "failed":
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
            elif test_status == "skipped":
                # No suite / empty discover must NEVER block approval — it only
                # used to because of Python 3.12+'s exit-5 on an empty discover.
                print("⚪ No tests collected — approval stands (test gate skipped).")
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
