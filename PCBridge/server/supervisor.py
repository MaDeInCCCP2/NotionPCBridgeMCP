"""PC Bridge supervisor: keeps the server, the ngrok tunnel and the local AI model running, shows the tray icon.

Started by START.bat (setup.ps1) with pythonw - no console window. It
  * restarts server.py / ngrok / llama-server when they die (sleep, network loss, crash) with a back-off;
  * writes logs/state.json (status, public URL), logs/pids.json and URL.txt;
  * tray icon: status, what the agent is doing now, Pause, Read only, notifications, copy URL/token, Stop all;
  * hotkeys: Ctrl+Alt+Shift+P = pause / resume, Ctrl+Alt+Shift+S = stop everything;
  * turns logs/events.jsonl (written by server.py) into Windows notifications;
  * web panel http://127.0.0.1:8767 (panel.py) and notifications with buttons "Show" / "Undo" for file changes.
STOP.bat creates logs/stop.flag - the supervisor then stops everything and exits.
"""
import hashlib
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("PCBRIDGE_ROOT") or APP.parent).resolve()
LOGS = ROOT / "logs"
CFG_PATH = ROOT / "config.json"
STATE = LOGS / "state.json"
PIDS = LOGS / "pids.json"
STOP_FLAG = LOGS / "stop.flag"
PAUSE_FLAG = LOGS / "paused.flag"
RO_FLAG = LOGS / "readonly.flag"
NOTIFY_OFF = LOGS / "notify_off.flag"
EVENTS = LOGS / "events.jsonl"
ACTIVITY = LOGS / "activity.json"
IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0
GROUP = 0x00000200 if IS_WIN else 0  # CREATE_NEW_PROCESS_GROUP
BACKOFF = (2, 5, 10, 30, 60)
# ngrok errors that a restart cannot fix (wrong authtoken, foreign domain, too old, session limit)
PERMANENT_NGROK = (105, 107, 4018, 313, 319, 320, 121, 108)
HOTKEY_MODS = 0x4007  # MOD_ALT | MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT
NGROK_HINTS = {
    105: "неверный authtoken ngrok", 107: "неверный authtoken ngrok", 4018: "нужен authtoken ngrok",
    313: "домен ngrok не ваш", 319: "домен ngrok не найден", 320: "домен ngrok не ваш",
    121: "ngrok слишком старый - удалите tools\\ngrok.exe и запустите START.bat",
    108: "ngrok уже запущен на другом ПК (лимит бесплатного тарифа)",
}


# ------------------------------------------------------------- helpers ----
def parse_ngrok_log(text: str) -> dict:
    """Last public URL / web API address / error code from ngrok's --log=stdout output."""
    out: dict = {}
    for line in text.splitlines():
        m = re.search(r"\burl=(https://\S+)", line)
        if m and ("started tunnel" in line or "obj=tunnels" in line):
            out["url"] = m.group(1).strip('"')
            out.pop("error", None)
        m = re.search(r'obj=web\b.*\baddr="?([\d.]+:\d+)', line)
        if m:
            out["api"] = m.group(1)
        m = re.search(r"ERR_NGROK_(\d+)", line)
        if m:
            out["error"] = f"ERR_NGROK_{m.group(1)}"
            out.pop("url", None)
    return out


def ngrok_args(cfg: dict, port: int) -> list:
    args = ["http", str(port), "--host-header=rewrite", "--inspect=false", "--log=stdout"]
    dom = str(cfg.get("ngrok_domain") or "").strip()
    if dom:
        dom = re.sub(r"^https?://", "", dom).strip("/")
        args.append("--domain=" + dom if cfg.get("ngrok_url_flag") == "domain" else "--url=https://" + dom)
    return args


def mcp_url(url: str, cfg: dict) -> str:
    base = url.rstrip("/")
    return f"{base}/mcp" if cfg.get("token_auth", True) is not False else f"{base}/{cfg.get('secret', '')}/mcp"


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=1.5):
            return True
    except OSError:
        return False


def ctrl_key(secret: str) -> str:
    """Key for the server's local control port (the same formula as in server.py)."""
    return hashlib.sha256(("pcbridge-ctrl:" + secret).encode("utf-8")).hexdigest()


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else many


def load_panel():
    spec = importlib.util.spec_from_file_location("pcbridge_panel", str(Path(__file__).with_name("panel.py")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TOAST_PS = ("$ErrorActionPreference='Stop';"
            "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;"
            "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]|Out-Null;"
            "$x=New-Object Windows.Data.Xml.Dom.XmlDocument;$x.LoadXml($env:PCB_TOAST);"
            "$n=[Windows.UI.Notifications.ToastNotification]::new($x);"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:PCB_AUMID).Show($n)")
TOAST_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8-sig"))


def write_json(p: Path, data) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


class Log:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def __call__(self, *parts) -> None:
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(p) for p in parts)
        with self.lock:
            try:
                if self.path.exists() and self.path.stat().st_size > 2_000_000:
                    os.replace(self.path, self.path.with_suffix(".old.log"))
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass


def kill_tree(proc) -> None:
    try:
        import psutil

        p = psutil.Process(proc.pid)
        kids = p.children(recursive=True)
        for q in kids + [p]:
            try:
                q.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(kids + [p], timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ----------------------------------------------------------- the managed ----
class Child:
    """One managed program with restart back-off."""

    def __init__(self, sup, key: str, title: str):
        self.sup, self.key, self.title = sup, key, title
        self.proc = None
        self.fails = 0
        self.next_try = 0.0
        self.started = 0.0
        self.status = "off"
        self.restarts = 0

    def cmd(self):  # -> (argv, stdout_path, stderr_path) or None when not configured
        raise NotImplementedError

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        spec = self.cmd()
        if not spec:
            return
        argv, out, err = spec
        fo = open(out, "wb")
        fe = open(err, "wb") if err and err != out else subprocess.STDOUT
        try:
            self.proc = subprocess.Popen([str(a) for a in argv], cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=fo,
                                         stderr=fe, creationflags=NO_WINDOW | GROUP)
        finally:
            fo.close()
            if fe is not subprocess.STDOUT:
                fe.close()
        self.started = time.time()
        self.status = "starting"
        self.sup.log("START", self.key, self.proc.pid, argv[0])
        self.sup.save_pids()

    def stop(self) -> None:
        if self.alive():
            self.sup.log("STOP", self.key, self.proc.pid)
            kill_tree(self.proc)
        self.proc = None
        self.status = "off"

    def failed(self, why: str) -> None:
        """Schedule a restart with back-off."""
        self.stop()
        delay = BACKOFF[min(self.fails, len(BACKOFF) - 1)]
        self.fails += 1
        self.restarts += 1
        self.next_try = time.time() + delay
        self.status = "restarting"
        self.sup.log("FAIL", self.key, why, f"- restart in {delay}s")

    def due(self) -> bool:
        return not self.alive() and time.time() >= self.next_try


class Server(Child):
    def cmd(self):
        py = Path(sys.executable)
        if py.name.lower() == "pythonw.exe" and (py.parent / "python.exe").exists():
            py = py.parent / "python.exe"  # console python, but without a window (NO_WINDOW)
        return [py, "-u", APP / "server.py"], LOGS / "server.log", LOGS / "server.err.log"

    def check(self) -> None:
        port = int(self.sup.cfg.get("port", 8765))
        if not self.alive():
            if self.proc is not None:
                self.sup.event("server_restart", "Сервер PC Bridge упал - перезапускаю")
                self.failed(f"exit code {self.proc.poll()}")
            if self.due():
                if port_open(port):  # another program holds the port
                    self.status = "port busy"
                    self.next_try = time.time() + 30
                    return
                self.start()
            return
        if port_open(port):
            if self.status != "ok":
                self.sup.log("UP server port", port)
            self.status = "ok"
            if time.time() - self.started > 120:
                self.fails = 0
        elif time.time() - self.started > 40:
            self.failed("the port does not answer")


class Ngrok(Child):
    def __init__(self, *a):
        super().__init__(*a)
        self.url = ""
        self.error = ""
        self.bad_checks = 0
        self.last_check = 0.0

    def exe(self) -> str:
        c = self.sup.cfg.get("ngrok_path")
        if c and Path(c).exists():
            return c
        local = ROOT / "tools" / ("ngrok.exe" if IS_WIN else "ngrok")
        if local.exists():
            return str(local)
        import shutil

        return shutil.which("ngrok") or ""

    def cmd(self):
        exe = self.exe()
        if not exe:
            self.error = "ngrok не найден - запустите START.bat"
            return None
        self.url = ""
        return [exe] + ngrok_args(self.sup.cfg, int(self.sup.cfg.get("port", 8765))), LOGS / "ngrok.log", None

    def check(self) -> None:
        if self.sup.server.status != "ok" and not self.alive():
            return  # the tunnel waits for the server
        if self.alive():
            try:
                info = parse_ngrok_log((LOGS / "ngrok.log").read_text(encoding="utf-8", errors="replace")[-200_000:])
            except OSError:
                info = {}
            if info.get("url"):
                if info["url"] != self.url:
                    old, self.url = self.url, info["url"]
                    self.sup.url_changed(old, self.url)
                if self.status != "ok":
                    self.sup.event("tunnel_up", "Туннель ngrok открыт - агент может подключаться")
                self.status, self.error = "ok", ""
                if time.time() - self.started > 120:
                    self.fails = 0
                self.probe()
            elif info.get("error"):
                self.error = info["error"]
                code = int(info["error"].split("_")[-1])
                if code in PERMANENT_NGROK:
                    self.sup.event("tunnel_error", f"ngrok: {NGROK_HINTS.get(code, info['error'])}")
                    self.stop()
                    self.status = "error"
                    self.next_try = time.time() + 600  # one more try in 10 minutes (the user may fix it)
                elif time.time() - self.started > 30:
                    self.failed(info["error"])
            elif time.time() - self.started > 45:
                self.failed("no tunnel after 45s")
            return
        if self.proc is not None:
            if self.status == "ok":
                self.sup.event("tunnel_down", "Туннель ngrok упал - перезапускаю")
            self.failed(f"exit code {self.proc.poll()}")
        if self.due():
            self.start()

    def probe(self) -> None:
        """Every minute: is the public URL really reachable? (after sleep ngrok can hang without exiting)"""
        if time.time() - self.last_check < 60 or not self.url:
            return
        self.last_check = time.time()
        req = urllib.request.Request(self.url.rstrip("/") + "/.well-known/pcbridge-ping",
                                     headers={"ngrok-skip-browser-warning": "1", "User-Agent": "pcbridge-supervisor"})
        ok = False
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                ok = not r.headers.get("ngrok-error-code")
        except urllib.error.HTTPError as e:
            ok = not e.headers.get("ngrok-error-code")  # 404 from OUR server = the tunnel works
        except Exception:
            ok = False
        if ok:
            self.bad_checks = 0
            return
        self.bad_checks += 1
        self.sup.log("PROBE failed", self.bad_checks)
        if self.bad_checks >= 3:
            self.bad_checks = 0
            self.sup.event("tunnel_down", "Туннель не отвечает (сон / сеть?) - переподключаю")
            self.failed("public URL does not answer")


class Llama(Child):
    def enabled(self) -> bool:
        c = self.sup.cfg
        return str(c.get("ai_check", "off")).lower() in ("on", "true", "1", "escalate") and \
            Path(str(c.get("llama_path") or "-")).exists() and Path(str(c.get("ai_model_path") or "-")).exists()

    def cmd(self):
        if not self.enabled():
            return None
        c = self.sup.cfg
        threads = max(2, min(8, (os.cpu_count() or 4) // 2))
        return ([c["llama_path"], "-m", c["ai_model_path"], "--host", "127.0.0.1", "--port", int(c.get("ai_port", 8766)),
                 "-c", 4096, "-t", threads, "-ngl", 0, "--parallel", 1],
                LOGS / "llama.log", LOGS / "llama.err.log")

    def healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{int(self.sup.cfg.get('ai_port', 8766))}/health",
                                        timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    def check(self) -> None:
        if not self.enabled():
            self.status = "off"
            return
        if self.alive():
            if self.healthy():
                self.status = "ok"
                if time.time() - self.started > 300:
                    self.fails = 0
            elif time.time() - self.started > 180:  # loading a 2 GB model on a slow disk takes a while
                self.failed("health check")
            return
        if self.proc is not None:
            if self.fails >= 2:
                self.sup.event("llm_down", "ИИ-проверка команд не запускается - работают только правила")
            self.failed(f"exit code {self.proc.poll()}")
        if self.due():
            self.start()


# ------------------------------------------------------------- supervisor ----
class Supervisor:
    def __init__(self):
        LOGS.mkdir(parents=True, exist_ok=True)
        self.log = Log(LOGS / "supervisor.log")
        self.cfg = load_cfg()
        self.server = Server(self, "server", "Сервер")
        self.ngrok = Ngrok(self, "ngrok", "Туннель")
        self.llm = Llama(self, "llm", "ИИ-проверка")
        self.children = (self.server, self.ngrok, self.llm)
        self.quit = threading.Event()
        self.icon = None
        self._notes: dict = {}
        self._note_t = 0.0
        self.panel = None
        self._phase = False
        self.version = "?"
        try:
            self.version = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
        except OSError:
            pass

    # -- notifications ------------------------------------------------------
    def notify(self, text: str, title: str = "PC Bridge", buttons=(), launch: str = "", force: bool = False) -> None:
        """buttons = [("Show", url), ...] -> a Windows toast with buttons (falls back to a plain notification)."""
        if NOTIFY_OFF.exists() or self.cfg.get("notifications", True) is False or self.icon is None or not text:
            return
        now = time.time()
        if not force and (now - self._notes.get(text, 0) < 60 or now - self._note_t < 4):  # no spam
            return
        self._notes[text] = self._note_t = now
        if buttons and IS_WIN and self.panel and self.cfg.get("toast_buttons", True) is not False:
            threading.Thread(target=self.toast, args=(text, title, list(buttons), launch), daemon=True).start()
            return
        self.plain_notify(text, title)

    def plain_notify(self, text: str, title: str) -> None:
        try:
            self.icon.notify(text[:250], title)
        except Exception as e:
            self.log("NOTIFY failed", e)

    def toast(self, text: str, title: str, buttons: list, launch: str = "") -> None:
        xml = self.panel_mod.toast_xml(title, text, buttons, launch or (buttons[0][1] if buttons else ""))
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                "-WindowStyle", "Hidden", "-Command", TOAST_PS],
                               env=dict(os.environ, PCB_TOAST=xml, PCB_AUMID=TOAST_AUMID), creationflags=NO_WINDOW,
                               capture_output=True, timeout=25)
            if r.returncode == 0:
                return
            self.log("TOAST failed", r.returncode, r.stderr.decode("utf-8", "replace")[-300:])
        except Exception as e:
            self.log("TOAST failed", repr(e))
        self.plain_notify(text, title)

    def event(self, kind: str, text: str) -> None:
        self.log("EVENT", kind, text)
        self.notify(text)

    def url_changed(self, old: str, new: str) -> None:
        url = mcp_url(new, self.cfg)
        try:
            (ROOT / "URL.txt").write_text(url + "\n", encoding="utf-8")
        except OSError:
            pass
        if old:
            self.event("url_changed", "Адрес PC Bridge изменился - обновите его в настройках агента Notion "
                                      "(меню значка: Скопировать адрес)")

    # -- files for setup.ps1 / STOP.bat -------------------------------------
    def save_pids(self) -> None:
        d = {"supervisor": os.getpid(), "supervisor_name": Path(sys.executable).stem}
        for c in self.children:
            if c.alive():
                d[c.key] = c.proc.pid
                d[c.key + "_name"] = {"server": "python", "ngrok": "ngrok", "llm": "llama-server"}[c.key]
        names = {"server": "python", "ngrok": "ngrok", "llm": "llama-server"}
        for k, v in names.items():
            if k in d:
                d[k + "_name"] = v
        try:
            write_json(PIDS, d)
        except OSError:
            pass

    def save_state(self) -> None:
        s = {"supervisor_pid": os.getpid(), "version": self.version, "url": self.ngrok.url,
             "mcp_url": mcp_url(self.ngrok.url, self.cfg) if self.ngrok.url else "",
             "server": self.server.status, "ngrok": self.ngrok.status, "ngrok_error": self.ngrok.error,
             "llm": self.llm.status, "paused": PAUSE_FLAG.exists(),
             "read_only": RO_FLAG.exists() or bool(self.cfg.get("read_only")),
             "restarts": {c.key: c.restarts for c in self.children}, "updated": time.time()}
        try:
            write_json(STATE, s)
        except OSError:
            pass

    # -- main loop ----------------------------------------------------------
    def loop(self) -> None:
        last = time.time()
        while not self.quit.is_set():
            if STOP_FLAG.exists():
                self.log("stop.flag found")
                break
            now = time.time()
            if now - last > 60:  # the PC slept: check the tunnel right away
                self.log("WAKE after", int(now - last), "s")
                self.ngrok.last_check = 0
            last = now
            for c in self.children:
                try:
                    c.check()
                except Exception as e:
                    self.log("CHECK error", c.key, repr(e))
                    c.next_try = time.time() + 30
            self.save_state()
            self.update_icon()
            self.quit.wait(2)
        self.shutdown()

    def shutdown(self) -> None:
        if getattr(self, "_down", False):
            return
        self._down = True
        self.quit.set()
        for c in self.children:
            c.stop()
        for f in (PIDS, STATE, STOP_FLAG):
            try:
                f.unlink()
            except OSError:
                pass
        self.log("EXIT")
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass

    # -- events from server.py -> notifications ------------------------------
    def watch_events(self) -> None:
        pos = EVENTS.stat().st_size if EVENTS.exists() else 0
        while not self.quit.wait(1.5):
            try:
                size = EVENTS.stat().st_size if EVENTS.exists() else 0
                if size < pos:
                    pos = 0
                if size == pos:
                    continue
                with open(EVENTS, "rb") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                pos = size
                for line in chunk.decode("utf-8", "replace").splitlines():
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    btn = [("Открыть ленту", self.panel.url(tab="feed"))] if self.panel and e.get("kind") == "denied" else []
                    self.notify(str(e.get("text", "")), buttons=btn)
            except OSError:
                pass

    def watch_changes(self, quiet: float = 8.0, max_wait: float = 45.0) -> None:
        """File changes of the agent -> one notification per batch: "Changed 3 files" [Show] [Undo]."""
        index = ROOT / "history" / "index.jsonl"
        seen = {e.get("id") for e in self.panel_mod.load_history(ROOT)} if self.panel_mod else set()
        pend, first, last, size = [], 0.0, 0.0, -1
        while not self.quit.wait(2):
            try:
                s = index.stat().st_size if index.exists() else 0
                if s != size:
                    size = s
                    for e in self.panel_mod.load_history(ROOT)[-200:]:
                        cid = e.get("id")
                        if cid and cid not in seen:
                            seen.add(cid)
                            if e.get("op") != "undo":
                                pend.append(e)
                                last = time.time()
                                first = first or last
                now = time.time()
                if pend and (now - last >= quiet or now - first >= max_wait):
                    self.changes_notice(pend)
                    pend, first = [], 0.0
            except Exception as ex:
                self.log("watch_changes", repr(ex))

    def changes_notice(self, entries: list) -> None:
        names = list(dict.fromkeys(Path(s.get("path", "")).name for e in entries for s in e.get("snaps", [])))
        n = len(names)
        if not n:
            return
        ids = ",".join(e["id"] for e in entries)[:1500]
        text = f"Агент изменил {n} {plural(n, 'файл', 'файла', 'файлов')}\n" + ", ".join(names[:4]) + (" ..." if n > 4 else "")
        self.log("CHANGES", n, ids[:200])
        self.notify(text, buttons=[("Показать", self.panel.url(show=ids)), ("Отменить", self.panel.url(undo=ids))],
                    force=True)

    # -- tray ----------------------------------------------------------------
    def status_text(self) -> str:
        if PAUSE_FLAG.exists():
            return "ПАУЗА - агент получает отказ"
        st = {"ok": "работает", "starting": "запускается", "restarting": "перезапуск", "port busy": "порт занят",
              "error": "ошибка", "off": "выключен"}
        t = f"Сервер: {st.get(self.server.status, self.server.status)}, туннель: " \
            f"{st.get(self.ngrok.status, self.ngrok.status)}"
        if self.ngrok.error:
            t += f" ({self.ngrok.error})"
        return t

    def activity_text(self) -> str:
        try:
            a = json.loads(ACTIVITY.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "Агент: ещё ничего не делал"
        ago = int(time.time() - a.get("t", 0))
        when = f"{ago} с назад" if ago < 120 else f"{ago // 60} мин назад"
        return f"Агент: {a.get('tool', '')} {a.get('detail', '')}"[:90] + f" ({when})"

    GREEN, YELLOW, RED, GREY = (30, 170, 80), (230, 170, 0), (210, 50, 50), (150, 150, 150)

    def color(self):
        """green = works, yellow = pause or read only, red = error, grey = starting."""
        if PAUSE_FLAG.exists():
            return self.YELLOW
        if self.server.status == "ok" and self.ngrok.status == "ok":
            return self.YELLOW if (RO_FLAG.exists() or self.cfg.get("read_only")) else self.GREEN
        if self.ngrok.status in ("error", "restarting") or self.server.status in ("port busy", "error", "restarting"):
            return self.RED
        return self.GREY

    def busy(self) -> bool:
        """The agent is doing something right now (a tool runs or was called a moment ago)."""
        try:
            a = json.loads(ACTIVITY.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        now = time.time()
        running = int(a.get("running", 0) or 0) > 0 and now - float(a.get("busy_t", 0) or 0) < 1800
        return running or now - float(a.get("t", 0) or 0) < 3

    def animate(self) -> None:
        while not self.quit.wait(0.5):
            busy = self.busy()
            self._phase = (not self._phase) if busy else False
            self.update_icon(menu=False)

    def image(self, rgb, lit: bool = False, glyph: str = ""):
        """glyph: "pause" = two bars, "lock" = read only, "" = nothing. lit = the bright phase of blinking."""
        from PIL import Image, ImageDraw

        if lit:
            rgb = tuple(min(255, int(c + (255 - c) * 0.45)) for c in rgb)
        W = (255, 255, 255, 255)
        im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle((2, 2, 62, 62), 14, fill=tuple(rgb) + (255,))
        d.rectangle((14, 18, 50, 40), outline=W, width=4)  # a little monitor
        d.rectangle((26, 44, 38, 48), fill=W)
        if glyph == "pause":
            d.rectangle((25, 23, 29, 35), fill=W)
            d.rectangle((35, 23, 39, 35), fill=W)
        elif glyph == "lock":
            d.rectangle((26, 28, 38, 36), fill=W)
            d.arc((27, 20, 37, 32), 180, 360, fill=W, width=3)
        if lit:
            d.ellipse((44, 4, 60, 20), fill=W)  # "working" dot
            d.ellipse((48, 8, 56, 16), fill=tuple(rgb) + (255,))
        return im

    def glyph(self) -> str:
        if PAUSE_FLAG.exists():
            return "pause"
        return "lock" if (RO_FLAG.exists() or self.cfg.get("read_only")) else ""

    def update_icon(self, menu: bool = True) -> None:
        if self.icon is None:
            return
        key = (self.color(), self.glyph(), bool(self._phase))
        if key != getattr(self, "_icon_key", None):
            self._icon_key = key
            try:
                self.icon.icon = self.image(key[0], lit=key[2], glyph=key[1])
            except Exception:
                pass
        if not menu:
            return
        tip = f"PC Bridge {self.version}\n{self.status_text()}\n{self.activity_text()}"[:127]
        if tip != getattr(self, "_tip", None):
            self._tip = tip
            try:
                self.icon.title = tip
                self.icon.update_menu()
            except Exception:
                pass

    def toggle(self, flag_file: Path, on_text: str, off_text: str) -> None:
        if flag_file.exists():
            flag_file.unlink()
            text = off_text
        else:
            flag_file.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            text = on_text
        self.log("TOGGLE", flag_file.name, flag_file.exists())
        self._notes.pop(text, None)
        self._note_t = 0
        if flag_file is not NOTIFY_OFF:
            self.notify(text)
        self.update_icon()

    def toggle_pause(self, *_):
        self.toggle(PAUSE_FLAG, "Пауза: агент ничего не может делать на ПК", "Пауза снята - агент снова работает")

    def copy(self, text: str) -> None:
        if not text:
            return
        subprocess.run(["clip"], input=text.encode("utf-16-le"), creationflags=NO_WINDOW) if IS_WIN else None

    MODULE_NAMES = (("enable_internet", "Интернет"), ("enable_blender", "Blender"), ("enable_roblox", "Roblox Studio"),
                    ("enable_unreal", "Unreal Engine 5"), ("enable_screen", "Скриншоты"),
                    ("enable_input", "Мышь и клавиатура"), ("enable_desktop", "Буфер, файлы, процессы"),
                    ("enable_shell", "Командная строка"), ("full_disk", "Весь диск"))

    def headline(self) -> str:
        """One short line for the top of the tray menu."""
        if PAUSE_FLAG.exists():
            return "⏸  Пауза - агент ничего не может делать"
        if self.server.status == "ok" and self.ngrok.status == "ok":
            ro = "  (только чтение)" if RO_FLAG.exists() or self.cfg.get("read_only") else ""
            return f"✔  PC Bridge {self.version} работает{ro}"
        return "✖  " + self.status_text()

    @staticmethod
    def startup_lnk() -> Path:
        return Path(os.environ.get("APPDATA", str(ROOT))) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / \
            "Startup" / "PC Bridge.lnk"

    def set_cfg(self, key: str, value) -> None:
        """Change one key in config.json (and in memory) without touching the rest."""
        try:
            data = load_cfg()
            data[key] = value
            write_json(CFG_PATH, data)
            self.cfg[key] = value
        except (OSError, ValueError) as e:
            self.log("CFG write failed", key, repr(e))

    def toggle_autostart(self, *_):
        lnk = self.startup_lnk()
        if lnk.exists():
            try:
                lnk.unlink()
            except OSError as e:
                self.log("autostart off failed", repr(e))
            self.set_cfg("autostart", False)
            self.notify("Автозапуск выключен")
        elif IS_WIN:
            pyw = Path(sys.executable)
            if pyw.name.lower() == "python.exe" and (pyw.parent / "pythonw.exe").exists():
                pyw = pyw.parent / "pythonw.exe"
            sup = ROOT / "server" / "supervisor.py"
            ps = ("$s=New-Object -ComObject WScript.Shell;$l=$s.CreateShortcut($env:PCB_LNK);"
                  "$l.TargetPath=$env:PCB_PY;$l.Arguments='\"'+$env:PCB_SUP+'\"';"
                  "$l.WorkingDirectory=$env:PCB_ROOT;$l.Description='PC Bridge';$l.Save()")
            env = dict(os.environ, PCB_LNK=str(lnk), PCB_PY=str(pyw), PCB_SUP=str(sup), PCB_ROOT=str(ROOT))
            r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], env=env,
                               creationflags=NO_WINDOW, capture_output=True, timeout=30)
            ok = lnk.exists()
            self.log("autostart on", ok, r.returncode)
            if ok:
                self.set_cfg("autostart", True)
            self.notify("Автозапуск включён" if ok else "Не удалось включить автозапуск")
        self.update_icon()

    def restart_bridge(self, *_):
        """Restart the server, tunnel and AI check; the main loop starts them again."""
        self.log("RESTART from the tray")
        for c in self.children:
            c.stop()
            c.fails = 0
            c.next_try = 0.0
        self.notify("Перезапускаю мост...")
        self.update_icon()

    def open_settings(self, *_):
        """SETTINGS.bat stops this bridge itself and starts it again with the new settings."""
        if not IS_WIN:
            return
        try:
            subprocess.Popen(["cmd", "/c", str(ROOT / "SETTINGS.bat")], cwd=str(ROOT), creationflags=0x00000010)
        except OSError as e:
            self.log("settings failed", repr(e))

    def open_path(self, path) -> None:
        if IS_WIN:
            try:
                os.startfile(str(path))
            except OSError as e:
                self.log("open failed", path, repr(e))

    def notion_url(self) -> str:
        return mcp_url(self.ngrok.url, self.cfg) if self.ngrok.url else ""

    def copy_both(self, *_):
        url = self.notion_url()
        if url:
            self.copy(f"Адрес: {url}\r\nТокен: {self.cfg.get('secret', '')}")

    def menu(self):
        import pystray

        I, M, SEP = pystray.MenuItem, pystray.Menu, pystray.Menu.SEPARATOR
        token = self.cfg.get("token_auth", True) is not False
        has_url = lambda _: bool(self.ngrok.url)  # noqa: E731
        ro_fixed = bool(self.cfg.get("read_only"))
        modules = [I(name, None, checked=(lambda k: lambda _: bool(self.cfg.get(k)))(key), enabled=False)
                   for key, name in self.MODULE_NAMES]
        return M(
            I(lambda _: self.headline(), None, enabled=False),
            I(lambda _: self.activity_text(), None, enabled=False),
            SEP,
            I("Панель управления", lambda *_: self.open_panel(), default=True, visible=bool(getattr(self, "panel", None))),
            I("Консоль: что делает агент", lambda *_: self.open_console(), default=not getattr(self, "panel", None)),
            I("Проверить подключение", lambda *_: self.open_panel(check=1), visible=bool(getattr(self, "panel", None))),
            I("Подключение к Notion", M(
                I("Скопировать адрес", lambda *_: self.copy(self.notion_url()), enabled=has_url),
                I("Скопировать токен", lambda *_: self.copy(str(self.cfg.get("secret", ""))), visible=token),
                I("Скопировать адрес и токен", self.copy_both, enabled=has_url, visible=token),
            )),
            SEP,
            I("Пауза   Ctrl+Alt+Shift+P", self.toggle_pause, checked=lambda _: PAUSE_FLAG.exists()),
            I("Только чтение" + (" (задано в настройках)" if ro_fixed else ""),
              lambda *_: self.toggle(RO_FLAG, "Режим «только чтение»: агент ничего не меняет",
                                     "Режим «только чтение» выключен"),
              checked=lambda _: RO_FLAG.exists() or bool(self.cfg.get("read_only")), enabled=not ro_fixed),
            SEP,
            I("Настройки", M(
                I("Уведомления Windows", lambda *_: self.toggle(NOTIFY_OFF, "", ""),
                  checked=lambda _: not NOTIFY_OFF.exists()),
                I("Автозапуск с Windows", self.toggle_autostart, checked=lambda _: self.startup_lnk().exists()),
                SEP,
                I("Модули (включены галочкой)", M(*modules)),
                I("Изменить модули и все настройки...", self.open_settings),
            )),
            I("Открыть", M(
                I("Рабочую папку", lambda *_: self.open_path(self.cfg.get("workspace") or ROOT)),
                I("Папку логов", lambda *_: self.open_path(LOGS)),
                I("Папку PC Bridge", lambda *_: self.open_path(ROOT)),
                I("Инструкцию (README)", lambda *_: self.open_path(ROOT / "README.txt")),
            )),
            SEP,
            I("Перезапустить мост", self.restart_bridge),
            I("Остановить всё   Ctrl+Alt+Shift+S", lambda *_: self.stop_all()),
        )

    def open_panel(self, **params) -> None:
        if not getattr(self, "panel", None):
            return self.open_console()
        import webbrowser

        try:
            webbrowser.open(self.panel.url(**params))
        except Exception as e:
            self.log("panel open failed", repr(e))

    def start_panel(self) -> None:
        try:
            self.panel_mod = load_panel()
            mod = types.SimpleNamespace(LOGS=LOGS, ROOT=ROOT, ACTIVITY=ACTIVITY, PAUSE_FLAG=PAUSE_FLAG, RO_FLAG=RO_FLAG,
                                        NOTIFY_OFF=NOTIFY_OFF, ctrl_key=ctrl_key)
            p = self.panel_mod.Panel(self, mod)
            if p.start():
                self.panel = p
                self.log("PANEL", f"http://127.0.0.1:{p.port}")
            else:
                self.log("PANEL: no free port")
        except Exception as e:
            self.log("PANEL failed", repr(e))

    def open_console(self) -> None:
        """A console window with the live feed of the agent's actions (server/console.py)."""
        py = Path(sys.executable)
        if py.name.lower() == "pythonw.exe" and (py.parent / "python.exe").exists():
            py = py.parent / "python.exe"
        try:
            subprocess.Popen([str(py), str(ROOT / "server" / "console.py")], cwd=str(ROOT),
                             creationflags=0x00000010 if IS_WIN else 0)  # CREATE_NEW_CONSOLE
        except OSError as e:
            self.log(f"console failed: {e}")

    def stop_all(self) -> None:
        self.log("STOP ALL from the tray / hotkey")
        self.quit.set()

    def hotkeys(self) -> None:
        if not IS_WIN:
            return
        import ctypes
        from ctypes import wintypes

        u = ctypes.WinDLL("user32", use_last_error=True)
        self._hk_tid = ctypes.WinDLL("kernel32").GetCurrentThreadId()
        ok_p = u.RegisterHotKey(None, 1, HOTKEY_MODS, 0x50)  # P
        ok_s = u.RegisterHotKey(None, 2, HOTKEY_MODS, 0x53)  # S
        self.log("HOTKEYS", bool(ok_p), bool(ok_s))
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                if msg.wParam == 1:
                    self.toggle_pause()
                elif msg.wParam == 2:
                    self.stop_all()
        u.UnregisterHotKey(None, 1)
        u.UnregisterHotKey(None, 2)

    def run(self) -> None:
        self.log("SUPERVISOR START", self.version, "pid", os.getpid())
        try:
            STOP_FLAG.unlink()
        except OSError:
            pass
        self.save_pids()
        self.panel_mod = None
        self.start_panel()
        threading.Thread(target=self.watch_events, daemon=True).start()
        threading.Thread(target=self.hotkeys, daemon=True).start()
        threading.Thread(target=self.animate, daemon=True).start()
        if self.panel_mod:
            threading.Thread(target=self.watch_changes, daemon=True).start()
        worker = threading.Thread(target=self.loop, daemon=True)
        try:
            import pystray

            self.icon = pystray.Icon("PCBridge", self.image(self.color(), glyph=self.glyph()), f"PC Bridge {self.version}",
                                     self.menu())
        except Exception as e:  # no tray (pystray missing): still supervise
            self.log("TRAY unavailable:", repr(e))
            self.icon = None
        worker.start()
        if self.icon is not None:
            def watcher():
                self.quit.wait()
                worker.join(30)
                try:
                    self.icon.stop()
                except Exception:
                    pass

            threading.Thread(target=watcher, daemon=True).start()
            self.icon.run()
        self.quit.set()
        worker.join(30)
        self.shutdown()
        if IS_WIN and getattr(self, "_hk_tid", None):
            import ctypes

            ctypes.WinDLL("user32").PostThreadMessageW(self._hk_tid, 0x0012, 0, 0)  # WM_QUIT


def single_instance() -> bool:
    if not IS_WIN:
        return True
    import ctypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    globals()["_MUTEX"] = k.CreateMutexW(None, False, "Local\\PCBridgeSupervisor")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


if __name__ == "__main__":
    if not single_instance():
        sys.exit(0)
    Supervisor().run()
