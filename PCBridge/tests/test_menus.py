"""Menus: the actions console keys, the tray menu structure and the setup.ps1 menu (static checks)."""
import importlib.util as iu
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server"


def load_console(root: str):
    os.environ["PCBRIDGE_ROOT"] = root
    sp = iu.spec_from_file_location("pcb_console_menu", str(SERVER / "console.py"))
    m = iu.module_from_spec(sp)
    sp.loader.exec_module(m)
    m.time.sleep = lambda *_: None
    m.say = lambda *a, **k: None
    return m


class ConsoleKeys(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        old = os.environ.get("PCBRIDGE_ROOT")
        self.C = load_console(self.root)
        if old is not None:
            os.environ["PCBRIDGE_ROOT"] = old
        self.C.LOGS.mkdir(exist_ok=True)
        self.st = self.C.State()

    def test_views(self):
        v = self.C.visible
        self.assertTrue(v("    │ output", "all"))
        self.assertFalse(v("    │ output", "brief"))
        self.assertFalse(v("    +added line", "brief"))
        self.assertTrue(v("  ~ изменён a.txt", "brief"))
        self.assertTrue(v("12:00  ✗ run_command: ошибка", "errors"))
        self.assertFalse(v("12:00  ✓ read_file", "errors"))

    def test_cycle_view(self):
        self.C.handle("V", self.st)
        self.assertEqual(self.st.view, "brief")
        self.C.handle("М", self.st)  # Russian layout
        self.assertEqual(self.st.view, "errors")
        self.C.handle("V", self.st)
        self.assertEqual(self.st.view, "all")

    def test_pause_and_read_only(self):
        self.C.handle("P", self.st)
        self.assertTrue(self.C.PAUSE_FLAG.exists())
        self.C.handle("З", self.st)
        self.assertFalse(self.C.PAUSE_FLAG.exists())
        self.C.handle("К", self.st)
        self.assertTrue(self.C.RO_FLAG.exists())
        self.assertEqual(self.C.RO_FLAG.name, "readonly.flag")  # same file as server.py / tray
        self.C.handle("R", self.st)
        self.assertFalse(self.C.RO_FLAG.exists())

    def test_freeze_holds_lines(self):
        self.C.handle(" ", self.st)
        self.assertTrue(self.st.frozen)
        self.C.emit("12:00  ▶ read_file", self.st)
        self.assertEqual(len(self.st.held), 1)
        self.C.handle(" ", self.st)
        self.assertFalse(self.st.frozen)
        self.assertEqual(self.st.held, [])

    def test_quit_needs_two_presses(self):
        self.assertTrue(self.C.handle("Q", self.st))
        self.assertFalse(self.C.STOP_FLAG.exists())
        self.assertFalse(self.C.handle("Й", self.st))
        self.assertTrue(self.C.STOP_FLAG.exists())

    def test_other_key_cancels_quit(self):
        self.C.handle("Q", self.st)
        self.C.handle("V", self.st)
        self.assertTrue(self.C.handle("Q", self.st))
        self.assertFalse(self.C.STOP_FLAG.exists())

    def test_close(self):
        for k in ("\x1b", "\r"):
            self.assertFalse(self.C.handle(k, self.st))
        self.assertTrue(self.C.handle("H", self.st))
        self.assertTrue(self.C.handle("F1", self.st))


class FakeItem:
    def __init__(self, text, action=None, **kw):
        self.text, self.action, self.kw = text, action, kw


class FakeMenu:
    SEPARATOR = "SEP"

    def __init__(self, *items):
        self.items = items


class TrayMenu(unittest.TestCase):
    def setUp(self):
        sys.modules["pystray"] = types.SimpleNamespace(MenuItem=FakeItem, Menu=FakeMenu)
        sys.path.insert(0, str(SERVER))
        if "supervisor" not in sys.modules:
            os.environ.setdefault("PCBRIDGE_ROOT", tempfile.mkdtemp())
        import supervisor
        self.S = supervisor
        sup = supervisor.Supervisor.__new__(supervisor.Supervisor)
        sup.cfg = {"token_auth": True, "secret": "abc", "enable_blender": True, "enable_shell": False}
        sup.version = "1.5.2"
        sup.server = types.SimpleNamespace(status="ok")
        sup.ngrok = types.SimpleNamespace(status="ok", url="https://x.ngrok-free.app", error="")
        sup.log = lambda *a, **k: None
        self.sup = sup

    def tearDown(self):
        sys.modules.pop("pystray", None)

    def texts(self, menu):
        return [i.text for i in menu.items if i != "SEP" and isinstance(i.text, str)]

    def test_structure(self):
        m = self.sup.menu()
        top = self.texts(m)
        for t in ("Консоль: что делает агент", "Подключение к Notion", "Настройки", "Открыть",
                  "Перезапустить мост"):
            self.assertIn(t, top)
        self.assertTrue(any(t.startswith("Пауза") for t in top))
        self.assertTrue(any(t.startswith("Остановить всё") for t in top))
        items = {i.text: i for i in m.items if i != "SEP" and isinstance(i.text, str)}
        self.assertTrue(items["Консоль: что делает агент"].kw.get("default"))
        notion = self.texts(items["Подключение к Notion"].action)
        self.assertIn("Скопировать адрес и токен", notion)
        settings = items["Настройки"].action
        st = {i.text: i for i in settings.items if i != "SEP"}
        self.assertIn("Автозапуск с Windows", st)
        mods = st["Модули (включены галочкой)"].action.items
        by = {i.text: i for i in mods}
        self.assertTrue(by["Blender"].kw["checked"](None))
        self.assertFalse(by["Командная строка"].kw["checked"](None))

    def test_headline(self):
        self.S.PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        for f in (self.S.PAUSE_FLAG, self.S.RO_FLAG):
            if f.exists():
                f.unlink()
        self.assertIn("работает", self.sup.headline())
        self.sup.ngrok.status = "error"
        self.assertTrue(self.sup.headline().startswith("✖"))

    def test_restart_resets_children(self):
        class Ch:
            def __init__(self):
                self.fails, self.next_try, self.stopped = 3, 1e12, False

            def stop(self):
                self.stopped = True
        self.sup.children = (Ch(), Ch())
        self.sup.notify = lambda *_: None
        self.sup.update_icon = lambda: None
        self.sup.restart_bridge()
        for c in self.sup.children:
            self.assertTrue(c.stopped)
            self.assertEqual((c.fails, c.next_try), (0, 0.0))


class SetupMenu(unittest.TestCase):
    def test_static(self):
        raw = (HERE.parent / "setup.ps1").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        t = raw.decode("utf-8-sig")
        self.assertNotIn("\n", t.replace("\r\n", ""))  # CRLF only
        self.assertIn("function Show-Menu", t)
        self.assertIn("[Console]::ReadKey($true)", t)
        self.assertLess(t.index("$doWizard ="), t.index("if ($doWizard"))
        self.assertNotIn("= try {", t)
        for k in ("enable_internet", "enable_blender", "enable_roblox", "enable_unreal", "enable_screen", "enable_input",
                  "enable_desktop", "enable_shell", "full_disk", "read_only", "notifications", "autostart",
                  "dangerous_commands"):
            self.assertIn(f'k = "{k}"', t, k)
        self.assertEqual(t.count("{"), t.count("}"))
        self.assertEqual(t.count("("), t.count(")"))


if __name__ == "__main__":
    unittest.main()
