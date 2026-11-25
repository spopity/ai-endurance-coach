"""/help and the Telegram command menu come from one list - every handled command is in it."""
import importlib.util
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bot():
    spec = importlib.util.spec_from_file_location("telegram_bot", os.path.join(ROOT, "telegram_bot.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_command_is_in_help():
    src = open(os.path.join(ROOT, "telegram_bot.py")).read()
    handled = set()
    for m in re.finditer(r'CommandHandler\((\[[^\]]*\]|"[^"]+")', src):
        handled |= set(re.findall(r'"([^"]+)"', m.group(1)))
    bot = _bot()
    listed = {c for _, c, *_ in bot.COMMANDS}
    assert handled - {"start"} == listed
    text = bot.help_text()
    assert all(f"/{c}" in text for c in listed)
