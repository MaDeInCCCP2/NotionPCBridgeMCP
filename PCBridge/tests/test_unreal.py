"""Tests for the Unreal Engine 5 module with a FAKE engine (no real Unreal needed).
Run: RUN_TESTS.bat  (or  .venv\\Scripts\\python -m unittest discover -s tests)"""
import functools
import importlib.util
import inspect
import json
import os
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

import anyio

APP = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="pcbridge_ue_"))
WORK = TMP / "ws"
WORK.mkdir()
ENG = TMP / "UE_5.6"
IS_WIN = os.name == "nt"

# ------------------------------------------------------------ fake engine ----
FAKE_UNREAL = '''
import os
class _L:
    pass
class SystemLibrary:
    @staticmethod
    def get_engine_version(): return "5.6.1-fake"
    @staticmethod
    def get_command_line(): return os.environ.get("FAKE_UE_CMDLINE", "")
class Paths:
    @staticmethod
    def project_saved_dir(): return os.environ["FAKE_UE_SAVED"]
    @staticmethod
    def convert_relative_path_to_full(p): return os.path.abspath(p)
class ScopedEditorTransaction:
    count = 0
    def __init__(self, name): self.name = name
    def __enter__(self):
        ScopedEditorTransaction.count += 1
        return self
    def __exit__(self, *a): return False
TICKS = []
def register_slate_post_tick_callback(fn):
    TICKS.append(fn)
    return len(TICKS)
def unregister_slate_post_tick_callback(h): pass
def log(m): pass
def log_error(m): pass
'''
STUBS = TMP / "stubs"
STUBS.mkdir()
(STUBS / "unreal.py").write_text(FAKE_UNREAL, encoding="utf-8")

FAKE_CMD = f'''#!{sys.executable}
import sys, runpy
sys.path.insert(0, {str(STUBS)!r})
script = next(a.split("=", 1)[1].strip('"') for a in sys.argv if a.startswith("-script="))
print("LogInit: fake UnrealEditor-Cmd", " ".join(sys.argv[1:3]))
runpy.run_path(script, run_name="__main__")
'''
bin_dir = ENG / "Engine" / "Binaries" / ("Win64" if IS_WIN else "Linux")
bin_dir.mkdir(parents=True)
for n in (("UnrealEditor.exe", "UnrealEditor-Cmd.exe") if IS_WIN else ("UnrealEditor", "UnrealEditor-Cmd")):
    f = bin_dir / n
    f.write_text(FAKE_CMD, encoding="utf-8")
    f.chmod(f.stat().st_mode | stat.S_IEXEC)
(ENG / "Engine" / "Build").mkdir(parents=True)
(ENG / "Engine" / "Build" / "Build.version").write_text(json.dumps(
    {"MajorVersion": 5, "MinorVersion": 6, "PatchVersion": 1}), encoding="utf-8")
(ENG / "Engine" / "Build" / "InstalledBuild.txt").write_text("", encoding="utf-8")
bp = ENG / "Templates" / "TP_ThirdPersonBP"
(bp / "Content" / "ThirdPerson").mkdir(parents=True)
(bp / "Config").mkdir()
(bp / "TP_ThirdPersonBP.uproject").write_text(json.dumps(
    {"FileVersion": 3, "EngineAssociation": "", "Category": "x", "Description": "y"}), encoding="utf-8")
(bp / "Content" / "ThirdPerson" / "Lvl_ThirdPerson.umap").write_bytes(b"\0umap")
(bp / "Config" / "TemplateDefs.ini").write_text("[/Script/GameProjectGeneration.TemplateProjectDefs]", encoding="utf-8")
(bp / "Saved").mkdir()
cp = ENG / "Templates" / "TP_ThirdPerson"
(cp / "Source" / "TP_ThirdPerson").mkdir(parents=True)
(cp / "Config").mkdir()
(cp / "TP_ThirdPerson.uproject").write_text(json.dumps(
    {"FileVersion": 3, "Modules": [{"Name": "TP_ThirdPerson", "Type": "Runtime"}]}), encoding="utf-8")
(cp / "Source" / "TP_ThirdPerson" / "TP_ThirdPerson.Build.cs").write_text(
    "public class TP_ThirdPerson : ModuleRules {}", encoding="utf-8")
(cp / "Source" / "TP_ThirdPerson" / "TP_ThirdPersonCharacter.h").write_text(
    "class TP_THIRDPERSON_API ATP_ThirdPersonCharacter {};", encoding="utf-8")
(cp / "Source" / "TP_ThirdPerson.Target.cs").write_text("ExtraModuleNames.Add(\"TP_ThirdPerson\");", encoding="utf-8")
(cp / "Config" / "DefaultEngine.ini").write_text("[/Script/EngineSettings.GameMapsSettings]\n", encoding="utf-8")

(TMP / "config.json").write_text(json.dumps({
    "workspace": str(WORK), "secret": "x" * 64, "token_auth": True, "enable_internet": False,
    "enable_unreal": True, "unreal_path": str(ENG), "enable_shell": False, "full_disk": False,
}), encoding="utf-8")
os.environ["PCBRIDGE_ROOT"] = str(TMP)
spec = importlib.util.spec_from_file_location("pcbridge_server_ue", APP / "server" / "server.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


def call(fn, **kw):
    if inspect.iscoroutinefunction(fn):
        return anyio.run(functools.partial(fn, **kw))
    return fn(**kw)


class Engines(unittest.TestCase):
    def test_finds_engine_from_config(self):
        e = call(S.unreal_list_engines)["engines"]
        self.assertTrue(e)
        self.assertEqual(e[0]["version"], "5.6.1")
        self.assertTrue(e[0]["installed_build"])

    def test_exe_path_is_accepted(self):
        self.assertEqual(S._ue_root_of(Path("C:/E/UE_5.6/Engine/Binaries/Win64/UnrealEditor.exe")), Path("C:/E/UE_5.6"))

    def test_templates(self):
        t = call(S.unreal_list_templates)["templates"]
        self.assertIn("TP_ThirdPersonBP", t)
        self.assertIn("TP_ThirdPerson", t)

    def test_server_info_and_tools(self):
        self.assertIn("unreal", S.MODULES)
        self.assertIn("unreal_read_log", S.READ_TOOLS)
        self.assertIn("Unreal Engine 5", S.INSTRUCTIONS)


class CreateProject(unittest.TestCase):
    def test_blueprint_project(self):
        r = call(S.unreal_create_project, name="BpGame", template="ThirdPerson")
        up = Path(r["project"])
        self.assertEqual(up.name, "BpGame.uproject")
        self.assertFalse(r["cpp"])
        d = json.loads(up.read_text(encoding="utf-8"))
        self.assertEqual(d["EngineAssociation"], "5.6")
        self.assertNotIn("Category", d)
        self.assertTrue(any(p["Name"] == "PythonScriptPlugin" and p["Enabled"] for p in d["Plugins"]))
        self.assertTrue((up.parent / "Content" / "ThirdPerson" / "Lvl_ThirdPerson.umap").is_file())
        self.assertFalse((up.parent / "Saved" / "x").exists())
        self.assertFalse((up.parent / "Config" / "TemplateDefs.ini").exists())
        self.assertTrue((up.parent / "Content" / "Python" / "pcbridge_listener.py").is_file())
        self.assertIn("pcbridge_listener", (up.parent / "Content" / "Python" / "init_unreal.py").read_text("utf-8"))

    def test_cpp_project_is_renamed(self):
        r = call(S.unreal_create_project, name="CppGame", template="ThirdPerson", cpp=True)
        d = Path(r["project"]).parent
        self.assertTrue(r["cpp"])
        h = (d / "Source" / "CppGame" / "CppGameCharacter.h").read_text("utf-8")
        self.assertIn("CPPGAME_API", h)
        self.assertTrue((d / "Source" / "CppGame" / "CppGame.Build.cs").is_file())
        self.assertTrue((d / "Source" / "CppGame.Target.cs").is_file())
        self.assertFalse(list(d.rglob("*TP_ThirdPerson*")))
        self.assertEqual(json.loads((d / "CppGame.uproject").read_text("utf-8"))["Modules"][0]["Name"], "CppGame")
        self.assertIn('OldGameName="/Script/TP_ThirdPerson",NewGameName="/Script/CppGame"',
                      (d / "Config" / "DefaultEngine.ini").read_text("utf-8"))

    def test_bad_name_and_existing(self):
        with self.assertRaises(ValueError):
            call(S.unreal_create_project, name="1 bad name")
        call(S.unreal_create_project, name="Twice")
        with self.assertRaises(ValueError):
            call(S.unreal_create_project, name="Twice")


class InitUnreal(unittest.TestCase):
    def test_existing_init_is_kept(self):
        r = call(S.unreal_create_project, name="KeepInit")
        up = Path(r["project"])
        init = up.parent / "Content" / "Python" / "init_unreal.py"
        init.write_text("print('mine')", encoding="utf-8")
        S._ensure_python(up)
        S._ensure_python(up)
        txt = init.read_text("utf-8")
        self.assertIn("print('mine')", txt)
        self.assertEqual(txt.count("import pcbridge_listener"), 1)


@unittest.skipIf(IS_WIN, "the fake engine binary is a Python script with a shebang (Linux/macOS only)")
class BatchRun(unittest.TestCase):
    def test_hidden_run_returns_output_and_result(self):
        call(S.unreal_create_project, name="BatchGame")
        r = call(S.unreal_run_python, project="BatchGame", code="print('hello', unreal.SystemLibrary.get_engine_version())\n"
                 "RESULT = {'n': 3}", timeout=60)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["mode"], "batch")
        self.assertIn("hello 5.6.1-fake", r["log"])
        self.assertEqual(r["result"], {"n": 3})

    def test_hidden_run_error(self):
        call(S.unreal_create_project, name="BatchErr")
        r = call(S.unreal_run_python, project="BatchErr", code="raise RuntimeError('boom')", timeout=60)
        self.assertFalse(r["ok"])
        self.assertIn("boom", r["error"])

    def test_live_required_but_closed(self):
        call(S.unreal_create_project, name="NoEditor")
        r = call(S.unreal_run_python, project="NoEditor", code="pass", mode="live")
        self.assertFalse(r["ok"])


class LiveRun(unittest.TestCase):
    def test_listener_in_fake_editor(self):
        up = Path(call(S.unreal_create_project, name="LiveGame")["project"])
        os.environ["FAKE_UE_SAVED"] = str(up.parent / "Saved")
        os.environ["FAKE_UE_CMDLINE"] = f'"{up}"'
        sys.path.insert(0, str(STUBS))
        try:
            import unreal  # the fake one
            ls = importlib.util.spec_from_file_location("pcbridge_listener_t",
                                                        up.parent / "Content" / "Python" / "pcbridge_listener.py")
            L = importlib.util.module_from_spec(ls)
            ls.loader.exec_module(L)
        finally:
            sys.path.remove(str(STUBS))
        self.assertTrue(unreal.TICKS)
        stop = threading.Event()

        def editor():
            while not stop.is_set():
                unreal.TICKS[-1](0.016)
                time.sleep(0.05)

        t = threading.Thread(target=editor, daemon=True)
        t.start()
        try:
            self.assertTrue(S._live(up))
            r = call(S.unreal_run_python, project=str(up), code="print('live!')\nRESULT = 42", timeout=20)
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["mode"], "live")
            self.assertIn("live!", r["log"])
            self.assertEqual(r["result"], 42)
            self.assertGreaterEqual(unreal.ScopedEditorTransaction.count, 1)
            r2 = call(S.unreal_run_python, project=str(up), code="1/0", timeout=20)
            self.assertFalse(r2["ok"])
            self.assertIn("ZeroDivisionError", r2["error"])
        finally:
            stop.set()
            t.join(2)


class Logs(unittest.TestCase):
    def test_errors_only(self):
        up = Path(call(S.unreal_create_project, name="LogGame")["project"])
        (up.parent / "Saved" / "Logs").mkdir(parents=True, exist_ok=True)
        (up.parent / "Saved" / "Logs" / "LogGame.log").write_text(
            "LogInit: ok\nLogPython: Error: Traceback bad\nLogTemp: fine\nLogBlueprint: Error: [Compiler] x\n",
            encoding="utf-8")
        r = call(S.unreal_read_log, project="LogGame", errors_only=True)
        self.assertEqual(r["lines"], 2)
        self.assertNotIn("LogTemp", r["log"])

    def test_compile_skips_blueprint_project(self):
        call(S.unreal_create_project, name="NoCpp")
        self.assertTrue(call(S.unreal_compile, project="NoCpp")["skipped"])

    def test_batch_command_keeps_script_quotes(self):
        up = Path(call(S.unreal_create_project, name="CmdGame")["project"])
        cmd = S._batch_cmd(up, S._engine(), Path("C:/my folder/w.py"))
        txt = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.assertIn("-run=pythonscript", txt)
        self.assertIn("-nullrhi", txt)
        self.assertIn("w.py", txt)


if __name__ == "__main__":
    unittest.main()
