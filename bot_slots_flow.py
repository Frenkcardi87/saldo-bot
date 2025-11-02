# bot_slots_flow.py — full minimal working version for Railway (PTB 21.6 + SQLite on /var/data)
import os
import pathlib
import sqlite3
import logging
from typing import Optional, Tuple

# Telegram types for handlers
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

log = logging.getLogger(__name__)

# -------------------- DB LAYER (safe for Railway volumes) --------------------
os.environ.setdefault("TMPDIR", "/var/data")
os.environ.setdefault("TEMP", "/var/data")
os.environ.setdefault("TMP", "/var/data")
os.environ.setdefault("SQLITE_TMPDIR", "/var/data")

DEFAULT_DB_PATH = "/var/data/kwh_slots.db"


def open_sqlite(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path,
        timeout=30,
        check_same_thread=False,
        isolation_level=None,
        cached_statements=0
    )
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=OFF;")
    cur.execute("PRAGMA synchronous=OFF;")
    cur.execute("PRAGMA temp_store=MEMORY;")
    cur.execute("PRAGMA mmap_size=0;")
    cur.execute("PRAGMA locking_mode=NORMAL;")
    return conn


class DB:
    def __init__(self, db_path_env: Optional[str]):
        p = (db_path_env or "").strip()
        self.path = p if p else DEFAULT_DB_PATH

        dirpath = os.path.dirname(self.path) or "/var/data"
        pathlib.Path(dirpath).mkdir(parents=True, exist_ok=True)

        # write test
        testfile = os.path.join(dirpath, ".rw_test")
        try:
            with open(testfile, "w") as f:
                f.write("ok")
            os.remove(testfile)
            log.info("DB dir write test OK on %s", dirpath)
        except Exception:
            log.exception("DB dir write test FAILED on %s", dirpath)

        self.conn: sqlite3.Connection = open_sqlite(self.path)
        self._init_db()

    def _init_db(self):
        try:
            with self.conn as con:
                cur = con.cursor()
                cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    chat_id INTEGER,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    approved INTEGER DEFAULT 1,
                    slot1_kwh REAL DEFAULT 0,
                    slot3_kwh REAL DEFAULT 0,
                    slot5_kwh REAL DEFAULT 0,
                    slot8_kwh REAL DEFAULT 0,
                    wallet_kwh REAL DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS pending (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    slot_type TEXT,
                    kwh REAL,
                    photo_path TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    text TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS recharges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    amount REAL,
                    approved INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS admin_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER,
                    action TEXT,
                    target_user_id INTEGER,
                    note TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.close()
            log.info("DB init OK on %s", self.path)
        except sqlite3.OperationalError:
            log.exception('DB init failed at %s', self.path)
            raise

    # ----------------- Convenience methods -----------------
    def get_or_create_user(self, chat_id: int, username: str, first_name: str, last_name: str) -> int:
        with self.conn as con:
            cur = con.cursor()
            cur.execute("SELECT id FROM users WHERE chat_id=?", (chat_id,))
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute("""
                INSERT INTO users (chat_id, username, first_name, last_name, approved, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, datetime('now'), datetime('now'))
            """, (chat_id, username, first_name, last_name))
            return cur.lastrowid

    def get_balance_summary(self, chat_id: int) -> Tuple[float, float, float, float, float]:
        with self.conn as con:
            cur = con.cursor()
            cur.execute("""
                SELECT slot1_kwh, slot3_kwh, slot5_kwh, slot8_kwh, wallet_kwh
                FROM users WHERE chat_id=?
            """, (chat_id,))
            row = cur.fetchone()
            if not row:
                return (0,0,0,0,0)
            return tuple(row)

    def add_note(self, user_id: int, text: str):
        with self.conn as con:
            cur = con.cursor()
            cur.execute("INSERT INTO notes (user_id, text) VALUES (?,?)", (user_id, text))

    def add_pending_recharge(self, user_id: int, slot_type: str, kwh: float):
        with self.conn as con:
            cur = con.cursor()
            cur.execute("""
                INSERT INTO pending (user_id, slot_type, kwh) VALUES (?,?,?)
            """, (user_id, slot_type, kwh))

# Create DB instance at import
def _init_db_instance() -> DB:
    DB_PATH = os.environ.get("DB_PATH", DEFAULT_DB_PATH).strip()
    try:
        db = DB(DB_PATH)
        return db
    except Exception:
        log.exception("DB init failed (path=%s). Falling back to in-memory for boot.", DB_PATH)
        mem = DB(":memory:")
        return mem

DBI = _init_db_instance()

# -------------------- BOT HANDLERS (minimal set) --------------------

def _fmt_name(u: Update):
    chat_id = u.effective_chat.id if u.effective_chat else 0
    user = u.effective_user
    username = (user.username or "") if user else ""
    first_name = (user.first_name or "") if user else ""
    last_name = (user.last_name or "") if user else ""
    return chat_id, username, first_name, last_name


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, username, first_name, last_name = _fmt_name(update)
    DBI.get_or_create_user(chat_id, username, first_name, last_name)
    if update.message:
        await update.message.reply_text("Ciao! ✅ Bot attivo. Inviami un comando o un messaggio.")


async def cmd_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, username, first_name, last_name = _fmt_name(update)
    DBI.get_or_create_user(chat_id, username, first_name, last_name)
    s1, s3, s5, s8, wal = DBI.get_balance_summary(chat_id)
    msg = (
        "📊 *Situazione KWh*\n"
        f"• Slot1: `{s1}`\n"
        f"• Slot3: `{s3}`\n"
        f"• Slot5: `{s5}`\n"
        f"• Slot8: `{s8}`\n"
        f"• Wallet: `{wal}`"
    )
    if update.message:
        await update.message.reply_markdown(msg)


async def cmd_ricarica(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, username, first_name, last_name = _fmt_name(update)
    user_id = DBI.get_or_create_user(chat_id, username, first_name, last_name)

    # formato: /ricarica <slot> <kwh>
    args = context.args or []
    if len(args) != 2:
        await update.message.reply_text("Usa: /ricarica <slot1|slot3|slot5|slot8|wallet> <kwh>")
        return
    slot = args[0].lower().strip()
    try:
        kwh = float(args[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("KWh non valido. Esempio: /ricarica slot3 4.5")
        return

    if slot not in ("slot1","slot3","slot5","slot8","wallet"):
        await update.message.reply_text("Slot non valido. Usa: slot1, slot3, slot5, slot8, wallet")
        return

    DBI.add_pending_recharge(user_id, slot, kwh)
    await update.message.reply_text(f"Richiesta ricarica creata: {slot} +{kwh} kWh (in attesa di approvazione).")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # qualunque testo viene salvato come nota
    chat_id, username, first_name, last_name = _fmt_name(update)
    user_id = DBI.get_or_create_user(chat_id, username, first_name, last_name)
    text = update.message.text if update.message else ""
    if text.startswith("/"):
        return  # ignora comandi non gestiti
    DBI.add_note(user_id, text)
    await update.message.reply_text("📝 Nota salvata. (Digita /saldo per vedere i tuoi kWh)")


# -------------------- Application builder used by serve_bot_webhook --------------------
def build_application():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("Missing TELEGRAM_TOKEN")

    app = Application.builder().token(token).build()

    # registra i tuoi handler
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("saldo", cmd_saldo))
    app.add_handler(CommandHandler("ricarica", cmd_ricarica))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    return app

# ==================== HANDLERS MINIMI + build_application COMPLETA ====================
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import os

def _fmt_name(u: Update):
    chat_id = u.effective_chat.id if u.effective_chat else 0
    user = u.effective_user
    username = (user.username or "") if user else ""
    first_name = (user.first_name or "") if user else ""
    last_name = (user.last_name or "") if user else ""
    return chat_id, username, first_name, last_name

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, username, first_name, last_name = _fmt_name(update)
    DBI.get_or_create_user(chat_id, username, first_name, last_name)
    if update.message:
        await update.message.reply_text("Ciao! ✅ Bot attivo. Inviami un comando o un messaggio.")

async def cmd_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, username, first_name, last_name = _fmt_name(update)
    DBI.get_or_create_user(chat_id, username, first_name, last_name)
    s1, s3, s5, s8, wal = DBI.get_balance_summary(chat_id)
    msg = (
        "📊 *Situazione KWh*\n"
        f"• Slot1: `{s1}`\n"
        f"• Slot3: `{s3}`\n"
        f"• Slot5: `{s5}`\n"
        f"• Slot8: `{s8}`\n"
        f"• Wallet: `{wal}`"
    )
    if update.message:
        await update.message.reply_markdown(msg)

async def cmd_ricarica(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, username, first_name, last_name = _fmt_name(update)
    user_id = DBI.get_or_create_user(chat_id, username, first_name, last_name)

    args = context.args or []
    if len(args) != 2:
        await update.message.reply_text("Usa: /ricarica <slot1|slot3|slot5|slot8|wallet> <kwh>")
        return
    slot = args[0].lower().strip()
    try:
        kwh = float(args[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("KWh non valido. Esempio: /ricarica slot3 4.5")
        return
    if slot not in ("slot1","slot3","slot5","slot8","wallet"):
        await update.message.reply_text("Slot non valido. Usa: slot1, slot3, slot5, slot8, wallet")
        return

    DBI.add_pending_recharge(user_id, slot, kwh)
    await update.message.reply_text(f"Richiesta ricarica creata: {slot} +{kwh} kWh (in attesa di approvazione).")

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, username, first_name, last_name = _fmt_name(update)
    user_id = DBI.get_or_create_user(chat_id, username, first_name, last_name)
    text = update.message.text if update.message else ""
    if text.startswith("/"):
        return
    DBI.add_note(user_id, text)
    await update.message.reply_text("📝 Nota salvata. (Digita /saldo per vedere i tuoi kWh)")

def build_application():
    """Restituisce l'Application PTB con gli handler registrati."""
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("Missing TELEGRAM_TOKEN")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("saldo", cmd_saldo))
    app.add_handler(CommandHandler("ricarica", cmd_ricarica))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return app
# ==================== FINE BLOCCO ====================
