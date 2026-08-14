"""Sanity checks: importability and a decorator-glued-to-wrong-function regression guard —
a router decorator can end up above the wrong `async def` after a careless edit, which
aiogram fails on silently (a DI error the global handler reports as a generic message)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DECORATOR_RE = re.compile(r'^@(client_router|admin_router)\.(message|callback_query)\(')
DEF_RE = re.compile(r'^async def (\w+)\(([^)]*)\):')


def test_all_modules_import_cleanly():
    import backup  # noqa: F401
    import config  # noqa: F401
    import database  # noqa: F401
    import handlers  # noqa: F401
    import keyboards  # noqa: F401
    import main  # noqa: F401
    import payments  # noqa: F401
    import scheduler  # noqa: F401


def test_no_decorator_glued_to_wrong_function():
    content = (ROOT / "handlers.py").read_text(encoding="utf-8")
    lines = content.split("\n")
    offenders = []
    for i, line in enumerate(lines):
        if not DECORATOR_RE.match(line):
            continue
        j = i + 1
        while j < len(lines) and lines[j].strip() == "":
            j += 1
        m = DEF_RE.match(lines[j]) if j < len(lines) else None
        if not m:
            continue
        first_param = m.group(2).split(",")[0].strip().split(":")[0].strip()
        if first_param not in ("message", "callback"):
            offenders.append(f"line {i + 1}: {m.group(0)}")
    assert not offenders, "Decorator(s) glued to the wrong function:\n" + "\n".join(offenders)


def test_handlers_file_stays_small():
    """The whole point of MINI: a buyer can read the bot logic in one sitting."""
    lines = (ROOT / "handlers.py").read_text(encoding="utf-8").splitlines()
    assert len(lines) < 1500, f"handlers.py grew to {len(lines)} lines — MINI is supposed to stay small"
