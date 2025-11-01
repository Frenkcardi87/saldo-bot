# serve_bot.py
import threading
import asyncio
from fastapi import FastAPI
from bot_slots_flow import create_application
from telegram import Update

app = FastAPI(title="saldo-bot")

_bot_thread = None

def _run_bot():
    application = create_application()
    asyncio.run(application.run_polling(allowed_updates=Update.ALL_TYPES))

@app.on_event("startup")
def on_startup():
    global _bot_thread
    if _bot_thread is None:
        _bot_thread = threading.Thread(target=_run_bot, daemon=True)
        _bot_thread.start()

@app.get("/")
def root():
    return {"status": "ok"}
