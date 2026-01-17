# Small token tester; run only temporarily to check getMe
from telegram import Bot
import os, sys, json

t = os.getenv("BOT_TOKEN")
if not t:
    print(json.dumps({"ok": False, "error": "BOT_TOKEN env missing"}))
    sys.exit(1)

try:
    b = Bot(t)
    me = b.get_me()
    print(json.dumps({"ok": True, "result": me.to_dict()}, indent=2))
except Exception as e:
    print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
    sys.exit(2)