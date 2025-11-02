
# serve_bot_webhook.py
# FastAPI + python-telegram-bot (PTB v21) webhook runner for saldo-bot
#
# Env:
#   TELEGRAM_TOKEN        (required)
#   PUBLIC_URL            (required) e.g. https://your-app.up.railway.app
#   WEBHOOK_PATH          (optional, default: /telegram)
#   WEBHOOK_SECRET_TOKEN  (optional but recommended; must match Telegram setWebhook "secret_token")
#   PORT                  (optional, default: 8080)
#
# Run (Railway/Render start command):
#   uvicorn serve_bot_webhook:app --host 0.0.0.0 --port 8080
#
# Notes:
# - Uses PTB Application built in bot_slots_flow.build_application()
# - Sets webhook at startup; removes webhook at shutdown
# - Validates X-Telegram-Bot-Api-Secret-Token if provided
# - Health endpoints: "/", "/live", "/ready"

import os
import json
import logging
import asyncio
from typing import Optional

from fastapi import FastAPI, Request, Response, status, Header
from fastapi.responses import JSONResponse

from telegram import Update
from telegram.constants import UpdateType
from telegram.error import TelegramError
from telegram.ext import Application

from bot_slots_flow import build_application

__VERSION__ = "1.3.0"

# ---------- Logging ----------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("serve_bot_webhook")

# ---------- Env ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
PUBLIC_URL = os.getenv("PUBLIC_URL")  # e.g. https://saldo-bot-production.up.railway.app
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram")
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN")  # optional but recommended
PORT = int(os.getenv("PORT", "8080"))

if not TELEGRAM_TOKEN:
    log.error("Missing TELEGRAM_TOKEN env var")
if not PUBLIC_URL:
    log.error("Missing PUBLIC_URL env var (e.g. your https base URL)")

WEBHOOK_URL = (PUBLIC_URL or "").rstrip("/") + WEBHOOK_PATH

# ---------- FastAPI ----------
app = FastAPI(title="saldo-bot webhook", version=__VERSION__)

# PTB application (global)
ptb_app: Optional[Application] = None

@app.on_event("startup")
async def on_startup():
    global ptb_app
    log.info("STARTUP: building PTB Application")
    ptb_app = build_application(token=TELEGRAM_TOKEN)
    await ptb_app.initialize()
    await ptb_app.start()  # start PTB background tasks
    log.info("PTB Application started")

    if PUBLIC_URL:
        log.info("Setting webhook to %s", WEBHOOK_URL)
        try:
            await ptb_app.bot.set_webhook(
                url=WEBHOOK_URL,
                allowed_updates=[
                    UpdateType.MESSAGE,
                    UpdateType.CALLBACK_QUERY,
                    UpdateType.MY_CHAT_MEMBER,
                    UpdateType.CHAT_MEMBER,
                    UpdateType.MESSAGE_REACTION,
                    UpdateType.MESSAGE_REACTION_COUNT,
                ],
                secret_token=WEBHOOK_SECRET_TOKEN,
                drop_pending_updates=True,
                max_connections=40,
            )
            log.info("Webhook set to %s", WEBHOOK_URL)
        except TelegramError as e:
            log.exception("Failed to set webhook: %s", e)
    else:
        log.warning("PUBLIC_URL is not set; webhook not configured.")

@app.on_event("shutdown")
async def on_shutdown():
    global ptb_app
    if ptb_app:
        log.info("SHUTDOWN: deleting webhook and stopping PTB Application")
        try:
            await ptb_app.bot.delete_webhook(drop_pending_updates=True)
        except TelegramError:
            pass
        await ptb_app.stop()
        await ptb_app.shutdown()
        log.info("PTB Application stopped")

# ---------- Health ----------
@app.get("/")
async def root():
    return {"ok": True, "service": "saldo-bot", "version": __VERSION__}

@app.get("/live")
async def live():
    return {"ok": True}

@app.get("/ready")
async def ready():
    ready = ptb_app is not None
    return {"ok": ready}

# ---------- Webhook ----------
@app.post(WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
):
    # optional secret token validation
    if WEBHOOK_SECRET_TOKEN and (x_telegram_bot_api_secret_token != WEBHOOK_SECRET_TOKEN):
        log.warning("Forbidden: wrong secret token header")
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=status.HTTP_403_FORBIDDEN)

    data = await request.json()
    if LOG_LEVEL == "DEBUG":
        log.debug("Incoming update: %s", json.dumps(data, ensure_ascii=False))

    if not ptb_app:
        log.error("PTB Application not ready")
        return JSONResponse({"ok": False, "error": "ptb_not_ready"}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    try:
        update = Update.de_json(data, ptb_app.bot)  # build Update object
        await ptb_app.update_queue.put(update)      # enqueue for PTB processing
    except Exception as e:
        log.exception("Failed to enqueue update: %s", e)
        return JSONResponse({"ok": False}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    return JSONResponse({"ok": True})
