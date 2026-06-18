# shared/ — the Claude ⇄ Gemini adversarial workspace

This folder is the **single source of truth** for one adversarial run. Both
agents and the human read/write here; nothing important lives in chat.

| file | written by | meaning |
|------|-----------|---------|
| `task.md` | **you (human)** | the spec. The loop won't start until you replace the placeholder comment. |
| `work.md` | Claude (Coder) | what the coder did this round |
| `review.md` | Gemini (Reviewer) | the reviewer's verdict + concrete issues |
| `state.json` | orchestrator | `{turn, round, status, history}` — where the loop stands |

## Roles are fixed (by capability, not preference)

- **Claude = Coder.** Only worker wired to edit files (`claude -p` in `WORKSPACE_DIR`).
- **Gemini = Reviewer.** API text only — it can't read files, so the orchestrator
  feeds it `git diff HEAD`. It must return `VERDICT: APPROVED` or
  `VERDICT: CHANGES_REQUESTED` with actionable bullets.

## Run it

```bash
# from the repo root (telegram_bot_standalone/)
python run_adversarial.py "your task here"     # or write task.md first, then:
python run_adversarial.py
python run_adversarial.py --rounds 4
```

## Safety (inherited from the bot)

- **Budget:** per-task `--max-budget-usd` + daily token ledger (`budget.py`).
- **Kill-switch:** `killswitch.py` freezes both workers mid-flight.
- **Round ceiling:** hard cap of 5 — the loop can never ping-pong forever.
- **Approval gate:** on APPROVED it commits only if `AUTO_PUSH=true`; otherwise
  it stops and waits for you. `auto_commit_push` refuses to push secret-looking files.

## Notes / known v1 limits

- The reviewer judges from the **diff only** — great for local correctness,
  weaker on architecture it can't see. Feed bigger context later if needed.
- `WORKSPACE_DIR` (from `.env`/`config.py`) is where Claude codes; it should be
  a **git repo** so the reviewer gets a real diff.
- Coder autonomy depends on `CLAUDE_PERMISSION_MODE` (default `acceptEdits`
  auto-accepts edits but denies Bash in headless mode; set `bypassPermissions`
  for full autonomy).
