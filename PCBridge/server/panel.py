"""PC Bridge web panel: http://127.0.0.1:8767 (started by supervisor.py, local only - ngrok never forwards it).

Status, connection check, switches, live feed of the agent's actions, the list of file changes with a
side-by-side diff and an Undo button. Protection: listens on 127.0.0.1 only, checks the Host header (DNS rebinding),
the page needs a random key from the tray link (new each start), API calls need it in a header (a foreign web page
cannot send it), GET requests never change anything.
"""
import difflib
import hmac
import json
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

MAX_TEXT = 400_000
FEED_START = 300_000
MODULE_KEYS = ("enable_internet", "enable_blender", "enable_roblox", "enable_unreal", "enable_screen",
               "enable_input", "enable_desktop", "enable_shell", "full_disk")
RISKY_KEYS = ("enable_shell", "full_disk", "enable_input")


# ------------------------------------------------------------------ helpers ----
def ago(t: float, now: float = None) -> str:
    if not t:
        return "ещё не подключался"
    d = max(0, int((now or time.time()) - t))
    if d < 60:
        return f"{d} с назад"
    if d < 3600:
        return f"{d // 60} мин назад"
    if d < 86400:
        return f"{d // 3600} ч {d % 3600 // 60} мин назад"
    return time.strftime("%d.%m %H:%M", time.localtime(t))


def read_json(p: Path, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {} if default is None else default


def load_history(root: Path) -> list:
    out = []
    try:
        for line in (Path(root) / "history" / "index.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def _text(data):
    if data is None:
        return None
    if len(data) > MAX_TEXT or b"\x00" in data[:8192]:
        return False
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return False


def diff_rows(a: str, b: str, ctx: int = 3) -> list:
    """Rows for a side-by-side diff: {"t": eq|del|add|chg|skip, "l", "ln", "r", "rn"}."""
    al, bl = a.splitlines(), b.splitlines()
    rows = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, al, bl, autojunk=False).get_opcodes():
        if op == "equal":
            n = i2 - i1
            keep = list(range(n)) if n <= ctx * 2 + 1 else list(range(ctx)) + [-1] + list(range(n - ctx, n))
            if not rows:  # start of the file: no leading context
                keep = [k for k in keep if k == -1 or k >= n - ctx] if n > ctx else keep
                if keep and keep[0] != -1 and n > ctx:
                    keep = [-1] + keep
            for k in keep:
                if k == -1:
                    rows.append({"t": "skip"})
                else:
                    rows.append({"t": "eq", "l": al[i1 + k], "ln": i1 + k + 1, "r": bl[j1 + k], "rn": j1 + k + 1})
        else:
            for k in range(max(i2 - i1, j2 - j1)):
                li, rj = i1 + k, j1 + k
                row = {"t": "chg" if op == "replace" else ("del" if op == "delete" else "add")}
                if li < i2:
                    row.update(l=al[li], ln=li + 1)
                if rj < j2:
                    row.update(r=bl[rj], rn=rj + 1)
                if op == "replace" and (li >= i2 or rj >= j2):
                    row["t"] = "add" if li >= i2 else "del"
                rows.append(row)
    while rows and rows[-1]["t"] == "skip":
        rows.pop()
    if rows and all(r["t"] in ("eq", "skip") for r in rows):
        return []
    return rows[:4000]


def change_files(root: Path, entries: list, cid: str) -> list:
    """What one change did to each file: before = its backup, after = the next backup of the file or the file now."""
    blobs = Path(root) / "history" / "blobs"
    idx = next((i for i, e in enumerate(entries) if e.get("id") == cid), None)
    if idx is None:
        return []
    out = []
    for s in entries[idx].get("snaps", []):
        p = s.get("path", "")
        st = s.get("state")
        item = {"path": p, "name": Path(p).name}
        if st not in ("file", "absent"):
            item["note"] = {"dir": "папка", "tree": "папка с файлами", "too_big": "большой файл - без показа"}.get(st, st)
            out.append(item)
            continue
        try:
            before = (blobs / s["blob"]).read_bytes() if st == "file" else b""
        except (OSError, KeyError):
            before = None
        after, after_note = None, ""
        nxt = next((x for e in entries[idx + 1:] for x in e.get("snaps", []) if x.get("path") == p), None)
        if nxt is not None:
            if nxt.get("state") == "file":
                try:
                    after = (blobs / nxt["blob"]).read_bytes()
                except (OSError, KeyError):
                    after_note = "копия удалена"
            elif nxt.get("state") != "absent":
                after_note = "потом стало папкой"
            item["later"] = True
        else:
            f = Path(p)
            if f.is_file():
                try:
                    after = f.read_bytes() if f.stat().st_size <= MAX_TEXT else b"\x00"
                except OSError:
                    after_note = "нет доступа"
            elif f.is_dir():
                after_note = "теперь папка"
        item["kind"] = "created" if st == "absent" else ("deleted" if after is None and not after_note else "changed")
        a, b = _text(before), _text(after if after is not None else b"")
        if after_note:
            item["note"] = after_note
        elif a is False or b is False:
            item["note"] = "двоичный или большой файл - без показа"
        elif a is None:
            item["note"] = "резервная копия уже удалена"
        else:
            item["rows"] = diff_rows(a, b or "")
            item["added"] = sum(1 for r in item["rows"] if r["t"] in ("add", "chg"))
            item["removed"] = sum(1 for r in item["rows"] if r["t"] in ("del", "chg"))
        out.append(item)
    return out


def change_summary(e: dict) -> dict:
    return {"id": e.get("id"), "time": e.get("time", 0), "op": e.get("op", ""), "undone": bool(e.get("undone")),
            "is_undo": e.get("op") == "undo", "undoes": e.get("undoes", ""),
            "paths": [s.get("path", "") for s in e.get("snaps", [])][:50]}


def read_feed(path: Path, pos: int = -1) -> dict:
    """New text of actions.log since `pos` (bytes); pos < 0 = the last ~300 KB."""
    try:
        size = Path(path).stat().st_size
    except OSError:
        return {"text": "", "pos": 0, "reset": pos > 0}
    reset = pos < 0 or pos > size
    start = max(0, size - FEED_START) if reset else pos
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(min(size - start, 2_000_000))
    if reset and start > 0:
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl >= 0 else data
    return {"text": data.decode("utf-8", errors="replace"), "pos": start + len(data) if not reset else size,
            "reset": reset}


def toast_xml(title: str, text: str, actions=(), launch: str = "") -> str:
    """Windows toast with buttons; every button opens a link (the panel)."""
    q = lambda s: xml_escape(str(s), {'"': "&quot;"})  # noqa: E731
    head = f'<toast activationType="protocol" launch="{q(launch)}">' if launch else "<toast>"
    lines = "".join(f"<text>{q(x)}</text>" for x in [title] + str(text).split("\n")[:2])
    acts = "".join(f'<action content="{q(c)}" activationType="protocol" arguments="{q(u)}"/>' for c, u in actions)
    return (head + f'<visual><binding template="ToastGeneric">{lines}</binding></visual>'
            + (f"<actions>{acts}</actions>" if acts else "") + "</toast>")


def _http(url: str, method: str = "GET", headers=None, body: bytes = None, timeout: float = 8):
    """(status, headers) - never raises."""
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
    req.add_header("User-Agent", "PCBridge-check")
    req.add_header("ngrok-skip-browser-warning", "1")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {})
    except Exception as e:  # DNS, timeout, TLS...
        return 0, {"error": str(e)}


def check_connection(cfg: dict, ngrok_url: str, ngrok_error: str = "", last_call: float = 0, http=_http,
                     port_open=None) -> list:
    """Step by step: server -> tunnel -> address from the internet -> token. Each step says what to do if it fails."""
    port = int(cfg.get("port", 8765))
    token_auth = cfg.get("token_auth", True) is not False
    secret = str(cfg.get("secret", ""))
    steps = []

    def step(name, ok, detail, fix=""):
        steps.append({"name": name, "ok": ok, "detail": detail, "fix": "" if ok else fix})
        return ok

    if port_open is None:
        def port_open(p):
            try:
                with socket.create_connection(("127.0.0.1", p), timeout=1.5):
                    return True
            except OSError:
                return False
    if not step("Сервер на этом ПК", port_open(port), f"порт {port}",
                "Сервер не отвечает. Нажмите «Перезапустить мост»; если не помогло - откройте logs\\server.log."):
        return steps
    if not step("Туннель ngrok", bool(ngrok_url), ngrok_url or (ngrok_error or "нет адреса"),
                "Туннель не запущен. Проверьте интернет и authtoken ngrok (SETTINGS.bat), потом «Перезапустить мост»."):
        return steps
    base = ngrok_url.rstrip("/")
    code, hdr = http(base + "/.well-known/pcbridge-ping")
    low = {k.lower(): v for k, v in hdr.items()}
    reach = code != 0 and "ngrok-error-code" not in low
    detail = (f"ответ {code}" if code else low.get("error", "нет ответа")) + \
        (f", ошибка ngrok {low['ngrok-error-code']}" if "ngrok-error-code" in low else "")
    if not step("Адрес доступен из интернета", reach, detail,
                "Адрес не открывается снаружи. Чаще всего это интернет, VPN/антивирус или туннель только что "
                "перезапустился - подождите минуту и проверьте снова."):
        return steps
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "pcbridge-check", "version": "1"}}}
    ).encode()
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token_auth:
        no_tok, _ = http(base + "/mcp", "POST", h, init)
        tok, _ = http(base + "/mcp", "POST", dict(h, Authorization="Bearer " + secret), init)
        ok = no_tok == 401 and 200 <= tok < 300
        step("Токен для Notion", ok, f"без токена: {no_tok or 'нет ответа'}, с токеном: {tok or 'нет ответа'}",
             "Сервер не принимает свой же токен. Нажмите «Перезапустить мост»; если не помогло - запустите "
             "SETTINGS.bat и создайте новый токен.")
    else:
        code, _ = http(base + f"/{secret}/mcp", "POST", h, init)
        step("Секретный адрес", 200 <= code < 300, f"ответ {code or 'нет'}",
             "Сервер не отвечает по секретному адресу. Запустите SETTINGS.bat.")
    if steps[-1]["ok"]:
        recent = last_call and time.time() - last_call < 3600
        step("Notion-агент", True, "подключался " + ago(last_call) if last_call else "ещё не подключался")
        if not recent:
            steps[-1]["fix"] = ("Мост работает. Если агент в Notion видит ошибку - скопируйте здесь адрес и токен заново "
                                "и вставьте в настройки MCP-подключения (адрес мог смениться после перезапуска).")
    return steps


# ------------------------------------------------------------------ server ----
class Panel:
    def __init__(self, sup, mod):
        """sup = the Supervisor, mod = the supervisor module (paths and flags)."""
        self.sup, self.m = sup, mod
        self.key = secrets.token_urlsafe(24)
        self.port = 0
        self.httpd = None
        self.check_result = {"t": 0, "steps": [], "running": False}

    def url(self, **params) -> str:
        q = urllib.parse.urlencode(dict(k=self.key, **params))
        return f"http://127.0.0.1:{self.port}/?{q}" if self.port else ""

    def start(self):
        first = int(self.sup.cfg.get("panel_port", int(self.sup.cfg.get("port", 8765)) + 2))
        handler = type("H", (Handler,), {"panel": self})
        for port in range(first, first + 10):
            try:
                self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
                self.port = port
                break
            except OSError:
                continue
        if not self.httpd:
            return None
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="panel").start()
        try:
            (self.m.LOGS / "panel_url.txt").write_text(self.url(), encoding="utf-8")
        except OSError:
            pass
        return self.port

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()

    # ---- data ----
    def state(self) -> dict:
        s, m = self.sup, self.m
        act = read_json(m.ACTIVITY)
        ro_cfg = bool(s.cfg.get("read_only"))
        try:
            color = s.color()
        except Exception:
            color = (150, 150, 150)
        return {
            "version": s.version, "headline": s.headline(), "status": s.status_text(), "activity": s.activity_text(),
            "color": "#%02x%02x%02x" % tuple(color),
            "server": s.server.status, "ngrok": s.ngrok.status, "ngrok_error": getattr(s.ngrok, "error", ""),
            "url": s.notion_url(), "token_auth": s.cfg.get("token_auth", True) is not False,
            "token": str(s.cfg.get("secret", "")),
            "last_call": act.get("t", 0), "last_call_text": ago(act.get("t", 0)),
            "running": int(act.get("running", 0) or 0), "last_tool": act.get("tool", ""),
            "paused": m.PAUSE_FLAG.exists(), "read_only": m.RO_FLAG.exists() or ro_cfg, "read_only_fixed": ro_cfg,
            "notifications": not m.NOTIFY_OFF.exists(), "autostart": s.startup_lnk().exists(),
            "modules": [{"key": k, "name": n, "on": bool(s.cfg.get(k)), "risky": k in RISKY_KEYS}
                        for k, n in s.MODULE_NAMES],
            "workspace": str(s.cfg.get("workspace") or m.ROOT), "check": self.check_result,
        }

    def run_check(self) -> dict:
        if self.check_result.get("running"):
            return self.check_result
        self.check_result = dict(self.check_result, running=True)
        try:
            steps = check_connection(self.sup.cfg, self.sup.ngrok.url, getattr(self.sup.ngrok, "error", ""),
                                     read_json(self.m.ACTIVITY).get("t", 0))
        except Exception as e:
            steps = [{"name": "Проверка", "ok": False, "detail": str(e), "fix": "Попробуйте ещё раз."}]
        self.check_result = {"t": time.time(), "steps": steps, "running": False,
                             "ok": all(x["ok"] for x in steps)}
        return self.check_result

    def undo(self, ids: list) -> dict:
        cfg = self.sup.cfg
        port = int(cfg.get("ctrl_port", int(cfg.get("port", 8765)) + 3))
        key = self.m.ctrl_key(str(cfg.get("secret", "")))
        req = urllib.request.Request(f"http://127.0.0.1:{port}/undo", method="POST",
                                     data=json.dumps({"ids": ids}).encode(),
                                     headers={"X-PCB-Key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"error": f"сервер ответил {e.code}"}
        except Exception as e:
            return {"error": "сервер не запущен - отмена невозможна (" + str(e) + ")"}

    def action(self, what: str, data: dict) -> dict:
        s, m = self.sup, self.m
        if what == "pause":
            s.toggle_pause()
        elif what == "readonly":
            if not s.cfg.get("read_only"):
                s.toggle(m.RO_FLAG, "Режим «только чтение»: агент ничего не меняет", "Режим «только чтение» выключен")
        elif what == "notifications":
            s.toggle(m.NOTIFY_OFF, "", "")
        elif what == "autostart":
            s.toggle_autostart()
        elif what == "restart":
            s.restart_bridge()
        elif what == "module":
            key = data.get("key")
            if key not in MODULE_KEYS:
                return {"error": "нет такого модуля"}
            s.set_cfg(key, bool(data.get("on")))
            s.log("PANEL module", key, bool(data.get("on")))
            s.restart_bridge()
        elif what == "open":
            target = {"workspace": s.cfg.get("workspace") or m.ROOT, "logs": m.LOGS, "root": m.ROOT,
                      "readme": m.ROOT / "README.txt"}.get(data.get("target"))
            if target is None:
                return {"error": "нечего открывать"}
            s.open_path(target)
        elif what == "console":
            s.open_console()
        elif what == "settings":
            s.open_settings()
        elif what == "copy":
            s.copy(s.notion_url() if data.get("what") == "url" else str(s.cfg.get("secret", "")))
        elif what == "stop":
            threading.Timer(1.0, s.stop_all).start()
        else:
            return {"error": "неизвестное действие"}
        return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    panel: Panel = None
    server_version = "PCBridgePanel"

    def log_message(self, *a):
        pass

    def _send(self, code: int, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, (bytes, str)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; "
                                                   "style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        p = self.panel.port
        return self.headers.get("Host", "") in (f"127.0.0.1:{p}", f"localhost:{p}")

    def _key_ok(self, value: str) -> bool:
        return bool(value) and hmac.compare_digest(value, self.panel.key)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "host"})
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            if not self._key_ok((q.get("k") or [""])[0]):
                return self._send(403, NO_KEY_HTML, "text/html; charset=utf-8")
            return self._send(200, PAGE.replace("__KEY__", self.panel.key), "text/html; charset=utf-8")
        if not u.path.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        if not self._key_ok(self.headers.get("X-PCB-Panel", "")):
            return self._send(403, {"error": "key"})
        P, m = self.panel, self.panel.m
        if u.path == "/api/state":
            return self._send(200, P.state())
        if u.path == "/api/feed":
            try:
                pos = int((q.get("pos") or ["-1"])[0])
            except ValueError:
                pos = -1
            return self._send(200, read_feed(m.LOGS / "actions.log", pos))
        if u.path == "/api/changes":
            entries = load_history(m.ROOT)
            return self._send(200, {"changes": [change_summary(e) for e in reversed(entries[-300:])]})
        if u.path == "/api/diff":
            cid = (q.get("id") or [""])[0]
            return self._send(200, {"id": cid, "files": change_files(m.ROOT, load_history(m.ROOT), cid)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "host"})
        if not self._key_ok(self.headers.get("X-PCB-Panel", "")):
            return self._send(403, {"error": "key"})
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        P = self.panel
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/check":
            return self._send(200, P.run_check())
        if path == "/api/undo":
            ids = [str(i) for i in (data.get("ids") or [])][:200]
            return self._send(200, P.undo(ids) if ids else {"error": "ничего не выбрано"})
        if path == "/api/action":
            try:
                return self._send(200, P.action(str(data.get("do", "")), data))
            except Exception as e:
                return self._send(500, {"error": str(e)})
        return self._send(404, {"error": "not found"})


NO_KEY_HTML = """<!doctype html><meta charset="utf-8"><title>PC Bridge</title>
<body style="font:15px Segoe UI,sans-serif;padding:40px;color:#333"><h2>PC Bridge</h2>
<p>Откройте панель из меню значка в трее: «Панель управления». Ссылка защищена ключом и меняется
при каждом запуске моста.</p></body>"""

PAGE = (Path(__file__).with_name("panel.html")).read_text(encoding="utf-8") \
    if Path(__file__).with_name("panel.html").exists() else NO_KEY_HTML
