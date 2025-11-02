# bot_slots_flow.py — extended functional bot for Railway (PTB 21.6)
import os
import pathlib
import sqlite3
import logging
from typing import Optional, Tuple, List

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot_slots_flow")

# ===================== DB LAYER (safe for Railway volumes) =====================
os.environ.setdefault("TMPDIR", "/var/data")
os.environ.setdefault("TEMP", "/var/data")
os.environ.setdefault("TMP", "/var/data")
os.environ.setdefault("SQLITE_TMPDIR", "/var/data")

DATA_DIR = "/var/data"
DEFAULT_DB_PATH = f"{DATA_DIR}/kwh_slots.db"
PHOTOS_DIR = f"{DATA_DIR}/photos"


def open_sqlite(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path,
        timeout=30,
        check_same_thread=False,
        isolation_level=None,
        cached_statements=0,
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

        dirpath = os.path.dirname(self.path) or DATA_DIR
        pathlib.Path(dirpath).mkdir(parents=True, exist_ok=True)
        pathlib.Path(PHOTOS_DIR).mkdir(parents=True, exist_ok=True)

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
                    chat_id INTEGER UNIQUE,
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
                # columns to help moderation
                try:
                    cur.execute("ALTER TABLE pending ADD COLUMN note TEXT;")
                except sqlite3.OperationalError:
                    pass
                try:
                    cur.execute("ALTER TABLE pending ADD COLUMN requested_by INTEGER;")
                except sqlite3.OperationalError:
                    pass
                try:
                    cur.execute("ALTER TABLE pending ADD COLUMN status TEXT DEFAULT 'pending';")
                except sqlite3.OperationalError:
                    pass
                try:
                    cur.execute("ALTER TABLE pending ADD COLUMN approved_by INTEGER;")
                except sqlite3.OperationalError:
                    pass

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
                    slot_type TEXT,
                    approved INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    approved_by INTEGER,
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

    def get_user_id(self, chat_id: int) -> Optional[int]:
        with self.conn as con:
            cur = con.cursor()
            cur.execute("SELECT id FROM users WHERE chat_id=?", (chat_id,))
            row = cur.fetchone()
            return row[0] if row else None

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

    def list_users(self, limit: int = 20) -> List[tuple]:
        with self.conn as con:
            cur = con.cursor()
            cur.execute("""
                SELECT id, chat_id, COALESCE(username,''), COALESCE(first_name,''), COALESCE(last_name,''),
                       slot1_kwh, slot3_kwh, slot5_kwh, slot8_kwh, wallet_kwh
                FROM users ORDER BY id DESC LIMIT ?
            """, (limit,))
            return cur.fetchall()

    def add_note(self, user_id: int, text: str):
        with self.conn as con:
            cur = con.cursor()
            cur.execute("INSERT INTO notes (user_id, text) VALUES (?,?)", (user_id, text))

    def add_pending_recharge(self, user_id: int, slot_type: str, kwh: float, photo_path: Optional[str], requested_by: int, note: str = "") -> int:
        with self.conn as con:
            cur = con.cursor()
            cur.execute("""
                INSERT INTO pending (user_id, slot_type, kwh, photo_path, requested_by, note, status)
                VALUES (?,?,?,?,?,?, 'pending')
            """, (user_id, slot_type, kwh, photo_path, requested_by, note))
            return cur.lastrowid

    def list_pending(self) -> List[tuple]:
        with self.conn as con:
            cur = con.cursor()
            cur.execute("""
                SELECT id, user_id, slot_type, kwh, COALESCE(photo_path,''), COALESCE(note,''), COALESCE(status,'pending'), created_at
                FROM pending WHERE status='pending' ORDER BY id ASC
            """)
            return cur.fetchall()

    def approve_pending(self, pending_id: int, admin_id: int) -> Optional[tuple]:
        """Apply pending recharge to user balance and mark approved. Returns (user_id, slot, kwh)"""
        with self.conn as con:
            cur = con.cursor()
            cur.execute("SELECT user_id, slot_type, kwh FROM pending WHERE id=? AND status='pending'", (pending_id,))
            row = cur.fetchone()
            if not row:
                return None
            user_id, slot, kwh = row
            # update balances
            if slot == "wallet":
                cur.execute("UPDATE users SET wallet_kwh = wallet_kwh + ?, updated_at=datetime('now') WHERE id=?", (kwh, user_id))
            elif slot in ("slot1","slot3","slot5","slot8"):
                cur.execute(f"UPDATE users SET {slot} = {slot} + ?, updated_at=datetime('now') WHERE id=?", (kwh, user_id))
            else:
                return None
            # mark approved
            cur.execute("UPDATE pending SET status='approved', approved_by=? WHERE id=?", (admin_id, pending_id))
            cur.execute("INSERT INTO recharges (user_id, amount, slot_type, approved, approved_by) VALUES (?,?,?,1,?)",
                        (user_id, kwh, slot, admin_id))
            return (user_id, slot, kwh)

    def reject_pending(self, pending_id: int, admin_id: int):
        with self.conn as con:
            cur = con.cursor()
            cur.execute("UPDATE pending SET status='rejected', approved_by=? WHERE id=?", (admin_id, pending_id))

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

# ===================== Helpers & security =====================
def _fmt_name(u: Update):
    chat_id = u.effective_chat.id if u.effective_chat else 0
    user = u.effective_user
    username = (user.username or "") if user else ""
    first_name = (user.first_name or "") if user else ""
    last_name = (user.last_name or "") if user else ""
    return chat_id, username, first_name, last_name

def _is_admin(chat_id: int) -> bool:
    ids = [x.strip() for x in os.environ.get("ADMIN_IDS","").split(",") if x.strip()]
    try:
        ids = set(int(x) for x in ids)
    except ValueError:
        ids = set()
    return chat_id in ids

# ===================== Handlers =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, username, first_name, last_name = _fmt_name(update)
    DBI.get_or_create_user(chat_id, username, first_name, last_name)
    if update.message:
        await update.message.reply_text("Ciao! ✅ Bot attivo. Inviami un comando o un messaggio.\n"
                                        "Comandi: /saldo, /ricarica, /help")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *Comandi disponibili*\n"
        "• /saldo — mostra i tuoi kWh\n"
        "• /ricarica `<slot1|slot3|slot5|slot8|wallet>` `<kwh>` — invia richiesta\n"
        "• Invia *foto* con didascalia: `slot3 4.5` per allegare prova\n\n"
        "👮 *Admin*:\n"
        "• /pending — elenca richieste in attesa\n"
        "• /approve `<id>` — approva richiesta\n"
        "• /reject `<id>` — rifiuta richiesta\n"
        "• /users — ultimi utenti con saldi\n"
        "• /credita `<chat_id>` `<slot>` `<kwh>` — accredito manuale",
        parse_mode="Markdown"
    )

async def cmd_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, username, first_name, last_name = _fmt_name(update)
    DBI.get_or_create_user(chat_id, username, first_name, last_name)
    s1, s3, s5, s8, wal = DBI.get_balance_summary(chat_id)
    msg = (
        "📊 *Situazione KWh*\n"
        f"• Slot1: *{s1}*\n"
        f"• Slot3: *{s3}*\n"
        f"• Slot5: *{s5}*\n"
        f"• Slot8: *{s8}*\n"
        f"• Wallet: *{wal}*"
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

    pid = DBI.add_pending_recharge(user_id, slot, kwh, None, requested_by=chat_id)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approva", callback_data=f"approve:{pid}"),
        InlineKeyboardButton("❌ Rifiuta", callback_data=f"reject:{pid}")
    ]]) if _is_admin(chat_id) else None

    await update.message.reply_text(
        f"Richiesta ricarica creata (ID *{pid}*): `{slot}` +*{kwh}* kWh.\nIn attesa di approvazione.",
        parse_mode="Markdown",
        reply_markup=kb
    )

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Foto con didascalia 'slot3 4.5' → crea pending con foto salvata su volume."""
    chat_id, username, first_name, last_name = _fmt_name(update)
    user_id = DBI.get_or_create_user(chat_id, username, first_name, last_name)

    photo = update.message.photo[-1] if update.message and update.message.photo else None
    caption = (update.message.caption or "").strip() if update.message else ""
    if not photo or not caption:
        return

    parts = caption.split()
    if len(parts) != 2:
        await update.message.reply_text("Didascalia non valida. Usa: `slot3 4.5`", parse_mode="Markdown")
        return
    slot = parts[0].lower()
    try:
        kwh = float(parts[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("KWh non valido nella didascalia.")
        return
    if slot not in ("slot1","slot3","slot5","slot8","wallet"):
        await update.message.reply_text("Slot non valido nella didascalia.")
        return

    file_id = photo.file_id
    file = await context.bot.get_file(file_id)
    local_path = f"{PHOTOS_DIR}/{file_id}.jpg"
    await file.download_to_drive(local_path)

    pid = DBI.add_pending_recharge(user_id, slot, kwh, local_path, requested_by=chat_id, note="foto")
    await update.message.reply_text(
        f"📎 Ricevuta registrata. Richiesta *{pid}*: `{slot}` +*{kwh}* kWh (con foto).",
        parse_mode="Markdown"
    )

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # qualunque testo viene salvato come nota
    chat_id, username, first_name, last_name = _fmt_name(update)
    user_id = DBI.get_or_create_user(chat_id, username, first_name, last_name)
    text = update.message.text if update.message else ""
    if text.startswith("/"):
        return  # ignora comandi non gestiti qui
    DBI.add_note(user_id, text)
    await update.message.reply_text("📝 Nota salvata. (Digita /saldo per vedere i tuoi kWh)")

# ---------------- Admin ----------------
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("⛔ Solo admin.")
        return
    rows = DBI.list_users(limit=30)
    if not rows:
        await update.message.reply_text("Nessun utente.")
        return
    lines = []
    for r in rows:
        uid, cid, un, fn, ln, s1, s3, s5, s8, wal = r
        name = un or (fn + " " + ln).strip() or str(cid)
        lines.append(f"• #{uid} {name} — s1:{s1} s3:{s3} s5:{s5} s8:{s8} wal:{wal}")
    await update.message.reply_text("👥 *Utenti*\n" + "\n".join(lines), parse_mode="Markdown")

async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("⛔ Solo admin.")
        return
    rows = DBI.list_pending()
    if not rows:
        await update.message.reply_text("Nessuna richiesta in attesa.")
        return
    lines = []
    kb = []
    for r in rows:
        pid, user_id, slot, kwh, path, note, status, created = r
        lines.append(f"• ID {pid}: user#{user_id} {slot}+{kwh} kWh [{status}]")
        kb.append([
            InlineKeyboardButton(f"✅ Approva {pid}", callback_data=f"approve:{pid}"),
            InlineKeyboardButton(f"❌ Rifiuta {pid}", callback_data=f"reject:{pid}")
        ])
    await update.message.reply_text("🕘 *Pending*\n" + "\n".join(lines), parse_mode="Markdown",
                                    reply_markup=InlineKeyboardMarkup(kb))

async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("⛔ Solo admin.")
        return
    args = context.args or []
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text("Usa: /approve <id>")
        return
    pid = int(args[0])
    res = DBI.approve_pending(pid, admin_id=chat_id)
    if not res:
        await update.message.reply_text("Richiesta non trovata o già gestita.")
        return
    user_id, slot, kwh = res
    await update.message.reply_text(f"✅ Approvata ID {pid}: user#{user_id} {slot}+{kwh} kWh")

async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("⛔ Solo admin.")
        return
    args = context.args or []
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text("Usa: /reject <id>")
        return
    pid = int(args[0])
    DBI.reject_pending(pid, admin_id=chat_id)
    await update.message.reply_text(f"❌ Rifiutata ID {pid}")

async def cmd_credita(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("⛔ Solo admin.")
        return
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_text("Usa: /credita <chat_id> <slot1|slot3|slot5|slot8|wallet> <kwh> [nota]")
        return
    try:
        target = int(args[0])
    except ValueError:
        await update.message.reply_text("chat_id non valido.")
        return
    slot = args[1].lower()
    try:
        kwh = float(args[2].replace(",", "."))
    except ValueError:
        await update.message.reply_text("kwh non valido.")
        return
    note = " ".join(args[3:]) if len(args) > 3 else "manuale"

    uid = DBI.get_user_id(target)
    if uid is None:
        # crea utente se manca
        uid = DBI.get_or_create_user(target, "", "", "")
    # crea voce pending e approva subito
    pid = DBI.add_pending_recharge(uid, slot, kwh, None, requested_by=chat_id, note=note)
    res = DBI.approve_pending(pid, admin_id=chat_id)
    if res:
        await update.message.reply_text(f"✅ Accreditati {kwh} kWh su {slot} per chat_id {target}")
    else:
        await update.message.reply_text("Errore in accredito.")

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    data = update.callback_query.data or ""
    if not data or ":" not in data:
        await update.callback_query.answer()
        return
    action, pid_s = data.split(":", 1)
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.callback_query.answer("Solo admin", show_alert=True)
        return
    if not pid_s.isdigit():
        await update.callback_query.answer("ID non valido", show_alert=True)
        return
    pid = int(pid_s)
    if action == "approve":
        res = DBI.approve_pending(pid, admin_id=chat_id)
        if res:
            await update.callback_query.answer("Approvata ✅", show_alert=True)
        else:
            await update.callback_query.answer("Già gestita/inesistente", show_alert=True)
    elif action == "reject":
        DBI.reject_pending(pid, admin_id=chat_id)
        await update.callback_query.answer("Rifiutata ❌", show_alert=True)

# ===================== Application builder (used by FastAPI) =====================
def build_application():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("Missing TELEGRAM_TOKEN")

    app = Application.builder().token(token).build()

    # Utente
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("saldo", cmd_saldo))
    app.add_handler(CommandHandler("ricarica", cmd_ricarica))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    # Admin
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("credita", cmd_credita))
    app.add_handler(CallbackQueryHandler(on_callback))

    log.info("Handlers registered: /start /help /saldo /ricarica [PHOTO] text, admin /users /pending /approve /reject /credita")
    return app
