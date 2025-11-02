# serve_bot_webhook.py — FastAPI + PTB via build_application() from bot_slots_flow
import os
import logging
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import PlainTextResponse
from telegram import Update

# *** Importa l'Application completa dal tuo bot ***
from bot_slots_flow import build_application as build_ptb_app

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("serve_bot_webhook")

APP_URL = os.environ.get("APP_URL", "https://saldo-bot-production.up.railway.app").rstrip("/")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/telegram")
WEBHOOK_SECRET = os.environ.get("TG_WEBHOOK_SECRET", "PLEASE_CHANGE_ME")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN env var")

app = FastAPI(title="saldo-bot webhook")

# Crea l'istanza PTB usando il tuo build_application()
ptb_app = build_ptb_app()


@app.on_event("startup")
async def on_startup():
    await ptb_app.initialize()
    await ptb_app.start()
    url = f"{APP_URL}{WEBHOOK_PATH}"
    await ptb_app.bot.set_webhook(
        url=url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query", "chat_member"]
    )
    log.info("Webhook set to %s", url)


@app.on_event("shutdown")
async def on_shutdown():
    try:
        await ptb_app.stop()
        await ptb_app.shutdown()
    except Exception:
        log.exception("Error on PTB shutdown")


@app.get("/")
async def root():
    return PlainTextResponse("OK")


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    # Verifica secret header (richiesto da Telegram se impostato nel set_webhook)
    h = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if h != WEBHOOK_SECRET:
        raise HTTPException(403, "Forbidden")

    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return Response(status_code=200)
