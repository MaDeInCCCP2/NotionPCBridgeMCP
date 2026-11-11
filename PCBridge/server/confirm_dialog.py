"""PC Bridge: the confirmation window for a dangerous command (started by server.py as a separate process).

    python confirm_dialog.py request.json   ->   prints one word: yes | similar | no | timeout

request.json: {"command", "reasons": [...], "cwd", "timeout", "spans": [[start, end], ...], "allow_similar": bool,
               "similar_minutes": 10}
The safe answer is the default everywhere: Enter / Esc / closing the window / no answer = "no".
The Allow buttons are disabled for the first moments so a key pressed while typing cannot approve anything.
If tkinter is missing the process exits with code 3 and server.py falls back to the plain Yes/No message box.
"""
import json
import sys

ARM_DELAY_MS = 1500


def main() -> int:
    try:
        req = json.loads(open(sys.argv[1], encoding="utf-8").read())
    except (IndexError, OSError, ValueError):
        return 2
    try:
        import tkinter as tk
        from tkinter import font as tkfont
    except Exception:
        return 3

    command = str(req.get("command", ""))
    reasons = [str(r) for r in req.get("reasons", [])] or ["опасная команда"]
    timeout = max(10, int(req.get("timeout", 120)))
    minutes = int(req.get("similar_minutes", 10))
    answer = {"v": "no"}

    root = tk.Tk()
    root.title("PC Bridge - подтвердите опасную команду")
    root.configure(bg="#ffffff")
    root.attributes("-topmost", True)
    root.resizable(True, True)
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except Exception:
        pass
    base = tkfont.nametofont("TkDefaultFont")
    base.configure(family="Segoe UI", size=10)
    bold = base.copy()
    bold.configure(weight="bold")
    big = base.copy()
    big.configure(size=13, weight="bold")
    mono = tkfont.Font(family="Consolas", size=10)

    head = tk.Frame(root, bg="#c62828")
    head.pack(fill="x")
    tk.Label(head, text="⚠  Notion-агент хочет выполнить опасную команду", font=big, fg="white", bg="#c62828",
             anchor="w", padx=16, pady=10).pack(fill="x")

    body = tk.Frame(root, bg="#ffffff", padx=16, pady=10)
    body.pack(fill="both", expand=True)
    tk.Label(body, text="Почему опасно:", font=bold, bg="#ffffff", anchor="w").pack(fill="x")
    for r in reasons[:6]:
        tk.Label(body, text="•  " + r, bg="#ffffff", fg="#b71c1c", anchor="w", justify="left",
                 wraplength=640).pack(fill="x", padx=(8, 0))
    tk.Label(body, text="Папка:  " + str(req.get("cwd", "")), bg="#ffffff", fg="#555555", anchor="w",
             justify="left", wraplength=660).pack(fill="x", pady=(8, 4))

    box = tk.Frame(body, bg="#d0d0d0", bd=0, padx=1, pady=1)
    box.pack(fill="both", expand=True)
    lines = command.count("\n") + 1
    txt = tk.Text(box, font=mono, height=max(3, min(14, lines + 1)), width=88, wrap="word", bg="#fafafa",
                  relief="flat", padx=8, pady=6)
    sb = tk.Scrollbar(box, command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    txt.pack(side="left", fill="both", expand=True)
    txt.insert("1.0", command)
    txt.tag_configure("danger", background="#ffcdd2", foreground="#b71c1c", font=("Consolas", 10, "bold"))
    for s, e in req.get("spans", [])[:50]:
        try:
            txt.tag_add("danger", f"1.0+{int(s)}c", f"1.0+{int(e)}c")
        except Exception:
            pass
    txt.configure(state="disabled")

    tk.Label(body, text="Если вы не просили агента об этом - нажмите «Запретить».", bg="#ffffff", anchor="w",
             font=bold).pack(fill="x", pady=(10, 0))
    left = {"s": timeout}
    timer = tk.Label(body, bg="#ffffff", fg="#555555", anchor="w")
    timer.pack(fill="x")
    bar_bg = tk.Frame(body, bg="#eeeeee", height=4)
    bar_bg.pack(fill="x", pady=(4, 0))
    bar = tk.Frame(bar_bg, bg="#c62828", height=4)
    bar.place(x=0, y=0, relheight=1, relwidth=1)

    btns = tk.Frame(root, bg="#f3f3f3", padx=16, pady=12)
    btns.pack(fill="x")

    def done(v):
        answer["v"] = v
        root.destroy()

    deny = tk.Button(btns, text="Запретить", font=bold, bg="#c62828", fg="white", activebackground="#8e0000",
                     activeforeground="white", relief="flat", padx=18, pady=6, command=lambda: done("no"))
    deny.pack(side="right")
    once = tk.Button(btns, text="Разрешить один раз", relief="flat", padx=14, pady=6, bg="#e0e0e0",
                     state="disabled", command=lambda: done("yes"))
    once.pack(side="right", padx=(0, 8))
    similar = None
    if req.get("allow_similar"):
        similar = tk.Button(btns, text=f"Разрешить похожие на {minutes} мин", relief="flat", padx=14, pady=6,
                            bg="#e0e0e0", state="disabled", command=lambda: done("similar"))
        similar.pack(side="right", padx=(0, 8))
    tk.Label(btns, text="Enter / Esc = запретить", bg="#f3f3f3", fg="#777777").pack(side="left")

    def arm():
        for b in (once, similar):
            if b is not None:
                b.configure(state="normal")

    def tick():
        if left["s"] <= 0:
            done("timeout")
            return
        timer.configure(text=f"Без ответа команда будет отменена через {left['s']} с")
        bar.place_configure(relwidth=max(0.0, left["s"] / timeout))
        left["s"] -= 1
        root.after(1000, tick)

    root.bind("<Return>", lambda _e: done("no"))
    root.bind("<Escape>", lambda _e: done("no"))
    root.protocol("WM_DELETE_WINDOW", lambda: done("no"))
    root.after(ARM_DELAY_MS, arm)
    tick()
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    x = (root.winfo_screenwidth() - w) // 2
    y = max(0, (root.winfo_screenheight() - h) // 3)
    root.geometry(f"+{x}+{y}")
    root.lift()
    root.focus_force()
    deny.focus_set()
    root.bell()
    root.mainloop()
    print(answer["v"], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
