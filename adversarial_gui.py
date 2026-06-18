"""
Desktop GUI for the Claude<->Gemini adversarial loop.

A dark, Porsche-themed window: type a task, hit RUN, watch the loop stream live.
When a run finishes you can type a follow-up and RUN again (it builds on the same
workspace), or just close. This is a thin wrapper — it shells out to
run_adversarial.py and streams its stdout; all the real logic stays there.

Launched by Run-Adversarial-GUI.bat (double-click). No external dependencies —
tkinter ships with Python.
"""
import sys
import json
import queue
import threading
import subprocess
from pathlib import Path
import tkinter as tk
from tkinter import scrolledtext, font as tkfont

BASE = Path(__file__).parent.resolve()
SCRIPT = BASE / "run_adversarial.py"
STATE = BASE / "shared" / "state.json"

# Porsche Aftersales palette (.claude/rules/common/brand_rules.md)
BG    = "#0F0F12"   # carbon background
PANEL = "#1C1C24"   # card background
EDGE  = "#2A2A33"   # subtle borders
RED   = "#D5001C"   # Guards Red — accent / active step
GREEN = "#88D413"   # Acid Green — success
AMBER = "#E0A000"   # in-progress
TEXT  = "#E8E8EC"
MUTED = "#8A8A95"


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Adversarial Loop — Claude × Gemini")
        self.root.configure(bg=BG)
        self.root.geometry("840x640")
        self.root.minsize(640, 480)
        self.proc = None
        self.q: queue.Queue = queue.Queue()

        ui = self._font(["Inter", "Outfit", "Segoe UI"], 11)
        big = self._font(["Inter", "Outfit", "Segoe UI"], 15, "bold")
        mono = self._font(["Cascadia Code", "Consolas", "Courier New"], 10)

        tk.Label(self.root, text="ADVERSARIAL LOOP", bg=BG, fg=TEXT,
                 font=big).pack(anchor="w", padx=18, pady=(16, 0))
        tk.Label(self.root, text="Claude codes  ·  Gemini reviews", bg=BG, fg=MUTED,
                 font=ui).pack(anchor="w", padx=18)

        tk.Label(self.root, text="დავალება:", bg=BG, fg=TEXT,
                 font=ui).pack(anchor="w", padx=18, pady=(14, 4))
        self.task = tk.Text(self.root, height=4, bg=PANEL, fg=TEXT, insertbackground=TEXT,
                            relief="flat", font=ui, wrap="word", padx=10, pady=8,
                            highlightthickness=1, highlightbackground=EDGE, highlightcolor=RED)
        self.task.pack(fill="x", padx=18)

        row = tk.Frame(self.root, bg=BG)
        row.pack(fill="x", padx=18, pady=12)
        tk.Label(row, text="რაუნდები:", bg=BG, fg=MUTED, font=ui).pack(side="left")
        self.rounds = tk.Spinbox(row, from_=1, to=5, width=3, bg=PANEL, fg=TEXT, relief="flat",
                                 buttonbackground=PANEL, font=ui, justify="center",
                                 highlightthickness=1, highlightbackground=EDGE)
        self.rounds.delete(0, "end")
        self.rounds.insert(0, "3")
        self.rounds.pack(side="left", padx=(6, 16))
        self.run_btn = tk.Button(row, text="▶  RUN", bg=RED, fg="white", relief="flat",
                                 font=big, activebackground="#A50016", activeforeground="white",
                                 padx=18, pady=4, cursor="hand2", command=self.on_run)
        self.run_btn.pack(side="left")
        self.status = tk.Label(row, text="მზად", bg=BG, fg=MUTED, font=ui)
        self.status.pack(side="left", padx=14)

        self.log = scrolledtext.ScrolledText(self.root, bg="#0A0A0D", fg=TEXT, relief="flat",
                                             font=mono, wrap="word", padx=10, pady=8, state="disabled")
        self.log.pack(fill="both", expand=True, padx=18, pady=(0, 12))
        self.log.tag_config("ok", foreground=GREEN)
        self.log.tag_config("err", foreground=RED)
        self.log.tag_config("muted", foreground=MUTED)

        tk.Button(self.root, text="დახურვა", bg=PANEL, fg=TEXT, relief="flat", font=ui,
                  activebackground=EDGE, activeforeground=TEXT, padx=14, pady=4,
                  cursor="hand2", command=self.root.destroy).pack(anchor="e", padx=18, pady=(0, 14))

        # Ctrl+Enter also runs.
        self.task.bind("<Control-Return>", lambda e: (self.on_run(), "break")[1])
        self.task.focus_set()
        self.root.after(80, self._drain)

    def _font(self, families, size, weight="normal"):
        avail = set(tkfont.families())
        fam = next((f for f in families if f in avail), families[-1])
        return tkfont.Font(family=fam, size=size, weight=weight)

    def _append(self, text, tag=None):
        self.log.config(state="normal")
        self.log.insert("end", text, tag or ())
        self.log.see("end")
        self.log.config(state="disabled")

    def on_run(self):
        if self.proc is not None:
            return
        task = self.task.get("1.0", "end").strip()
        if not task:
            self.status.config(text="ჩაწერე დავალება", fg=RED)
            return
        try:
            rounds = max(1, min(int(self.rounds.get()), 5))
        except ValueError:
            rounds = 3
        self.run_btn.config(state="disabled")
        self.task.config(state="disabled")
        self.status.config(text="მუშაობს…", fg=AMBER)
        self._append(f"\n{'=' * 64}\n▶ {task}\n{'=' * 64}\n", "muted")
        argv = [sys.executable, str(SCRIPT), task, "--rounds", str(rounds)]
        threading.Thread(target=self._worker, args=(argv,), daemon=True).start()

    def _worker(self, argv):
        try:
            self.proc = subprocess.Popen(
                argv, cwd=str(BASE),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
            for line in self.proc.stdout:
                self.q.put(("line", line))
            self.proc.wait()
            self.q.put(("done", self.proc.returncode))
        except Exception as e:
            self.q.put(("line", f"\n[GUI error] {e}\n"))
            self.q.put(("done", -1))

    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "line":
                    low = payload.lower()
                    tag = ("ok" if "approved" in low
                           else "err" if ("⛔" in payload or "ceiling" in low or "failed" in low)
                           else None)
                    self._append(payload, tag)
                elif kind == "done":
                    self._finish()
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def _read_status(self) -> str:
        try:
            return json.loads(STATE.read_text(encoding="utf-8")).get("status", "")
        except Exception:
            return ""

    def _finish(self):
        self.proc = None
        self.run_btn.config(state="normal")
        self.task.config(state="normal")
        status = self._read_status()
        if status == "approved":
            self.status.config(text="✅ APPROVED — დაამატე ან დახურე", fg=GREEN)
        elif status == "ceiling_reached":
            self.status.config(text="⚠ ჭერი — შენი გადასაწყვეტია", fg=RED)
        elif status == "coder_error":
            self.status.config(text="⛔ Coder შეცდომა", fg=RED)
        else:
            self.status.config(text="დასრულდა — დაამატე ან დახურე", fg=MUTED)
        self._append("\n— ციკლი დასრულდა. ახალი დავალება ჩაწერე და RUN, ან დახურე. —\n", "muted")
        self.task.delete("1.0", "end")
        self.task.focus_set()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
