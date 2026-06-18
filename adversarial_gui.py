"""
Desktop GUI for the Claude<->Gemini adversarial loop.
Styled to look like a premium Porsche-themed developer cockpit.
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
ENV_FILE = BASE / ".env"

# Porsche-inspired Dark Palette
BG         = "#0A0A0C"   # Deep carbon black
CARD_BG    = "#141417"   # Panel/Card background
INPUT_BG   = "#1D1D22"   # Text input fields
BORDER_COLOR = "#25252B" # Subtle borders
TEXT_COLOR = "#F2F2F5"   # High contrast off-white
MUTED      = "#757582"   # Muted gray for labels
RED        = "#D5001C"   # Guards Red - primary action / active status
GREEN      = "#82D200"   # Acid Green - success status
AMBER      = "#FF9B00"   # Porsche Amber - in progress status

class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("AI Cockpit — Claude × Gemini")
        self.root.configure(bg=BG)
        self.root.geometry("900x700")
        self.root.minsize(700, 500)
        self.proc = None
        self.q = queue.Queue()
        self.selected_rounds = 3
        self.round_btns = []

        # Try to resolve fonts
        self.ui_font = self._resolve_font(["Inter", "Outfit", "Segoe UI"], 10)
        self.label_font = self._resolve_font(["Inter", "Outfit", "Segoe UI"], 9, "bold")
        self.big_font = self._resolve_font(["Inter", "Outfit", "Segoe UI"], 16, "bold")
        self.mono_font = self._resolve_font(["Cascadia Code", "Consolas", "Courier New"], 10)

        # 1. HEADER AREA
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=24, pady=(20, 10))
        
        tk.Label(header, text="AI COCKPIT", bg=BG, fg=TEXT_COLOR,
                 font=self.big_font).pack(anchor="w")
        tk.Label(header, text="Adversarial Review Loop  ·  Claude [Coder]  ⇄  Gemini [Reviewer]", 
                 bg=BG, fg=MUTED, font=self.ui_font).pack(anchor="w", pady=(2, 0))

        # 2. STATUS BAR (Metrics / Configuration)
        status_bar = tk.Frame(self.root, bg=BG)
        status_bar.pack(fill="x", padx=24, pady=(0, 14))

        # Metric 1: Workspace
        ws_name = self._get_workspace_basename()
        self._create_metric_box(status_bar, "WORKSPACE", ws_name).pack(side="left", padx=(0, 12))
        
        # Metric 2: Auto-Push
        push_status = self._get_env_value("AUTO_PUSH", "DISABLED").upper()
        self._create_metric_box(status_bar, "AUTO-PUSH", push_status).pack(side="left", padx=12)

        # Metric 3: Model config
        self._create_metric_box(status_bar, "REVIEWER MODEL", "Gemini 3.5 Flash").pack(side="left", padx=12)

        # 3. CONTROL CARD (Unified Input Panel)
        control_card = tk.Frame(self.root, bg=CARD_BG, highlightthickness=1, 
                                highlightbackground=BORDER_COLOR)
        control_card.pack(fill="x", padx=24, pady=(0, 16))

        # Task label
        tk.Label(control_card, text="დავალების აღწერა / TASK SPECIFICATION", bg=CARD_BG, fg=MUTED,
                 font=self.label_font).pack(anchor="w", padx=16, pady=(12, 4))
        
        # Task input
        self.task = tk.Text(control_card, height=4, bg=INPUT_BG, fg=TEXT_COLOR, insertbackground=TEXT_COLOR,
                            relief="flat", font=self.ui_font, wrap="word", padx=12, pady=10,
                            highlightthickness=1, highlightbackground=BORDER_COLOR, highlightcolor=RED)
        self.task.pack(fill="x", padx=16, pady=(0, 12))

        # Control Row (Rounds and buttons)
        ctrl_row = tk.Frame(control_card, bg=CARD_BG)
        ctrl_row.pack(fill="x", padx=16, pady=(0, 14))

        # Segmented Rounds Control
        rounds_lbl = tk.Label(ctrl_row, text="რაუნდები:", bg=CARD_BG, fg=TEXT_COLOR, font=self.ui_font)
        rounds_lbl.pack(side="left", pady=4)
        
        rounds_frame = tk.Frame(ctrl_row, bg=CARD_BG)
        rounds_frame.pack(side="left", padx=(8, 20))
        
        for i in range(1, 6):
            btn = tk.Button(
                rounds_frame, text=str(i), width=3, bg=RED if i == 3 else INPUT_BG, fg=TEXT_COLOR,
                relief="flat", bd=0, cursor="hand2", font=self.ui_font,
                activebackground=RED, activeforeground="white",
                command=lambda val=i: self.set_rounds(val)
            )
            btn.pack(side="left", padx=2)
            self._bind_hover(btn, RED, INPUT_BG, index=i)
            self.round_btns.append(btn)

        # Run Button
        self.run_btn = tk.Button(ctrl_row, text="▶  RUN TASK", bg=RED, fg="white", relief="flat", bd=0,
                                 font=self._resolve_font(["Inter", "Outfit", "Segoe UI"], 11, "bold"), 
                                 activebackground="#B50017", activeforeground="white",
                                 padx=20, pady=6, cursor="hand2", command=self.on_run)
        self.run_btn.pack(side="left")
        self.run_btn.bind("<Enter>", lambda e: self.run_btn.config(bg="#B50017"))
        self.run_btn.bind("<Leave>", lambda e: self.run_btn.config(bg=RED))

        # Status Circle Indicator
        status_frame = tk.Frame(ctrl_row, bg=CARD_BG)
        status_frame.pack(side="left", padx=20)
        self.status_dot = tk.Label(status_frame, text="●", bg=CARD_BG, fg=MUTED, font=("Segoe UI", 14))
        self.status_dot.pack(side="left")
        self.status_lbl = tk.Label(status_frame, text="მზად", bg=CARD_BG, fg=MUTED, font=self.ui_font)
        self.status_lbl.pack(side="left", padx=4)

        # 4. CONSOLE OUTPUT CARD
        console_card = tk.Frame(self.root, bg=BG)
        console_card.pack(fill="both", expand=True, padx=24, pady=(0, 16))

        tk.Label(console_card, text="ლოგების ნაკადი / COLLABORATION LOG", bg=BG, fg=MUTED,
                 font=self.label_font).pack(anchor="w", pady=(0, 4))

        self.log = scrolledtext.ScrolledText(console_card, bg="#050507", fg=TEXT_COLOR, relief="flat",
                                             font=self.mono_font, wrap="word", padx=14, pady=12, state="disabled",
                                             highlightthickness=1, highlightbackground=BORDER_COLOR)
        self.log.pack(fill="both", expand=True)
        self.log.tag_config("ok", foreground=GREEN)
        self.log.tag_config("err", foreground=RED)
        self.log.tag_config("muted", foreground=MUTED)

        # 5. FOOTER
        footer = tk.Frame(self.root, bg=BG)
        footer.pack(fill="x", padx=24, pady=(0, 16))

        self.close_btn = tk.Button(footer, text="დახურვა / CLOSE", bg=CARD_BG, fg=TEXT_COLOR, relief="flat", bd=0,
                                   font=self.ui_font, activebackground=BORDER_COLOR, activeforeground=TEXT_COLOR,
                                   padx=16, pady=6, cursor="hand2", command=self.root.destroy)
        self.close_btn.pack(side="right")
        self.close_btn.bind("<Enter>", lambda e: self.close_btn.config(bg=BORDER_COLOR))
        self.close_btn.bind("<Leave>", lambda e: self.close_btn.config(bg=CARD_BG))

        # Bindings
        self.task.bind("<Control-Return>", lambda e: (self.on_run(), "break")[1])
        self.task.focus_set()
        self.root.after(80, self._drain)

    def _resolve_font(self, families, size, weight="normal"):
        avail = set(tkfont.families())
        fam = next((f for f in families if f in avail), families[-1])
        return tkfont.Font(family=fam, size=size, weight=weight)

    def _create_metric_box(self, parent, label, value):
        box = tk.Frame(parent, bg=CARD_BG, padx=14, pady=6, highlightthickness=1, 
                       highlightbackground=BORDER_COLOR)
        tk.Label(box, text=label, bg=CARD_BG, fg=MUTED, font=self.label_font).pack(anchor="w")
        tk.Label(box, text=value, bg=CARD_BG, fg=TEXT_COLOR, 
                 font=self._resolve_font(["Inter", "Outfit", "Segoe UI"], 10, "bold")).pack(anchor="w", pady=(2, 0))
        return box

    def _bind_hover(self, widget, hover_bg, normal_bg, index):
        widget.bind("<Enter>", lambda e: widget.config(bg=hover_bg) if self.selected_rounds != index else None)
        widget.bind("<Leave>", lambda e: widget.config(bg=hover_bg if self.selected_rounds == index else normal_bg))

    def set_rounds(self, val):
        self.selected_rounds = val
        for idx, btn in enumerate(self.round_btns, 1):
            btn.config(bg=RED if idx == val else INPUT_BG)

    def _get_workspace_basename(self) -> str:
        try:
            # Simple parsing of workspace dir from .env / config
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                if line.startswith("WORKSPACE_DIR="):
                    p = Path(line.split("=", 1)[1].strip())
                    return p.name or str(p)
        except Exception:
            pass
        return "WORKSPACE"

    def _get_env_value(self, key, default) -> str:
        try:
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
        except Exception:
            pass
        return default

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
            self._update_status("ჩაწერე დავალება", RED)
            return
        self.run_btn.config(state="disabled", bg=BORDER_COLOR)
        self.task.config(state="disabled")
        self._update_status("მუშაობს…", AMBER)
        self._append(f"\n{'=' * 64}\n▶ {task}\n{'=' * 64}\n", "muted")
        argv = [sys.executable, str(SCRIPT), task, "--rounds", str(self.selected_rounds)]
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

    def _update_status(self, text, color):
        self.status_lbl.config(text=text, fg=color)
        self.status_dot.config(fg=color)

    def _finish(self):
        self.proc = None
        self.run_btn.config(state="normal", bg=RED)
        self.task.config(state="normal")
        status = self._read_status()
        if status == "approved":
            self._update_status("APPROVED — დაამატე ან დახურე", GREEN)
        elif status == "ceiling_reached":
            self._update_status("ჭერი — შენი გადასაწყვეტია", RED)
        elif status == "coder_error":
            self._update_status("Coder შეცდომა", RED)
        else:
            self._update_status("დასრულდა", MUTED)
        self._append("\n— ციკლი დასრულდა. ახალი დავალება ჩაწერე და RUN, ან დახურე. —\n", "muted")
        self.task.delete("1.0", "end")
        self.task.focus_set()

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    App().run()
