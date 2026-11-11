import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server"))
_tmp = tempfile.mkdtemp()
os.environ["PCBRIDGE_ROOT"] = _tmp
import supervisor as S  # noqa: E402

LOG = """t=1 lvl=info msg="starting web service" obj=web addr=127.0.0.1:4041 allow_hosts=[]
t=2 lvl=info msg="started tunnel" obj=tunnels name=command_line addr=http://localhost:8765 url=https://abc.ngrok-free.app
"""


class NgrokLog(unittest.TestCase):
    def test_url(self):
        self.assertEqual(S.parse_ngrok_log(LOG), {"url": "https://abc.ngrok-free.app", "api": "127.0.0.1:4041"})

    def test_error(self):
        r = S.parse_ngrok_log('t=1 lvl=eror msg="failed" err="authentication failed ERR_NGROK_4018"')
        self.assertEqual(r, {"error": "ERR_NGROK_4018"})

    def test_url_after_error(self):
        r = S.parse_ngrok_log('t=0 err="ERR_NGROK_8012"\n' + LOG)
        self.assertEqual(r.get("url"), "https://abc.ngrok-free.app")
        self.assertNotIn("error", r)

    def test_empty(self):
        self.assertEqual(S.parse_ngrok_log(""), {})


class Helpers(unittest.TestCase):
    def test_args(self):
        self.assertEqual(S.ngrok_args({}, 8765), ["http", "8765", "--host-header=rewrite", "--inspect=false", "--log=stdout"])
        self.assertEqual(S.ngrok_args({"ngrok_domain": "https://x.ngrok.app/"}, 1)[-1], "--url=https://x.ngrok.app")
        self.assertEqual(S.ngrok_args({"ngrok_domain": "x.ngrok.app", "ngrok_url_flag": "domain"}, 1)[-1],
                         "--domain=x.ngrok.app")

    def test_mcp_url(self):
        self.assertEqual(S.mcp_url("https://a/", {}), "https://a/mcp")
        self.assertEqual(S.mcp_url("https://a", {"token_auth": False, "secret": "S"}), "https://a/S/mcp")

    def test_permanent(self):
        for c in (105, 107, 4018, 121, 108):
            self.assertIn(c, S.PERMANENT_NGROK)


class SupervisorState(unittest.TestCase):
    def setUp(self):
        S.LOGS.mkdir(parents=True, exist_ok=True)
        S.CFG_PATH.write_text(json.dumps({"port": 1, "secret": "s"}), encoding="utf-8")

    def test_state_and_backoff(self):
        sup = S.Supervisor()
        sup.save_state()
        st = json.loads(S.STATE.read_text(encoding="utf-8"))
        self.assertEqual(st["server"], "off")
        self.assertEqual(st["mcp_url"], "")
        c = sup.server
        c.failed("x")
        c.failed("y")
        self.assertEqual(c.restarts, 2)
        self.assertGreater(c.next_try, 0)
        self.assertFalse(sup.llm.enabled())
        sup.save_pids()
        self.assertEqual(json.loads(S.PIDS.read_text(encoding="utf-8"))["supervisor"], os.getpid())
        S.PAUSE_FLAG.write_text("1")
        self.assertIn("ПАУЗА", sup.status_text())
        S.PAUSE_FLAG.unlink()

    def test_url_changed_writes_file(self):
        sup = S.Supervisor()
        sup.url_changed("", "https://q.ngrok-free.app")
        self.assertEqual((S.ROOT / "URL.txt").read_text(encoding="utf-8").strip(), "https://q.ngrok-free.app/mcp")


if __name__ == "__main__":
    unittest.main()
