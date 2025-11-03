# bot_slots_flow.py
# PTB 21.6 – Async
import os
import io
import csv
import logging
import aiosqlite
from enum import IntEnum
from datetime import datetime, timedelta, timezone
from typing import Optional, Iterable

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

__VERSION__ = "1.3.3"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bot_slots_flow")

DB_PATH = os.getenv("DB_PATH", "kwh_slots.db")

def _as_float_env(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except Exception:
        return default

MAX_WALLET_KWH = _as_float_env("MAX_WALLET_KWH", 10000.0)
MAX_CREDIT_PER_OP = _as_float_env("MAX_CREDIT_PER_OP", 50000.0)

def _env_allow_negative_default() -> bool:
    return os.getenv("ALLOW_NEGATIVE", "0") == "1"

def _admin_ids() -> set[int]:
    ids = os.getenv("ADMIN_IDS", "").strip()
    if not ids:
        return set()
    try:
        return set(int(x.strip()) for x in ids.split(",") if x.strip())
    except Exception:
        return set()

ADMIN_IDS = _admin_ids()
TZ = timezone(timedelta(hours=1))  # Europe/Rome basic

async def _get_table_columns(db, table: str) -> set[str]:
    cols = set()
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        async for row in cur:
            cols.add(row[1])
    return cols

async def init_db():
    log.info("DB_INIT_START db_path=%s", DB_PATH)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS kwh_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                delta_kwh REAL NOT NULL,
                reason TEXT,
                slot TEXT,
                admin_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        await db.commit()

        cols = await _get_table_columns(db, "users")
        if "tg_id" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN tg_id INTEGER")
        if "full_name" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN full_name TEXT")
        if "wallet_kwh" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN wallet_kwh REAL NOT NULL DEFAULT 0")
        if "allow_negative_user" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN allow_negative_user INTEGER")
        await db.commit()

        await db.execute("UPDATE users SET wallet_kwh=0 WHERE wallet_kwh IS NULL")
        await db.commit()

        try:
            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_tgid ON users(tg_id)")
        except Exception:
            pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_name ON users(full_name)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_allowneg ON users(allow_negative_user)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_kwh_ops_user ON kwh_operations(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_kwh_ops_created ON kwh_operations(created_at)")
        await db.commit()
    log.info("DB_INIT_DONE ")

def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def _is_number(text: str) -> bool:
    try:
        float(str(text).replace(",", "."))
        return True
    except Exception:
        return False

async def ensure_user(tg_id: int, full_name: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, full_name FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        if row:
            uid, old_name = row
            if full_name and full_name != old_name:
                await db.execute("UPDATE users SET full_name=? WHERE id=?", (full_name, uid))
                await db.commit()
            return uid
        await db.execute(
            "INSERT INTO users (id, tg_id, full_name, wallet_kwh) VALUES (?,?,?,0)",
            (tg_id, tg_id, full_name or "")
        )
        await db.commit()
    log.info("USER_CREATED tg_id=%s name=%s", tg_id, full_name or "")
    return tg_id

async def get_tgid_by_userid(user_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT tg_id FROM users WHERE id=?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row and row[0] is not None else None

async def get_user_by_tgid(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, full_name, wallet_kwh FROM users WHERE tg_id=?", (tg_id,))
        return await cur.fetchone()

async def get_user_by_id(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, full_name, wallet_kwh FROM users WHERE id=?", (user_id,))
        return await cur.fetchone()

async def _get_user_name(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT full_name FROM users WHERE id=?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else None

async def get_user_negative_policy(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT allow_negative_user FROM users WHERE id=?", (user_id,))
        row = await cur.fetchone()
        if not row:
            return False, "GLOBAL", None, _env_allow_negative_default()
        user_val = row[0]
        g = _env_allow_negative_default()
        if user_val is None:
            return g, "GLOBAL", None, g
        return bool(user_val), "USER", bool(user_val), g

async def set_user_allow_negative(user_id: int, enabled: bool | None) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        if enabled is None:
            cur = await db.execute("UPDATE users SET allow_negative_user=NULL WHERE id=?", (user_id,))
        else:
            cur = await db.execute(
                "UPDATE users SET allow_negative_user=? WHERE id=?",
                (1 if enabled else 0, user_id)
            )
        await db.commit()
        return cur.rowcount > 0

async def apply_delta_kwh(user_id: int, delta: float, reason: str, slot: str | None, admin_id: int | None):
    if not isinstance(delta, (int, float)) or delta == 0:
        return False, None, None
    if abs(delta) > MAX_CREDIT_PER_OP:
        return False, None, None

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("BEGIN")
            cur = await db.execute(
                "SELECT wallet_kwh, COALESCE(allow_negative_user, -1) FROM users WHERE id=?",
                (user_id,)
            )
            row = await cur.fetchone()
            if not row:
                await db.execute("ROLLBACK"); return False, None, None

            old_balance = float(row[0] or 0.0)
            user_flag = int(row[1])  # -1 unset, 0 false, 1 true
            allow_neg = _env_allow_negative_default() if user_flag == -1 else (user_flag == 1)
            new_balance = old_balance + float(delta)

            if not allow_neg and new_balance < 0:
                await db.execute("ROLLBACK"); return False, old_balance, old_balance
            if new_balance > MAX_WALLET_KWH:
                await db.execute("ROLLBACK"); return False, None, None

            await db.execute("UPDATE users SET wallet_kwh=? WHERE id=?", (new_balance, user_id))
            await db.execute("""
                INSERT INTO kwh_operations (user_id, delta_kwh, reason, slot, admin_id)
                VALUES (?,?,?,?,?)
            """, (user_id, float(delta), reason, slot, admin_id))
            await db.commit()
            return True, old_balance, new_balance
        except Exception:
            try: await db.execute("ROLLBACK")
            except Exception: pass
            log.exception("ERR apply_delta_kwh")
            return False, None, None

async def accredita_kwh(user_id: int, amount: float, slot: str | None, admin_id: int | None):
    if amount is None or amount <= 0:
        return False, None, None
    return await apply_delta_kwh(user_id, +abs(float(amount)), "admin_credit", slot, admin_id)

async def addebita_kwh(user_id: int, amount: float, slot: str | None, admin_id: int | None):
    if amount is None or amount <= 0:
        return False, None, None
    return await apply_delta_kwh(user_id, -abs(float(amount)), "admin_debit", slot, admin_id)

PAGE_SIZE = 10

async def fetch_users_page(page: int = 0):
    offset = max(0, page) * PAGE_SIZE
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, COALESCE(full_name,'Utente') as name, wallet_kwh
            FROM users ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?
        """, (PAGE_SIZE, offset))
        rows = await cur.fetchall()
        cur2 = await db.execute("SELECT COUNT(*) FROM users")
        total = (await cur2.fetchone())[0]
        return rows, total

def build_users_kb(rows, page, total):
    buttons = [[InlineKeyboardButton("🔎 Cerca utente", callback_data="AC_FIND")]]
    for uid, name, bal in rows:
        label = f"{name} (id {uid}) — {bal:.2f} kWh"
        buttons.append([InlineKeyboardButton(label, callback_data=f"ACU:{uid}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Indietro", callback_data=f"ACP:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Avanti ➡️", callback_data=f"ACP:{page+1}"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(buttons)

async def search_users_by_name(q: str, limit: int = 20):
    like = f"%{q.strip()}%"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, COALESCE(full_name,'Utente') as name, wallet_kwh
            FROM users WHERE COALESCE(full_name,'') LIKE ? COLLATE NOCASE
            ORDER BY name LIMIT ?
        """, (like, limit))
        return await cur.fetchall()

def build_search_kb(rows, query):
    buttons = []
    for uid, name, bal in rows:
        buttons.append([InlineKeyboardButton(f"{name} (id {uid}) — {bal:.2f} kWh", callback_data=f"ACU:{uid}")])
    buttons.append([InlineKeyboardButton("↩️ Torna all’elenco", callback_data="AC_START")])
    return InlineKeyboardMarkup(buttons)

async def fetch_user_ops(user_id: int, limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT created_at, delta_kwh, reason, slot, admin_id
            FROM kwh_operations WHERE user_id=? ORDER BY id DESC LIMIT ?
        """, (user_id, limit))
        return await cur.fetchall()

def admin_home_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Ricarica", callback_data="AC_START")],
        [InlineKeyboardButton("➖ Addebita", callback_data="AD_START")],
    ])

class ACState(IntEnum):
    SELECT_USER = 1
    ASK_AMOUNT = 2
    ASK_SLOT = 3
    CONFIRM = 4
    FIND_USER = 5

class ADState(IntEnum):
    SELECT_USER = 11
    ASK_AMOUNT = 12
    ASK_SLOT = 13
    CONFIRM = 14
    FIND_USER = 15

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await init_db()
    except Exception as e:
        log.exception("INIT_DB_FAILED: %s", e)

    user = update.effective_user
    chat = update.effective_chat
    try:
        if user:
            await ensure_user(user.id, getattr(user, "full_name", None))
    except Exception as e:
        log.exception("ENSURE_USER_FAILED: %s", e)

    if user and (user.id in ADMIN_IDS):
        msg = (
            f"👋 *Admin* — saldo‑bot v{__VERSION__}\n\n"
            "Pannello rapido:\n"
            "• ➕ *Ricarica*: accredita kWh a un utente\n"
            "• ➖ *Addebita*: addebita kWh a un utente\n\n"
            "ℹ️ *Comandi disponibili*\n"
            "• /saldo — mostra i tuoi kWh\n"
            "• /storico — ultime operazioni\n\n"
            "👮 *Admin:*\n"
            "• /pending — richieste in attesa\n"
            "• /approve <id> — approva richiesta\n"
            "• /reject <id> — rifiuta richiesta\n"
            "• /users — ultimi utenti con saldi\n"
            "• /credita <chat_id> <slot> <kwh>\n"
            "• /allow_negative <user_id> on|off|default\n"
            "• /export_ops — esporta operazioni\n\n"
            f"DB: `{DB_PATH}`"
        )
        kb = admin_home_kb()
    else:
        msg = (
            f"👋 Ciao! Questo è saldo‑bot v{__VERSION__}.\n\n"
            "ℹ️ *Comandi*\n"
            "• /saldo — mostra i tuoi kWh\n"
            "• /storico — ultime operazioni\n"
            "• Invia una foto con didascalia: `slot3 4.5`\n"
        )
        kb = None

    try:
        if chat:
            await context.bot.send_message(chat_id=chat.id, text=msg, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)
        elif update.message:
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)
    except Exception as e:
        log.exception("START_REPLY_FAILED: %s", e)
        try:
            if chat:
                await context.bot.send_message(chat_id=chat.id, text=msg, reply_markup=kb)
            elif update.message:
                await update.message.reply_text(msg, reply_markup=kb)
        except Exception:
            pass

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.effective_message.reply_text("pong")
    except Exception:
        chat = update.effective_chat
        if chat:
            await context.bot.send_message(chat_id=chat.id, text="pong")

async def cmd_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user.id
    await ensure_user(caller, update.effective_user.full_name)
    args = context.args
    target_user_id = None
    if args and _is_admin(caller):
        try:
            target_user_id = int(args[0])
        except Exception:
            await update.message.reply_text("Uso admin: /saldo <user_id>")
            return
    if target_user_id is None:
        row = await get_user_by_tgid(caller)
        if not row:
            await update.message.reply_text("Non sei registrato.")
            return
        user_id, full_name, balance = row
    else:
        row = await get_user_by_id(target_user_id)
        if not row:
            await update.message.reply_text(f"Utente {target_user_id} non trovato.")
            return
        user_id, full_name, balance = row
    ops = await fetch_user_ops(user_id, 5)
    title = f"💡 Saldo kWh — {full_name or user_id}"
    lines = [title, "─" * len(title), f"Saldo attuale: {balance:.2f} kWh", ""]
    if ops:
        lines.append("Ultime operazioni:")
        for (created_at, delta, reason, slot, admin_id) in ops:
            sign = "➕" if delta >= 0 else "➖"
            sslot = f" (slot {slot})" if slot else ""
            lines.append(f"{created_at} — {sign}{abs(delta):g} kWh • {reason}{sslot}")
    else:
        lines.append("Nessuna operazione recente.")
    await update.message.reply_text("\n".join(lines))

async def cmd_storico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await ensure_user(uid, update.effective_user.full_name)
    row = await get_user_by_tgid(uid)
    if not row:
        await update.message.reply_text("Non sei registrato.")
        return
    user_id, full_name, _ = row
    rows = await fetch_user_ops(user_id, 10)
    if not rows:
        await update.message.reply_text("Nessuna operazione registrata.")
        return
    msg = ["📜 *Ultime 10 operazioni*", ""]
    for created_at, delta, reason, slot, admin_id in rows:
        sign = "➕" if delta >= 0 else "➖"
        sslot = f" (slot {slot})" if slot else ""
        msg.append(f"{created_at} — {sign}{abs(delta):g} kWh • {reason}{sslot}")
    await update.message.reply_text("\n".join(msg), parse_mode=ParseMode.MARKDOWN_V2)

# Credit flow minimal (entry + confirm) to ensure buttons trigger
async def on_ac_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_admin(q.from_user.id):
        await q.edit_message_text("Funzione riservata agli admin.")
        return ConversationHandler.END
    await q.edit_message_text("Inserisci la quantità di kWh da accreditare (es. 10 o 12,5).")
    return ACState.ASK_AMOUNT

async def on_ac_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if not _is_number(txt):
        await update.message.reply_text("Valore non valido. Inserisci un numero (es. 10 oppure 12,5).")
        return ACState.ASK_AMOUNT
    amount = round(float(txt.replace(",", ".")), 3)
    context.user_data['ac'] = {'amount': amount, 'user_id': update.effective_user.id}
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Conferma", callback_data="ACC:OK")]])
    await update.message.reply_text(f"Confermi accredito di {amount:g} kWh a te stesso?", reply_markup=kb)
    return ACState.CONFIRM

async def on_ac_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = context.user_data.get('ac', {})
    uid = data.get('user_id', q.from_user.id)
    amount = data.get('amount', 0)
    ok, old_bal, new_bal = await accredita_kwh(uid, amount, None, q.from_user.id)
    if not ok:
        await q.edit_message_text("❗ Errore durante l’accredito (limiti/policy).")
        return ConversationHandler.END
    await q.edit_message_text(f"✅ Accreditati {amount:g} kWh. Saldo: {old_bal:.2f} → {new_bal:.2f} kWh")
    return ConversationHandler.END

# Debit flow minimal
async def on_ad_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_admin(q.from_user.id):
        await q.edit_message_text("Funzione riservata agli admin.")
        return ConversationHandler.END
    await q.edit_message_text("Inserisci la quantità di kWh da addebitare (es. 5 o 7,5).")
    return ADState.ASK_AMOUNT

async def on_ad_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if not _is_number(txt):
        await update.message.reply_text("Valore non valido. Inserisci un numero (es. 5 oppure 7,5).")
        return ADState.ASK_AMOUNT
    amount = round(float(txt.replace(",", ".")), 3)
    context.user_data['ad'] = {'amount': amount, 'user_id': update.effective_user.id}
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Conferma", callback_data="ADD:OK")]])
    await update.message.reply_text(f"Confermi addebito di {amount:g} kWh a te stesso?", reply_markup=kb)
    return ADState.CONFIRM

async def on_ad_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = context.user_data.get('ad', {})
    uid = data.get('user_id', q.from_user.id)
    amount = data.get('amount', 0)
    ok, old_bal, new_bal = await addebita_kwh(uid, amount, None, q.from_user.id)
    if not ok:
        await q.edit_message_text("❗ Errore (limiti/policy). Operazione annullata.")
        return ConversationHandler.END
    await q.edit_message_text(f"✅ Addebitati {amount:g} kWh. Saldo: {old_bal:.2f} → {new_bal:.2f} kWh")
    return ConversationHandler.END

async def on_nop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("GLOBAL_ERROR: %s", context.error)

# Build application with handlers

def build_application(token: str | None = None) -> Application:
    app = Application.builder().token(token or os.getenv("TELEGRAM_TOKEN")).build()

    async def _post_init(app_: Application):
        await init_db()
        log.info("APP_READY version=%s", __VERSION__)
    app.post_init = _post_init

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("saldo", cmd_saldo))
    app.add_handler(CommandHandler("storico", cmd_storico))

    # Credit flow (minimal)
    ac_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_ac_start, pattern="^AC_START$")],
        states={
            ACState.ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_ac_amount)],
            ACState.CONFIRM: [CallbackQueryHandler(on_ac_confirm, pattern="^ACC:OK$")],
        },
        fallbacks=[],
        name="admin_credit_flow",
        persistent=False,
    )
    app.add_handler(ac_conv, group=0)

    # Debit flow (minimal)
    ad_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_ad_start, pattern="^AD_START$")],
        states={
            ADState.ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_ad_amount)],
            ADState.CONFIRM: [CallbackQueryHandler(on_ad_confirm, pattern="^ADD:OK$")],
        },
        fallbacks=[],
        name="admin_debit_flow",
        persistent=False,
    )
    app.add_handler(ad_conv, group=0)

    # Inline misc
    app.add_handler(CallbackQueryHandler(on_nop, pattern="^NOP$"), group=0)

    # Error handler
    app.add_error_handler(handle_error)

    return app
