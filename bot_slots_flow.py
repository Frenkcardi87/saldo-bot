# bot_slots_flow.py — WALLET-only accounting with confirmations
# Patch: ensure photo prompt after kWh, stronger state checks, and notifications for /credita
import os
import pathlib
import sqlite3
import logging
from typing import Optional, List

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

# -------------------- FS/SQLite safety for Railway --------------------
os.environ.setdefault("TMPDIR", "/var/data")
os.environ.setdefault("TEMP", "/var/data")
os.environ.setdefault("TMP", "/var/data")
os.environ.setdefault("SQLITE_TMPDIR", "/var/data")

DATA_DIR = "/var/data"
DEFAULT_DB_PATH = f"{DATA_DIR}/kwh_slots.db"
PHOTOS_DIR = f"{DATA_DIR}/photos"


def open_sqlite(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path, timeout=30, check_same_thread=False, isolation_level=None, cached_statements=0
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

        pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
        pathlib.Path(PHOTOS_DIR).mkdir(parents=True, exist_ok=True)

        # Write test
        try:
            with open(os.path.join(DATA_DIR, ".rw_test"), "w") as f:
                f.write("ok")
            os.remove(os.path.join(DATA_DIR, ".rw_test"))
        except Exception:
            log.exception("DB dir write test FAILED on %s", DATA_DIR)

        self.conn = open_sqlite(self.path)
        self._init_db()

    def _init_db(self):
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
                note TEXT,
                requested_by INTEGER,
                status TEXT DEFAULT 'pending',
                approved_by INTEGER,
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
                approved_by INTEGER,
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
            CREATE TABLE IF NOT EXISTS admin_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                action TEXT,
                target_user_id INTEGER,
                note TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """)

    # -------- Convenience --------
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

    def get_chat_id_by_user_id(self, user_id: int) -> Optional[int]:
        with self.conn as con:
            cur = con.cursor()
            cur.execute("SELECT chat_id FROM users WHERE id=?", (user_id,))
            row = cur.fetchone()
            return row[0] if row else None

    def get_wallet(self, chat_id: int) -> float:
        with self.conn as con:
            cur = con.cursor()
            cur.execute("SELECT wallet_kwh FROM users WHERE chat_id=?", (chat_id,))
            row = cur.fetchone()
            return float(row[0]) if row else 0.0

    def get_wallet_by_user_id(self, user_id: int) -> float:
        with self.conn as con:
            cur = con.cursor()
            cur.execute("SELECT wallet_kwh FROM users WHERE id=?", (user_id,))
            row = cur.fetchone()
            return float(row[0]) if row else 0.0

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
        """Apply pending recharge to WALLET and mark approved. Returns (user_id, slot, kwh)."""
        with self.conn as con:
            cur = con.cursor()
            cur.execute("SELECT user_id, slot_type, kwh FROM pending WHERE id=? AND status='pending'", (pending_id,))
            row = cur.fetchone()
            if not row:
                return None
            user_id, slot, kwh = row
            cur.execute("UPDATE users SET wallet_kwh = wallet_kwh + ?, updated_at=datetime('now') WHERE id=?",
                        (kwh, user_id))
            cur.execute("UPDATE pending SET status='approved', approved_by=? WHERE id=?", (admin_id, pending_id))
            cur.execute("INSERT INTO recharges (user_id, amount, slot_type, approved, approved_by) VALUES (?,?,?,1,?)",
                        (user_id, kwh, slot, admin_id))
            return (user_id, slot, kwh)

    def reject_pending(self, pending_id: int, admin_id: int):
        with self.conn as con:
            cur = con.cursor()
            cur.execute("UPDATE pending SET status='rejected', approved_by=? WHERE id=?", (admin_id, pending_id))

DBI = (lambda: DB(os.environ.get("DB_PATH", DEFAULT_DB_PATH).strip() or DEFAULT_DB_PATH))()

# -------------------- Helpers --------------------
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

def main_keyboard():
    rows = [
        [KeyboardButton("+ Ricarica")],
        [KeyboardButton("/saldo"), KeyboardButton("/annulla")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)

# -------------------- User --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, username, first_name, last_name = _fmt_name(update)
    DBI.get_or_create_user(chat_id, username, first_name, last_name)
    await update.message.reply_text(
        "Ciao! ✅ Bot attivo. Comandi: /saldo, /ricarica, /help",
        reply_markup=main_keyboard()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *Comandi disponibili*\n"
        "• /saldo — mostra il tuo *Wallet*\n"
        "• /ricarica (usa il wizard col pulsante + Ricarica)\n\n"
        "👮 *Admin*:\n"
        "• /pending, /approve `<id>`, /reject `<id>`\n"
        "• /users\n"
        "• /credita — wizard accredito nel Wallet",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

async def cmd_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, username, first_name, last_name = _fmt_name(update)
    DBI.get_or_create_user(chat_id, username, first_name, last_name)
    wallet = DBI.get_wallet(chat_id)
    await update.message.reply_markdown(f"💼 *Wallet*: *{wallet}* kWh", reply_markup=main_keyboard())

# -------------------- Wizard Ricarica (forzato) --------------------
WZ_KEY = "ricarica_wz"
WZ_EXPECT = "expect"
WZ_DATA = "data"
def _wz_reset(context): context.user_data.pop(WZ_KEY, None)
def _wz_start(context): context.user_data[WZ_KEY] = {WZ_EXPECT: "slot", WZ_DATA: {}}
def _wz(context): return context.user_data.get(WZ_KEY, None)

async def wizard_ricarica_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _wz_start(context)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Slot1", callback_data="slot:slot1"),
         InlineKeyboardButton("Slot3", callback_data="slot:slot3")],
        [InlineKeyboardButton("Slot5", callback_data="slot:slot5"),
         InlineKeyboardButton("Slot8", callback_data="slot:slot8")],
        [InlineKeyboardButton("Wallet", callback_data="slot:wallet")]
    ])
    await update.message.reply_text(
        "Seleziona lo *slot usato* (solo per controllo, i kWh andranno nel *Wallet*):",
        parse_mode="Markdown", reply_markup=kb
    )

# Foto con didascalia "slot3 4.5" (scorciatoia)
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
        f"📎 Ricevuta registrata. Richiesta *{pid}*: `{slot}` +*{kwh}* kWh → *Wallet*.",
        parse_mode="Markdown", reply_markup=main_keyboard()
    )
    await _notify_admins_new_pending(context, pid, user_id, slot, kwh, "foto", local_path)

# -------------------- Admin: lists & approvals --------------------
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_chat.id):
        await update.message.reply_text("⛔ Solo admin.", reply_markup=main_keyboard()); return
    rows = DBI.list_users(limit=30)
    if not rows:
        await update.message.reply_text("Nessun utente.", reply_markup=main_keyboard()); return
    lines = []
    for r in rows:
        uid, cid, un, fn, ln, s1, s3, s5, s8, wal = r
        name = un or (fn + " " + ln).strip() or str(cid)
        lines.append(f"• #{uid} {name} — Wallet:{wal}")
    await update.message.reply_text("👥 *Utenti*\n" + "\n".join(lines), parse_mode="Markdown", reply_markup=main_keyboard())

async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_chat.id):
        await update.message.reply_text("⛔ Solo admin.", reply_markup=main_keyboard()); return
    rows = DBI.list_pending()
    if not rows:
        await update.message.reply_text("Nessuna richiesta in attesa.", reply_markup=main_keyboard()); return
    lines = []
    kb = []
    for r in rows:
        pid, user_id, slot, kwh, path, note, status, created = r
        lines.append(f"• ID {pid}: user#{user_id} {slot}+{kwh} kWh → Wallet [{status}]")
        kb.append([
            InlineKeyboardButton(f"✅ Approva {pid}", callback_data=f"approve:{pid}"),
            InlineKeyboardButton(f"❌ Rifiuta {pid}", callback_data=f"reject:{pid}")
        ])
    await update.message.reply_text("🕘 *Pending*\n" + "\n".join(lines), parse_mode="Markdown",
                                    reply_markup=InlineKeyboardMarkup(kb))

async def _notify_user_wallet(context: ContextTypes.DEFAULT_TYPE, user_id: int, add_kwh: float):
    chat_id = DBI.get_chat_id_by_user_id(user_id)
    wallet = DBI.get_wallet_by_user_id(user_id)
    if chat_id:
        txt = f"✅ Ricarica *accreditata*: +*{add_kwh}* kWh → Wallet.\nSaldo attuale: *{wallet}* kWh"
        try:
            await context.bot.send_message(chat_id, txt, parse_mode="Markdown", reply_markup=main_keyboard())
        except Exception:
            log.exception("Notify user failed")

async def _notify_admins_text(context: ContextTypes.DEFAULT_TYPE, text: str):
    for admin in _admin_ids():
        try:
            await context.bot.send_message(admin, text, parse_mode="Markdown")
        except Exception:
            log.exception("Notify admin text failed: %s", admin)

async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_chat.id
    if not _is_admin(admin_id):
        await update.message.reply_text("⛔ Solo admin.", reply_markup=main_keyboard()); return
    args = context.args or []
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text("Usa: /approve <id>", reply_markup=main_keyboard()); return
    pid = int(args[0])
    res = DBI.approve_pending(pid, admin_id=admin_id)
    if not res:
        await update.message.reply_text("Richiesta non trovata o già gestita.", reply_markup=main_keyboard()); return
    user_id, slot, kwh = res
    await update.message.reply_text(f"✅ Approvata ID {pid}: user#{user_id} {slot}+{kwh} kWh → Wallet", reply_markup=main_keyboard())
    await _notify_user_wallet(context, user_id, kwh)
    await _notify_admins_text(context, f"ℹ️ *Log*: approvata ID {pid} da admin {admin_id} — user#{user_id} +{kwh} kWh ({slot}).")

async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_chat.id
    if not _is_admin(admin_id):
        await update.message.reply_text("⛔ Solo admin.", reply_markup=main_keyboard()); return
    args = context.args or []
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text("Usa: /reject <id>", reply_markup=main_keyboard()); return
    pid = int(args[0])
    DBI.reject_pending(pid, admin_id=admin_id)
    await update.message.reply_text(f"❌ Rifiutata ID {pid}", reply_markup=main_keyboard())
    await _notify_admins_text(context, f"ℹ️ *Log*: rifiutata ID {pid} da admin {admin_id}.")

# -------------------- Admin Credit Wizard (wallet only) --------------------
AC_KEY = "admin_credit_wz"
AC_EXPECT = "expect"         # 'user' | 'kwh'
AC_DATA = "data"             # {'chat_id':..., 'kwh':...}
def _ac_reset(context): context.user_data.pop(AC_KEY, None)
def _ac_start(context): context.user_data[AC_KEY] = {AC_EXPECT: "user", AC_DATA: {}}
def _ac(context): return context.user_data.get(AC_KEY, None)

async def cmd_credita(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_chat.id):
        await update.message.reply_text("⛔ Solo admin.", reply_markup=main_keyboard()); return
    _ac_start(context)
    rows = DBI.list_users(limit=50)
    if not rows:
        await update.message.reply_text("Nessun utente trovato.", reply_markup=main_keyboard()); return
    buttons = []
    for (uid, cid, un, fn, ln, *_rest) in rows:
        name = un or (fn + " " + ln).strip() or str(cid)
        buttons.append([InlineKeyboardButton(f"{name} ({cid})", callback_data=f"ac_user:{cid}")])
    await update.message.reply_text("👤 Seleziona l'utente da accreditare (Wallet):", reply_markup=InlineKeyboardMarkup(buttons))

async def ac_choose_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query: return
    data = update.callback_query.data or ""
    if not data.startswith("ac_user:"): return
    cid_s = data.split(":",1)[1]
    try:
        cid = int(cid_s)
    except ValueError:
        await update.callback_query.answer("chat_id non valido", show_alert=True); return
    wz = _ac(context)
    if not wz or wz[AC_EXPECT] != "user":
        await update.callback_query.answer("Sessione scaduta. /credita", show_alert=True); return
    wz[AC_DATA]["chat_id"] = cid
    wz[AC_EXPECT] = "kwh"
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        f"Utente *{cid}* selezionato. Inserisci i *kWh* da accreditare al *Wallet* (es: 3.5):",
        parse_mode="Markdown", reply_markup=main_keyboard()
    )

async def ac_input_kwh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("AC_KWH: wz=%s txt=%s", _ac(context), (update.message.text if update.message else None))
    wz = _ac(context)
    if not wz or wz[AC_EXPECT] != "kwh":
        return
    txt = (update.message.text or "").strip()
    try:
        kwh = float(txt.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Valore non valido. Inserisci un numero (es: 2.0).", reply_markup=main_keyboard())
        return
    cid = wz[AC_DATA]["chat_id"]
    admin_chat = update.effective_chat.id
    uid = DBI.get_user_id(cid) or DBI.get_or_create_user(cid, "", "", "")
    pid = DBI.add_pending_recharge(uid, "wallet", kwh, None, requested_by=admin_chat, note="accredito admin")
    res = DBI.approve_pending(pid, admin_id=admin_chat)
    _ac_reset(context)
    if res:
        await update.message.reply_text(
            f"✅ Accreditati *{kwh}* kWh sul *Wallet* dell'utente *{cid}*.\nID operazione: {pid}",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )
        await _notify_user_wallet(context, uid, kwh)
        await _notify_admins_text(context, f"ℹ️ *Log accredito admin*: admin {admin_chat} → user#{uid} ({cid}) +{kwh} kWh (ID {pid}).")
    else:
        await update.message.reply_text("Errore in accredito.", reply_markup=main_keyboard())

# -------------------- Wizard Ricarica --------------------
async def _notify_admins_new_pending(context: ContextTypes.DEFAULT_TYPE, pid: int, user_id: int, slot: str, kwh: float, note: str, photo_path: Optional[str]):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Approva {pid}", callback_data=f"approve:{pid}"),
        InlineKeyboardButton(f"❌ Rifiuta {pid}", callback_data=f"reject:{pid}"),
    ]])
    txt = (
        f"🆕 *Dichiarazione ricarica*\n"
        f"• ID: *{pid}*\n"
        f"• user_id: *{user_id}*\n"
        f"• Slot dichiarato: *{slot}*\n"
        f"• +*{kwh}* kWh → *Wallet*\n"
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

async def wizard_choose_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query: return
    data = update.callback_query.data or ""
    if not data.startswith("slot:"): return
    slot = data.split(":",1)[1]
    wz = _wz(context)
    if not wz:
        await update.callback_query.answer("Sessione scaduta. Premi + Ricarica.", show_alert=True); return
    wz[WZ_DATA]["slot"] = slot
    wz[WZ_EXPECT] = "kwh"
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        f"Hai scelto *{slot}*.\nOra *inserisci i kWh* (es: 4.5):",
        parse_mode="Markdown", reply_markup=main_keyboard()
    )

async def wizard_input_kwh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("WZ_KWH: wz=%s txt=%s", _wz(context), (update.message.text if update.message else None))
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
        f"OK 👍 Riepilogo:\n• Slot: {wz[WZ_DATA]['slot']}\n• kWh: {kwh}\n\n*ORA INVIA UNA FOTO* della ricarica.",
        parse_mode="Markdown", reply_markup=main_keyboard()
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
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📨 Dichiara senza nota", callback_data="decl:send"),
        InlineKeyboardButton("📝 Aggiungi nota", callback_data="decl:note"),
    ]])
    await update.message.reply_text("Foto acquisita. Dichiari subito o aggiungi una nota?", reply_markup=kb)

async def wizard_declare_or_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query: return
    data = update.callback_query.data or ""
    if not data.startswith("decl:"): return
    choice = data.split(":",1)[1]
    wz = _wz(context)
    if not wz or wz.get(WZ_EXPECT) not in ("choose_action","note"):
        await update.callback_query.answer("Sessione scaduta. Premi + Ricarica.", show_alert=True); return
    if choice == "note":
        wz[WZ_EXPECT] = "note"
        await update.callback_query.answer()
        await update.callback_query.message.reply_text("Scrivi la *nota* da allegare:", parse_mode="Markdown")
        return
    await update.callback_query.answer()
    await _wizard_finalize(update, context, note="")

async def wizard_input_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("WZ_NOTE: wz=%s txt=%s", _wz(context), (update.message.text if update.message else None))
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
        f"✅ Dichiarazione inviata (ID *{pid}*): slot *{slot}* +*{kwh}* kWh → *Wallet*\n"
        + (f"📝 Nota: _{note}_\n" if note else "")
        + "In attesa di approvazione."
    )
    if hasattr(update_or_cb, "message") and update_or_cb.message:
        await update_or_cb.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_keyboard())
    else:
        await update_or_cb.callback_query.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_keyboard())

async def cmd_annulla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cleared = False
    if _ac(context): _ac_reset(context); cleared = True
    if _wz(context): _wz_reset(context); cleared = True
    await update.message.reply_text("❎ Operazione annullata." if cleared else "Nessuna operazione in corso.", reply_markup=main_keyboard())

# -------------------- Callbacks approve/reject --------------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query: return
    data = update.callback_query.data or ""
    if ":" not in data:
        await update.callback_query.answer(); return
    action, pid_s = data.split(":", 1)
    admin_id = update.effective_chat.id
    if not _is_admin(admin_id):
        await update.callback_query.answer("Solo admin", show_alert=True); return
    if not pid_s.isdigit():
        await update.callback_query.answer("ID non valido", show_alert=True); return
    pid = int(pid_s)
    if action == "approve":
        res = DBI.approve_pending(pid, admin_id=admin_id)
        if res:
            user_id, slot, kwh = res
            await update.callback_query.answer("Approvata ✅", show_alert=True)
            await update.callback_query.message.reply_text(f"✅ Approvata ID {pid}: user#{user_id} {slot}+{kwh} → Wallet", reply_markup=main_keyboard())
            await _notify_user_wallet(context, user_id, kwh)
            await _notify_admins_text(context, f"ℹ️ *Log*: approvata ID {pid} da admin {admin_id} — user#{user_id} +{kwh} kWh ({slot}).")
        else:
            await update.callback_query.answer("Già gestita/inesistente", show_alert=True)
    elif action == "reject":
        DBI.reject_pending(pid, admin_id=admin_id)
        await update.callback_query.answer("Rifiutata ❌", show_alert=True)
        await update.callback_query.message.reply_text(f"❌ Rifiutata ID {pid}", reply_markup=main_keyboard())
        await _notify_admins_text(context, f"ℹ️ *Log*: rifiutata ID {pid} da admin {admin_id}.")

# -------------------- Application builder --------------------
def build_application():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("Missing TELEGRAM_TOKEN")

    app = Application.builder().token(token).build()

    # Utente
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("saldo", cmd_saldo))

    # Admin: credit wizard (TEXT handler first, block=False)
    app.add_handler(CommandHandler("credita", cmd_credita))
    app.add_handler(CallbackQueryHandler(ac_choose_user, pattern=r'^ac_user:'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ac_input_kwh, block=False))

    # Wizard ricarica
    app.add_handler(CommandHandler("ricarica", wizard_ricarica_start))
    app.add_handler(MessageHandler(filters.Regex(r'^\s*\+\s*Ricarica\s*$'), wizard_ricarica_start))
    app.add_handler(CallbackQueryHandler(wizard_choose_slot, pattern=r'^slot:'))
    app.add_handler(MessageHandler(filters.PHOTO, wizard_photo))
    app.add_handler(CallbackQueryHandler(wizard_declare_or_note, pattern=r'^decl:'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_input_kwh, block=False))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_input_note, block=False))

    # Catch-all text AFTER wizard handlers
    async def _on_text_or_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if _wz(context) or _ac(context):
            return
        await on_message(update, context)
    app.add_handler(CommandHandler("annulla", cmd_annulla))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text_or_note))

    # Admin approvals & lists
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CallbackQueryHandler(on_callback))

    log.info("Handlers ready. Wallet-only with photo prompt + notifications.")
    return app
