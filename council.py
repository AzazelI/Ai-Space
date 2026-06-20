"""
Council Room — the council (v1-lite).

NOT the full 5-advisor stress test (that is v2, and the council itself flagged it
as over-engineered for v1). v1 is the smallest thing that is still a *real*
council: the two independent voices answer, then each rebuts the other, then
Claude (the engineer who will build) writes the synthesis + plan.md. The
disagreement is genuine because it comes from two different models, not one
role-played panel.

  Phase 1  independent takes (parallel — neither voice sees the other yet)
  Phase 2  cross-rebuttal (each now sees the other's Phase-1 answer)
  Phase 3  synthesis + plan.md, chaired by Claude
"""
import asyncio
from datetime import datetime
from pathlib import Path

import agents

SHARED = Path(__file__).parent / "shared"
PLAN_FILE = SHARED / "plan.md"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def run_council(question: str, on_turn=None) -> dict:
    """Run a 2-voice council on `question`.

    `on_turn(speaker, text)` — optional sync callback fired as each turn lands so a
    GUI can stream it live (speaker ∈ {"Claude", "Antigravity", "Synthesis"}).
    Returns every turn as a dict and writes shared/plan.md. Never raises: agent
    errors arrive as ⚠️/🛑/⛔ strings from agents.py.
    """
    SHARED.mkdir(exist_ok=True)
    q = question.strip()

    def emit(speaker: str, text: str) -> None:
        if on_turn:
            on_turn(speaker, text)

    # Phase 1 — independent positions, in parallel. Neither sees the other.
    claude_p1, gemini_p1 = await asyncio.gather(
        agents.ask_claude(
            f"საბჭოს კითხვა:\n{q}\n\nმიეცი შენი დამოუკიდებელი პოზიცია — მოკლედ, არსით. "
            "ეს შენი პირველი დებულებაა; Antigravity პარალელურად პასუხობს."),
        agents.ask_gemini(
            f"საბჭოს კითხვა:\n{q}\n\nმიეცი შენი დამოუკიდებელი პოზიცია — მოკლედ, არსით. "
            "ეს შენი პირველი დებულებაა; Claude პარალელურად პასუხობს."),
    )
    emit("Claude", claude_p1)
    emit("Antigravity", gemini_p1)

    # Phase 2 — cross-rebuttal. Each voice now reads the other's Phase-1 answer.
    claude_p2, gemini_p2 = await asyncio.gather(
        agents.ask_claude(
            f"საბჭოს კითხვა:\n{q}\n\nAntigravity-ის პოზიცია:\n{gemini_p1}\n\n"
            "სად ეთანხმები, სად არა და რატომ? დაასახელე კონკრეტული რისკი ან ალტერნატივა, "
            "ბრმად ნუ დაეთანხმები."),
        agents.ask_gemini(
            f"საბჭოს კითხვა:\n{q}\n\nClaude-ის პოზიცია:\n{claude_p1}\n\n"
            "სად ეთანხმები, სად არა და რატომ? დაასახელე კონკრეტული რისკი ან ალტერნატივა, "
            "ბრმად ნუ დაეთანხმები."),
    )
    emit("Claude", claude_p2)
    emit("Antigravity", gemini_p2)

    # Phase 3 — synthesis + plan, chaired by Claude (the voice that will build).
    synthesis = await agents.ask_claude(
        "შენ ხარ საბჭოს თავმჯდომარე. ქვემოთ ერთ კითხვაზე ორი ხმის ორ-ორი რეპლიკაა. "
        "გააკეთე საბოლოო სინთეზი ქართულად, ზუსტად სამი სათაურით:\n"
        "1. შეთანხმებული გადაწყვეტილება\n"
        "2. გადასაჭრელი რეალური რისკები\n"
        "3. კონკრეტული ნაბიჯების გეგმა (checklist, `- [ ]`)\n\n"
        f"კითხვა:\n{q}\n\n"
        f"Claude #1:\n{claude_p1}\n\nClaude #2:\n{claude_p2}\n\n"
        f"Antigravity #1:\n{gemini_p1}\n\nAntigravity #2:\n{gemini_p2}\n"
    )
    emit("Synthesis", synthesis)

    PLAN_FILE.write_text(
        f"# Council Plan\n_{_now()}_\n\n## კითხვა\n{q}\n\n## სინთეზი და გეგმა\n{synthesis}\n",
        encoding="utf-8",
    )

    return {
        "question": q,
        "claude_p1": claude_p1, "gemini_p1": gemini_p1,
        "claude_p2": claude_p2, "gemini_p2": gemini_p2,
        "synthesis": synthesis,
        "plan_file": str(PLAN_FILE),
    }
