#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, logging
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.ext import Application
from bot_slots_flow import build_application

os.environ.setdefault("PUBLIC_URL", "https://saldo-bot-production.up.railway.app")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

app = FastAPI()
tg_app: Application | None = None
WEBHOOK_PATH = "/telegram"

@app.on_event("startup")
async def on_startup():
    global tg_app
    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(name)s: %(message)s')
    if not PUBLIC_URL or not PUBLIC_URL.startswith("https://"):
        logging.error("PUBLIC_URL non configurato o non HTTPS. Imposta PUBLIC_URL per usare il webhook.")
        return
    tg_app = build_application()
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.bot.set_webhook(url=PUBLIC_URL + WEBHOOK_PATH, secret_token=WEBHOOK_SECRET, drop_pending_updates=True)
    logging.info("Webhook set to %s%s", PUBLIC_URL, WEBHOOK_PATH)

@app.on_event("shutdown")
async def on_shutdown():
    global tg_app
    if tg_app:
        await tg_app.bot.delete_webhook()
        await tg_app.stop()
        await tg_app.shutdown()

@app.get("/")
async def health():
    return {"status":"ok","mode":"webhook","public_url": PUBLIC_URL}

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret token")
    if not tg_app:
        raise HTTPException(status_code=503, detail="Bot not initialized")
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return JSONResponse({"ok": True})
