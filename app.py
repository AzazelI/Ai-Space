"""
Council Room — desktop app.

One shared transcript where the User, Claude (Lead Engineer), and Antigravity
(Gemini, Master Architect) sit together. Four modes:

  * Discuss — you speak; Claude answers, then Gemini answers seeing Claude's reply.
  * Council — the 2-voice council (council.run_council) → writes shared/plan.md.
  * Build   — hand a task to the hardened Claude<->Gemini build loop
              (run_adversarial.py) which edits files in WORKSPACE_DIR.
  * Push    — commit + push WORKSPACE_DIR (the finished project) to its repo.

tkinter is single-threaded and the agent calls are async/long-running, so a
background asyncio loop runs the coroutines and every UI update is marshalled back
to the Tk main thread through a thread-safe queue (never touch widgets off-thread).
"""
import sys
import queue
import asyncio
import threading
import subprocess
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import scrolledtext, font as tkfont

import config
import agents
import council
import claude_runner
import killswitch

BASE = Path(__file__).parent.resolve()
SHARED = BASE / "shared"
TRANSCRIPT = SHARED / "transcript.md"
BUILD_SCRIPT = BASE / "run_adversarial.py"

# --- Porsche-inspired dark palette (carried over from the cockpit) ---
BG, CARD_BG, INPUT_BG = "#0A0A0C", "#141417", "#1D1D22"
BORDER, TEXT, MUTED = "#25252B", "#F2F2F5", "#757582"
RED, GREEN, AMBER, BLUE = "#D5001C", "#82D200", "#FF9B00", "#3B9EFF"

# Per-speaker colour in the transcript.
SPEAKER_COLOR = {
    "You": AMBER, "Claude": GREEN, "Antigravity": RED,
    "Synthesis": TEXT, "Build": MUTED, "System": MUTED,
}


class AsyncLoop:
    """A background thread running a persistent asyncio event loop the GUI submits
    coroutines to. Keeps the Tk main loop free while agents think."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro, on_done=None):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        if on_done:
            fut.add_done_callback(on_done)
        return fut


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Council Room — You × Claude × Antigravity")
        self.root.configure(bg=BG)
        self.root.geometry("960x760")
        self.root.minsize(760, 560)

        self.aloop = AsyncLoop()
        self.q = queue.Queue()
        self.turns = []          # [(speaker, text)] — appended ONLY on the main thread
        self.busy = False

        self.ui_font = self._font(["Inter", "Outfit", "Segoe UI"], 10)
        self.big_font = self._font(["Inter", "Outfit", "Segoe UI"], 16, "bold")
        self.mono_font = self._font(["Cascadia Code", "Consolas", "Courier New"], 10)

        self._build_header()
        self._build_transcript()
        self._build_controls()

        SHARED.mkdir(exist_ok=True)
        self._post("System", f"Council Room მზადაა. Workspace: {config.WORKSPACE_DIR}")
        ok, missing = config.is_configured()
        if not ok:
            self._post("System", f"⚠️ აკლია კონფიგი: {', '.join(missing)} (.env)")
        self.root.after(80, self._drain)

    # ----- layout ---------------------------------------------------------
    def _build_header(self):
        h = tk.Frame(self.root, bg=BG)
        h.pack(fill="x", padx=22, pady=(18, 8))
        tk.Label(h, text="COUNCIL ROOM", bg=BG, fg=TEXT, font=self.big_font).pack(anchor="w")
        tk.Label(h, text="You  ·  Claude [Engineer]  ⇄  Antigravity [Architect]",
                 bg=BG, fg=MUTED, font=self.ui_font).pack(anchor="w", pady=(2, 0))

    def _build_transcript(self):
        card = tk.Frame(self.root, bg=BG)
        card.pack(fill="both", expand=True, padx=22, pady=(0, 10))
        self.log = scrolledtext.ScrolledText(
            card, bg="#050507", fg=TEXT, relief="flat", font=self.mono_font,
            wrap="word", padx=14, pady=12, state="disabled",
            highlightthickness=1, highlightbackground=BORDER)
        self.log.pack(fill="both", expand=True)
        for sp, color in SPEAKER_COLOR.items():
            self.log.tag_config(f"sp_{sp}", foreground=color,
                                font=self._font(["Inter", "Segoe UI"], 10, "bold"))
        self.log.tag_config("body", foreground=TEXT)

    def _build_controls(self):
        card = tk.Frame(self.root, bg=CARD_BG, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="x", padx=22, pady=(0, 8))

        self.entry = tk.Text(card, height=3, bg=INPUT_BG, fg=TEXT, insertbackground=TEXT,
                             relief="flat", font=self.ui_font, wrap="word", padx=12, pady=10,
                             highlightthickness=1, highlightbackground=BORDER, highlightcolor=RED)
        self.entry.pack(fill="x", padx=14, pady=(12, 10))
        self.entry.bind("<Control-Return>", lambda e: (self._on_discuss(), "break")[1])
        self.entry.bind("<KeyPress>", self._on_key_press)
        self.entry.bind("<<Paste>>", self._on_paste)
        self.entry.focus_set()

        row = tk.Frame(card, bg=CARD_BG)
        row.pack(fill="x", padx=14, pady=(0, 12))
        self.btns = {}
        for key, label, color, cmd in (
            ("discuss", "💬 Discuss", RED, self._on_discuss),
            ("council", "⚖ Council", BLUE, self._on_council),
            ("build", "🛠 Build", AMBER, self._on_build),
            ("push", "⤴ Push", GREEN, self._on_push),
        ):
            b = tk.Button(row, text=label, bg=INPUT_BG, fg=TEXT, relief="flat", bd=0,
                          font=self.ui_font, activebackground=color, activeforeground="white",
                          padx=14, pady=6, cursor="hand2", command=cmd)
            b.pack(side="left", padx=(0, 8))
            self.btns[key] = b

        tk.Label(row, text="rounds:", bg=CARD_BG, fg=MUTED, font=self.ui_font).pack(side="left", padx=(8, 4))
        self.rounds_var = tk.StringVar(value="3")
        tk.Spinbox(row, from_=1, to=5, width=3, textvariable=self.rounds_var,
                   bg=INPUT_BG, fg=TEXT, relief="flat", font=self.ui_font,
                   buttonbackground=INPUT_BG, justify="center").pack(side="left")

        self.status = tk.Label(row, text="● მზად", bg=CARD_BG, fg=MUTED, font=self.ui_font)
        self.status.pack(side="right")

        footer = tk.Frame(self.root, bg=BG)
        footer.pack(fill="x", padx=22, pady=(0, 14))
        tk.Button(footer, text="💾 Save Transcript", bg=CARD_BG, fg=TEXT, relief="flat", bd=0,
                  font=self.ui_font, padx=12, pady=6, cursor="hand2",
                  command=self._save_transcript).pack(side="left")
        tk.Button(footer, text="🛑 Kill", bg=CARD_BG, fg=RED, relief="flat", bd=0,
                  font=self.ui_font, padx=12, pady=6, cursor="hand2",
                  command=self._on_kill).pack(side="left", padx=(8, 0))
        tk.Button(footer, text="დახურვა", bg=CARD_BG, fg=TEXT, relief="flat", bd=0,
                  font=self.ui_font, padx=12, pady=6, cursor="hand2",
                  command=self.root.destroy).pack(side="right")

    # ----- transcript rendering (MAIN THREAD ONLY) ------------------------
    def _post(self, speaker, text):
        """Render a turn and append it to history + transcript.md. Main thread only."""
        self.turns.append((speaker, text))
        self.log.config(state="normal")
        self.log.insert("end", f"\n{speaker}\n", f"sp_{speaker}")
        self.log.insert("end", f"{text}\n", "body")
        self.log.see("end")
        self.log.config(state="disabled")
        try:
            with open(TRANSCRIPT, "a", encoding="utf-8") as f:
                f.write(f"### {speaker} · {datetime.now():%H:%M:%S}\n{text}\n\n")
        except Exception:
            pass

    def _context_block(self, limit=8):
        """A compact snapshot of the last few turns, for an agent's context."""
        recent = self.turns[-limit:]
        return "\n\n".join(f"{sp}: {txt}" for sp, txt in recent)

    def _drain(self):
        """Pump queued messages from worker threads onto the UI. Main thread."""
        try:
            while True:
                kind, a, b = self.q.get_nowait()
                if kind == "post":
                    self._post(a, b)
                elif kind == "busy":
                    self._set_busy(a)
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    # ----- state ----------------------------------------------------------
    def _set_busy(self, busy):
        self.busy = busy
        self.status.config(text="● მუშაობს…" if busy else "● მზად",
                           fg=AMBER if busy else MUTED)
        state = "disabled" if busy else "normal"
        for b in self.btns.values():
            b.config(state=state)

    def _take_input(self):
        """Read + clear the input box. Returns "" if busy or empty."""
        if self.busy:
            return ""
        text = self.entry.get("1.0", "end").strip()
        if text:
            self.entry.delete("1.0", "end")
        return text

    # ----- mode handlers --------------------------------------------------
    def _on_discuss(self):
        msg = self._take_input()
        if not msg:
            return
        self._post("You", msg)
        context = self._context_block()
        self._set_busy(True)
        self.aloop.submit(self._discuss(msg, context),
                          on_done=lambda f: self.q.put(("busy", False, None)))

    async def _discuss(self, msg, context):
        claude = await agents.ask_claude(
            f"ჩატის ბოლო რეპლიკები:\n{context}\n\nUser-ის ახალი შეტყობინება: {msg}\n\n"
            "უპასუხე როგორც Claude. ამის შემდეგ Antigravity უპასუხებს, ასე რომ მოკლედ და არსით.")
        self.q.put(("post", "Claude", claude))
        claude_ctx = f"{context}\n\nUser: {msg}\n\nClaude: {claude}"
        gemini = await agents.ask_gemini(
            f"ჩატის მსვლელობა:\n{claude_ctx}\n\nClaude-მ უკვე უპასუხა (ზემოთ). "
            "უპასუხე როგორც Antigravity — ჩაერთე Claude-ის ნათქვამში, დაეთანხმე ან შეედავე მიზეზით.")
        self.q.put(("post", "Antigravity", gemini))

    def _on_council(self):
        q = self._take_input()
        if not q:
            self._post("System", "⚖ Council: ჯერ ჩაწერე კითხვა/თემა შესაფასებლად.")
            return
        self._post("You", q)
        self._post("System", "⚖ საბჭო იწყება — 2 ხმა, cross-rebuttal, სინთეზი → shared/plan.md")
        self._set_busy(True)
        on_turn = lambda sp, txt: self.q.put(("post", sp, txt))
        def done(f):
            try:
                res = f.result()
                self.q.put(("post", "System", f"✅ გეგმა შენახულია: {res['plan_file']}"))
            except Exception as e:
                self.q.put(("post", "System", f"⚠️ Council error: {e}"))
            self.q.put(("busy", False, None))
        self.aloop.submit(council.run_council(q, on_turn=on_turn), on_done=done)

    def _on_build(self):
        task = self._take_input()
        if not task:
            self._post("System", "🛠 Build: ჩაწერე დავალება (ან ჯერ Council გაუშვი გეგმისთვის).")
            return
        try:
            rounds = max(1, min(int(self.rounds_var.get()), 5))
        except ValueError:
            rounds = 3
        self._post("You", task)
        self._post("System", f"🛠 Build loop იწყება ({rounds} რაუნდი) — Claude წერს, Gemini ამოწმებს.")
        self._set_busy(True)
        threading.Thread(target=self._build_worker, args=(task, rounds), daemon=True).start()

    def _build_worker(self, task, rounds):
        """Run the hardened adversarial build loop as a subprocess, streaming output."""
        try:
            # -u forces the child's stdout unbuffered; without it Python block-buffers
            # a piped (non-TTY) stdout, so the loop's progress prints never reach the
            # GUI until a round finishes — making a long coder run look frozen.
            proc = subprocess.Popen(
                [sys.executable, "-u", str(BUILD_SCRIPT), task, "--rounds", str(rounds)],
                cwd=str(BASE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.q.put(("post", "Build", line))
            proc.wait()
            self.q.put(("post", "System", f"🛠 Build დასრულდა (exit {proc.returncode})."))
        except Exception as e:
            self.q.put(("post", "System", f"⚠️ Build error: {e}"))
        finally:
            self.q.put(("busy", False, None))

    def _on_push(self):
        self._post("System", f"⤴ Push: commit + push → {config.WORKSPACE_DIR}")
        self._set_busy(True)
        def done(f):
            try:
                res = f.result()
                self.q.put(("post", "System", f"⤴ {res.get('summary')}"))
            except Exception as e:
                self.q.put(("post", "System", f"⚠️ Push error: {e}"))
            self.q.put(("busy", False, None))
        self.aloop.submit(
            claude_runner.auto_commit_push("council-room: ship project from the Council Room"),
            on_done=done)

    def _on_kill(self):
        try:
            killswitch.engage()
            self._post("System", "🛑 Kill-switch ჩართულია — ახალი სამუშაო გაიყინა.")
        except Exception as e:
            self._post("System", f"⚠️ Kill failed: {e}")

    def _save_transcript(self):
        self._post("System", f"💾 ტრანსკრიპტი ცოცხლად იწერება: {TRANSCRIPT}")

    # ----- input helpers (Georgian on Windows, paste) ---------------------
    def _on_key_press(self, event):
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.ToUnicodeEx.argtypes = [
                    wintypes.UINT, wintypes.UINT, ctypes.POINTER(wintypes.BYTE * 256),
                    wintypes.LPWSTR, ctypes.c_int, wintypes.UINT, wintypes.HKL]
                user32.ToUnicodeEx.restype = ctypes.c_int
                kbd = (wintypes.BYTE * 256)()
                user32.GetKeyboardState(ctypes.byref(kbd))
                buf = ctypes.create_unicode_buffer(5)
                hkl = user32.GetKeyboardLayout(0)
                if user32.ToUnicodeEx(event.keycode, 0, ctypes.byref(kbd), buf, 5, 0, hkl) > 0:
                    if any(ord(c) > 127 for c in buf.value):
                        event.widget.insert("insert", buf.value)
                        return "break"
            except Exception:
                pass

    def _on_paste(self, event):
        try:
            event.widget.insert("insert", self.root.clipboard_get())
        except Exception:
            pass
        return "break"

    def _font(self, families, size, weight="normal"):
        avail = set(tkfont.families())
        fam = next((f for f in families if f in avail), families[-1])
        return tkfont.Font(family=fam, size=size, weight=weight)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
