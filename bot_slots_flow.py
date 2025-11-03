# bot_slots_flow.py
# PTB 21.6 – Async
# ====
# Features:
# - Admin menu with ➕ Ricarica (credit) and ➖ Addebita (debit)
# - Conversation flows with user selection (list + search), amount, optional slot, confirm
# - SQLite (aiosqlite) with users, kwh_operations, per-user allow_negative override (with global fallback)
# - /saldo (user & admin), /storico (user), /export_ops (admin CSV), /addebita (admin)
# - /allow_negative <user_id> on|off|default + inline buttons to toggle
# - Safe DB migrations for existing DBs (PRAGMA table_info) + UNIQUE index on tg_id
# - Global error handler for PTB
# - /start robusto, /ping
#
# Env:
#   TELEGRAM_TOKEN
#   ADMIN_IDS    (e.g. "111,222")
#   DB_PATH      (default: kwh_slots.db; es. Railway volume: /data/kwh_slots.db)
#   MAX_WALLET_KWH     (default: 10000)
#   MAX_CREDIT_PER_OP  (default: 50000)
#   ALLOW_NEGATIVE     (default: "0" / False)
#
# Requires: python-telegram-bot==21.6, aiosqlite

import os
import io
import csv
import logging
import aiosqlite
from enum import IntEnum
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

__VERSION__ = "1.3.2"

# ---- Logging ----
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bot_slots_flow")


def _log_event(evt: str, **kwargs):
    extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
    log.info("%s %s", evt, extra)


# ---- Config & Defaults ----

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

TZ = timezone(timedelta(hours=1))  # Europe/Rome (semplice; per DST usare zoneinfo)


# ---- Safe migrations ----

async def _get_table_columns(db, table: str) -> set[str]:
    cols = set()
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        async for row in cur:
            cols.add(row[1])  # name
    return cols


async def init_db():
    _log_event("DB_INIT_START", db_path=DB_PATH)
    async with aiosqlite.connect(DB_PATH) as db:
        # 1) Ensure tables exist (minimal schemas)
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

        # 2) Users columns migration (add missing columns safely)
        cols = await _get_table_columns(db, "users")
        if "tg_id" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN tg_id INTEGER")
            _log_event("DB_MIGRATE_ADD_COL", table="users", column="tg_id")
        if "full_name" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN full_name TEXT")
            _log_event("DB_MIGRATE_ADD_COL", table="users", column="full_name")
        if "wallet_kwh" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN wallet_kwh REAL NOT NULL DEFAULT 0")
            _log_event("DB_MIGRATE_ADD_COL", table="users", column="wallet_kwh")
        if "allow_negative_user" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN allow_negative_user INTEGER")
            _log_event("DB_MIGRATE_ADD_COL", table="users", column="allow_negative_user")
        await db.commit()

        # 3) Backfill defaults
        await db.execute("UPDATE users SET wallet_kwh=0 WHERE wallet_kwh IS NULL")
        await db.commit()

        # 4) Indices
        try:
            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_tgid ON users(tg_id)")
        except Exception as e:
            log.warning("UNIQUE index on tg_id not created (possible duplicates): %s", e)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_name ON users(full_name)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_allowneg ON users(allow_negative_user)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_kwh_ops_user ON kwh_operations(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_kwh_ops_created ON kwh_operations(created_at)")
        await db.commit()
    _log_event("DB_INIT_DONE")


# ---- Helpers ----

def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _is_number(text: str) -> bool:
    try:
        float(str(text).replace(",", "."))
        return True
    except Exception:
        return False


async def ensure_user(tg_id: int, full_name: str | None):
    """Create (id=tg_id) if missing; update name if changed."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, full_name FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        if row:
            uid, old_name = row
            if full_name and full_name != old_name:
                await db.execute("UPDATE users SET full_name=? WHERE id=?", (full_name, uid))
                await db.commit()
            return uid
        # not found: create new with id=tg_id so that internal id == chat id
        await db.execute(
            "INSERT INTO users (id, tg_id, full_name, wallet_kwh) VALUES (?,?,?,0)",
            (tg_id, tg_id, full_name or "")
        )
        await db.commit()
    _log_event("USER_CREATED", tg_id=tg_id, name=full_name or "")
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


# allow_negative policy (per-user override with global fallback)
async def get_user_negative_policy(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT allow_negative_user FROM users WHERE id=?", (user_id,))
        row = await cur.fetchone()
        if not row:
            return False, "GLOBAL", None, _env_allow_negative_default()
        user_val = row[0]  # None|0|1
        g = _env_allow_negative_default()
        if user_val is None:
            return g, "GLOBAL", None, g
        return bool(user_val), "USER", bool(user_val), g


# enabled: True=ON, False=OFF, None=DEFAULT (clear override)
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
        _log_event("ALLOW_NEG_SET", user_id=user_id,
                   value=("DEFAULT" if enabled is None else ("ON" if enabled else "OFF")))
        return cur.rowcount > 0


# Money engine
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
                await db.execute("ROLLBACK")
                return False, None, None

            old_balance = float(row[0] or 0.0)
            user_flag = int(row[1])  # -1=unset, 0=no, 1=yes
            allow_neg = _env_allow_negative_default() if user_flag == -1 else (user_flag == 1)

            new_balance = old_balance + float(delta)

            if not allow_neg and new_balance < 0:
                await db.execute("ROLLBACK")
                _log_event("DELTA_BLOCKED_NEGATIVE", user_id=user_id, delta=delta,
                           old=old_balance, new=new_balance)
                return False, old_balance, old_balance

            if new_balance > MAX_WALLET_KWH:
                await db.execute("ROLLBACK")
                _log_event("DELTA_BLOCKED_MAX", user_id=user_id, delta=delta,
                           old=old_balance, new=new_balance)
                return False, None, None

            await db.execute("UPDATE users SET wallet_kwh=? WHERE id=?", (new_balance, user_id))
            await db.execute("""
                INSERT INTO kwh_operations (user_id, delta_kwh, reason, slot, admin_id)
                VALUES (?,?,?,?,?)
            """, (user_id, float(delta), reason, slot, admin_id))
            await db.commit()
            _log_event("DELTA_APPLIED", user_id=user_id, delta=delta, reason=reason, slot=slot,
                       admin=admin_id, old=old_balance, new=new_balance)
            return True, old_balance, new_balance

        except Exception as e:
            try:
                await db.execute("ROLLBACK")
            except Exception:
                pass
            log.exception("ERR apply_delta_kwh: %s", e)
            return False, None, None


async def accredita_kwh(user_id: int, amount: float, slot: str | None, admin_id: int | None):
    if amount is None or amount <= 0:
        return False, None, None
    return await apply_delta_kwh(user_id, +abs(float(amount)), "admin_credit", slot, admin_id)


async def addebita_kwh(user_id: int, amount: float, slot: str | None, admin_id: int | None):
    if amount is None or amount <= 0:
        return False, None, None
    return await apply_delta_kwh(user_id, -abs(float(amount)), "admin_debit", slot, admin_id)


# queries
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


# date filters
def parse_italian_date(s: str) -> datetime:
    s = s.strip()
    today = datetime.now(TZ)
    for fmt in ("%d/%m/%Y", "%d/%m"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=TZ)
            if fmt == "%d/%m":
                dt = dt.replace(year=today.year)
            return dt
        except ValueError:
            pass
    raise ValueError("Formato data non valido. Usa gg/mm o gg/mm/aaaa")


async def fetch_ops_filtered(user_id: int | None, date_from: datetime | None,
                             date_to: datetime | None, limit: int | None = None):
    where = []
    params = []
    if user_id is not None:
        where.append("user_id = ?")
        params.append(user_id)
    if date_from is not None:
        where.append("datetime(created_at) >= datetime(?)")
        params.append(date_from.strftime("%Y-%m-%d %H:%M:%S"))
    if date_to is not None:
        where.append("datetime(created_at) < datetime(?)")
        next_day = (date_to + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        params.append(next_day)

    sql = "SELECT id,user_id,delta_kwh,reason,slot,admin_id,created_at FROM kwh_operations"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, tuple(params))
        rows = await cur.fetchall()
        return rows


# ---- Inline admin UI ----

async def build_user_admin_kb(user_id: int):
    eff, source, user_override, g = await get_user_negative_policy(user_id)
    label = f"{'✅' if eff else '⛔️'} Allow negative: {('ON' if eff else 'OFF')} ({'user' if source == 'USER' else 'global'})"
    kb = [
        [InlineKeyboardButton(label, callback_data="NOP")],
        [
            InlineKeyboardButton("ON", callback_data=f"ALN_SET:{user_id}:on"),
            InlineKeyboardButton("OFF", callback_data=f"ALN_SET:{user_id}:off"),
            InlineKeyboardButton("DEFAULT", callback_data=f"ALN_SET:{user_id}:default"),
        ]
    ]
    return InlineKeyboardMarkup(kb)


def admin_home_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Ricarica", callback_data="AC_START")],
        [InlineKeyboardButton("➖ Addebita", callback_data="AD_START")],
    ])


# ---- States ----

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


# ---- Commands ----

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Safe /start."""
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

    _log_event("CMD_START", tg_id=(user.id if user else None), name=(getattr(user, "full_name", None)))

    if user and (user.id in ADMIN_IDS):
        msg = (
            f"👋 *Admin* — saldo-bot v{__VERSION__}\n\n"
            "Pannello rapido:\n"
            "• ➕ *Ricarica*: accredita kWh a un utente\n"
            "• ➖ *Addebita*: addebita kWh a un utente\n\n"
            "ℹ️ *Comandi disponibili*\n"
            "• /saldo — mostra i tuoi kWh\n"
            "• /ricarica <slot1|slot3|slot5|slot8|wallet> <kwh>\n"
            "• Invia foto con didascalia: `slot3 4.5`\n\n"
            "👮 *Admin:*\n"
            "• /pending — richieste in attesa\n"
            "• /approve <id> — approva richiesta\n"
            "• /reject <id> — rifiuta richiesta\n"
            "• /users — ultimi utenti con saldi\n"
            "• /credita <chat_id> <slot> <kwh>\n\n"
            "_Extra:_ /allow_negative, /export_ops\n"
            f"_DB:_ `{DB_PATH}`"
        )
        kb = admin_home_kb()
    else:
        msg = (
            f"👋 Ciao! Questo è *saldo-bot* v{__VERSION__}.\n\n"
            "ℹ️ *Comandi*\n"
            "• /saldo — mostra i tuoi kWh\n"
            "• /ricarica <slot1|slot3|slot5|slot8|wallet> <kwh>\n"
            "• Invia foto con didascalia: `slot3 4.5`\n\n"
            "Se ti serve assistenza contatta un amministratore."
        )
        kb = None

    try:
        if chat:
            await context.bot.send_message(chat_id=chat.id, text=msg, parse_mode="Markdown", reply_markup=kb)
        elif update.message:
            await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        log.exception("START_REPLY_FAILED: %s", e)


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
    _log_event("CMD_SALDO", caller=caller, args=" ".join(context.args or []))
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
    _log_event("CMD_STORICO", caller=uid)
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
    await update.message.reply_text("\n".join(msg), parse_mode="Markdown")


async def cmd_export_ops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user.id
    _log_event("CMD_EXPORT_OPS", caller=caller, args=" ".join(context.args or []))
    if not _is_admin(caller):
        await update.message.reply_text("Comando riservato agli admin.")
        return

    args = context.args
    q_user = None
    d_from = None
    d_to = None

    for tok in list(args):
        if tok.lower().startswith("user:"):
            try:
                q_user = int(tok.split(":", 1)[1])
            except Exception:
                pass

    date_tokens = [t for t in args if "/" in t]
    try:
        if len(date_tokens) >= 1:
            d_from = parse_italian_date(date_tokens[0])
        if len(date_tokens) >= 2:
            d_to = parse_italian_date(date_tokens[1])
        if d_from and not d_to:
            d_to = d_from
    except Exception:
        await update.message.reply_text("Date non valide. Usa formati: 15/10 o 15/10/2025")
        return

    limit = None if (q_user or d_from or d_to) else 5000
    rows = await fetch_ops_filtered(q_user, d_from, d_to, limit=limit)
    if not rows:
        await update.message.reply_text("Nessuna operazione trovata con i filtri indicati.")
        return

    sio = io.StringIO()
    cw = csv.writer(sio)
    cw.writerow(["id", "user_id", "delta_kwh", "reason", "slot", "admin_id", "created_at"])
    for (id_, user_id, delta, reason, slot, admin_id, created_at) in rows:
        cw.writerow([id_, user_id, float(delta), reason or "", slot or "", admin_id or "", created_at])
    data = sio.getvalue().encode("utf-8-sig")
    bio = io.BytesIO(data)
    bio.name = "kwh_operations.csv"

    cap = "Esportazione operazioni"
    if q_user:
        cap += f" • user {q_user}"
    if d_from and d_to:
        cap += f" • {d_from.strftime('%d/%m/%Y')}–{d_to.strftime('%d/%m/%Y')}"
    elif d_from:
        cap += f" • dal {d_from.strftime('%d/%m/%Y')}"

    await update.message.reply_document(document=bio, caption=cap)


async def cmd_addebita(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user.id
    _log_event("CMD_ADDEBITA", caller=caller, args=" ".join(context.args or []))
    if not _is_admin(caller):
        await update.message.reply_text("Comando riservato agli admin.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /addebita <user_id> <kwh> [slot]")
        return
    try:
        uid = int(context.args[0])
        amount = float(str(context.args[1]).replace(",", "."))
        slot = context.args[2] if len(context.args) >= 3 else None
    except Exception:
        await update.message.reply_text("Parametri non validi. Esempio: /addebita 123 7,5 slot8")
        return
    if amount <= 0:
        await update.message.reply_text("La quantità deve essere > 0.")
        return

    ok, old_bal, new_bal = await addebita_kwh(uid, amount, slot, caller)
    if not ok:
        if old_bal is not None and new_bal is not None and old_bal == new_bal and (old_bal - amount) < 0:
            await update.message.reply_text("❗ Saldo insufficiente e negativo non consentito per questo utente.")
        else:
            await update.message.reply_text("❗ Errore (limiti o policy).")
        return

    name = await _get_user_name(uid)
    await update.message.reply_text(
        f"✅ Addebitati {amount:g} kWh a {name or uid}\nSaldo: {old_bal:.2f} → {new_bal:.2f} kWh"
    )


async def cmd_allow_negative(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user.id
    _log_event("CMD_ALLOW_NEG", caller=caller, args=" ".join(context.args or []))
    if not _is_admin(caller):
        await update.message.reply_text("Comando riservato agli admin.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Uso: /allow_negative <user_id> on|off|default")
        return
    try:
        uid = int(context.args[0])
    except Exception:
        await update.message.reply_text("user_id non valido.")
        return

    mode = context.args[1].lower()
    if mode not in ("on", "off", "default"):
        await update.message.reply_text("Secondo parametro deve essere: on | off | default")
        return

    target = None if mode == "default" else (mode == "on")
    ok = await set_user_allow_negative(uid, target)
    if not ok:
        await update.message.reply_text(f"Utente {uid} non trovato.")
        return

    eff, source, user_override, g = await get_user_negative_policy(uid)
    src = "override UTENTE" if source == "USER" else "DEFAULT GLOBALE"
    await update.message.reply_text(
        f"Allow negative per utente {uid}: {'ON' if eff else 'OFF'} ({src}).\n"
        f"(Globale: {'ON' if g else 'OFF'}; Override: {('ON' if user_override else 'OFF') if user_override is not None else '—'})"
    )


# ---- AC (credit) flow ----

async def on_ac_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_admin(q.from_user.id):
        await q.edit_message_text("Funzione riservata agli admin.")
        return ConversationHandler.END
    context.user_data['ac'] = {}
    rows, total = await fetch_users_page(0)
    _log_event("AC_START", admin=q.from_user.id, page=0, total=total)
    await q.edit_message_text(
        "Seleziona l’utente da accreditare:",
        reply_markup=build_users_kb(rows, 0, total)
    )
    return ACState.SELECT_USER


async def on_ac_users_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_admin(q.from_user.id):
        return ConversationHandler.END
    page = int(q.data.split(":", 1)[1])
    rows, total = await fetch_users_page(page)
    _log_event("AC_PAGE", admin=q.from_user.id, page=page, total=total)
    await q.edit_message_reply_markup(reply_markup=build_users_kb(rows, page, total))
    return ACState.SELECT_USER


async def on_ac_find_press(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_admin(q.from_user.id):
        return ConversationHandler.END
    await q.edit_message_text("Scrivi una parte del nome/cognome da cercare:")
    return ACState.FIND_USER


async def on_ac_find_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    qtxt = (update.message.text or "").strip()
    if len(qtxt) < 2:
        await update.message.reply_text("Inserisci almeno 2 caratteri.")
        return ACState.FIND_USER
    rows = await search_users_by_name(qtxt)
    _log_event("AC_FIND", query=qtxt, results=len(rows))
    if not rows:
        await update.message.reply_text("Nessun risultato. Riprova.")
        return ACState.FIND_USER
    await update.message.reply_text(
        f"Risultati per “{qtxt}”:",
        reply_markup=build_search_kb(rows, qtxt)
    )
    return ACState.SELECT_USER


async def on_ac_pick_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_admin(q.from_user.id):
        return ConversationHandler.END
    if not q.data.startswith("ACU:"):
        return ConversationHandler.END
    uid = int(q.data.split(":", 1)[1])
    context.user_data.setdefault('ac', {})['user_id'] = uid
    _log_event("AC_PICK_USER", admin=q.from_user.id, user_id=uid)

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📜 Storico ultime 10", callback_data=f"ACH:{uid}")]])
    await q.edit_message_text("Inserisci la quantità di kWh da accreditare (es. 10 o 12,5):\n\nPuoi anche vedere lo storico.")
    await q.edit_message_reply_markup(reply_markup=kb)
    return ACState.ASK_AMOUNT


async def on_ac_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if not _is_number(txt):
        await update.message.reply_text("Valore non valido. Inserisci un numero (es. 10 oppure 12,5).")
        return ACState.ASK_AMOUNT

    amount = round(float(txt.replace(",", ".")), 3)
    if amount <= 0:
        await update.message.reply_text("L’importo deve essere maggiore di zero.")
        return ACState.ASK_AMOUNT
    if amount > MAX_CREDIT_PER_OP:
        await update.message.reply_text(f"L’importo massimo per singola operazione è {MAX_CREDIT_PER_OP:g} kWh.")
        return ACState.ASK_AMOUNT

    context.user_data['ac']['amount'] = amount
    _log_event("AC_AMOUNT_SET", amount=amount)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Slot 8", callback_data="ACS:8"),
         InlineKeyboardButton("Slot 3", callback_data="ACS:3"),
         InlineKeyboardButton("Slot 5", callback_data="ACS:5")],
        [InlineKeyboardButton("Salta", callback_data="ACS:-")]
    ])
    await update.message.reply_text(
        f"Ok, accredito **{amount:g} kWh**.\nVuoi indicare lo slot (solo controllo)?",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    return ACState.ASK_SLOT


async def on_ac_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    slot = None
    if q.data.startswith("ACS:"):
        _s = q.data.split(":", 1)[1]
        slot = None if _s == "-" else _s
    context.user_data['ac']['slot'] = slot
    _log_event("AC_SLOT_SET", slot=slot)

    data = context.user_data['ac']
    uid = data['user_id']
    amount = data['amount']
    slot = data.get('slot')
    text = f"Confermi l’accredito di **{amount:g} kWh** all’utente `{uid}`" + (f" (slot {slot})" if slot else "") + "?"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Conferma", callback_data="ACC:OK"),
         InlineKeyboardButton("❌ Annulla", callback_data="ACC:NO")]
    ])
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    return ACState.CONFIRM


async def on_ac_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "ACC:NO":
        await q.edit_message_text("Operazione annullata.")
        return ConversationHandler.END

    data = context.user_data.get('ac', {})
    uid = data['user_id']
    amount = data['amount']
    slot = data.get('slot')
    admin_id = q.from_user.id

    ok, old_bal, new_bal = await accredita_kwh(uid, amount, slot, admin_id)
    if not ok:
        _log_event("AC_CREDIT_FAIL", user_id=uid, amount=amount)
        await q.edit_message_text("❗ Errore durante l’accredito (limiti/policy).")
        return ConversationHandler.END

    name = await _get_user_name(uid)
    summary = (
        "✅ *Accredito completato*\n\n"
        f"*Utente:* {name or uid}\n"
        f"*Quantità:* {amount:g} kWh{f' (slot {slot})' if slot else ''}\n\n"
        f"*Saldo prima:* {old_bal:.2f} kWh\n"
        f"*Saldo dopo:*  {new_bal:.2f} kWh"
    )
    await q.edit_message_text(summary, parse_mode="Markdown")

    try:
        tg = await get_tgid_by_userid(uid)
        if tg:
            await context.bot.send_message(
                chat_id=tg,
                text=f"✅ Ti sono stati accreditati {amount:g} kWh.\nSaldo: {old_bal:.2f} → {new_bal:.2f} kWh"
            )
    except Exception:
        pass
    _log_event("AC_CREDIT_OK", user_id=uid, amount=amount, old=old_bal, new=new_bal)

    return ConversationHandler.END


# history inline button (admin)
async def on_ac_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not q.data.startswith("ACH:"):
        return ConversationHandler.END
    uid = int(q.data.split(":", 1)[1])
    rows = await fetch_user_ops(uid, 10)
    _log_event("AC_HISTORY", user_id=uid, count=len(rows or []))
    if not rows:
        await q.edit_message_text("Nessuna operazione registrata per questo utente.")
        return ACState.SELECT_USER
    lines = ["📜 *Ultime 10 operazioni*", ""]
    for created_at, delta, reason, slot, admin_id in rows:
        sign = "➕" if delta >= 0 else "➖"
        sslot = f" (slot {slot})" if slot else ""
        lines.append(f"{created_at} — {sign}{abs(delta):g} kWh • {reason}{sslot} • admin {admin_id or '-'}")
    await q.edit_message_text("\n".join(lines), parse_mode="Markdown")
    return ACState.SELECT_USER


# ---- AD (debit) flow ----

async def on_ad_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_admin(q.from_user.id):
        await q.edit_message_text("Funzione riservata agli admin.")
        return ConversationHandler.END
    context.user_data['ad'] = {}
    rows, total = await fetch_users_page(0)
    _log_event("AD_START", admin=q.from_user.id, page=0, total=total)
    await q.edit_message_text(
        "Seleziona l’utente da addebitare:",
        reply_markup=build_users_kb(rows, 0, total)
    )
    return ADState.SELECT_USER


async def on_ad_users_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_admin(q.from_user.id):
        return ConversationHandler.END
    page = int(q.data.split(":", 1)[1])
    rows, total = await fetch_users_page(page)
    _log_event("AD_PAGE", admin=q.from_user.id, page=page, total=total)
    await q.edit_message_reply_markup(reply_markup=build_users_kb(rows, page, total))
    return ADState.SELECT_USER


async def on_ad_find_press(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_admin(q.from_user.id):
        return ConversationHandler.END
    await q.edit_message_text("Scrivi una parte del nome/cognome da cercare:")
    return ADState.FIND_USER


async def on_ad_find_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    qtxt = (update.message.text or "").strip()
    if len(qtxt) < 2:
        await update.message.reply_text("Inserisci almeno 2 caratteri.")
        return ADState.FIND_USER
    rows = await search_users_by_name(qtxt)
    _log_event("AD_FIND", query=qtxt, results=len(rows))
    if not rows:
        await update.message.reply_text("Nessun risultato. Riprova.")
        return ADState.FIND_USER
    await update.message.reply_text(
        f"Risultati per “{qtxt}”:",
        reply_markup=build_search_kb(rows, qtxt)
    )
    return ADState.SELECT_USER


async def on_ad_pick_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_admin(q.from_user.id):
        return ConversationHandler.END
    if not (q.data.startswith("ACU:") or q.data.startswith("ADU:")):
        return ConversationHandler.END
    uid = int(q.data.split(":", 1)[1])
    context.user_data.setdefault('ad', {})['user_id'] = uid
    _log_event("AD_PICK_USER", admin=q.from_user.id, user_id=uid)

    await q.edit_message_text("Inserisci la quantità di kWh da addebitare (es. 5 o 7,5).")
    return ADState.ASK_AMOUNT


async def on_ad_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if not _is_number(txt):
        await update.message.reply_text("Valore non valido. Inserisci un numero (es. 5 oppure 7,5).")
        return ADState.ASK_AMOUNT
    amount = round(float(txt.replace(",", ".")), 3)
    if amount <= 0:
        await update.message.reply_text("L’importo deve essere maggiore di zero.")
        return ADState.ASK_AMOUNT
    if amount > MAX_CREDIT_PER_OP:
        await update.message.reply_text(f"Massimo per singola operazione: {MAX_CREDIT_PER_OP:g}.")
        return ADState.ASK_AMOUNT

    context.user_data['ad']['amount'] = amount
    _log_event("AD_AMOUNT_SET", amount=amount)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Slot 8", callback_data="ADS:8"),
         InlineKeyboardButton("Slot 3", callback_data="ADS:3"),
         InlineKeyboardButton("Slot 5", callback_data="ADS:5")],
        [InlineKeyboardButton("Salta", callback_data="ADS:-")]
    ])
    await update.message.reply_text(
        f"Ok, addebito **{amount:g} kWh**.\nVuoi indicare lo slot (solo controllo)?",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    return ADState.ASK_SLOT


async def on_ad_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    slot = None
    if q.data.startswith("ADS:"):
        _s = q.data.split(":", 1)[1]
        slot = None if _s == "-" else _s
    context.user_data['ad']['slot'] = slot
    _log_event("AD_SLOT_SET", slot=slot)

    data = context.user_data['ad']
    uid = data['user_id']
    amount = data['amount']
    slot = data.get('slot')
    text = f"Confermi l’*addebito* di **{amount:g} kWh** all’utente `{uid}`" + (f" (slot {slot})" if slot else "") + "?"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Conferma", callback_data="ADD:OK"),
         InlineKeyboardButton("❌ Annulla", callback_data="ADD:NO")]
    ])
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    return ADState.CONFIRM


async def on_ad_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "ADD:NO":
        await q.edit_message_text("Operazione annullata.")
        return ConversationHandler.END

    data = context.user_data.get('ad', {})
    uid = data['user_id']
    amount = data['amount']
    slot = data.get('slot')
    admin_id = q.from_user.id

    ok, old_bal, new_bal = await addebita_kwh(uid, amount, slot, admin_id)
    if not ok:
        _log_event("AD_DEBIT_FAIL", user_id=uid, amount=amount)
        if old_bal is not None and new_bal is not None and old_bal == new_bal and (old_bal - amount) < 0:
            await q.edit_message_text("❗ Saldo insufficiente e negativo non consentito per questo utente.")
        else:
            await q.edit_message_text("❗ Errore (limiti/policy). Operazione annullata.")
        return ConversationHandler.END

    name = await _get_user_name(uid)
    summary = (
        "✅ *Addebito completato*\n\n"
        f"*Utente:* {name or uid}\n"
        f"*Quantità:* {amount:g} kWh{f' (slot {slot})' if slot else ''}\n\n"
        f"*Saldo prima:* {old_bal:.2f} kWh\n"
        f"*Saldo dopo:*  {new_bal:.2f} kWh"
    )
    await q.edit_message_text(summary, parse_mode="Markdown")

    try:
        tg = await get_tgid_by_userid(uid)
        if tg:
            await context.bot.send_message(
                chat_id=tg,
                text=f"⚠️ Ti sono stati *addebitati* {amount:g} kWh.\nSaldo: {old_bal:.2f} → {new_bal:.2f} kWh",
                parse_mode="Markdown"
            )
    except Exception:
        pass
    _log_event("AD_DEBIT_OK", user_id=uid, amount=amount, old=old_bal, new=new_bal)

    return ConversationHandler.END


# ---- Allow negative inline toggle ----

async def on_allowneg_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_admin(q.from_user.id):
        await q.edit_message_text("Funzione riservata agli admin.")
        return
    _, payload = q.data.split("ALN_SET:", 1)
    uid_str, mode = payload.split(":")
    uid = int(uid_str)

    target = None if mode == "default" else (mode == "on")
    ok = await set_user_allow_negative(uid, target)
    if not ok:
        await q.edit_message_text(f"Utente {uid} non trovato.")
        return
    kb = await build_user_admin_kb(uid)
    try:
        await q.edit_message_reply_markup(reply_markup=kb)
    except Exception:
        eff, source, user_override, g = await get_user_negative_policy(uid)
        src = "override UTENTE" if source == "USER" else "DEFAULT GLOBALE"
        await q.edit_message_text(
            f"Allow negative per {uid}: {'ON' if eff else 'OFF'} ({src}).",
            reply_markup=kb
        )


# ---- Misc ----

async def on_admin_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        _log_event("CMD_ADMIN_MENU", caller=update.effective_user.id)
        await update.message.reply_text("Pannello admin:", reply_markup=admin_home_kb())


async def on_nop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()


# ---- Global error handler ----

async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    log.exception("GLOBAL_ERROR: %s", err)


# ---- Conversation registrations ----

def build_application(token: str | None = None) -> Application:
    app = Application.builder().token(token or os.getenv("TELEGRAM_TOKEN")).build()

    async def _post_init(app_: Application):
        await init_db()
        _log_event("APP_READY", version=__VERSION__)
    app.post_init = _post_init

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("saldo", cmd_saldo))
    app.add_handler(CommandHandler("storico", cmd_storico))
    app.add_handler(CommandHandler("export_ops", cmd_export_ops))
    app.add_handler(CommandHandler("addebita", cmd_addebita))
    app.add_handler(CommandHandler("allow_negative", cmd_allow_negative))
    app.add_handler(CommandHandler("admin", on_admin_home))

    # Credit flow
    ac_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_ac_start, pattern="^AC_START$")],
        states={
            ACState.SELECT_USER: [
                CallbackQueryHandler(on_ac_pick_user, pattern="^ACU:"),
                CallbackQueryHandler(on_ac_users_page, pattern="^ACP:\\d+$"),
                CallbackQueryHandler(on_ac_find_press, pattern="^AC_FIND$"),
                CallbackQueryHandler(on_ac_history, pattern="^ACH:\\d+$"),
            ],
            ACState.FIND_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_ac_find_query)],
            ACState.ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_ac_amount)],
            ACState.ASK_SLOT: [CallbackQueryHandler(on_ac_slot, pattern="^ACS:")],
            ACState.CONFIRM: [CallbackQueryHandler(on_ac_confirm, pattern="^ACC:(OK|NO)$")],
        },
        fallbacks=[],
        name="admin_credit_flow",
        persistent=False,
    )
    app.add_handler(ac_conv, group=0)

    # Debit flow
    ad_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_ad_start, pattern="^AD_START$")],
        states={
            ADState.SELECT_USER: [
                CallbackQueryHandler(on_ad_pick_user, pattern="^(ACU|ADU):"),
                CallbackQueryHandler(on_ad_users_page, pattern="^ACP:\\d+$"),
                CallbackQueryHandler(on_ad_find_press, pattern="^AC_FIND$"),
            ],
            ADState.FIND_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_ad_find_query)],
            ADState.ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_ad_amount)],
            ADState.ASK_SLOT: [CallbackQueryHandler(on_ad_slot, pattern="^ADS:")],
            ADState.CONFIRM: [CallbackQueryHandler(on_ad_confirm, pattern="^ADD:(OK|NO)$")],
        },
        fallbacks=[],
        name="admin_debit_flow",
        persistent=False,
    )
    app.add_handler(ad_conv, group=0)

    # Inline misc
    app.add_handler(CallbackQueryHandler(on_allowneg_set, pattern="^ALN_SET:\\d+:(on|off|default)$"), group=0)
    app.add_handler(CallbackQueryHandler(on_ac_history, pattern="^ACH:\\d+$"), group=0)
    app.add_handler(CallbackQueryHandler(on_nop, pattern="^NOP$"), group=0)

    # Global error handler
    app.add_error_handler(handle_error)

    return app


# ====
# === PATCHED EXTENSIONS ===
# ====
# - Pending list with photo preview & navigation
# - Admin notifications helpers
# - Optional: handler per dichiarazione con foto

from typing import Optional, Iterable

PENDING_PAGE = "pending:page:"
PENDING_PHOTO = "pending:photo:"


def get_admin_ids() -> list[int]:
    raw = os.getenv("ADMIN_IDS", "").strip()
    ids: list[int] = []
    for p in raw.split(","):
        p = p.strip()
        if not p:
            continue
        try:
            ids.append(int(p))
        except ValueError:
            pass
    return ids


async def notify_admins(application, text: str):
    for admin_id in get_admin_ids():
        try:
            await application.bot.send_message(chat_id=admin_id, text=text, disable_web_page_preview=True)
        except Exception:
            pass


async def admin_pending_render(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    DB = os.getenv("DB_PATH", "kwh_slots.db")
    PAGE = 5
    page = max(0, int(page))
    offset = page * PAGE

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("""
            SELECT id, user_chat_id, kwh, slot, note, photo_file_id, created_at
            FROM recharges
            WHERE status='pending'
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """, (PAGE, offset))
        rows = await cur.fetchall()
        cur2 = await db.execute("SELECT COUNT(*) FROM recharges WHERE status='pending'")
        total = (await cur2.fetchone())[0]

    if not rows:
        text = "Nessuna richiesta in attesa."
        if update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await update.effective_chat.send_message(text)
        return

    lines = [f"📥 *Richieste in attesa* — pagina {page+1}/{max(1, (total + PAGE - 1)//PAGE)}"]
    kb = []
    for rec in rows:
        rid, chat_id, kwh, slot, note, photo_file_id, created_at = rec
        note = note or ""
        lines.append(f"• #{rid} — {kwh} kWh (slot {slot}) da {chat_id}" + (f"\n   📝 {note}" if note else ""))
        kb.append([InlineKeyboardButton("📷 Foto", callback_data=f"{PENDING_PHOTO}{rid}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Indietro", callback_data=f"{PENDING_PAGE}{page-1}"))
    if (page + 1) * PAGE < total:
        nav.append(InlineKeyboardButton("Avanti ➡️", callback_data=f"{PENDING_PAGE}{page+1}"))
    if nav:
        kb.append(nav)

    markup = InlineKeyboardMarkup(kb)
    text = "\n".join(lines)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        await update.effective_chat.send_message(text, parse_mode="Markdown", reply_markup=markup)


async def admin_pending_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, recharge_id: int):
    DB = os.getenv("DB_PATH", "kwh_slots.db")
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT photo_file_id, kwh, slot, user_chat_id FROM recharges WHERE id=? AND status='pending'",
            (recharge_id,),
        )
        row = await cur.fetchone()

    if not row or not row[0]:
        await update.callback_query.answer("Foto non disponibile.", show_alert=True)
        return

    file_id, kwh, slot, chat_id = row
    caption = f"#{recharge_id} — {kwh} kWh (slot {slot}) da {chat_id}"
    await update.effective_chat.send_photo(photo=file_id, caption=caption)
    await update.callback_query.answer()


async def admin_pending_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id not in get_admin_ids():
        await update.effective_chat.send_message("Non sei autorizzato.")
        return
    await admin_pending_render(update, context, page=0)


async def cb_pending_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data or ""
    page = int(data.replace(PENDING_PAGE, "") or "0")
    await admin_pending_render(update, context, page=page)


async def cb_pending_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data or ""
    rid = int(data.replace(PENDING_PHOTO, "") or "0")
    if rid <= 0:
        await update.callback_query.answer("ID non valido", show_alert=True)
        return
    await admin_pending_photo(update, context, recharge_id=rid)


async def on_declaration_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Salva foto e crea record 'recharges' (richiede tabella con colonne: user_chat_id, kwh, slot, note, photo_file_id, status, created_at)."""
    msg = update.message
    if not msg or not msg.photo:
        await update.effective_chat.send_message(
            "Per favore invia una *foto* come prova della ricarica.", parse_mode="Markdown"
        )
        return

    file_id = msg.photo[-1].file_id
    context.user_data["decl_photo_file_id"] = file_id

    import time
    DB = os.getenv("DB_PATH", "kwh_slots.db")
    user_chat_id = update.effective_user.id if update.effective_user else None
    kwh = context.user_data.get("decl_kwh")
    slot = context.user_data.get("decl_slot")
    note = context.user_data.get("decl_note", "")

    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO recharges (user_chat_id, kwh, slot, note, photo_file_id, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (user_chat_id, kwh, slot, note, file_id, "pending", int(time.time())),
        )
        await db.commit()

    await update.effective_chat.send_message(
        "✅ *Dichiarazione inviata!* Ti avviseremo appena un admin la verifica.", parse_mode="Markdown"
    )
    try:
        await notify_admins(context.application, f"🆕 Nuova richiesta in attesa: {kwh} kWh (slot {slot}) da {user_chat_id}")
    except Exception:
        pass


def _register_patch_handlers(application):
    try:
        application.add_handler(CommandHandler("pending", admin_pending_entry))
        application.add_handler(CallbackQueryHandler(cb_pending_page, pattern=f"^{PENDING_PAGE}"))
        application.add_handler(CallbackQueryHandler(cb_pending_photo, pattern=f"^{PENDING_PHOTO}"))
    except Exception:
        pass
    return application


# Wrap existing factory build_application / create_application if present
try:
    _ORIG_BUILD_APP = build_application  # type: ignore
    def build_application(*args, **kwargs):  # type: ignore
        app = _ORIG_BUILD_APP(*args, **kwargs)
        return _register_patch_handlers(app)
except NameError:
    pass

try:
    _ORIG_CREATE_APP = create_application  # type: ignore
    def create_application(*args, **kwargs):  # type: ignore
        app = _ORIG_CREATE_APP(*args, **kwargs)
        return _register_patch_handlers(app)
except NameError:
    pass
