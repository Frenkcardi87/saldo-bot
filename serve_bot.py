#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FastAPI minimale per healthcheck + bootstrap del bot come *web service*.
Avvia il bot in background su import. Adatto a Railway/Render.
"""
import asyncio
import threading
from fastapi import FastAPI

from bot_slots_flow import build_application

app = FastAPI()

# Avvio bot in background (thread dedicato)
_bot_started = False


def _start_bot():
    global _bot_started
    if _bot_started:
        return
    _bot_started = True
    application = build_application()
    # usa long polling in thread separato
    threading.Thread(target=lambda: application.run_polling(close_loop=False), daemon=True).start()


@app.on_event("startup")
async def on_startup():
    _start_bot()


@app.get("/")
async def root():
    return {"status": "ok", "service": "saldo-bot"}
