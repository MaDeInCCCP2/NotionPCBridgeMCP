"""PC Bridge console: a live feed of everything the Notion agent does on this PC.

Reads logs/actions.log (written by server.py): every tool call with its arguments and result, command output
and a colored diff of every changed file. Opened by CONSOLE.bat, by the tray menu and at the end of START.bat.
Closing this window does NOT stop the bridge.

Keys (Windows): H = help, U = copy the Notion address, T = copy the token, P = pause / resume the agent,
R = read-only on/off, V = view (all / brief / errors only), Space = freeze, O / L = open workspace / logs,
C = clear, Q twice = stop the bridge, Enter / Esc = close this window. Russian layout works too.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("PCBRIDGE_ROOT") or Path(__file__).resolve().parent.parent)
LOGS = ROOT / "logs"
FEED = LOGS / "actions.log"
PAUSE_FLAG = LOGS / "paused.flag"
STOP_FLAG = LOGS / "stop.flag"
RO_FLAG = LOGS / "readonly.flag"
IS_WIN = os.name == "nt"

R, DIM, GREEN, RED, CYAN, YELLOW, WHITE, MAGENTA = ("\x1b[0m", "\x1b[90m", "\x1b[32m", "\x1b[31m", "\x1b[36m",
                                                   "\x1b[33m", "\x1b[97m", "\x1b[35m")


def color(line: str) -> str:
    s = line.rstrip("\r\n")
    if s.startswith("    │"):  # command / script output
        return DIM + s + R
    if s.startswith("    "):  # diff body
        b = s[4:]
        if b.startswith("@@"):
            return CYAN + s + R
        if b.startswith("+"):
            return GREEN + s + R
        if b.startswith("-"):
            return RED + s + R
        return DIM + s + R
    if s.startswith("  "):  # changed file header
        t = s.lstrip()
        if t.startswith("+"):
            return GREEN + s + R
        if t.startswith("-"):
            return RED + s + R
        return YELLOW + s + R
    if "  ▶ " in s:
        return WHITE + s + R
    if "  ✓ " in s:
        return GREEN + s + R
    if "  ✗ " in s:
        return RED + s + R
    if "  ⚠ " in s:
        return MAGENTA + s + R
    if "  ● " in s:
        return CYAN + s + R
    if "  · " in s:
        return DIM + s + R
    return s


def say(text: str, c: str = CYAN) -> None:
    print(c + text + R, flush=True)


def copy(text: str, what: str) -> None:
    if not text:
        say(f"  [!] {what}: пусто (мост ещё не запущен?)", YELLOW)
        return
    try:
        subprocess.run(["clip"], input=text.encode("utf-16-le"), creationflags=0x08000000, check=False)
        say(f"  [OK] {what} скопирован в буфер обмена", GREEN)
    except OSError as e:
        say(f"  [!] Не удалось скопировать: {e}", YELLOW)


def read(name: str) -> str:
    try:
        return (ROOT / name).read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


BAR = ("  [H] помощь  [U] адрес  [T] токен  [P] пауза  [R] только чтение  [V] вид  [Пробел] стоп-кадр  "
       "[C] очистить  [Q] стоп  [Esc] закрыть")
HELP = """
  ───────────────────────────── клавиши ─────────────────────────────
   U        скопировать адрес для Notion
   T        скопировать токен
   P        пауза / продолжить (агент получает отказ на всё)
   R        только чтение вкл/выкл (агент смотрит, но ничего не меняет)
   V        вид: всё  →  кратко (без вывода команд и diff)  →  только ошибки
   Пробел   стоп-кадр: новые строки копятся и появятся после повторного нажатия
   O / L    открыть рабочую папку / папку логов
   C        очистить экран
   Q        остановить мост (нужно нажать дважды)
   Esc      закрыть окно - мост продолжит работать
  Русская раскладка тоже работает (Р, Г, Е, З, К, М, Щ, Д, С, Й).
  ───────────────────────────────────────────────────────────────────
"""
VIEWS = ("all", "brief", "errors")
VIEW_NAMES = {"all": "всё", "brief": "кратко", "errors": "только ошибки"}
ALIASES = {"Р": "H", "?": "H", ",": "H", "F1": "H", "Г": "U", "Е": "T", "З": "P", "К": "R", "М": "V",
           "Щ": "O", "Д": "L", "С": "C", "Й": "Q", "\r": "ESC", "\n": "ESC", "\x1b": "ESC", " ": "SPACE"}


class State:
    def __init__(self):
        self.view = "all"
        self.frozen = False
        self.held: list = []
        self.quit_armed = 0.0


STATE = State()


def visible(line: str, view: str) -> bool:
    """Should this feed line be shown in the given view?"""
    s = line.rstrip("\r\n")
    if view == "brief":
        return not s.startswith("    ")  # hide command output and diff bodies, keep headers
    if view == "errors":
        return "  ✗ " in s or "  ⚠ " in s
    return True


def update_title(st: State = STATE) -> None:
    parts = ["PC Bridge - действия агента"]
    if PAUSE_FLAG.exists():
        parts.append("ПАУЗА")
    if RO_FLAG.exists():
        parts.append("только чтение")
    if st.view != "all":
        parts.append("вид: " + VIEW_NAMES[st.view])
    if st.frozen:
        parts.append(f"стоп-кадр ({len(st.held)} новых)")
    if IS_WIN:
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(" | ".join(parts))
        except Exception:
            pass


def workspace() -> Path:
    try:
        return Path(json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig")).get("workspace") or ROOT)
    except (OSError, ValueError):
        return ROOT


def open_path(p: Path) -> None:
    if IS_WIN:
        try:
            os.startfile(str(p))
            say(f"  Открываю {p}", DIM)
        except OSError as e:
            say(f"  [!] Не удалось открыть {p}: {e}", YELLOW)


def flag_toggle(flag: Path) -> bool:
    """Flip a flag file; True = it is on now."""
    if flag.exists():
        flag.unlink()
        return False
    flag.write_text(str(time.time()), encoding="utf-8")
    return True


def emit(line: str, st: State = STATE) -> None:
    if not visible(line, st.view):
        return
    if st.frozen:
        st.held.append(line)
        if len(st.held) > 5000:
            del st.held[:1000]
        return
    print(color(line))


def key():
    """The pressed key (upper case, 'F1' for F1) or None. Windows only."""
    if not IS_WIN:
        return None
    import msvcrt

    if not msvcrt.kbhit():
        return None
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):  # arrows / F-keys
        return "F1" if msvcrt.getwch() == ";" else None
    return ch.upper()


def handle(k: str, st: State = STATE) -> bool:
    """Process a key. False = close the window."""
    k = ALIASES.get(k, k)
    if k != "Q":
        st.quit_armed = 0.0
    if k == "ESC":
        return False
    if k == "H":
        say(HELP, WHITE)
    elif k == "U":
        copy(read("URL.txt"), "Адрес")
    elif k == "T":
        copy(read("TOKEN.txt"), "Токен")
    elif k == "P":
        if flag_toggle(PAUSE_FLAG):
            say("  ⏸ ПАУЗА: агент ничего не может делать на ПК (P - продолжить)", YELLOW)
        else:
            say("  ▶ Агент снова может работать", GREEN)
    elif k == "R":
        if flag_toggle(RO_FLAG):
            say("  🔒 Только чтение: агент смотрит, но ничего не меняет (R - выключить)", YELLOW)
        else:
            say("  🔓 Только чтение выключено", GREEN)
    elif k == "V":
        st.view = VIEWS[(VIEWS.index(st.view) + 1) % len(VIEWS)]
        say(f"  Вид: {VIEW_NAMES[st.view]} (V - следующий)", CYAN)
    elif k == "SPACE":
        st.frozen = not st.frozen
        if st.frozen:
            say("  ❚❚ Стоп-кадр: новые строки копятся (Пробел - показать)", YELLOW)
        else:
            held, st.held = st.held, []
            say(f"  ▶ Продолжаю, накопилось строк: {len(held)}", CYAN)
            for ln in held:
                print(color(ln))
    elif k == "O":
        open_path(workspace())
    elif k == "L":
        open_path(LOGS)
    elif k == "C":
        os.system("cls" if IS_WIN else "clear")
        say(BAR, DIM)
    elif k == "Q":
        if time.time() - st.quit_armed > 5:
            st.quit_armed = time.time()
            say("  Остановить мост? Нажмите Q ещё раз (любая другая клавиша - отмена)", YELLOW)
        else:
            STOP_FLAG.write_text(str(time.time()), encoding="utf-8")
            say("  Останавливаю мост...", YELLOW)
            time.sleep(3)
            say("  Мост остановлен.", CYAN)
            time.sleep(1)
            return False
    update_title(st)
    return True


def follow(history: int = 40) -> None:
    pos = 0
    if FEED.exists():  # show the last lines of what happened before
        data = FEED.read_bytes()
        pos = len(data)
        lines = data.decode("utf-8", "replace").splitlines()[-history:]
        if lines:
            say("  --- последние действия ---", DIM)
            for ln in lines:
                emit(ln)
            say("  --- дальше в реальном времени ---", DIM)
    else:
        say("  Жду первых действий агента...", DIM)
    buf = b""
    last_title = 0.0
    while True:
        k = key()
        if k is not None and not handle(k):
            return
        if time.time() - last_title > 2:  # pause / read-only may be switched from the tray
            update_title()
            last_title = time.time()
        try:
            size = FEED.stat().st_size
        except OSError:
            size = 0
        if size < pos:  # the log was rotated
            pos, buf = 0, b""
        if size > pos:
            with open(FEED, "rb") as fh:
                fh.seek(pos)
                chunk = fh.read(size - pos)
            pos += len(chunk)
            buf += chunk
            *done, buf = buf.split(b"\n")
            for ln in done:
                emit(ln.decode("utf-8", "replace"))
            sys.stdout.flush()
        else:
            time.sleep(0.25)


def main() -> None:
    if IS_WIN:
        os.system("")  # turn on ANSI colors in the Windows console
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    LOGS.mkdir(exist_ok=True)
    update_title()
    if "--embedded" not in sys.argv:
        say("  PC Bridge - всё, что делает Notion-агент: вызовы, команды, изменения файлов.", WHITE)
    say(BAR, DIM)
    try:
        follow()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
