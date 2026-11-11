# Python that runs INSIDE Unreal Engine 5 (PythonScriptPlugin). Placeholders are replaced with str.replace.

LISTENER = r'''# PC Bridge listener (installed by the Notion agent bridge). Lets the agent run Python in the OPEN Unreal Editor:
# requests come as Saved/PCBridge/requests/*.json, results go to Saved/PCBridge/results/<id>.json.
# Every request is one undo step (Ctrl+Z in the editor). Safe to delete: the bridge reinstalls it when needed.
import io
import json
import os
import sys
import time
import traceback

import unreal

VERSION = "__VERSION__"
ROOT = os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir()), "PCBridge")
REQ = os.path.join(ROOT, "requests")
RES = os.path.join(ROOT, "results")
_state = {"next": 0.0, "beat": 0.0, "busy": False}


class _Tee(io.TextIOBase):
    def __init__(self, orig, buf):
        self.orig, self.buf = orig, buf

    def write(self, s):
        if self.buf.tell() < 200000:
            self.buf.write(s)
        try:
            self.orig.write(s)
        except Exception:
            pass
        return len(s)

    def flush(self):
        try:
            self.orig.flush()
        except Exception:
            pass


def _jsonable(v):
    try:
        json.dumps(v)
        return v
    except Exception:
        return repr(v)[:20000]


def write_result(rid, ok, out, err, data=None):
    os.makedirs(RES, exist_ok=True)
    tmp = os.path.join(RES, rid + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"ok": ok, "log": out, "error": err, "result": _jsonable(data),
                   "unreal": unreal.SystemLibrary.get_engine_version(), "mode": "live"}, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(RES, rid + ".json"))


def run_file(rid, path, transaction=True):
    buf = io.StringIO()
    err = None
    g = {"__name__": "__main__", "unreal": unreal}
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _Tee(old_out, buf), _Tee(old_err, buf)
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        code = compile(src, "agent_code", "exec")
        if transaction:
            with unreal.ScopedEditorTransaction("PC Bridge: Notion agent"):
                exec(code, g)
        else:
            exec(code, g)
    except SystemExit:
        pass
    except Exception:
        err = traceback.format_exc()
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    if err:
        unreal.log_error("[PC Bridge] " + err)
    write_result(rid, err is None, buf.getvalue(), err, g.get("RESULT"))


def _beat():
    try:
        os.makedirs(ROOT, exist_ok=True)
        tmp = os.path.join(ROOT, "listener.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": VERSION, "unreal": unreal.SystemLibrary.get_engine_version(), "pid": os.getpid(),
                       "t": time.time()}, f)
        os.replace(tmp, os.path.join(ROOT, "listener.json"))
    except Exception:
        pass


def _tick(_dt):
    now = time.time()
    if now >= _state["beat"]:
        _state["beat"] = now + 2
        _beat()
    if now < _state["next"] or _state["busy"]:
        return
    _state["next"] = now + 0.4
    try:
        names = sorted(n for n in os.listdir(REQ) if n.endswith(".json"))
    except OSError:
        return
    for name in names:
        p = os.path.join(REQ, name)
        try:
            with open(p, encoding="utf-8") as f:
                r = json.load(f)
        except Exception:
            continue
        try:
            os.remove(p)  # before running: if the code crashes the editor, it is not repeated
        except OSError:
            pass
        _state["busy"] = True
        try:
            run_file(r["id"], r["script"], r.get("transaction", True))
        except Exception:
            write_result(r.get("id", "unknown"), False, "", traceback.format_exc())
        finally:
            _state["busy"] = False
            try:
                os.remove(r["script"])
            except Exception:
                pass
        return  # one request per tick keeps the editor responsive


def start():
    cmd = unreal.SystemLibrary.get_command_line().lower()
    if "-run=" in cmd or "-nullrhi" in cmd:  # commandlets / hidden runs: no listener
        return
    os.makedirs(REQ, exist_ok=True)
    os.makedirs(RES, exist_ok=True)
    old = getattr(sys, "_pcbridge_tick", None)
    if old is not None:
        try:
            unreal.unregister_slate_post_tick_callback(old)
        except Exception:
            pass
    sys._pcbridge_tick = unreal.register_slate_post_tick_callback(_tick)
    _beat()
    unreal.log("[PC Bridge] listener " + VERSION + " is running - the Notion agent can work in this editor")


start()
'''

INIT_LINE = "import pcbridge_listener  # PC Bridge: lets the Notion agent run Python in this editor\n"

WRAPPER = r'''# Temporary script from PC Bridge (Notion agent), hidden run without the editor window.
import io
import json
import os
import sys
import traceback

import unreal

RID = __RID__
CODE = __CODE__
RES = __RES__
buf = io.StringIO()
err = None
g = {"__name__": "__main__", "unreal": unreal}
old_out, old_err = sys.stdout, sys.stderr


class _Tee(io.TextIOBase):
    def __init__(self, orig):
        self.orig = orig

    def write(self, s):
        if buf.tell() < 200000:
            buf.write(s)
        try:
            self.orig.write(s)
        except Exception:
            pass
        return len(s)


sys.stdout, sys.stderr = _Tee(old_out), _Tee(old_err)
try:
    with open(CODE, encoding="utf-8") as f:
        exec(compile(f.read(), "agent_code", "exec"), g)
except SystemExit:
    pass
except Exception:
    err = traceback.format_exc()
finally:
    sys.stdout, sys.stderr = old_out, old_err
if err:
    unreal.log_error("[PC Bridge] " + err)
data = g.get("RESULT")
try:
    json.dumps(data)
except Exception:
    data = repr(data)[:20000]
os.makedirs(RES, exist_ok=True)
with open(os.path.join(RES, RID + ".tmp"), "w", encoding="utf-8") as f:
    json.dump({"ok": err is None, "log": buf.getvalue(), "error": err, "result": data,
               "unreal": unreal.SystemLibrary.get_engine_version(), "mode": "batch"}, f, ensure_ascii=False)
os.replace(os.path.join(RES, RID + ".tmp"), os.path.join(RES, RID + ".json"))
'''
