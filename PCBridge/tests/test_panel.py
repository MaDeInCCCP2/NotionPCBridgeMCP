import hashlib
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server"
sys.path.insert(0, str(SERVER))
os.environ.setdefault("PCBRIDGE_ROOT", tempfile.mkdtemp())
spec = importlib.util.spec_from_file_location("pcb_panel_t", str(SERVER / "panel.py"))
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)
import supervisor as S  # noqa: E402


class Helpers(unittest.TestCase):
    def test_ago(self):
        self.assertEqual(P.ago(0), "ещё не подключался")
        self.assertEqual(P.ago(100, 130), "30 с назад")
        self.assertEqual(P.ago(100, 100 + 300), "5 мин назад")
        self.assertEqual(P.ago(100, 100 + 3 * 3600 + 120), "3 ч 2 мин назад")

    def test_diff_rows(self):
        a = "\n".join(f"line{i}" for i in range(30))
        b = a.replace("line15", "LINE15") + "\nnew"
        rows = P.diff_rows(a, b)
        kinds = [r["t"] for r in rows]
        self.assertIn("chg", kinds)
        self.assertIn("add", kinds)
        self.assertIn("skip", kinds)
        chg = next(r for r in rows if r["t"] == "chg")
        self.assertEqual((chg["l"], chg["r"], chg["ln"]), ("line15", "LINE15", 16))
        self.assertEqual(P.diff_rows(a, a), [])
        self.assertEqual([r["t"] for r in P.diff_rows("", "x\ny")], ["add", "add"])

    def test_toast_xml_escaping(self):
        x = P.toast_xml('T&"', "a<b\nline2\nline3", [("Показать", "http://127.0.0.1:1/?k=a&show=1")], "http://x/?a=1&b=2")
        self.assertIn("T&amp;&quot;", x)
        self.assertIn("a&lt;b", x)
        self.assertNotIn("line3", x)
        self.assertIn('arguments="http://127.0.0.1:1/?k=a&amp;show=1"', x)
        self.assertIn('launch="http://x/?a=1&amp;b=2"', x)
        import xml.dom.minidom
        xml.dom.minidom.parseString(x)  # valid XML

    def test_read_feed(self):
        d = Path(tempfile.mkdtemp())
        f = d / "actions.log"
        self.assertEqual(P.read_feed(f, -1)["text"], "")
        f.write_text("12:00:00  ▶ read_file a\n", encoding="utf-8")
        r = P.read_feed(f, -1)
        self.assertTrue(r["reset"])
        self.assertIn("read_file", r["text"])
        with open(f, "a", encoding="utf-8") as fh:
            fh.write("12:00:01  ✓ ok\n")
        r2 = P.read_feed(f, r["pos"])
        self.assertEqual(r2["text"], "12:00:01  ✓ ok\n")
        self.assertFalse(r2["reset"])
        self.assertTrue(P.read_feed(f, 10 ** 9)["reset"])

    def test_change_files(self):
        root = Path(tempfile.mkdtemp())
        blobs = root / "history" / "blobs"
        blobs.mkdir(parents=True)
        target = root / "w" / "a.py"
        target.parent.mkdir()
        (blobs / "b1").write_bytes(b"x = 1\n")
        target.write_text("x = 2\n", encoding="utf-8")
        new = root / "w" / "n.txt"
        new.write_text("hello\n", encoding="utf-8")
        entries = [{"id": "c1", "op": "edit", "snaps": [{"path": str(target), "state": "file", "blob": "b1"},
                                                        {"path": str(new), "state": "absent"},
                                                        {"path": str(root / "d"), "state": "dir"}]}]
        files = P.change_files(root, entries, "c1")
        self.assertEqual(files[0]["kind"], "changed")
        self.assertEqual((files[0]["added"], files[0]["removed"]), (1, 1))
        self.assertEqual(files[1]["kind"], "created")
        self.assertEqual(files[2]["note"], "папка")
        self.assertEqual(P.change_files(root, entries, "nope"), [])
        s = P.change_summary(dict(entries[0], op="undo"))
        self.assertTrue(s["is_undo"])
        self.assertEqual(len(s["paths"]), 3)


class Check(unittest.TestCase):
    CFG = {"port": 8765, "secret": "s3", "token_auth": True}

    def fake(self, ping=200, no_tok=401, tok=200, ngrok_err=False):
        def http(url, method="GET", headers=None, body=None, timeout=8):
            if url.endswith("pcbridge-ping"):
                return ping, ({"ngrok-error-code": "ERR_NGROK_3200"} if ngrok_err else {})
            if headers and headers.get("Authorization") == "Bearer s3":
                return tok, {}
            return no_tok, {}
        return http

    def run_check(self, port=True, url="https://a.ngrok.app", **kw):
        return P.check_connection(self.CFG, url, "", time.time() - 10, http=self.fake(**kw), port_open=lambda p: port)

    def test_all_ok(self):
        steps = self.run_check()
        self.assertTrue(all(s["ok"] for s in steps))
        self.assertEqual(steps[-1]["name"], "Notion-агент")
        self.assertEqual(steps[-1]["fix"], "")

    def test_server_down(self):
        steps = self.run_check(port=False)
        self.assertEqual(len(steps), 1)
        self.assertFalse(steps[0]["ok"])
        self.assertTrue(steps[0]["fix"])

    def test_no_tunnel(self):
        steps = self.run_check(url="")
        self.assertEqual([s["ok"] for s in steps], [True, False])

    def test_unreachable(self):
        self.assertFalse(self.run_check(ping=0)[-1]["ok"])
        steps = self.run_check(ngrok_err=True)
        self.assertFalse(steps[-1]["ok"])
        self.assertIn("ERR_NGROK_3200", steps[-1]["detail"])

    def test_token(self):
        self.assertFalse(self.run_check(tok=401)[-1]["ok"])
        self.assertFalse(self.run_check(no_tok=200)[-1]["ok"])

    def test_never_connected_has_hint(self):
        steps = P.check_connection(self.CFG, "https://a", "", 0, http=self.fake(), port_open=lambda p: True)
        self.assertTrue(steps[-1]["ok"])
        self.assertIn("адрес и токен", steps[-1]["fix"])


class FakeSup:
    def __init__(self, root):
        self.cfg = {"port": 8765, "panel_port": 0, "secret": "s"}
        self.calls = []
        self.version = "t"

    def __getattr__(self, name):
        def f(*a, **k):
            self.calls.append((name, a))
        return f


class PanelHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(tempfile.mkdtemp())
        (root / "logs").mkdir()
        (root / "logs" / "actions.log").write_text("12:00:00  ▶ write_file a.txt\n", encoding="utf-8")
        cls.sup = FakeSup(root)
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.sup.cfg["panel_port"] = s.getsockname()[1]
        s.close()
        m = types.SimpleNamespace(LOGS=root / "logs", ROOT=root, ACTIVITY=root / "logs" / "activity.json",
                                  PAUSE_FLAG=root / "p", RO_FLAG=root / "r", NOTIFY_OFF=root / "n",
                                  ctrl_key=lambda s: "k")
        cls.p = P.Panel(cls.sup, m)
        assert cls.p.start()

    @classmethod
    def tearDownClass(cls):
        cls.p.stop()

    def req(self, method, path, body=None, key=True, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.p.port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{self.p.port}", "Content-Type": "application/json"}
        if key:
            h["X-PCB-Panel"] = self.p.key
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8")
        c.close()
        return r.status, data, r

    def test_page_needs_key(self):
        self.assertEqual(self.req("GET", "/")[0], 403)
        code, html, r = self.req("GET", "/?k=" + self.p.key)
        self.assertEqual(code, 200)
        self.assertIn(self.p.key, html)
        self.assertNotIn("__KEY__", html)
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")

    def test_bad_host(self):  # DNS rebinding
        self.assertEqual(self.req("GET", "/?k=" + self.p.key, host="evil.com")[0], 403)
        self.assertEqual(self.req("POST", "/api/action", {"do": "stop"}, host="evil.com")[0], 403)

    def test_api_needs_header(self):
        self.assertEqual(self.req("GET", "/api/feed", key=False)[0], 403)
        self.assertEqual(self.req("POST", "/api/action", {"do": "pause"}, key=False)[0], 403)

    def test_feed_and_changes(self):
        code, data, _ = self.req("GET", "/api/feed?pos=-1")
        self.assertEqual(code, 200)
        self.assertIn("write_file", json.loads(data)["text"])
        self.assertEqual(json.loads(self.req("GET", "/api/changes")[1]), {"changes": []})

    def test_actions(self):
        self.assertIn("error", json.loads(self.req("POST", "/api/action", {"do": "rm -rf"})[1]))
        self.assertIn("error", json.loads(self.req("POST", "/api/action", {"do": "module", "key": "secret"})[1]))
        r = json.loads(self.req("POST", "/api/action", {"do": "module", "key": "enable_shell", "on": True})[1])
        self.assertEqual(r, {"ok": True})
        names = [c[0] for c in self.sup.calls]
        self.assertIn("set_cfg", names)
        self.assertIn("restart_bridge", names)
        self.assertIn("error", json.loads(self.req("POST", "/api/undo", {"ids": []})[1]))

    def test_undo_without_server(self):
        self.sup.cfg["ctrl_port"] = 1
        r = json.loads(self.req("POST", "/api/undo", {"ids": ["x"]})[1])
        self.assertIn("сервер не запущен", r["error"])


class Tray(unittest.TestCase):
    def sup(self, server="ok", ngrok="ok", cfg=None):
        s = object.__new__(S.Supervisor)
        s.server = types.SimpleNamespace(status=server)
        s.ngrok = types.SimpleNamespace(status=ngrok)
        s.cfg = cfg or {}
        return s

    def setUp(self):
        for f in (S.PAUSE_FLAG, S.RO_FLAG):
            try:
                f.unlink()
            except OSError:
                pass

    tearDown = setUp

    def test_colors(self):
        self.assertEqual(self.sup().color(), S.Supervisor.GREEN)
        self.assertEqual(self.sup(cfg={"read_only": True}).color(), S.Supervisor.YELLOW)
        self.assertEqual(self.sup(cfg={"read_only": True}).glyph(), "lock")
        self.assertEqual(self.sup(ngrok="error").color(), S.Supervisor.RED)
        self.assertEqual(self.sup(server="restarting").color(), S.Supervisor.RED)
        self.assertEqual(self.sup(server="starting", ngrok="starting").color(), S.Supervisor.GREY)
        S.LOGS.mkdir(parents=True, exist_ok=True)
        S.PAUSE_FLAG.write_text("1")
        self.assertEqual(self.sup(ngrok="error").color(), S.Supervisor.YELLOW)
        self.assertEqual(self.sup().glyph(), "pause")

    def test_busy(self):
        s = self.sup()
        S.LOGS.mkdir(parents=True, exist_ok=True)
        S.write_json(S.ACTIVITY, {"t": time.time() - 100, "running": 1, "busy_t": time.time()})
        self.assertTrue(s.busy())
        S.write_json(S.ACTIVITY, {"t": time.time() - 100, "running": 0})
        self.assertFalse(s.busy())
        S.write_json(S.ACTIVITY, {"t": time.time(), "running": 0})
        self.assertTrue(s.busy())
        S.write_json(S.ACTIVITY, {"t": 0, "running": 2, "busy_t": time.time() - 4000})  # stale counter
        self.assertFalse(s.busy())

    def test_image(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("no Pillow")
        s = self.sup()
        a = s.image(S.Supervisor.GREEN, lit=False, glyph="lock")
        b = s.image(S.Supervisor.GREEN, lit=True, glyph="lock")
        self.assertNotEqual(a.tobytes(), b.tobytes())

    def test_plural_and_key(self):
        self.assertEqual([S.plural(n, "файл", "файла", "файлов") for n in (1, 3, 5, 11, 21, 22, 112)],
                         ["файл", "файла", "файлов", "файлов", "файл", "файла", "файлов"])
        self.assertEqual(S.ctrl_key("x"), hashlib.sha256(b"pcbridge-ctrl:x").hexdigest())

    def test_changes_notice(self):
        s = self.sup()
        got = []
        s.log = lambda *a: None
        s.notify = lambda text, **kw: got.append((text, kw))
        s.panel = types.SimpleNamespace(url=lambda **p: "u?" + "&".join(f"{k}={v}" for k, v in p.items()))
        s.changes_notice([{"id": "a", "snaps": [{"path": "/w/x.py"}, {"path": "/w/y.py"}]},
                          {"id": "b", "snaps": [{"path": "/w/x.py"}]}])
        text, kw = got[0]
        self.assertTrue(text.startswith("Агент изменил 2 файла\nx.py, y.py"))
        self.assertEqual(kw["buttons"], [("Показать", "u?show=a,b"), ("Отменить", "u?undo=a,b")])

    def test_toast_falls_back(self):
        s = self.sup()
        s.log = lambda *a: None
        plain = []
        s.plain_notify = lambda t, ti: plain.append(t)
        s.panel_mod = P
        orig = S.subprocess.run
        S.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("powershell"))
        try:
            s.toast("hi", "PC Bridge", [("Показать", "http://x")])
        finally:
            S.subprocess.run = orig
        self.assertEqual(plain, ["hi"])


if __name__ == "__main__":
    unittest.main()
