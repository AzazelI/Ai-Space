# Council Room

**In English:** a local desktop app (Python, tkinter) where I work with two AI models in one shared transcript: Claude as lead engineer, through the `claude` CLI, and Gemini as architect-reviewer. It has four modes. **Discuss** is a three-way conversation. **Council** has both models take independent positions, rebut each other and agree on a written plan. **Build** is a loop in which Claude writes the code and Gemini reviews each diff in rounds. **Push** commits the result. Because the agents spend money and act on the machine, the guardrails are part of the design: a daily token budget that is checked before every task, a per-task cost ceiling, and a sticky kill switch that freezes every paid or machine-changing action until a person lifts it. 34 automated tests. The rest of this README is in Georgian.

ლოკალური „საბჭოს ოთახი" — შენ, **Claude** (Lead Engineer) და **Antigravity / Gemini**
(Master Architect) ერთ ჩატში, ერთ ტრანსკრიპტში. ერთად განიხილავთ, იწვევთ საბჭოს,
აშენებთ პროექტს და push-ავთ.

## საჭიროებები (Prerequisites)
- **Python 3.11+**
- **`claude` CLI** დაყენებული და **Claude Pro**-ზე შესული (`claude` ბრძანება ხელმისაწვდომი
  PATH-ში). API key არ სჭირდება — მუშაობს Pro subscription-ით.
- **Gemini API key** — https://aistudio.google.com

## დაყენება (Setup)
```bash
pip install -r requirements.txt
cp .env.template .env        # Windows: copy .env.template .env
# .env-ში ჩაწერე GEMINI_API_KEY
```

## გაშვება (Run)
```bash
python app.py
```
ან Windows-ზე ორმაგად დააწკაპუნე **`Council-Room.bat`**-ს (repo-ს შიგნიდან —
ის ავტომატურად პოულობს თავის საქაღალდეს).

## რეჟიმები
- **💬 Discuss** — წერ; Claude პასუხობს, მერე Antigravity პასუხობს Claude-ის ნანახით — ერთ ტრანსკრიპტში.
- **⚖ Council** — 2-ხმიანი საბჭო (დამოუკიდებელი პოზიციები → cross-rebuttal → სინთეზი) → `shared/plan.md`.
- **🛠 Build** — დავალებას აძლევს Claude↔Gemini build-loop-ს (`run_adversarial.py`); Claude წერს
  ფაილებს `WORKSPACE_DIR`-ში, Gemini diff-ს ამოწმებს რაუნდებად.
- **⤴ Push** — commit + push `WORKSPACE_DIR`-ს (აშენებული პროექტი).

## კონფიგი (.env)
| ცვლადი | აღწერა |
|---|---|
| `GEMINI_API_KEY` | **სავალდებულო** — Antigravity-ის ხმა. |
| `WORKSPACE_DIR` | სად აშენდეს პროექტი (ცარიელი = `./workspace`). |
| `CLAUDE_PERMISSION_MODE` | `acceptEdits` (default) ან `bypassPermissions`. |
| `MAX_TASK_COST_USD` | per-build ჭერი (0 = off). |
| `DAILY_TOKEN_BUDGET` | დღიური ლიმიტი; cache 0.1x წონით ითვლება (0 = off). |

## ტესტები
```bash
python -m unittest discover -s tests
```

## სტრუქტურა
- `app.py` — GUI (Council Room).
- `agents.py` — Claude (CLI) + Gemini (API) ხმები, advisor-პერსონებით.
- `council.py` — 2-ხმიანი საბჭო → `shared/plan.md`.
- `run_adversarial.py` — Claude↔Gemini build-loop (გამაგრებული).
- `claude_runner.py` · `budget.py` · `killswitch.py` · `config.py` — ბირთვი.
