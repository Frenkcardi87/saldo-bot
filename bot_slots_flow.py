# bot_slots_flow.py — full bot with wizard recharge, persistent keyboard, admin flow
import os
import pathlib
import sqlite3
import logging
from typing import Optional, Tuple, List

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
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
                # columns for moderation
                for stmt in [
                    "ALTER TABLE pending ADD COLUMN note TEXT;",
                    "ALTER TABLE pending ADD COLUMN requested_by INTEGER;",
                    "ALTER TABLE pending ADD COLUMN status TEXT DEFAULT 'pending';",
                    "ALTER TABLE pending ADD COLUMN approved_by INTEGER;",
                ]:
                    try:
                        cur.execute(stmt)
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
            cur.execute(
                """
                INSERT INTO pending (user_id, slot_type, kwh, photo_path, requested_by, note, status)
                VALUES (?,?,?,?,?,?, 'pending')
                """,
                (user_id, slot_type, kwh, photo_path, requested_by, note)
            )
            return cur.lastrowid

    def list_pending(self) -> List[tuple]:
        with self.conn as con:
            cur = con.cursor()
            cur.execute(
                """
                SELECT id, user_id, slot_type, kwh, COALESCE(photo_path,''), COALESCE(note,''), COALESCE(status,'pending'), created_at
                FROM pending WHERE status='pending' ORDER BY id ASC
                """
            )
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
                cur.execute(f"UPDATE users SET {slot} = {slot} + ?, updated_at=datetime('now') WHERE id=", (kwh, user_id))
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

def _admin_ids():
    ids = [x.strip() for x in os.environ.get("ADMIN_IDS","").split(",") if x.strip()]
    try:
        return [int(x) for x in ids]
    except ValueError:
        return []

# ---------- Reply Keyboard (sempre visibile) ----------
def main_keyboard():
    rows = [
        [KeyboardButton("+ Ricarica")],
        [KeyboardButton("/saldo"), KeyboardButton("/annulla")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)

# ===================== User Handlers (base) =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, username, first_name, last_name = _fmt_name(update)
    DBI.get_or_create_user(chat_id, username, first_name, last_name)
    if update.message:
        await update.message.reply_text(
            "Ciao! ✅ Bot attivo. Inviami un comando o un messaggio.\n"
            "Comandi: /saldo, /ricarica, /help",
            reply_markup=main_keyboard()
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *Comandi disponibili*\n"
        "• /saldo — mostra i tuoi kWh\n"
        "• /ricarica `<slot1|slot3|slot5|slot8|wallet>` `<kwh>` — invia richiesta (scorciatoia)\n"
        "• *Wizard ricarica*: premi “+ Ricarica” e segui i passi\n"
        "• Invia *foto* con didascalia: `slot3 4.5` per allegare prova\n\n"
        "👮 *Admin*:\n"
        "• /pending — elenca richieste in attesa\n"
        "• /approve `<id>` — approva richiesta\n"
        "• /reject `<id>` — rifiuta richiesta\n"
        "• /users — ultimi utenti con saldi\n"
        "• /credita `<chat_id>` `<slot>` `<kwh>` — accredito manuale",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
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
        await update.message.reply_markdown(msg, reply_markup=main_keyboard())

# Scorciatoia /ricarica <slot> <kwh>
async def cmd_ricarica(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, username, first_name, last_name = _fmt_name(update)
    user_id = DBI.get_or_create_user(chat_id, username, first_name, last_name)

    args = context.args or []
    if len(args) != 2:
        await update.message.reply_text("Usa: /ricarica <slot1|slot3|slot5|slot8|wallet> <kwh> oppure premi “+ Ricarica”.",
                                        reply_markup=main_keyboard())
        return
    slot = args[0].lower().strip()
    try:
        kwh = float(args[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("KWh non valido. Esempio: /ricarica slot3 4.5", reply_markup=main_keyboard())
        return
    if slot not in ("slot1","slot3","slot5","slot8","wallet"):
        await update.message.reply_text("Slot non valido. Usa: slot1, slot3, slot5, slot8, wallet", reply_markup=main_keyboard())
        return

    pid = DBI.add_pending_recharge(user_id, slot, kwh, None, requested_by=chat_id)
    await update.message.reply_text(
        f"Richiesta ricarica creata (ID *{pid}*): `{slot}` +*{kwh}* kWh.\nIn attesa di approvazione.",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )
    await _notify_admins_new_pending(context, pid, user_id, slot, kwh, note="", photo_path=None)

# Foto con didascalia “slot3 4.5” (fallback)
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, username, first_name, last_name = _fmt_name(update)
    user_id = DBI.get_or_create_user(chat_id, username, first_name, last_name)

    photo = update.message.photo[-1] if update.message and update.message.photo else None
    caption = (update.message.caption or "").strip() if update.message else ""
    if not photo or not caption:
        return

    parts = caption.split()
    if len(parts) != 2:
        await update.message.reply_text("Didascalia non valida. Usa: `slot3 4.5`", parse_mode="Markdown",
                                        reply_markup=main_keyboard())
        return
    slot = parts[0].lower()
    try:
        kwh = float(parts[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("KWh non valido nella didascalia.", reply_markup=main_keyboard())
        return
    if slot not in ("slot1","slot3","slot5","slot8","wallet"):
        await update.message.reply_text("Slot non valido nella didascalia.", reply_markup=main_keyboard())
        return

    file_id = photo.file_id
    file = await context.bot.get_file(file_id)
    local_path = f"{PHOTOS_DIR}/{file_id}.jpg"
    await file.download_to_drive(local_path)

    pid = DBI.add_pending_recharge(user_id, slot, kwh, local_path, requested_by=chat_id, note="foto")
    await update.message.reply_text(
        f"📎 Ricevuta registrata. Richiesta *{pid}*: `{slot}` +*{kwh}* kWh (con foto).",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )
    await _notify_admins_new_pending(context, pid, user_id, slot, kwh, note="foto", photo_path=local_path)

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, username, first_name, last_name = _fmt_name(update)
    user_id = DBI.get_or_create_user(chat_id, username, first_name, last_name)
    text = update.message.text if update.message else ""
    if text.startswith("/"):
        return
    DBI.add_note(user_id, text)
    await update.message.reply_text("📝 Nota salvata. (Digita /saldo per vedere i tuoi kWh)",
                                    reply_markup=main_keyboard())

# ===================== Admin Handlers =====================
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("⛔ Solo admin.", reply_markup=main_keyboard())
        return
    rows = DBI.list_users(limit=30)
    if not rows:
        await update.message.reply_text("Nessun utente.", reply_markup=main_keyboard())
        return
    lines = []
    for r in rows:
        uid, cid, un, fn, ln, s1, s3, s5, s8, wal = r
        name = un or (fn + " " + ln).strip() or str(cid)
        lines.append(f"• #{uid} {name} — s1:{s1} s3:{s3} s5:{s5} s8:{s8} wal:{wal}")
    await update.message.reply_text("👥 *Utenti*\n" + "\n".join(lines), parse_mode="Markdown", reply_markup=main_keyboard())

async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("⛔ Solo admin.", reply_markup=main_keyboard())
        return
    rows = DBI.list_pending()
    if not rows:
        await update.message.reply_text("Nessuna richiesta in attesa.", reply_markup=main_keyboard())
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
        await update.message.reply_text("⛔ Solo admin.", reply_markup=main_keyboard())
        return
    args = context.args or []
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text("Usa: /approve <id>", reply_markup=main_keyboard())
        return
    pid = int(args[0])
    res = DBI.approve_pending(pid, admin_id=chat_id)
    if not res:
        await update.message.reply_text("Richiesta non trovata o già gestita.", reply_markup=main_keyboard())
        return
    user_id, slot, kwh = res
    await update.message.reply_text(f"✅ Approvata ID {pid}: user#{user_id} {slot}+{kwh} kWh", reply_markup=main_keyboard())

async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("⛔ Solo admin.", reply_markup=main_keyboard())
        return
    args = context.args or []
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text("Usa: /reject <id>", reply_markup=main_keyboard())
        return
    pid = int(args[0])
    DBI.reject_pending(pid, admin_id=chat_id)
    await update.message.reply_text(f"❌ Rifiutata ID {pid}", reply_markup=main_keyboard())

async def cmd_credita(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("⛔ Solo admin.", reply_markup=main_keyboard())
        return
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_text("Usa: /credita <chat_id> <slot1|slot3|slot5|slot8|wallet> <kwh> [nota]", reply_markup=main_keyboard())
        return
    try:
        target = int(args[0])
    except ValueError:
        await update.message.reply_text("chat_id non valido.", reply_markup=main_keyboard())
        return
    slot = args[1].lower()
    try:
        kwh = float(args[2].replace(",", "."))
    except ValueError:
        await update.message.reply_text("kwh non valido.", reply_markup=main_keyboard())
        return
    note = " ".join(args[3:]) if len(args) > 3 else "manuale"

    uid = DBI.get_user_id(target)
    if uid is None:
        uid = DBI.get_or_create_user(target, "", "", "")
    pid = DBI.add_pending_recharge(uid, slot, kwh, None, requested_by=chat_id, note=note)
    res = DBI.approve_pending(pid, admin_id=chat_id)
    if res:
        await update.message.reply_text(f"✅ Accreditati {kwh} kWh su {slot} per chat_id {target}", reply_markup=main_keyboard())
    else:
        await update.message.reply_text("Errore in accredito.", reply_markup=main_keyboard())

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

# ===================== Wizard Ricarica =====================
WZ_KEY = "ricarica_wz"      # chiave in user_data
WZ_EXPECT = "expect"        # 'slot' | 'kwh' | 'photo' | 'choose_action' | 'note'
WZ_DATA = "data"            # dict: {'slot':..., 'kwh':..., 'photo_path':..., 'note':...}

def _wz_reset(context):
    context.user_data.pop(WZ_KEY, None)

def _wz_start(context):
    context.user_data[WZ_KEY] = {WZ_EXPECT: "slot", WZ_DATA: {}}

def _wz(context):
    return context.user_data.get(WZ_KEY, None)

async def _notify_admins_new_pending(context: ContextTypes.DEFAULT_TYPE, pid: int, user_id: int, slot: str, kwh: float, note: str, photo_path: str|None):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"✅ Approva {pid}", callback_data=f"approve:{pid}"),
            InlineKeyboardButton(f"❌ Rifiuta {pid}", callback_data=f"reject:{pid}"),
        ]
    ])
    txt = (
        f"🆕 *Dichiarazione ricarica*\n"
        f"• ID: *{pid}*\n"
        f"• user_id: *{user_id}*\n"
        f"• {slot} +*{kwh}* kWh\n"
        + (f"• Nota: _{note}_\n" if note else "")
    )
    for admin in _admin_ids():
        try:
            if photo_path and os.path.exists(photo_path):
                with open(photo_path, "rb") as f:
                    await context.bot.send_photo(admin, f, caption=txt, parse_mode="Markdown", reply_markup=kb)
            else:
                await context.bot.send_message(admin, txt, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            log.exception("Notify admin failed: %s", admin)

async def wizard_ricarica_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and context.args:
        return await cmd_ricarica(update, context)

    _wz_start(context)
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Slot1", callback_data="slot:slot1"),
            InlineKeyboardButton("Slot3", callback_data="slot:slot3"),
        ],
        [
            InlineKeyboardButton("Slot5", callback_data="slot:slot5"),
            InlineKeyboardButton("Slot8", callback_data="slot:slot8"),
        ],
        [InlineKeyboardButton("Wallet", callback_data="slot:wallet")]
    ])
    await update.message.reply_text(
        "Seleziona lo *slot* da ricaricare:",
        parse_mode="Markdown",
        reply_markup=kb
    )

async def wizard_choose_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    data = update.callback_query.data or ""
    if not data.startswith("slot:"):
        return
    slot = data.split(":",1)[1]
    wz = _wz(context)
    if not wz:
        await update.callback_query.answer("Sessione scaduta. Premi + Ricarica.", show_alert=True)
        return
    wz[WZ_DATA]["slot"] = slot
    wz[WZ_EXPECT] = "kwh"
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        f"Hai scelto *{slot}*.\nOra *inserisci i kWh* (es: 4.5):",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

async def wizard_input_kwh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wz = _wz(context)
    if not wz or wz.get(WZ_EXPECT) != "kwh":
        return
    txt = (update.message.text or "").strip()
    try:
        kwh = float(txt.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Valore non valido. Inserisci i kWh (es: 4.5).", reply_markup=main_keyboard())
        return
    wz[WZ_DATA]["kwh"] = kwh
    wz[WZ_EXPECT] = "photo"
    await update.message.reply_text(
        f"Riepilogo provvisorio:\n• Slot: {wz[WZ_DATA]['slot']}\n• kWh {kwh}\n\nOra *invia la foto* della ricarica.",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

async def wizard_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wz = _wz(context)
    if not wz or wz.get(WZ_EXPECT) != "photo":
        return
    photo = update.message.photo[-1] if update.message and update.message.photo else None
    if not photo:
        return
    file = await context.bot.get_file(photo.file_id)
    local_path = f"{PHOTOS_DIR}/{photo.file_id}.jpg"
    await file.download_to_drive(local_path)
    wz[WZ_DATA]["photo_path"] = local_path
    wz[WZ_EXPECT] = "choose_action"

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📨 Dichiara senza nota", callback_data="decl:send"),
            InlineKeyboardButton("📝 Aggiungi nota", callback_data="decl:note"),
        ]
    ])
    await update.message.reply_text(
        "Foto acquisita.\nVuoi *dichiarare subito* o *aggiungere una nota*?",
        parse_mode="Markdown",
        reply_markup=kb
    )

async def wizard_declare_or_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    data = update.callback_query.data or ""
    if not data.startswith("decl:"):
        return
    choice = data.split(":",1)[1]
    wz = _wz(context)
    if not wz or wz.get(WZ_EXPECT) not in ("choose_action", "note"):
        await update.callback_query.answer("Sessione scaduta. Premi + Ricarica.", show_alert=True)
        return

    if choice == "note":
        wz[WZ_EXPECT] = "note"
        await update.callback_query.answer()
        await update.callback_query.message.reply_text("Scrivi la *nota* da allegare:", parse_mode="Markdown")
        return

    await update.callback_query.answer()
    await _wizard_finalize(update, context, note="")

async def wizard_input_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wz = _wz(context)
    if not wz or wz.get(WZ_EXPECT) != "note":
        return
    note = (update.message.text or "").strip()
    await _wizard_finalize(update, context, note=note)

async def _wizard_finalize(update_or_cb, context: ContextTypes.DEFAULT_TYPE, note: str):
    wz = _wz(context)
    chat_id, username, first_name, last_name = _fmt_name(update_or_cb)
    user_id = DBI.get_or_create_user(chat_id, username, first_name, last_name)

    slot = wz[WZ_DATA]["slot"]
    kwh = wz[WZ_DATA]["kwh"]
    photo_path = wz[WZ_DATA].get("photo_path")

    pid = DBI.add_pending_recharge(user_id, slot, kwh, photo_path, requested_by=chat_id, note=note)
    _wz_reset(context)

    await _notify_admins_new_pending(context, pid, user_id, slot, kwh, note, photo_path)

    msg = (
        f"✅ Dichiarazione inviata (ID *{pid}*): `{slot}` +*{kwh}* kWh\n"
        + (f"📝 Nota: _{note}_\n" if note else "")
        + "In attesa di approvazione."
    )
    if hasattr(update_or_cb, "message") and update_or_cb.message:
        await update_or_cb.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_keyboard())
    else:
        await update_or_cb.callback_query.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_keyboard())

async def cmd_annulla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _wz(context):
        _wz_reset(context)
        await update.message.reply_text("❎ Operazione annullata.", reply_markup=main_keyboard())
    else:
        await update.message.reply_text("Nessuna operazione in corso.", reply_markup=main_keyboard())

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

    # Wizard ricarica
    app.add_handler(CommandHandler("ricarica", wizard_ricarica_start))
    app.add_handler(MessageHandler(filters.Regex(r'^\s*\+\s*Ricarica\s*$'), wizard_ricarica_start))
    app.add_handler(CallbackQueryHandler(wizard_choose_slot, pattern=r'^slot:'))
    app.add_handler(MessageHandler(filters.PHOTO, wizard_photo))
    app.add_handler(CallbackQueryHandler(wizard_declare_or_note, pattern=r'^decl:'))
    # Due handler testuali per gli step KWh/Nota (prima del catch-all)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_input_kwh))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_input_note))

    # Catch-all testo (note) — dopo gli step del wizard
    async def _on_text_or_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if _wz(context):
            return
        await on_message(update, context)
    app.add_handler(CommandHandler("annulla", cmd_annulla))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text_or_note))

    # Admin
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("credita", cmd_credita))
    app.add_handler(CallbackQueryHandler(on_callback))

    log.info("Handlers registered: /start /help /saldo /ricarica + wizard (+foto, note), /annulla, admin (/users /pending /approve /reject /credita)")
    return app
