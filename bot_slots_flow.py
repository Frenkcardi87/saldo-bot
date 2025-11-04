# bot_slots_flow.py
# PTB 21.6 – Async
# Extended with Credit Request Approval System
# ====
# New Features:
    # - User-initiated credit requests with photo upload (/ricarica or photo with caption)
# - Admin approval/rejection system with inline buttons
# - Notifications to users on approval/rejection
# - /pending command for viewing pending requests
# - Credit photos stored in /credit_photos directory
# - Max 5 pending requests per user limit
#
# Env:
    #   TELEGRAM_TOKEN
#   ADMIN_IDS    (e.g. "111,222")
#   DB_PATH    (default: kwh_slots.db)
#   MAX_WALLET_KWH    (default: 10000)
#   MAX_CREDIT_PER_OP  (default: 50000)
#   ALLOW_NEGATIVE    (default: "0" / False)
#   SLOTS    (e.g. "slot1,slot3,slot5,slot8,wallet")
#   CREDIT_PHOTOS_PATH (default: /credit_photos)
#
# Requires: python-telegram-bot==21.6, aiosqlite

import os
import io
import csv
import logging
import aiosqlite
import uuid
from enum import IntEnum
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
from telegram.error import TelegramError

__VERSION__ = "2.0.0"

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
CREDIT_PHOTOS_PATH = os.getenv("CREDIT_PHOTOS_PATH", "/credit_photos")

# Create photos directory if it doesn't exist
Path(CREDIT_PHOTOS_PATH).mkdir(parents=True, exist_ok=True)

def _as_float_env(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except Exception:
        return default

MAX_WALLET_KWH    = _as_float_env("MAX_WALLET_KWH", 10000.0)
MAX_CREDIT_PER_OP = _as_float_env("MAX_CREDIT_PER_OP", 50000.0)
MAX_PENDING_REQUESTS = 5

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

def _get_slots() -> list[str]:
    slots = os.getenv("SLOTS", "slot1,slot3,slot5,slot8,wallet").strip()
    return [s.strip() for s in slots.split(",") if s.strip()]

ADMIN_IDS = _admin_ids()
SLOTS = _get_slots()

TZ = timezone(timedelta(hours=1))  # Europe/Rome

# ---- Database Migrations ----

async def _get_table_columns(db, table: str) -> set[str]:
    cols = set()
    try:
        async with db.execute(f"PRAGMA table_info({table})") as cur:
            async for row in cur:
                cols.add(row[1])  # name
    except Exception:
        pass
    return cols

async def _table_exists(db, table: str) -> bool:
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)
    )
    row = await cur.fetchone()
    return row is not None

async def init_db():
    _log_event("DB_INIT_START", db_path=DB_PATH)
    async with aiosqlite.connect(DB_PATH) as db:
        # 1) Ensure base tables exist
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
            )""")
        
        # 2) NEW: credit_requests table
        if not await _table_exists(db, "credit_requests"):
            await db.execute("""
                CREATE TABLE credit_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    slot TEXT NOT NULL,
                    kwh REAL NOT NULL,
                    photo_path TEXT,
                    note TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    processed_at TEXT,
                    processed_by INTEGER,
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(processed_by) REFERENCES users(id)
                )""")
            _log_event("DB_TABLE_CREATED", table="credit_requests")
        
        await db.commit()

        # 3) Users columns migration
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

        # 4) Backfill defaults
        await db.execute("UPDATE users SET wallet_kwh=0 WHERE wallet_kwh IS NULL")
        await db.commit()

        # 5) Indices
        try:
            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_tgid ON users(tg_id)")
        except Exception as e:
            log.warning("UNIQUE index on tg_id not created: %s", e)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_name ON users(full_name)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_allowneg ON users(allow_negative_user)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_kwh_ops_user ON kwh_operations(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_kwh_ops_created ON kwh_operations(created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_credit_req_status ON credit_requests(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_credit_req_user ON credit_requests(user_id)")
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
        # Create new user
        await db.execute("INSERT INTO users (id, tg_id, full_name, wallet_kwh) VALUES (?,?,?,0)", 
                        (tg_id, tg_id, full_name or ""))
        await db.commit()
        _log_event("USER_CREATED", tg_id=tg_id, name=full_name or "")
        return tg_id

async def get_tgid_by_userid(user_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT tg_id FROM users WHERE id=?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row and row[0] is not None else None

async def get_user_by_tgid(tg_id:int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, full_name, wallet_kwh FROM users WHERE tg_id=?", (tg_id,))
        return await cur.fetchone()

async def get_user_by_id(user_id:int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, full_name, wallet_kwh FROM users WHERE id=?", (user_id,))
        return await cur.fetchone()

async def _get_user_name(user_id:int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT full_name FROM users WHERE id=?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else None

# ---- Credit Request Functions ----

async def count_user_pending_requests(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM credit_requests WHERE user_id=? AND status='pending'",
            (user_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else 0

async def create_credit_request(user_id: int, slot: str, kwh: float, photo_path: str | None, note: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO credit_requests (user_id, slot, kwh, photo_path, note, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
        """, (user_id, slot, kwh, photo_path, note))
        await db.commit()
        return cur.lastrowid

async def get_credit_request(request_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, user_id, slot, kwh, photo_path, note, status, created_at, processed_at, processed_by
            FROM credit_requests WHERE id=?
        """, (request_id,))
        return await cur.fetchone()

async def get_pending_requests(user_id: int | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if user_id is not None:
            cur = await db.execute("""
                SELECT id, user_id, slot, kwh, photo_path, note, created_at
                FROM credit_requests 
                WHERE user_id=? AND status='pending'
                ORDER BY created_at DESC
            """, (user_id,))
        else:
            cur = await db.execute("""
                SELECT id, user_id, slot, kwh, photo_path, note, created_at
                FROM credit_requests 
                WHERE status='pending'
                ORDER BY created_at DESC
            """)
        return await cur.fetchall()

async def approve_credit_request(request_id: int, admin_id: int) -> tuple[bool, str]:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("BEGIN")
            
            # Get request details
            cur = await db.execute("""
                SELECT user_id, kwh, slot, status FROM credit_requests WHERE id=?
            """, (request_id,))
            row = await cur.fetchone()
            
            if not row:
                await db.execute("ROLLBACK")
                return False, "Richiesta non trovata"
            
            user_id, kwh, slot, status = row
            
            if status != 'pending':
                await db.execute("ROLLBACK")
                return False, f"Richiesta già {status}"
            
            # Deduct kWh from user balance
            cur = await db.execute("SELECT wallet_kwh FROM users WHERE id=?", (user_id,))
            row = await cur.fetchone()
            if not row:
                await db.execute("ROLLBACK")
                return False, "Utente non trovato"
            
            current_balance = float(row[0] or 0.0)
            new_balance = current_balance - kwh
            
            # Check if negative is allowed (simplified - you may want to use the existing policy)
            if new_balance < 0 and not _env_allow_negative_default():
                # Check user-specific policy
                cur = await db.execute("SELECT allow_negative_user FROM users WHERE id=?", (user_id,))
                row = await cur.fetchone()
                if row and row[0] != 1:  # If not explicitly allowed
                    await db.execute("ROLLBACK")
                    return False, "Saldo insufficiente"
            
            # Update user balance
            await db.execute("UPDATE users SET wallet_kwh=? WHERE id=?", (new_balance, user_id))
            
            # Record operation
            await db.execute("""
                INSERT INTO kwh_operations (user_id, delta_kwh, reason, slot, admin_id)
                VALUES (?, ?, 'credit_approved', ?, ?)
            """, (user_id, -kwh, slot, admin_id))
            
            # Update request status
            await db.execute("""
                UPDATE credit_requests 
                SET status='approved', processed_at=datetime('now'), processed_by=?
                WHERE id=?
            """, (admin_id, request_id))
            
            await db.commit()
            _log_event("CREDIT_REQUEST_APPROVED", request_id=request_id, user_id=user_id, kwh=kwh, admin=admin_id)
            return True, f"Saldo: {current_balance:.2f} → {new_balance:.2f} kWh"
            
        except Exception as e:
            try:
                await db.execute("ROLLBACK")
            except:
                pass
            log.exception("Error approving credit request: %s", e)
            return False, f"Errore: {str(e)}"

async def reject_credit_request(request_id: int, admin_id: int, reason: str | None = None) -> tuple[bool, str]:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            # Check if request exists and is pending
            cur = await db.execute("""
                SELECT status FROM credit_requests WHERE id=?
            """, (request_id,))
            row = await cur.fetchone()
            
            if not row:
                return False, "Richiesta non trovata"
            
            if row[0] != 'pending':
                return False, f"Richiesta già {row[0]}"
            
            # Update request
            note_field = f"rejected: {reason}" if reason else "rejected"
            await db.execute("""
                UPDATE credit_requests 
                SET status='rejected', processed_at=datetime('now'), processed_by=?, note=?
                WHERE id=?
            """, (admin_id, note_field, request_id))
            
            await db.commit()
            _log_event("CREDIT_REQUEST_REJECTED", request_id=request_id, admin=admin_id, reason=reason)
            return True, "Richiesta rifiutata"
            
        except Exception as e:
            log.exception("Error rejecting credit request: %s", e)
            return False, f"Errore: {str(e)}"

# ---- Notification Helpers ----

async def notify_admins(context: ContextTypes.DEFAULT_TYPE, request_id: int, user_id: int, slot: str, kwh: float, photo_path: str | None, note: str | None):
    """Send notification to all admins with approve/reject buttons"""
    user_name = await _get_user_name(user_id)
    tg_id = await get_tgid_by_userid(user_id)
    username = user_name or f"ID {user_id}"
    
    message = (
        f"🆕 *Nuova richiesta di ricarica*\n\n"
        f"📋 Richiesta #{request_id}\n"
        f"👤 Utente: {username} (TG: {tg_id})\n"
        f"📍 Slot: *{slot}*\n"
        f"⚡ kWh: *{kwh:g}*\n"
    )
    
    if note:
        message += f"📝 Nota: _{note}_\n"
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approva", callback_data=f"CR_APPROVE:{request_id}"),
            InlineKeyboardButton("❌ Rifiuta", callback_data=f"CR_REJECT:{request_id}")
        ]
    ])
    
    for admin_id in ADMIN_IDS:
        try:
            # Send photo if available
            if photo_path and os.path.exists(photo_path):
                with open(photo_path, 'rb') as photo:
                    await context.bot.send_photo(
                        chat_id=admin_id,
                        photo=photo,
                        caption=message,
                        reply_markup=keyboard
                    )
            else:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=message,
                    reply_markup=keyboard
                )
        except Exception as e:
            log.warning(f"Failed to notify admin {admin_id}: {e}")

async def notify_user_request_result(context: ContextTypes.DEFAULT_TYPE, user_id: int, approved: bool, kwh: float, slot: str, details: str = ""):
    """Notify user about approval or rejection"""
    tg_id = await get_tgid_by_userid(user_id)
    if not tg_id:
        return
    
    try:
        if approved:
            message = (
                f"✅ *Richiesta Approvata*\n\n"
                f"La tua richiesta di ricarica è stata approvata!\n"
                f"📍 Slot: {slot}\n"
                f"⚡ kWh scalati: {kwh:g}\n\n"
                f"{details}"
            )
        else:
            message = (
                f"❌ *Richiesta Rifiutata*\n\n"
                f"La tua richiesta di ricarica è stata rifiutata.\n"
                f"📍 Slot: {slot}\n"
                f"⚡ kWh: {kwh:g}\n\n"
                f"Contatta un amministratore per maggiori informazioni."
            )
        
        await context.bot.send_message(
            chat_id=tg_id,
            text=message
        )
    except Exception as e:
        log.warning(f"Failed to notify user {user_id}: {e}")

# ---- Allow negative policy ----

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

async def set_user_allow_negative(user_id: int, enabled: bool|None) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        if enabled is None:
            cur = await db.execute("UPDATE users SET allow_negative_user=NULL WHERE id=?", (user_id,))
        else:
            cur = await db.execute("UPDATE users SET allow_negative_user=? WHERE id=?", (1 if enabled else 0, user_id))
        await db.commit()
        _log_event("ALLOW_NEG_SET", user_id=user_id, value=("DEFAULT" if enabled is None else ("ON" if enabled else "OFF")))
        return cur.rowcount > 0

# ---- Money engine (existing functions) ----

async def apply_delta_kwh(user_id: int, delta: float, reason: str, slot: str|None, admin_id: int|None):
    if not isinstance(delta, (int, float)) or delta == 0:
        return False, None, None
    if abs(delta) > MAX_CREDIT_PER_OP:
        return False, None, None

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("BEGIN")
            cur = await db.execute("SELECT wallet_kwh, COALESCE(allow_negative_user, -1) FROM users WHERE id=?", (user_id,))
            row = await cur.fetchone()
            if not row:
                await db.execute("ROLLBACK")
                return False, None, None

            old_balance = float(row[0] or 0.0)
            user_flag = int(row[1])
            allow_neg = _env_allow_negative_default() if user_flag == -1 else (user_flag == 1)

            new_balance = old_balance + float(delta)

            if not allow_neg and new_balance < 0:
                await db.execute("ROLLBACK")
                _log_event("DELTA_BLOCKED_NEGATIVE", user_id=user_id, delta=delta, old=old_balance, new=new_balance)
                return False, old_balance, old_balance

            if new_balance > MAX_WALLET_KWH:
                await db.execute("ROLLBACK")
                _log_event("DELTA_BLOCKED_MAX", user_id=user_id, delta=delta, old=old_balance, new=new_balance)
                return False, None, None

            await db.execute("UPDATE users SET wallet_kwh=? WHERE id=?", (new_balance, user_id))
            await db.execute("""
                INSERT INTO kwh_operations (user_id, delta_kwh, reason, slot, admin_id)
                VALUES (?,?,?,?,?)
            """, (user_id, float(delta), reason, slot, admin_id))
            await db.commit()
            _log_event("DELTA_APPLIED", user_id=user_id, delta=delta, reason=reason, slot=slot, admin=admin_id, old=old_balance, new=new_balance)
            return True, old_balance, new_balance

        except Exception as e:
            try: await db.execute("ROLLBACK")
            except: pass
            log.exception("ERR apply_delta_kwh: %s", e)
            return False, None, None

async def accredita_kwh(user_id: int, amount: float, slot: str|None, admin_id: int|None):
    if amount is None or amount <= 0:
        return False, None, None
    return await apply_delta_kwh(user_id, +abs(float(amount)), "admin_credit", slot, admin_id)

async def addebita_kwh(user_id: int, amount: float, slot: str|None, admin_id: int|None):
    if amount is None or amount <= 0:
        return False, None, None
    return await apply_delta_kwh(user_id, -abs(float(amount)), "admin_debit", slot, admin_id)

# ---- User queries ----

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
    buttons.append([InlineKeyboardButton("↩️ Torna all'elenco", callback_data="AC_START")])
    return InlineKeyboardMarkup(buttons)

async def fetch_user_ops(user_id: int, limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT created_at, delta_kwh, reason, slot, admin_id
            FROM kwh_operations WHERE user_id=? ORDER BY id DESC LIMIT ?
        """, (user_id, limit))
        return await cur.fetchall()

# ---- Date parsing ----

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

async def fetch_ops_filtered(user_id: int|None, date_from: datetime|None, date_to: datetime|None, limit: int|None=None):
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
    label = f"{'✅' if eff else '⛔️'} Allow negative: {('ON' if eff else 'OFF')} ({'user' if source=='USER' else 'global'})"
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

# ---- Conversation States ----

class ACState(IntEnum):
    SELECT_USER = 1
    ASK_AMOUNT  = 2
    ASK_SLOT    = 3
    CONFIRM     = 4
    FIND_USER   = 5

class ADState(IntEnum):
    SELECT_USER = 11
    ASK_AMOUNT  = 12
    ASK_SLOT    = 13
    CONFIRM     = 14
    FIND_USER   = 15

class CRState(IntEnum):  # Credit Request (User flow)
    ASK_SLOT    = 20
    ASK_KWH     = 21
    ASK_PHOTO   = 22
    ASK_NOTE    = 23
    CONFIRM     = 24

# ====================
# COMMANDS
# ====================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command with different messages for admin vs users"""
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
            "🔧 *Pannello Amministrazione*\n\n"
            "📋 *Comandi disponibili:*\n"
            "• /pending — visualizza richieste in attesa\n"
            "• /saldo [user_id] — controlla saldo utente\n"
            "• /ricarica — invia richiesta di ricarica\n"
            "• /storico — visualizza storico operazioni\n"
            "• /export_ops — esporta operazioni CSV\n"
            "• /addebita <user_id> <kwh> [slot] — addebito manuale\n"
            "• /allow_negative <user_id> on|off|default\n\n"
            f"DB: `{DB_PATH}`"
        )
        kb = admin_home_kb()
    else:
        msg = (
            f"👋 Ciao! Questo è *saldo-bot* v{__VERSION__}\n\n"
            "💡 *Comandi disponibili:*\n"
            "• /saldo — visualizza il tuo saldo\n"
            "• /ricarica — invia richiesta di ricarica\n"
            "• /storico — visualizza storico\n"
            "• /pending — visualizza tue richieste in attesa\n\n"
            "📸 *Puoi anche inviare una foto* con didascalia nel formato:\n"
            "`slot3 4.5` o `slot8 10 nota opzionale`\n\n"
            "Per assistenza contatta un amministratore."
        )
        kb = None

    try:
        if chat:
            await context.bot.send_message(chat_id=chat.id, text=msg, reply_markup=kb)
        elif update.message:
            await update.message.reply_text(msg, reply_markup=kb)
    except Exception as e:
        log.exception("START_REPLY_FAILED: %s", e)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick health check command."""
    try:
        await update.effective_message.reply_text("pong 🏓")
    except Exception:
        chat = update.effective_chat
        if chat:
            await context.bot.send_message(chat_id=chat.id, text="pong 🏓")

async def cmd_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check balance - users check own, admins can check any user"""
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
    lines = [title, "─" * len(title), f"💰 Saldo attuale: *{balance:.2f} kWh*", ""]
    if ops:
        lines.append("📋 *Ultime operazioni:*")
        for (created_at, delta, reason, slot, admin_id) in ops:
            sign = "➕" if delta >= 0 else "➖"
            sslot = f" (slot {slot})" if slot else ""
            lines.append(f"{created_at} — {sign}{abs(delta):g} kWh • {reason}{sslot}")
    else:
        lines.append("Nessuna operazione recente.")
    await update.message.reply_text("\n".join(lines))

async def cmd_storico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user operation history"""
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
    msg = ["📜 *Ultime 10 operazioni*",""]
    for created_at, delta, reason, slot, admin_id in rows:
        sign = "➕" if delta >= 0 else "➖"
        sslot = f" (slot {slot})" if slot else ""
        msg.append(f"{created_at} — {sign}{abs(delta):g} kWh • {reason}{sslot}")
    await update.message.reply_text("\n".join(msg))

async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show pending credit requests - admin sees all, users see only their own"""
    caller = update.effective_user.id
    await ensure_user(caller, update.effective_user.full_name)
    _log_event("CMD_PENDING", caller=caller)
    
    is_admin = _is_admin(caller)
    
    if is_admin:
        # Admin sees all pending requests
        requests = await get_pending_requests()
        if not requests:
            await update.message.reply_text("📭 Nessuna richiesta in attesa.")
            return
        
        lines = ["📥 *Tutte le richieste in attesa*\n"]
        for req in requests:
            req_id, user_id, slot, kwh, photo_path, note, created_at = req
            user_name = await _get_user_name(user_id)
            tg_id = await get_tgid_by_userid(user_id)
            lines.append(
                f"🔸 *Richiesta #{req_id}*\n"
                f"👤 {user_name or f'User {user_id}'} (TG: {tg_id})\n"
                f"📍 Slot: {slot} | ⚡ {kwh:g} kWh\n"
                f"📅 {created_at}\n"
                f"{'📝 ' + note if note else ''}\n"
            )
        
        msg = "\n".join(lines)
        
        # Add action buttons if there are requests
        if len(requests) > 0:
            keyboard = []
            for req in requests[:5]:  # Show buttons for first 5
                req_id = req[0]
                keyboard.append([
                    InlineKeyboardButton(f"✅ Approva #{req_id}", callback_data=f"CR_APPROVE:{req_id}"),
                    InlineKeyboardButton(f"❌ Rifiuta #{req_id}", callback_data=f"CR_REJECT:{req_id}")
                ])
            markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(msg, reply_markup=markup)
        else:
            await update.message.reply_text(msg)
    else:
        # Regular user sees only their own pending requests
        row = await get_user_by_tgid(caller)
        if not row:
            await update.message.reply_text("Non sei registrato.")
            return
        user_id = row[0]
        
        requests = await get_pending_requests(user_id)
        if not requests:
            await update.message.reply_text("📭 Non hai richieste in attesa.")
            return
        
        lines = ["📥 *Le tue richieste in attesa*\n"]
        for req in requests:
            req_id, _, slot, kwh, photo_path, note, created_at = req
            lines.append(
                f"🔸 *Richiesta #{req_id}*\n"
                f"📍 Slot: {slot} | ⚡ {kwh:g} kWh\n"
                f"📅 {created_at}\n"
                f"{'📝 ' + note if note else ''}\n"
            )
        
        await update.message.reply_text("\n".join(lines))

async def cmd_export_ops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export operations to CSV (admin only)"""
    caller = update.effective_user.id
    _log_event("CMD_EXPORT_OPS", caller=caller, args=" ".join(context.args or []))
    if not _is_admin(caller):
        await update.message.reply_text("Comando riservato agli admin.")
        return

    args = context.args
    q_user = None
    d_from = None
    d_to   = None

    for tok in list(args):
        if tok.lower().startswith("user:"):
            try:
                q_user = int(tok.split(":",1)[1])
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
    cw.writerow(["id","user_id","delta_kwh","reason","slot","admin_id","created_at"])
    for (id_, user_id, delta, reason, slot, admin_id, created_at) in rows:
        cw.writerow([id_, user_id, float(delta), reason or "", slot or "", admin_id or "", created_at])
    data = sio.getvalue().encode("utf-8-sig")
    bio = io.BytesIO(data)
    bio.name = "kwh_operations.csv"

    cap = "Esportazione operazioni"
    if q_user: cap += f" • user {q_user}"
    if d_from and d_to: cap += f" • {d_from.strftime('%d/%m/%Y')}–{d_to.strftime('%d/%m/%Y')}"
    elif d_from: cap += f" • dal {d_from.strftime('%d/%m/%Y')}"

    await update.message.reply_document(document=bio, caption=cap)

async def cmd_addebita(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual debit command (admin only)"""
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
            await update.message.reply_text("❗ Errore: limiti o saldo insufficiente.")
        return

    name = await _get_user_name(uid)
    await update.message.reply_text(
        f"✅ Addebitati {amount:g} kWh a {name or uid}\nSaldo: {old_bal:.2f} → {new_bal:.2f} kWh"
    )

async def cmd_allow_negative(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow negative balance command (admin only)"""
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
    if mode not in ("on","off","default"):
        await update.message.reply_text("Secondo parametro deve essere: on | off | default")
        return

    target = None if mode == "default" else (mode == "on")
    ok = await set_user_allow_negative(uid, target)
    if not ok:
        await update.message.reply_text(f"Utente {uid} non trovato.")
        return

    eff, source, user_override, g = await get_user_negative_policy(uid)
    src = "override UTENTE" if source=="USER" else "DEFAULT GLOBALE"
    await update.message.reply_text(
        f"Allow negative per utente {uid}: {'ON' if eff else 'OFF'} ({src}).\n"
        f"(Globale: {'ON' if g else 'OFF'}; Override: {('ON' if user_override else 'OFF') if user_override is not None else '—'})"
    )

# ====================
# ADMIN CREDIT FLOW (AC) - Existing admin functions
# ====================

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
        "Seleziona l'utente da accreditare:",
        reply_markup=build_users_kb(rows, 0, total)
    )
    return ACState.SELECT_USER

async def on_ac_users_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_admin(q.from_user.id):
        return ConversationHandler.END
    page = int(q.data.split(":",1)[1])
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
        f'Risultati per "{qtxt}":',
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
    uid = int(q.data.split(":",1)[1])
    context.user_data.setdefault('ac', {})['user_id'] = uid
    _log_event("AC_PICK_USER", admin=q.from_user.id, user_id=uid)

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📜 Storico ultime 10", callback_data=f"ACH:{uid}")]])
    await q.edit_message_text("✏️ Inserisci i kWh da accreditare (es. 10 o 15,345):")
    await q.edit_message_reply_markup(reply_markup=kb)
    return ACState.ASK_AMOUNT

async def on_ac_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if not _is_number(txt):
        await update.message.reply_text("⚠️ Inserisci i kWh ricaricati (es. 10 o 15,345).")
        return ACState.ASK_AMOUNT

    amount = round(float(txt.replace(",", ".")), 3)
    if amount <= 0:
        await update.message.reply_text("⚠️ Il valore deve essere maggiore di zero.")
        return ACState.ASK_AMOUNT
    if amount > MAX_CREDIT_PER_OP:
        await update.message.reply_text(f"L'importo massimo per singola operazione è {MAX_CREDIT_PER_OP:g} kWh.")
        return ACState.ASK_AMOUNT

    context.user_data['ac']['amount'] = amount
    _log_event("AC_AMOUNT_SET", amount=amount)
    
    # Build slot buttons dynamically from SLOTS env
    slot_buttons = []
    for slot in SLOTS[:3]:  # First 3 in a row
        slot_buttons.append(InlineKeyboardButton(slot.title(), callback_data=f"ACS:{slot}"))
    
    kb_rows = [slot_buttons]
    if len(SLOTS) > 3:
        more_buttons = []
        for slot in SLOTS[3:6]:  # Next 3 in another row
            more_buttons.append(InlineKeyboardButton(slot.title(), callback_data=f"ACS:{slot}"))
        if more_buttons:
            kb_rows.append(more_buttons)
    
    kb_rows.append([InlineKeyboardButton("Salta", callback_data="ACS:-")])
    kb = InlineKeyboardMarkup(kb_rows)
    
    await update.message.reply_text(
        f"Ok, accredito **{amount:g} kWh**.\nVuoi indicare lo slot?",
        reply_markup=kb
    )
    return ACState.ASK_SLOT

async def on_ac_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    slot = None
    if q.data.startswith("ACS:"):
        _s = q.data.split(":",1)[1]
        slot = None if _s == "-" else _s
    context.user_data['ac']['slot'] = slot
    _log_event("AC_SLOT_SET", slot=slot)

    data = context.user_data['ac']
    uid = data['user_id']; amount = data['amount']; slot = data.get('slot')
    text = f"Confermi l'accredito di **{amount:g} kWh** all'utente `{uid}`" + (f" (slot {slot})" if slot else "") + "?"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Conferma", callback_data="ACC:OK"),
         InlineKeyboardButton("❌ Annulla",  callback_data="ACC:NO")]
    ])
    await q.edit_message_text(text, reply_markup=kb)
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
        await q.edit_message_text("❗ Errore: limiti o saldo insufficiente.")
        return ConversationHandler.END

    name = await _get_user_name(uid)
    summary = (
        f"✅ *Accredito completato*\n\n"
        f"*Utente:* {name or uid}\n"
        f"*Quantità:* {amount:g} kWh{f' (slot {slot})' if slot else ''}\n\n"
        f"*Saldo prima:* {old_bal:.2f} kWh\n"
        f"*Saldo dopo:*  {new_bal:.2f} kWh"
    )
    await q.edit_message_text(summary)

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

async def on_ac_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not q.data.startswith("ACH:"):
        return ConversationHandler.END
    uid = int(q.data.split(":",1)[1])
    rows = await fetch_user_ops(uid, 10)
    _log_event("AC_HISTORY", user_id=uid, count=len(rows or []))
    if not rows:
        await q.edit_message_text("Nessuna operazione registrata per questo utente.")
        return ACState.SELECT_USER
    lines = ["📜 *Ultime 10 operazioni*",""]
    for created_at, delta, reason, slot, admin_id in rows:
        sign = "➕" if delta >= 0 else "➖"
        sslot = f" (slot {slot})" if slot else ""
        lines.append(f"{created_at} — {sign}{abs(delta):g} kWh • {reason}{sslot}")
    await q.edit_message_text("\n".join(lines))
    return ACState.SELECT_USER

# ====================
# ADMIN DEBIT FLOW (AD) - Existing admin functions
# ====================

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
        "Seleziona l'utente da addebitare:",
        reply_markup=build_users_kb(rows, 0, total)
    )
    return ADState.SELECT_USER

async def on_ad_users_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_admin(q.from_user.id):
        return ConversationHandler.END
    page = int(q.data.split(":",1)[1])
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
        f'Risultati per "{qtxt}":',
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
    uid = int(q.data.split(":",1)[1])
    context.user_data.setdefault('ad', {})['user_id'] = uid
    _log_event("AD_PICK_USER", admin=q.from_user.id, user_id=uid)

    await q.edit_message_text("✏️ Inserisci i kWh da addebitare (es. 10 o 15,345).")
    return ADState.ASK_AMOUNT

async def on_ad_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if not _is_number(txt):
        await update.message.reply_text("⚠️ Inserisci i kWh ricaricati (es. 10 o 15,345).")
        return ADState.ASK_AMOUNT
    amount = round(float(txt.replace(",", ".")), 3)
    if amount <= 0:
        await update.message.reply_text("⚠️ Il valore deve essere maggiore di zero.")
        return ADState.ASK_AMOUNT
    if amount > MAX_CREDIT_PER_OP:
        await update.message.reply_text(f"Massimo per singola operazione: {MAX_CREDIT_PER_OP:g}.")
        return ADState.ASK_AMOUNT

    context.user_data['ad']['amount'] = amount
    _log_event("AD_AMOUNT_SET", amount=amount)
    
    # Build slot buttons dynamically
    slot_buttons = []
    for slot in SLOTS[:3]:
        slot_buttons.append(InlineKeyboardButton(slot.title(), callback_data=f"ADS:{slot}"))
    
    kb_rows = [slot_buttons]
    if len(SLOTS) > 3:
        more_buttons = []
        for slot in SLOTS[3:6]:
            more_buttons.append(InlineKeyboardButton(slot.title(), callback_data=f"ADS:{slot}"))
        if more_buttons:
            kb_rows.append(more_buttons)
    
    kb_rows.append([InlineKeyboardButton("Salta", callback_data="ADS:-")])
    kb = InlineKeyboardMarkup(kb_rows)
    
    await update.message.reply_text(
        f"Ok, addebito **{amount:g} kWh**.\nVuoi indicare lo slot?",
        reply_markup=kb
    )
    return ADState.ASK_SLOT

async def on_ad_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    slot = None
    if q.data.startswith("ADS:"):
        _s = q.data.split(":",1)[1]
        slot = None if _s == "-" else _s
    context.user_data['ad']['slot'] = slot
    _log_event("AD_SLOT_SET", slot=slot)

    data = context.user_data['ad']
    uid = data['user_id']; amount = data['amount']; slot = data.get('slot')
    text = f"Confermi l'*addebito* di **{amount:g} kWh** all'utente `{uid}`" + (f" (slot {slot})" if slot else "") + "?"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Conferma", callback_data="ADD:OK"),
         InlineKeyboardButton("❌ Annulla",  callback_data="ADD:NO")]
    ])
    await q.edit_message_text(text, reply_markup=kb)
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
            await q.edit_message_text("❗ Errore: limiti o saldo insufficiente.")
        return ConversationHandler.END

    name = await _get_user_name(uid)
    summary = (
        f"✅ *Addebito completato*\n\n"
        f"*Utente:* {name or uid}\n"
        f"*Quantità:* {amount:g} kWh{f' (slot {slot})' if slot else ''}\n\n"
        f"*Saldo prima:* {old_bal:.2f} kWh\n"
        f"*Saldo dopo:*  {new_bal:.2f} kWh"
    )
    await q.edit_message_text(summary)

    try:
        tg = await get_tgid_by_userid(uid)
        if tg:
            await context.bot.send_message(
                chat_id=tg,
                text=f"⚠️ Ti sono stati *addebitati* {amount:g} kWh.\nSaldo: {old_bal:.2f} → {new_bal:.2f} kWh"
            )
    except Exception:
        pass
    _log_event("AD_DEBIT_OK", user_id=uid, amount=amount, old=old_bal, new=new_bal)

    return ConversationHandler.END

# ====================
# USER CREDIT REQUEST FLOW (CR) - NEW
# ====================

async def cmd_ricarica(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start credit request flow for users"""
    user_id = update.effective_user.id
    await ensure_user(user_id, update.effective_user.full_name)
    
    # Check pending requests limit
    pending_count = await count_user_pending_requests(user_id)
    if pending_count >= MAX_PENDING_REQUESTS:
        await update.message.reply_text(
            f"⚠️ Hai già {pending_count} richieste in attesa.\n"
            f"Massimo consentito: {MAX_PENDING_REQUESTS}.\n"
            f"Attendi che vengano elaborate prima di inviarne altre."
        )
        return ConversationHandler.END
    
    context.user_data['cr'] = {}
    _log_event("CR_START", user_id=user_id)
    
    # Build slot selection keyboard (3 per riga)
    rows = []
    row = []
    for slot in SLOTS:
        row.append(InlineKeyboardButton(slot.title(), callback_data=f"CRS:{slot}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if not rows:
        rows = [[InlineKeyboardButton("Wallet", callback_data="CRS:wallet")]]
    kb = InlineKeyboardMarkup(rows)
    
    await update.message.reply_text(
        "📋 *Richiesta di Ricarica*\n\n"
        "Seleziona lo slot da ricaricare:",
        reply_markup=kb
    )
    return CRState.ASK_SLOT

async def on_cr_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle slot selection"""
    q = update.callback_query
    await q.answer()
    
    slot = q.data.split(":",1)[1]
    context.user_data['cr']['slot'] = slot
    _log_event("CR_SLOT_SET", slot=slot)
    
    await q.edit_message_text(
        f"📍 Slot selezionato: *{slot}*\n\n"
        f"Inserisci i kWh da ricaricare (es. 10 o 15,345):"
    )
    return CRState.ASK_KWH

async def on_cr_kwh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle kWh input"""
    txt = (update.message.text or "").strip()
    if not _is_number(txt):
        await update.message.reply_text("⚠️ ⚠️ Inserisci i kWh ricaricati (es. 10 o 15,345).")
        return CRState.ASK_KWH
    
    kwh = round(float(txt.replace(",", ".")), 3)
    if kwh <= 0:
        await update.message.reply_text("⚠️ Il valore deve essere maggiore di zero.")
        return CRState.ASK_KWH
    
    context.user_data['cr']['kwh'] = kwh
    _log_event("CR_KWH_SET", kwh=kwh)
    
    await update.message.reply_text(
        f"⚡ kWh: *{kwh:g}*\n\n"
        f"📸 Invia ora la *foto* della ricarica come prova.\n"
        f"_(Obbligatorio)_"
    )
    return CRState.ASK_PHOTO

async def on_cr_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo upload"""
    if not update.message.photo:
        await update.message.reply_text("⚠️ Devi inviare una foto. Riprova.")
        return CRState.ASK_PHOTO
    
    # Download and save photo
    photo = update.message.photo[-1]  # Highest resolution
    file = await context.bot.get_file(photo.file_id)
    
    # Generate unique filename
    user_id = update.effective_user.id
    filename = f"{user_id}_{uuid.uuid4().hex[:8]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    photo_path = os.path.join(CREDIT_PHOTOS_PATH, filename)
    
    try:
        await file.download_to_drive(photo_path)
        context.user_data['cr']['photo_path'] = photo_path
        _log_event("CR_PHOTO_SAVED", path=photo_path)
    except Exception as e:
        log.exception("Failed to save photo: %s", e)
        await update.message.reply_text("❗ Errore nel salvataggio della foto. Riprova.")
        return CRState.ASK_PHOTO
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏩ Salta", callback_data="CRN:skip")]
    ])
    
    await update.message.reply_text(
        "✅ Foto ricevuta!\n\n"
        "📝 Vuoi aggiungere una nota opzionale?\n"
        "_(Scrivi la nota o premi Salta)_",
        reply_markup=kb
    )
    return CRState.ASK_NOTE

async def on_cr_skip_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle skip note button"""
    q = update.callback_query
    await q.answer()
    
    context.user_data['cr']['note'] = None
    
    # Show confirmation
    data = context.user_data['cr']
    slot = data['slot']
    kwh = data['kwh']
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Conferma", callback_data="CRC:OK"),
         InlineKeyboardButton("❌ Annulla", callback_data="CRC:NO")]
    ])
    
    await q.edit_message_text(
        f"📋 Riepilogo richiesta\n\n"
        f"📍 Slot: {slot}\n"
        f"⚡ kWh: {kwh:g}\n"
        f"📸 Foto: allegata\n"
        f"📝 Nota: _nessuna_\n\n"
        f"Confermi l'invio?",
        reply_markup=kb
    )
    return CRState.CONFIRM

async def on_cr_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle note input"""
    note = (update.message.text or "").strip()
    context.user_data['cr']['note'] = note if note else None
    
    # Show confirmation
    data = context.user_data['cr']
    slot = data['slot']
    kwh = data['kwh']
    note_text = note if note else "_nessuna_"
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Conferma", callback_data="CRC:OK"),
         InlineKeyboardButton("❌ Annulla", callback_data="CRC:NO")]
    ])
    
    await update.message.reply_text(
        f"📋 Riepilogo richiesta\n\n"
        f"📍 Slot: {slot}\n"
        f"⚡ kWh: {kwh:g}\n"
        f"📸 Foto: allegata\n"
        f"📝 Nota: {note_text}\n\n"
        f"Confermi l'invio?",
        reply_markup=kb
    )
    return CRState.CONFIRM

async def on_cr_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle confirmation"""
    q = update.callback_query
    await q.answer()
    
    if q.data == "CRC:NO":
        # Clean up photo if exists
        photo_path = context.user_data.get('cr', {}).get('photo_path')
        if photo_path and os.path.exists(photo_path):
            try:
                os.remove(photo_path)
            except Exception:
                pass
        
        await q.edit_message_text("❌ Richiesta annullata.")
        return ConversationHandler.END
    
    # Create credit request
    user_id = update.effective_user.id
    data = context.user_data['cr']
    slot = data['slot']
    kwh = data['kwh']
    photo_path = data.get('photo_path')
    note = data.get('note')
    
    try:
        request_id = await create_credit_request(user_id, slot, kwh, photo_path, note)
        _log_event("CR_CREATED", request_id=request_id, user_id=user_id, slot=slot, kwh=kwh)
        
        await q.edit_message_text(
            f"✅ *Richiesta inviata!*\n\n"
            f"📋 Richiesta #{request_id}\n"
            f"📍 Slot: {slot}\n"
            f"⚡ kWh: {kwh:g}\n\n"
            f"Ti avviseremo appena un amministratore la verifica.\n"
            f"Usa /pending per controllare lo stato."
        )
        
        # Notify admins
        await notify_admins(context, request_id, user_id, slot, kwh, photo_path, note)
        
    except Exception as e:
        log.exception("Failed to create credit request: %s", e)
        await q.edit_message_text("❗ Errore nella creazione della richiesta. Riprova più tardi.")
    
    return ConversationHandler.END

# ====================
# PHOTO WITH CAPTION HANDLER - NEW
# ====================

async def on_photo_with_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo messages with caption in format: slot3 4.5 [note]"""
    if not update.message.photo or not update.message.caption:
        return
    
    user_id = update.effective_user.id
    await ensure_user(user_id, update.effective_user.full_name)
    
    # Check pending limit
    pending_count = await count_user_pending_requests(user_id)
    if pending_count >= MAX_PENDING_REQUESTS:
        await update.message.reply_text(
            f"⚠️ Hai già {pending_count} richieste in attesa.\n"
            f"Massimo: {MAX_PENDING_REQUESTS}. Attendi l'elaborazione."
        )
        return
    
    # Parse caption
    caption = update.message.caption.strip()
    parts = caption.split(maxsplit=2)
    
    if len(parts) < 2:
        await update.message.reply_text(
            "⚠️ Formato non valido.\n"
            "Usa: `slot3 4.5` o `slot8 10 nota opzionale`"
        )
        return
    
    slot = parts[0].lower()
    kwh_str = parts[1].replace(",", ".")
    note = parts[2] if len(parts) > 2 else None
    
    # Validate slot
    if slot not in [s.lower() for s in SLOTS]:
        await update.message.reply_text(
            f"⚠️ Slot non valido: {slot}\n"
            f"Slot disponibili: {', '.join(SLOTS)}"
        )
        return
    
    # Validate kWh
    if not _is_number(kwh_str):
        await update.message.reply_text("⚠️ Quantità kWh non valida.")
        return
    
    kwh = round(float(kwh_str), 3)
    if kwh <= 0:
        await update.message.reply_text("⚠️ Il valore deve essere maggiore di zero.")
        return
    
    # Download photo
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    filename = f"{user_id}_{uuid.uuid4().hex[:8]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    photo_path = os.path.join(CREDIT_PHOTOS_PATH, filename)
    
    try:
        await file.download_to_drive(photo_path)
    except Exception as e:
        log.exception("Failed to save photo: %s", e)
        await update.message.reply_text("❗ Errore nel salvataggio della foto.")
        return
    
    # Create request
    try:
        request_id = await create_credit_request(user_id, slot, kwh, photo_path, note)
        _log_event("CR_CREATED_PHOTO", request_id=request_id, user_id=user_id, slot=slot, kwh=kwh)
        
        await update.message.reply_text(
            f"✅ *Richiesta inviata!*\n\n"
            f"📋 #{request_id}\n"
            f"📍 {slot} | ⚡ {kwh:g} kWh\n"
            f"{'📝 ' + note if note else ''}\n\n"
            f"Ti avviseremo dell'esito."
        )
        
        # Notify admins
        await notify_admins(context, request_id, user_id, slot, kwh, photo_path, note)
        
    except Exception as e:
        log.exception("Failed to create credit request from photo: %s", e)
        await update.message.reply_text("❗ Errore nella creazione della richiesta.")

# ====================
# CREDIT REQUEST APPROVAL/REJECTION CALLBACKS - NEW
# ====================

async def on_cr_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle credit request approval"""
    q = update.callback_query
    await q.answer()
    
    if not _is_admin(q.from_user.id):
        await q.answer("Non sei autorizzato.", show_alert=True)
        return
    
    request_id = int(q.data.split(":")[1])
    admin_id = q.from_user.id
    
    # Get request details before approval
    req = await get_credit_request(request_id)
    if not req:
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("⚠️ Richiesta non trovata.")
        return
    
    _, user_id, slot, kwh, photo_path, note, status, created_at, _, _ = req
    
    if status != 'pending':
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(f"⚠️ Richiesta già {status}.")
        return
    
    # Approve request
    success, details = await approve_credit_request(request_id, admin_id)
    
    if success:
        # Update message
        admin_name = q.from_user.full_name or f"Admin {admin_id}"
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(
            f"✅ *Richiesta #{request_id} APPROVATA*\n"
            f"da {admin_name}\n\n"
            f"{details}"
        )
        
        # Notify user
        await notify_user_request_result(context, user_id, True, kwh, slot, details)
    else:
        await q.answer(f"❌ Errore: {details}", show_alert=True)

async def on_cr_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle credit request rejection"""
    q = update.callback_query
    await q.answer()
    
    if not _is_admin(q.from_user.id):
        await q.answer("Non sei autorizzato.", show_alert=True)
        return
    
    request_id = int(q.data.split(":")[1])
    admin_id = q.from_user.id
    
    # Get request details
    req = await get_credit_request(request_id)
    if not req:
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("⚠️ Richiesta non trovata.")
        return
    
    _, user_id, slot, kwh, photo_path, note, status, created_at, _, _ = req
    
    if status != 'pending':
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(f"⚠️ Richiesta già {status}.")
        return
    
    # Reject request
    success, details = await reject_credit_request(request_id, admin_id, "Rejected by admin")
    
    if success:
        # Update message
        admin_name = q.from_user.full_name or f"Admin {admin_id}"
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(
            f"❌ *Richiesta #{request_id} RIFIUTATA*\n"
            f"da {admin_name}"
        )
        
        # Notify user
        await notify_user_request_result(context, user_id, False, kwh, slot, "")
    else:
        await q.answer(f"❌ Errore: {details}", show_alert=True)

# ====================
# MISC HANDLERS
# ====================

async def on_admin_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        _log_event("CMD_ADMIN_MENU", caller=update.effective_user.id)
        await update.message.reply_text("Pannello admin:", reply_markup=admin_home_kb())

async def on_allowneg_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_admin(q.from_user.id):
        await q.edit_message_text("Funzione riservata agli admin.")
        return
    _, payload = q.data.split("ALN_SET:",1)
    uid_str, mode = payload.split(":")
    uid = int(uid_str)

    target = None if mode=="default" else (mode=="on")
    ok = await set_user_allow_negative(uid, target)
    if not ok:
        await q.edit_message_text(f"Utente {uid} non trovato.")
        return
    kb = await build_user_admin_kb(uid)
    try:
        await q.edit_message_reply_markup(reply_markup=kb)
    except Exception:
        eff, source, user_override, g = await get_user_negative_policy(uid)
        src = "override UTENTE" if source=="USER" else "DEFAULT GLOBALE"
        await q.edit_message_text(
            f"Allow negative per {uid}: {'ON' if eff else 'OFF'} ({src}).",
            reply_markup=kb
        )

async def on_nop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

# ====================
# ERROR HANDLER
# ====================

async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    log.exception("GLOBAL_ERROR: %s", err)

# ====================
# APPLICATION BUILDER
# ====================

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
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("export_ops", cmd_export_ops))
    app.add_handler(CommandHandler("addebita", cmd_addebita))
    app.add_handler(CommandHandler("allow_negative", cmd_allow_negative))
    app.add_handler(CommandHandler("admin", on_admin_home))

    # Admin Credit flow (existing)
    ac_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_ac_start, pattern="^AC_START$")],
        states={
            ACState.SELECT_USER: [
                CallbackQueryHandler(on_ac_pick_user, pattern="^ACU:"),
                CallbackQueryHandler(on_ac_users_page, pattern="^ACP:\\d+$"),
                CallbackQueryHandler(on_ac_find_press, pattern="^AC_FIND$"),
                CallbackQueryHandler(on_ac_history, pattern="^ACH:\\d+$"),
            ],
            ACState.FIND_USER:   [MessageHandler(filters.TEXT & ~filters.COMMAND, on_ac_find_query)],
            ACState.ASK_AMOUNT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, on_ac_amount)],
            ACState.ASK_SLOT:    [CallbackQueryHandler(on_ac_slot, pattern="^ACS:")],
            ACState.CONFIRM:     [CallbackQueryHandler(on_ac_confirm, pattern="^ACC:(OK|NO)$")],
        },
        fallbacks=[],
        name="admin_credit_flow",
        persistent=False,
    )
    app.add_handler(ac_conv, group=0)

    # Admin Debit flow (existing)
    ad_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_ad_start, pattern="^AD_START$")],
        states={
            ADState.SELECT_USER: [
                CallbackQueryHandler(on_ad_pick_user, pattern="^(ACU|ADU):"),
                CallbackQueryHandler(on_ad_users_page, pattern="^ACP:\\d+$"),
                CallbackQueryHandler(on_ad_find_press, pattern="^AC_FIND$"),
            ],
            ADState.FIND_USER:   [MessageHandler(filters.TEXT & ~filters.COMMAND, on_ad_find_query)],
            ADState.ASK_AMOUNT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, on_ad_amount)],
            ADState.ASK_SLOT:    [CallbackQueryHandler(on_ad_slot, pattern="^ADS:")],
            ADState.CONFIRM:     [CallbackQueryHandler(on_ad_confirm, pattern="^ADD:(OK|NO)$")],
        },
        fallbacks=[],
        name="admin_debit_flow",
        persistent=False,
    )
    app.add_handler(ad_conv, group=0)

    # User Credit Request flow (NEW)
    cr_conv = ConversationHandler(
        entry_points=[CommandHandler("ricarica", cmd_ricarica)],
        states={
            CRState.ASK_SLOT:   [CallbackQueryHandler(on_cr_slot, pattern="^CRS:")],
            CRState.ASK_KWH:    [MessageHandler(filters.TEXT & ~filters.COMMAND, on_cr_kwh)],
            CRState.ASK_PHOTO:  [MessageHandler(filters.PHOTO, on_cr_photo)],
            CRState.ASK_NOTE:   [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_cr_note),
                CallbackQueryHandler(on_cr_skip_note, pattern="^CRN:skip$")
            ],
            CRState.CONFIRM:    [CallbackQueryHandler(on_cr_confirm, pattern="^CRC:(OK|NO)$")],
        },
        fallbacks=[],
        name="user_credit_request_flow",
        persistent=False,
    )
    app.add_handler(cr_conv, group=0)

    # Photo with caption handler (NEW)
    app.add_handler(MessageHandler(filters.PHOTO & filters.CAPTION, on_photo_with_caption), group=1)

    # Credit request approval/rejection callbacks (NEW)
    app.add_handler(CallbackQueryHandler(on_cr_approve, pattern="^CR_APPROVE:\\d+$"), group=0)
    app.add_handler(CallbackQueryHandler(on_cr_reject, pattern="^CR_REJECT:\\d+$"), group=0)

    # Inline misc
    app.add_handler(CallbackQueryHandler(on_allowneg_set, pattern="^ALN_SET:\\d+:(on|off|default)$"), group=0)
    app.add_handler(CallbackQueryHandler(on_ac_history, pattern="^ACH:\\d+$"), group=0)
    app.add_handler(CallbackQueryHandler(on_nop, pattern="^NOP$"), group=0)

    # Global error handler
    app.add_error_handler(handle_error)

    return app


# Alias for compatibility
create_application = build_application


if __name__ == "__main__":
    import sys
    log.info("Starting bot in polling mode for testing...")
    app = build_application()
    app.run_polling()
