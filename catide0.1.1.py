#!/usr/bin/env python3
"""
CatIDE 0.1 -- "vibe coding" IDE powered by LM Studio.

A single-file Cursor-style IDE:
  * Code editor with line numbers + Python syntax highlighting
  * AI chat sidebar streaming from LM Studio (OpenAI-compatible local API)
  * Run console for executing the current buffer
  * Blue-hue theme: blue text, deep-blue background, black buttons

Requires only the Python standard library (tkinter). Python 3.10 - 3.14.
LM Studio must be running its local server (default http://localhost:1234).
"""

import json
import keyword
import queue
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.error
import urllib.request
from tkinter import filedialog, font as tkfont, messagebox

# ----------------------------------------------------------------------------
# Theme -- blue hue everywhere, black buttons
# ----------------------------------------------------------------------------
BG        = "#050a18"   # window background (near-black blue)
PANEL     = "#081028"   # panel background
EDITOR_BG = "#060c1e"   # editor background
CONSOLE_BG = "#040814"
TEXT_BLUE = "#5aa9ff"   # primary blue text
DIM_BLUE  = "#2e5e9e"   # secondary / line numbers
BRIGHT    = "#9cd2ff"   # bright accents
ACCENT    = "#1f6feb"   # selection / highlights
BTN_BG    = "#000000"   # buttons = black
BTN_FG    = "#5aa9ff"
BTN_ACTIVE = "#0d1b3d"
CURSOR_COL = "#9cd2ff"

SYNTAX = {
    "kw":      "#7aa2ff",  # keywords
    "builtin": "#4dd0e1",  # builtins
    "string":  "#82b1ff",  # strings
    "comment": "#3d5a80",  # comments
    "number":  "#40c4ff",  # numbers
    "deco":    "#448aff",  # decorators
    "def":     "#9cd2ff",  # def/class names
}

LM_STUDIO_BASE = "http://localhost:1234/v1"

SYSTEM_PROMPT = (
    "You are CatIDE AI, the built-in vibe coding assistant of CatIDE 0.1. "
    "You help the user write, fix and understand code. Be concise and chill. "
    "When you output code, always use fenced markdown code blocks."
)


# ----------------------------------------------------------------------------
# LM Studio client (stdlib only, streaming)
# ----------------------------------------------------------------------------
class LMStudio:
    def __init__(self, base=LM_STUDIO_BASE):
        self.base = base

    def list_models(self):
        req = urllib.request.Request(self.base + "/models")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m["id"] for m in data.get("data", [])]

    def stream_chat(self, messages, model, on_token, on_done, on_error, stop_flag):
        """Stream a chat completion; callbacks fire from the worker thread."""
        payload = json.dumps({
            "model": model,
            "messages": messages,
            "temperature": 0.7,
            "stream": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                for raw in resp:
                    if stop_flag.is_set():
                        break
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        delta = json.loads(chunk)["choices"][0]["delta"]
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    token = delta.get("content")
                    if token:
                        on_token(token)
            on_done()
        except urllib.error.URLError as e:
            on_error(
                "Can't reach LM Studio at " + self.base +
                "\nStart the local server in LM Studio (Developer tab) "
                "and load a model.\n\n" + str(e)
            )
        except Exception as e:  # noqa: BLE001 -- surface anything to the chat
            on_error(str(e))


# ----------------------------------------------------------------------------
# Editor with line numbers + syntax highlighting
# ----------------------------------------------------------------------------
class Editor(tk.Frame):
    def __init__(self, master, **kw):
        super().__init__(master, bg=PANEL, **kw)
        mono = self._pick_font()

        self.linenos = tk.Text(
            self, width=5, padx=6, takefocus=0, bd=0,
            bg=PANEL, fg=DIM_BLUE, font=mono, state="disabled",
            highlightthickness=0,
        )
        self.linenos.pack(side="left", fill="y")

        self.text = tk.Text(
            self, wrap="none", undo=True, bd=0, padx=8, pady=6,
            bg=EDITOR_BG, fg=TEXT_BLUE, insertbackground=CURSOR_COL,
            selectbackground=ACCENT, selectforeground="#ffffff",
            font=mono, highlightthickness=0, tabs=(mono.measure("    "),),
        )
        self.text.pack(side="left", fill="both", expand=True)

        ysb = tk.Scrollbar(self, orient="vertical", command=self._yscroll)
        ysb.pack(side="right", fill="y")
        self.text.configure(yscrollcommand=lambda a, b: (ysb.set(a, b), self._sync()))

        for tag, color in SYNTAX.items():
            self.text.tag_configure(tag, foreground=color)
        self.text.tag_configure("comment", font=mono)

        self.text.bind("<KeyRelease>", self._on_change)
        self.text.bind("<Return>", self._auto_indent)
        self.text.bind("<Tab>", self._soft_tab)
        self._sync()

    @staticmethod
    def _pick_font():
        wanted = ("SF Mono", "Menlo", "JetBrains Mono", "Consolas", "Courier New")
        avail = set(tkfont.families())
        for name in wanted:
            if name in avail:
                return tkfont.Font(family=name, size=13)
        return tkfont.Font(family="Courier", size=13)

    # -- scrolling / line numbers ------------------------------------------
    def _yscroll(self, *args):
        self.text.yview(*args)
        self._sync()

    def _sync(self):
        lines = int(self.text.index("end-1c").split(".")[0])
        content = "\n".join(str(i) for i in range(1, lines + 1))
        self.linenos.configure(state="normal")
        self.linenos.delete("1.0", "end")
        self.linenos.insert("1.0", content)
        self.linenos.configure(state="disabled")
        self.linenos.yview_moveto(self.text.yview()[0])

    # -- editing helpers -----------------------------------------------------
    def _auto_indent(self, _event):
        line = self.text.get("insert linestart", "insert")
        indent = re.match(r"[ \t]*", line).group(0)
        if line.rstrip().endswith(":"):
            indent += "    "
        self.text.insert("insert", "\n" + indent)
        self.after_idle(self._on_change)
        return "break"

    def _soft_tab(self, _event):
        self.text.insert("insert", "    ")
        return "break"

    def _on_change(self, _event=None):
        self._sync()
        self.highlight()

    # -- syntax highlighting -------------------------------------------------
    _patterns = [
        ("comment", re.compile(r"#[^\n]*")),
        ("string",  re.compile(r"('''.*?'''|\"\"\".*?\"\"\"|'[^'\n]*'|\"[^\"\n]*\")", re.S)),
        ("deco",    re.compile(r"@\w+")),
        ("number",  re.compile(r"\b\d+(\.\d+)?\b")),
        ("def",     re.compile(r"(?<=\bdef\s)\w+|(?<=\bclass\s)\w+")),
        ("kw",      re.compile(r"\b(" + "|".join(keyword.kwlist) + r")\b")),
        ("builtin", re.compile(r"\b(print|len|range|str|int|float|list|dict|set|tuple|open|type|super|self|True|False|None|isinstance|enumerate|zip|map|filter)\b")),
    ]

    def highlight(self):
        src = self.text.get("1.0", "end-1c")
        for tag in SYNTAX:
            self.text.tag_remove(tag, "1.0", "end")
        for tag, pat in self._patterns:
            for m in pat.finditer(src):
                self.text.tag_add(tag, f"1.0+{m.start()}c", f"1.0+{m.end()}c")

    # -- content API -----------------------------------------------------------
    def get(self):
        return self.text.get("1.0", "end-1c")

    def set(self, content):
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self._on_change()


# ----------------------------------------------------------------------------
# Main application
# ----------------------------------------------------------------------------
class CatIDE(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CatIDE 0.1 — vibe coding, powered by LM Studio")
        self.geometry("1280x800")
        self.configure(bg=BG)
        self.minsize(900, 560)

        self.lm = LMStudio()
        self.model = None
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.file_path = None
        self.stop_flag = threading.Event()
        self.ui_queue = queue.Queue()
        self.streaming = False
        self.last_ai_reply = ""

        self._build_ui()
        self._bind_keys()
        self.after(80, self._pump)
        threading.Thread(target=self._detect_model, daemon=True).start()

    # -- UI construction --------------------------------------------------------
    def _btn(self, parent, label, cmd):
        return tk.Button(
            parent, text=label, command=cmd,
            bg=BTN_BG, fg=BTN_FG, activebackground=BTN_ACTIVE,
            activeforeground=BRIGHT, bd=0, padx=14, pady=6,
            font=("Helvetica", 12, "bold"), cursor="hand2",
            highlightbackground=BG, highlightthickness=1,
        )

    def _build_ui(self):
        # top bar --------------------------------------------------------------
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=8, pady=(8, 4))

        tk.Label(
            bar, text="🐱 CatIDE 0.1", bg=BG, fg=BRIGHT,
            font=("Helvetica", 15, "bold"),
        ).pack(side="left", padx=(2, 16))

        for label, cmd in (
            ("New", self.new_file), ("Open", self.open_file),
            ("Save", self.save_file), ("▶ Run", self.run_code),
        ):
            self._btn(bar, label, cmd).pack(side="left", padx=3)

        self.model_label = tk.Label(
            bar, text="model: connecting…", bg=BG, fg=DIM_BLUE,
            font=("Helvetica", 11),
        )
        self.model_label.pack(side="right", padx=6)

        # main split: editor+console | chat -------------------------------------
        main = tk.PanedWindow(
            self, orient="horizontal", bg=BG, sashwidth=6,
            bd=0, sashrelief="flat",
        )
        main.pack(fill="both", expand=True, padx=8, pady=4)

        left = tk.PanedWindow(main, orient="vertical", bg=BG, sashwidth=6, bd=0)
        main.add(left, stretch="always", minsize=420)

        self.editor = Editor(left)
        left.add(self.editor, stretch="always", minsize=260)

        console_frame = tk.Frame(left, bg=PANEL)
        left.add(console_frame, height=170, minsize=80)
        tk.Label(
            console_frame, text="  console", bg=PANEL, fg=DIM_BLUE,
            anchor="w", font=("Helvetica", 10, "bold"),
        ).pack(fill="x")
        self.console = tk.Text(
            console_frame, bg=CONSOLE_BG, fg=TEXT_BLUE, bd=0, padx=8, pady=4,
            font=("Menlo", 12), height=8, state="disabled",
            insertbackground=CURSOR_COL, highlightthickness=0,
        )
        self.console.pack(fill="both", expand=True)

        # chat sidebar ------------------------------------------------------------
        chat = tk.Frame(main, bg=PANEL)
        main.add(chat, width=420, minsize=300)

        head = tk.Frame(chat, bg=PANEL)
        head.pack(fill="x", pady=(6, 2), padx=8)
        tk.Label(
            head, text="✨ vibe chat", bg=PANEL, fg=BRIGHT,
            font=("Helvetica", 13, "bold"),
        ).pack(side="left")
        self._btn(head, "Apply code", self.apply_code).pack(side="right", padx=2)
        self._btn(head, "Clear", self.clear_chat).pack(side="right", padx=2)

        self.chat_log = tk.Text(
            chat, bg=EDITOR_BG, fg=TEXT_BLUE, bd=0, padx=10, pady=8,
            wrap="word", font=("Helvetica", 13), state="disabled",
            highlightthickness=0,
        )
        self.chat_log.pack(fill="both", expand=True, padx=8)
        self.chat_log.tag_configure("you", foreground=BRIGHT, font=("Helvetica", 13, "bold"))
        self.chat_log.tag_configure("cat", foreground="#4dd0e1", font=("Helvetica", 13, "bold"))
        self.chat_log.tag_configure("err", foreground="#ff6b6b")
        self.chat_log.tag_configure("code", foreground="#9cd2ff", font=("Menlo", 12))

        entry_row = tk.Frame(chat, bg=PANEL)
        entry_row.pack(fill="x", padx=8, pady=8)

        self.include_code = tk.BooleanVar(value=True)
        tk.Checkbutton(
            entry_row, text="attach editor code", variable=self.include_code,
            bg=PANEL, fg=DIM_BLUE, selectcolor=BTN_BG,
            activebackground=PANEL, activeforeground=TEXT_BLUE,
            font=("Helvetica", 10), highlightthickness=0,
        ).pack(anchor="w")

        box = tk.Frame(entry_row, bg=PANEL)
        box.pack(fill="x", pady=(4, 0))
        self.chat_entry = tk.Text(
            box, height=3, bg=CONSOLE_BG, fg=TEXT_BLUE, bd=0, padx=8, pady=6,
            wrap="word", font=("Helvetica", 13), insertbackground=CURSOR_COL,
            highlightthickness=1, highlightbackground=DIM_BLUE,
            highlightcolor=ACCENT,
        )
        self.chat_entry.pack(side="left", fill="both", expand=True)
        self.send_btn = self._btn(box, "Send ⏎", self.send_chat)
        self.send_btn.pack(side="right", fill="y", padx=(6, 0))

        # status bar ---------------------------------------------------------------
        self.status = tk.Label(
            self, text="ready to vibe ✨", bg=BG, fg=DIM_BLUE, anchor="w",
            font=("Helvetica", 10), padx=10,
        )
        self.status.pack(fill="x", pady=(0, 4))

        self.editor.set(
            '# Welcome to CatIDE 0.1 — vibe coding with LM Studio 🐱💙\n'
            '# Cmd/Ctrl+R to run · chat with the AI on the right →\n\n'
            'def vibe():\n'
            '    print("hello from CatIDE 0.1")\n\n'
            'vibe()\n'
        )

    def _bind_keys(self):
        mod = "Command" if sys.platform == "darwin" else "Control"
        self.bind(f"<{mod}-s>", lambda e: self.save_file())
        self.bind(f"<{mod}-o>", lambda e: self.open_file())
        self.bind(f"<{mod}-n>", lambda e: self.new_file())
        self.bind(f"<{mod}-r>", lambda e: self.run_code())
        self.chat_entry.bind("<Return>", self._chat_return)
        self.chat_entry.bind("<Shift-Return>", lambda e: None)

    def _chat_return(self, _event):
        self.send_chat()
        return "break"

    # -- model detection -------------------------------------------------------
    def _detect_model(self):
        try:
            models = self.lm.list_models()
            self.model = models[0] if models else "local-model"
            label = f"model: {self.model}"
        except Exception:
            self.model = "local-model"
            label = "model: LM Studio offline — start its local server"
        self.ui_queue.put(("model", label))

    # -- file ops ------------------------------------------------------------------
    def new_file(self):
        self.editor.set("")
        self.file_path = None
        self._set_status("new buffer")

    def open_file(self):
        path = filedialog.askopenfilename(
            filetypes=[("Python", "*.py"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.editor.set(f.read())
            self.file_path = path
            self._set_status(f"opened {path}")
        except OSError as e:
            messagebox.showerror("CatIDE", f"Couldn't open file:\n{e}")

    def save_file(self):
        path = self.file_path or filedialog.asksaveasfilename(
            defaultextension=".py",
            filetypes=[("Python", "*.py"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.editor.get())
            self.file_path = path
            self._set_status(f"saved {path}")
        except OSError as e:
            messagebox.showerror("CatIDE", f"Couldn't save file:\n{e}")

    # -- run ---------------------------------------------------------------------
    def run_code(self):
        code = self.editor.get()
        self._console_clear()
        self._console_write("▶ running…\n")
        threading.Thread(target=self._run_worker, args=(code,), daemon=True).start()

    def _run_worker(self, code):
        with tempfile.NamedTemporaryFile(
                "w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(code)
            path = f.name
        try:
            proc = subprocess.run(
                [sys.executable, path],
                capture_output=True, text=True, timeout=30,
            )
            out = proc.stdout + (("\n" + proc.stderr) if proc.stderr else "")
            out += f"\n— exit code {proc.returncode} —\n"
        except subprocess.TimeoutExpired:
            out = "\n— timed out after 30s —\n"
        self.ui_queue.put(("console", out))

    # -- chat ------------------------------------------------------------------------
    def send_chat(self):
        if self.streaming:
            self.stop_flag.set()
            return
        prompt = self.chat_entry.get("1.0", "end-1c").strip()
        if not prompt:
            return
        self.chat_entry.delete("1.0", "end")

        user_msg = prompt
        if self.include_code.get() and self.editor.get().strip():
            user_msg += (
                "\n\n[current editor code]\n```python\n"
                + self.editor.get() + "\n```"
            )
        self.history.append({"role": "user", "content": user_msg})

        self._chat_write("\nyou › ", "you")
        self._chat_write(prompt + "\n")
        self._chat_write("\ncat › ", "cat")

        self.streaming = True
        self.last_ai_reply = ""
        self.stop_flag.clear()
        self.send_btn.configure(text="Stop ■")
        self._set_status("thinking…")

        threading.Thread(
            target=self.lm.stream_chat,
            args=(
                list(self.history), self.model,
                lambda t: self.ui_queue.put(("token", t)),
                lambda: self.ui_queue.put(("done", None)),
                lambda e: self.ui_queue.put(("error", e)),
                self.stop_flag,
            ),
            daemon=True,
        ).start()

    def clear_chat(self):
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.chat_log.configure(state="normal")
        self.chat_log.delete("1.0", "end")
        self.chat_log.configure(state="disabled")
        self._set_status("chat cleared")

    def apply_code(self):
        """Pull the last fenced code block from the AI reply into the editor."""
        blocks = re.findall(r"```(?:\w+)?\n(.*?)```", self.last_ai_reply, re.S)
        if not blocks:
            self._set_status("no code block in last AI reply")
            return
        self.editor.set(blocks[-1].rstrip() + "\n")
        self._set_status("applied AI code to editor 💙")

    # -- UI-thread pump for worker events ------------------------------------------
    def _pump(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "token":
                    self.last_ai_reply += payload
                    self._chat_write(payload)
                elif kind == "done":
                    self._finish_stream()
                elif kind == "error":
                    self._chat_write("\n⚠ " + payload + "\n", "err")
                    self._finish_stream(error=True)
                elif kind == "console":
                    self._console_write(payload)
                elif kind == "model":
                    self.model_label.configure(text=payload)
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def _finish_stream(self, error=False):
        if self.last_ai_reply:
            self.history.append({"role": "assistant", "content": self.last_ai_reply})
        self._chat_write("\n")
        self.streaming = False
        self.send_btn.configure(text="Send ⏎")
        self._set_status("ready to vibe ✨" if not error else "LM Studio error")

    # -- small helpers ----------------------------------------------------------------
    def _chat_write(self, text, tag=None):
        self.chat_log.configure(state="normal")
        self.chat_log.insert("end", text, tag)
        self.chat_log.see("end")
        self.chat_log.configure(state="disabled")

    def _console_clear(self):
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")

    def _console_write(self, text):
        self.console.configure(state="normal")
        self.console.insert("end", text)
        self.console.see("end")
        self.console.configure(state="disabled")

    def _set_status(self, text):
        self.status.configure(text=text)


if __name__ == "__main__":
    CatIDE().mainloop()
