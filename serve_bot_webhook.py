# serve_bot_webhook.py (revision: fixed indentation + robust startup)
import os
import logging
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import JSONResponse

from telegram import Update
from telegram.error import TelegramError

def _get_app_factory():
    # Try create_application, then build_application from bot_slots_flow
    try:
        from bot_slots_flow import create_application as factory  # type: ignore
        logging.getLogger("railway").info("Using create_application() from bot_slots_flow.py")
        return factory
    except Exception:
        pass
    try:
        from bot_slots_flow import build_application as factory  # type: ignore
        logging.getLogger("railway").info("Using build_application() from bot_slots_flow.py")
        return factory
    except Exception:
        logging.getLogger("railway").exception("Cannot import application factory from bot_slots_flow.py")
        raise

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("railway")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram")
WEBHOOK_SECRET_TOKEN: Optional[str] = os.getenv("WEBHOOK_SECRET_TOKEN", "").strip() or None

if not TELEGRAM_TOKEN:
    log.error("Missing TELEGRAM_TOKEN env var.")
if not PUBLIC_URL:
    log.error("Missing PUBLIC_URL env var (e.g., https://<your-app>.up.railway.app)")

app = FastAPI(title="CalimerosBot Webhook on Railway")
_application = None  # telegram.ext.Application instance

@app.get("/")
async def root():
    return {"status": "ok", "service": "calimerosbot", "webhook_path": WEBHOOK_PATH}

@app.get("/live")
async def liveness():
    return {"ok": True}

@app.get("/ready")
async def readiness():
    ready = _application is not None
    return {"ok": ready}

@app.on_event("startup")
async def on_startup():
    global _application
    factory = _get_app_factory()
    _application = factory(TELEGRAM_TOKEN) if TELEGRAM_TOKEN else factory()

    # Initialize/start PTB app
    await _application.initialize()
    await __application.start()

    url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    try:
        # Do NOT pass allowed_updates to be compatible across PTB versions
        await _application.bot.set_webhook(
            url=url,
            secret_token=WEBHOOK_SECRET_TOKEN
        )
        log.info(f"Webhook set to {url} (secret={bool(WEBHOOK_SECRET_TOKEN)})")
    except TelegramError as e:
        log.exception("Failed to set webhook: %s", e)
        raise

    log.info("PTB application started. Ready to receive updates at %s", url)

@app.on_event("shutdown")
async def on_shutdown():
    global _application
    if _application:
        try:
            await _application.bot.delete_webhook()
        except Exception:
            pass
        await _application.stop()
        await _application.shutdown()
        log.info("PTB application stopped.")

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    global _application
    if _application is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Application not ready")

    if WEBHOOK_SECRET_TOKEN:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if header != WEBHOOK_SECRET_TOKEN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid secret token")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON")

    try:
        update = Update.de_json(data, _application.bot)
        await _application.process_update(update)
    except Exception as e:
        log.exception("Failed to process update: %s", e)
        return JSONResponse(status_code=200, content={"ok": False})

    return {"ok": True}
